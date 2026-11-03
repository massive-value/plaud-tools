from __future__ import annotations

import json
import logging
import os
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from ..core.appdata import events_path as _events_path
from ..core.client import (
    AUDIO_URL_TTL_S,
    DEFAULT_TRANSCRIPT_BLOCK,
    SEARCH_RESULT_CAP,
    SHARE_DEFAULT_DAYS,
    TRANSCRIPT_BLOCKS,
    VOCAB_LANGUAGE,
    PlaudClient,
    describe_unstarted_process,
)
from ..core.errors import PlaudApiError, PlaudSessionExpiredError, PlaudWaitTimeoutError
from ..core.export import TranscriptExportError
from ..core.export import export_transcript as _export_transcript
from ..core.query import (
    SHARE_REVOKE_NOTE,
    detail_summary_dict,
    folder_dict,
    format_transcript,
    model_dict,
    parse_isoish,
    parse_keep_ranges,
    query_recordings,
    share_dict,
    structured_segments,
    summarize_match,
    summarize_recording,
    template_dict,
    transcript_fingerprint,
    vocabulary_dict,
)
from ..core.session import SessionManager, SessionStore
from ..core.summary import notes_payload, render_summary
from .progress import current_progress, shifted

log = logging.getLogger(__name__)

# Rotate events.jsonl → events.jsonl.1 once the file exceeds this size.
# Keeps the reader's per-poll read_text() call small and prevents unbounded
# growth in deployments where the tray is not running to consume events.
# The tray's _event_poll_loop truncates the file every 5 s, so rotation
# should be rare in normal operation (issue audit / Wave 0 / A4).
_EVENTS_MAX_BYTES = 1_000_000  # ~1 MB


def _write_event(event_type: str, **kwargs: Any) -> None:
    """Append a structured event to the events file; never raises.

    If the file exceeds ``_EVENTS_MAX_BYTES`` before appending, it is rotated
    to ``events.jsonl.1`` (replacing any prior ``.1``) via ``os.replace`` —
    which is atomic on POSIX and as close as Windows gets.  ``Path.rename``
    is intentionally avoided: it raises ``FileExistsError`` on Windows when
    the destination already exists.  The rotation itself is wrapped in the same
    defensive try/except so a failure (read-only dir, locked file, etc.) cannot
    propagate — we fall through and still attempt the append.
    """
    try:
        path = _events_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        # --- size-based rotation -------------------------------------------
        # Check size before opening for append so we keep the hot path (no
        # rotation needed) to a single stat call.
        try:
            if path.exists() and path.stat().st_size >= _EVENTS_MAX_BYTES:
                os.replace(path, str(path) + ".1")
        except Exception:
            # A failed rotation is not fatal — fall through and append anyway.
            log.debug("Failed to rotate events file", exc_info=True)
        # --- append --------------------------------------------------------
        record = {"type": event_type, "ts": time.time(), **kwargs}
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record) + "\n")
    except Exception:
        log.debug("Failed to write event %r", event_type, exc_info=True)


# ---------------------------------------------------------------------------
# Session diagnostics — included in session_expired events so we can root-cause
# spurious logouts without needing to reproduce the failure (issue #78). All
# fields are safe metadata; token bytes never appear here.
# ---------------------------------------------------------------------------


def _diagnose_session_state(manager: SessionManager | None = None) -> dict[str, Any]:
    """Best-effort snapshot of how the MCP currently sees the user session.

    Thin wrapper: calls ``manager.diagnose()`` for the session-y fields, then
    merges in MCP-process-local fields (PID, app version, env-token-present
    flag).  This keeps all JWT introspection in session.py while leaving only
    facade-local metadata here.  See ADR 004.

    Pass the server's own *manager* when there is one: it reports the load
    that just failed instead of reading Credential Manager again, which for a
    signed-out user costs another ~3.6 s keyring retry cycle.
    """
    # Lazy import to avoid the circular import surfaced by
    # ``plaud_tools/__init__.py`` re-exporting ``build_handlers`` from this module.
    from .. import __version__ as _app_version

    diag: dict[str, Any] = {
        "mcp_pid": os.getpid(),
        "mcp_version": _app_version,
        "env_token_present": bool(os.getenv("PLAUD_ACCESS_TOKEN")),
    }
    if manager is None:
        diag.update(SessionManager(SessionStore()).diagnose())
    else:
        diag.update(manager.diagnose(reuse_last_load=True))
    return diag


def _emit_session_expired(reason: str, manager: SessionManager | None = None) -> None:
    """Log + write a session_expired event with full diagnostic context."""
    diag = _diagnose_session_state(manager)
    log.warning("MCP firing session_expired reason=%s diag=%s", reason, diag)
    _write_event("session_expired", reason=reason, **diag)


# ---------------------------------------------------------------------------
# Structured error helpers
# ---------------------------------------------------------------------------

# §6.2: a session-expired error is useless to an LLM caller unless it names
# the remedy — the MCP surface cannot show a login prompt itself, so the
# message tells the assistant what to relay to the human so they can
# self-serve the fix instead of getting stuck on an opaque failure.
_SESSION_EXPIRED_HINT = "Tell the user to open the PlaudTools tray and sign in, then retry."


def _json_result(value: Any, is_error: bool = False) -> dict[str, Any]:
    """Wrap a JSON-able payload as a tool result.

    The payload goes out twice: as compact JSON text (what every client,
    old or new, reads) and, for successful object payloads, as
    ``structuredContent`` for clients that use a tool's ``outputSchema``.
    Compact separators because every response pays the whitespace once per
    call, and ``indent=2`` cost 20-35% more tokens.
    """
    result: dict[str, Any] = {"content": [{"type": "text", "text": json.dumps(value, separators=(",", ":"))}]}
    if is_error:
        result["isError"] = True
    elif isinstance(value, dict):
        result["structuredContent"] = value
    return result


def _error_result(
    message: str,
    *,
    error_code: str,
    retryable: bool,
    http_status: int | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "error": message,
        "error_code": error_code,
        "retryable": retryable,
    }
    if http_status is not None:
        payload["http_status"] = http_status
    return _json_result(payload, is_error=True)


def _call(get_client: Callable[[], PlaudClient | None], fn: Callable[[PlaudClient], Any]) -> Any:
    client = get_client()
    if client is None:
        _emit_session_expired("no_session")
        return _error_result(
            f"No Plaud session. {_SESSION_EXPIRED_HINT}",
            error_code="session_expired",
            retryable=False,
        )
    try:
        return fn(client)
    except PlaudSessionExpiredError as exc:
        _emit_session_expired("token_expired", client.session_manager)
        return _error_result(
            f"{exc} {_SESSION_EXPIRED_HINT}",
            error_code="session_expired",
            retryable=False,
        )
    except PlaudApiError as exc:
        code, retryable = exc.classify()
        message = str(exc)
        if code == "session_expired":
            # #138 (Wave 1) reclassified HTTP 401 to "session_expired" here,
            # but only the dedicated PlaudSessionExpiredError branch above
            # ever fired the tray's re-auth event — a 401 arriving through
            # this generic branch silently skipped it (Wave 1 follow-up).
            # Fire it here too so a mid-session 401 still triggers the tray
            # toast / login window, not just a locally-detected expiry.
            _emit_session_expired("http_401", client.session_manager)
            message = f"{message} {_SESSION_EXPIRED_HINT}"
        return _error_result(
            message,
            error_code=code,
            retryable=retryable,
            http_status=exc.http_status,
        )
    except ValueError as exc:
        return _error_result(str(exc), error_code="validation", retryable=False)
    except RuntimeError as exc:
        return _error_result(str(exc), error_code="api_error", retryable=False)
    except OSError as exc:
        # Local filesystem failures (permission denied, disk full, temp file
        # races, …) must not escape the structured-error contract — issue #150.
        # Without this it would reach call_tool's catch-all in server.py and
        # be reported as a generic "internal" error.
        return _error_result(str(exc), error_code="io_error", retryable=False)


def _summarize_detail(detail: Any) -> dict[str, Any]:
    """mcp.py's ``get_recording`` shape: query.detail_summary_dict()'s base
    fields plus the MCP-specific extras (is_trash, language)."""
    output = detail_summary_dict(detail)
    output["is_trash"] = detail.is_trash
    output["language"] = detail.language
    return output


# Utterances returned per get_recording call when the caller doesn't say.
# Chosen well above the 50 the official Plaud MCP uses: our responses are
# compact JSON (no pretty-printing) and a single recording is usually the whole
# task, so paying for extra round trips is worse than one larger payload.  The
# cap exists so a 3-hour recording can't blow the client's message-size limit.
DEFAULT_TRANSCRIPT_UTTERANCES = 200
MAX_TRANSCRIPT_UTTERANCES = 1000


def _page_transcript(
    segments: list[dict[str, Any]], after: int, limit: int
) -> tuple[list[dict[str, Any]], int, int | None]:
    """Return one page of utterances, its actual start index, and the next cursor.

    Paging on utterance boundaries rather than characters: a character offset
    cuts mid-word and hands the model a fragment ("…and then Sar"), and forces
    it to do offset arithmetic to continue. An utterance index can't tear a
    record, and ``next_after`` is directly reusable as the next ``after``.

    ``after`` past the end yields an empty page starting at EOF and no cursor
    rather than an error — a caller that reuses a stale cursor after the
    transcript shrank gets "nothing more", which is true.
    """
    start = min(after, len(segments))
    page = segments[start : start + limit]
    end = start + len(page)
    next_after = end if end < len(segments) else None
    return page, start, next_after


def _transcript_unavailable_note(detail: Any, requested_block: str) -> str | None:
    """Explain an empty transcript, or return None when there is nothing to explain.

    An empty ``transcript`` has three very different causes and an LLM caller
    cannot tell them apart from the absence of text alone: the recording was
    never transcribed, the *requested* block does not exist for it (asking for
    the AI-polished pass on a recording Plaud never polished), or the
    transcript is genuinely empty. Each note names the next action.
    """
    if detail.transcript:
        return None
    available = list(detail.transcript_blocks_available or [])
    if not available:
        return (
            "No transcript has been generated for this recording yet. "
            "Call process_recording to transcribe it."
        )
    if requested_block not in available:
        return (
            f"The {requested_block!r} transcript block is not available for this recording. "
            f"Available blocks: {', '.join(available)}. "
            f"Retry with transcript_block set to one of those."
        )
    return None


PROCESS_WAIT_MODES = {"none", "transcript", "summary"}

# Largest browse page.  Every browse is one Plaud list request whatever the
# limit; the cap keeps a single response small enough for an agent's context.
MAX_BROWSE_LIMIT = 200

# How long one tool call may block waiting on a Plaud job (#151).  Most MCP
# clients give up on a call after 60-120 s, so waiting longer server-side only
# orphans the handler.  process_recording spends this once across both of its
# waits; merge_recordings and mutate_recording(action="trim") spend it on the
# poll for the new recording.  upload_recording is
# not bounded: an upload is an active transfer, and stopping it midway leaves
# nothing to check back on.  Every one of these waits also reports progress to
# clients that ask for it (see progress.py).  See ADR 007 for the job-handle
# response shape.
_WAIT_TIMEOUT_S = 90.0


def _still_processing(kind: str, job_id: str, poll_with: str, message: str, **fields: Any) -> dict[str, Any]:
    """Result for a Plaud job that outlived this call's wait budget (ADR 007).

    The job keeps running on Plaud, so this is a success-shaped result, not an
    error: ``job`` names what is running and which tool reports on it, and
    ``retryable: false`` tells the agent that calling the same tool again would
    start a duplicate rather than resume.
    """
    return _json_result(
        {
            **fields,
            "status": "still_processing",
            "job": {"kind": kind, "id": job_id, "poll_with": poll_with},
            "retryable": False,
            "message": message,
        }
    )


# mutate_recording actions that manage a recording's public share link.
_SHARE_ACTIONS = ("share_create", "share_get", "share_update", "share_revoke")


def _validation_error(message: str) -> dict[str, Any]:
    return _error_result(message, error_code="validation", retryable=False)


def _share_action(
    client: PlaudClient,
    action: str,
    recording_id: str,
    *,
    days: int | None,
    never_expires: bool,
    audio: bool | None,
    transcript: bool | None,
    summary: bool | None,
    confirm: bool,
) -> dict[str, Any]:
    """Run one share_* action of mutate_recording and shape its result.

    Creating a link publishes client information to anyone holding the URL,
    and revoking one cuts off people relying on it, so both need confirm=true
    (the stdio surface can show no prompt).  Creation defaults to a 7-day,
    transcript-only link.
    """
    if action == "share_get":
        link = client.get_share_link(recording_id)
        if link is None:
            return _json_result({"ok": True, "recording_id": recording_id, "shared": False})
        return _json_result(
            {"ok": True, "recording_id": recording_id, "shared": True, "share": share_dict(link)}
        )

    if action == "share_create":
        if not confirm:
            return _validation_error(
                "share_create makes a public link that anyone holding the URL can open. "
                "Re-invoke with confirm=true only after the human has confirmed "
                "they want to share this recording."
            )
        if never_expires:
            return _validation_error(
                "share_create expires after share_days (default 7); use share_update for never_expires"
            )
        link = client.create_share_link(
            recording_id,
            expire_days=SHARE_DEFAULT_DAYS if days is None else days,
            audio=bool(audio),
            transcript=True if transcript is None else transcript,
            summary=bool(summary),
        )
        return _json_result(
            {"ok": True, "recording_id": recording_id, "action": action, "share": share_dict(link)}
        )

    if action == "share_update":
        if not confirm:
            return _validation_error(
                "share_update can widen who sees what (audio, no expiry). "
                "Re-invoke with confirm=true only after the human has confirmed the change."
            )
        link = client.update_share_link(
            recording_id,
            expire_days=days,
            never_expires=never_expires,
            audio=audio,
            transcript=transcript,
            summary=summary,
        )
        return _json_result(
            {"ok": True, "recording_id": recording_id, "action": action, "share": share_dict(link)}
        )

    # share_revoke
    if not confirm:
        return _validation_error(
            "share_revoke stops the public link from working. "
            "Re-invoke with confirm=true only after the human has confirmed they want to revoke it."
        )
    client.revoke_share_link(recording_id)
    return _json_result(
        {
            "ok": True,
            "recording_id": recording_id,
            "action": action,
            "note": SHARE_REVOKE_NOTE,
        }
    )


PROCESS_ACTIONS = ("process", "resummarize", "add_note")


def _process_option_problem(
    action: str,
    note_name: str | None,
    language: str | None,
    diarization: bool | None,
    verbosity: str | None,
    confirm: bool,
) -> str | None:
    """Why these process_recording options don't fit *action*, or None when they do.

    Options another action owns are refused rather than silently ignored, so
    an agent learns its choice had no effect. resummarize is confirm-gated like
    delete_recording: Plaud drops the old summary (hand edits included) the
    moment the job starts, and there is no undo.
    """
    if action not in PROCESS_ACTIONS:
        return f"action must be one of: {', '.join(PROCESS_ACTIONS)}"
    if action == "resummarize" and not confirm:
        return (
            "resummarize requires explicit confirmation. It replaces the recording's current "
            "summary, including any hand edits, and Plaud may change the recording's title; "
            "neither can be undone. Re-invoke with confirm=true only after the human has "
            "confirmed they want the summary replaced."
        )
    if confirm and action != "resummarize":
        return "confirm applies only to action='resummarize'"
    if note_name is not None and action != "add_note":
        return "note_name applies only to action='add_note'"
    if (language is not None or diarization is not None) and action != "process":
        return "language and diarization apply only to action='process' (they set up transcription)"
    if verbosity is not None and action == "add_note":
        return "verbosity applies to the main summary, not to notes"
    return None


def _await_job(
    client: PlaudClient,
    task_id: str,
    kind: str,
    recording_id: str,
    wait: str,
    fields: dict[str, Any],
    still_running: str,
) -> dict[str, Any]:
    """Wait up to ``_WAIT_TIMEOUT_S`` for a note or re-summarize job (ADR 007).

    ``wait='none'`` returns as soon as Plaud accepts the job.  A job that
    outlives the budget comes back as a ``still_processing`` job handle
    polled with get_recording, the same as process_recording's other waits.
    """
    if wait == "none":
        return _json_result({**fields, "accepted": True})
    try:
        client.wait_for_task(task_id, what=kind, timeout_s=_WAIT_TIMEOUT_S, on_progress=current_progress())
    except PlaudApiError as exc:
        if not exc.is_soft_deadline_timeout():
            raise
        return _still_processing(kind, recording_id, "get_recording", still_running, **fields)
    return _json_result({"ok": True, **fields})


def build_handlers(get_client: Callable[[], PlaudClient | None]) -> dict[str, Callable[..., dict[str, Any]]]:
    def browse_recordings(
        limit: int = 20,
        since: str | None = None,
        until: str | None = None,
        query: str | None = None,
        folder: str | None = None,
        after: int = 0,
        trash: bool = False,
    ) -> dict[str, Any]:
        # #148: limit<=0 makes next_after == after forever, so an agent that
        # blindly re-invokes with the returned cursor loops without end.
        # Guard here in addition to the schema minimums (server.py _TOOLS) so
        # direct handler callers (tests, non-validating MCP clients) are also
        # protected.
        if not 1 <= limit <= MAX_BROWSE_LIMIT:
            return _error_result(
                f"limit must be between 1 and {MAX_BROWSE_LIMIT}",
                error_code="validation",
                retryable=False,
            )
        if after < 0:
            return _error_result(
                "after must be a non-negative integer (>= 0)",
                error_code="validation",
                retryable=False,
            )

        def inner(client: PlaudClient) -> dict[str, Any]:
            # The MCP `folder` parameter documents "" as the unfiled sentinel
            # (see server.py's tool schema); query.py's convention is unfiled=True.
            is_unfiled = folder == ""
            # trash=True lists the trash instead of active recordings, so an
            # agent can discover trashed IDs to restore.
            page, has_more = query_recordings(
                client,
                trash=trash,
                since_ms=parse_isoish(since, "since") if since else None,
                until_ms=parse_isoish(until, "until", end_of_day=True) if until else None,
                query=query,
                folder_id=None if is_unfiled else folder,
                unfiled=is_unfiled,
                after=after,
                limit=limit,
            )
            next_after = after + len(page) if has_more else None
            return _json_result(
                {
                    "items": [summarize_recording(item) for item in page],
                    "next_after": next_after,
                }
            )

        return _call(get_client, inner)

    def search_recordings(
        query: str,
        limit: int = SEARCH_RESULT_CAP,
        after: int = 0,
        since: str | None = None,
        until: str | None = None,
    ) -> dict[str, Any]:
        # Plaud answers one search with its SEARCH_RESULT_CAP best matches and
        # no server paging, so after/limit page over that fixed list.  Each
        # page re-runs the search; the ranking is stable between calls.
        if not query or not query.strip():
            return _error_result("query must not be empty", error_code="validation", retryable=False)
        if not 1 <= limit <= SEARCH_RESULT_CAP:
            return _error_result(
                f"limit must be between 1 and {SEARCH_RESULT_CAP}",
                error_code="validation",
                retryable=False,
            )
        if after < 0:
            return _error_result(
                "after must be a non-negative integer (>= 0)",
                error_code="validation",
                retryable=False,
            )

        def inner(client: PlaudClient) -> dict[str, Any]:
            matches = client.search_content(
                query.strip(),
                since_ms=parse_isoish(since, "since") if since else None,
                until_ms=parse_isoish(until, "until", end_of_day=True) if until else None,
            )
            page = matches[after : after + limit]
            end = after + len(page)
            payload: dict[str, Any] = {
                "items": [summarize_match(match) for match in page],
                "next_after": end if end < len(matches) else None,
                "capped": len(matches) >= SEARCH_RESULT_CAP,
            }
            if payload["capped"]:
                payload["notes"] = [
                    f"Plaud returns only its {SEARCH_RESULT_CAP} best matches, so other recordings may "
                    "also mention this. Narrow since/until to find them."
                ]
            return _json_result(payload)

        return _call(get_client, inner)

    def get_recording(
        recording_id: str,
        include: list[str] | None = None,
        transcript_after: int = 0,
        transcript_limit: int = DEFAULT_TRANSCRIPT_UTTERANCES,
        transcript_block: str = DEFAULT_TRANSCRIPT_BLOCK,
        raw_markdown: bool = False,
    ) -> dict[str, Any]:
        if transcript_block not in TRANSCRIPT_BLOCKS:
            return _error_result(
                f"transcript_block must be one of: {', '.join(TRANSCRIPT_BLOCKS)}",
                error_code="validation",
                retryable=False,
            )
        if transcript_after < 0:
            return _error_result(
                "transcript_after must be a non-negative integer (>= 0)",
                error_code="validation",
                retryable=False,
            )
        if not 1 <= transcript_limit <= MAX_TRANSCRIPT_UTTERANCES:
            return _error_result(
                f"transcript_limit must be between 1 and {MAX_TRANSCRIPT_UTTERANCES}",
                error_code="validation",
                retryable=False,
            )

        def inner(client: PlaudClient) -> dict[str, Any]:
            include_set = set(include or [])
            need_transcript = bool(include_set & {"transcript", "segments", "speakers"})
            need_summary = "summary" in include_set
            detail = client.get_recording(
                recording_id,
                include_transcript=need_transcript,
                include_summary=need_summary,
                transcript_block=transcript_block,
            )
            output = _summarize_detail(detail)
            # Every explanation for a missing piece goes here; several can
            # apply at once (no audio *and* no transcript yet).
            notes: list[str] = []
            if "speakers" in include_set:
                output["speakers"] = detail.speakers
            if "attachments" in include_set:
                output["attachments"] = [
                    {"id": a.id, "name": a.filename, "type": a.file_type}
                    for a in client.list_attachments(recording_id)
                ]
            if "audio_url" in include_set:
                link = client.get_audio_link(recording_id)
                output["audio_url"] = link.url if link else None
                if link is None:
                    notes.append(
                        "No audio is available for this recording — it may not have finished "
                        "syncing from the device yet."
                    )
                else:
                    output["audio_mime_type"] = link.mime_type
                    output["audio_url_expires_in_s"] = AUDIO_URL_TTL_S
            if include_set & {"transcript", "segments"}:
                segments = detail.transcript_segments or []
                page, start, next_after = _page_transcript(segments, transcript_after, transcript_limit)
                if "transcript" in include_set:
                    output["transcript"] = format_transcript(page)
                if "segments" in include_set:
                    output["transcript_segments"] = structured_segments(page, start)
                output["transcript_block"] = transcript_block
                output["transcript_utterance_count"] = len(segments)
                output["transcript_page_start"] = start
                output["transcript_page_end"] = start + len(page)
                # Keep transcript_truncated as the loud "this is partial" flag —
                # a caller that ignores transcript_next_after would otherwise
                # summarize half a meeting believing it had the whole thing.
                # It is NOT a continuation signal (a last page is still
                # partial); transcript_has_more / transcript_next_after are.
                output["transcript_truncated"] = next_after is not None or transcript_after > 0
                output["transcript_has_more"] = next_after is not None
                output["transcript_next_after"] = next_after
                # Hash of the whole block, identical on every page of the same
                # version; None when the requested block does not exist.
                block_available = transcript_block in (detail.transcript_blocks_available or [])
                output["transcript_fingerprint"] = (
                    transcript_fingerprint(segments, transcript_block) if block_available else None
                )
                note = _transcript_unavailable_note(detail, transcript_block)
                if note is not None:
                    notes.append(note)
            if "summary" in include_set:
                if detail.ai_content is None and detail.is_summary:
                    output["summary"] = None
                    # Don't dead-end the caller: Plaud says a summary exists but
                    # its content came back empty, which is transient
                    # far more often than not.  Name the retry explicitly.
                    notes.append(
                        "Plaud reports a summary exists for this recording but its content could "
                        "not be fetched. This is usually transient — retry get_recording in a "
                        "few moments."
                    )
                else:
                    output["summary"] = (
                        detail.ai_content if raw_markdown else render_summary(detail.ai_content)
                    )
                # Not "notes": that key already carries this response's advisory messages.
                output["extra_notes"] = notes_payload(detail.notes, raw=raw_markdown)
            if notes:
                output["notes"] = notes
            return _json_result(output)

        return _call(get_client, inner)

    def mutate_recording(
        recording_id: str | None = None,
        action: str | None = None,
        recording_ids: list[str] | None = None,
        new_name: str | None = None,
        folder_id: str | None = None,
        clear_folder: bool = False,
        keep: list[str] | None = None,
        share_days: int | None = None,
        share_never_expires: bool = False,
        share_audio: bool | None = None,
        share_transcript: bool | None = None,
        share_summary: bool | None = None,
        confirm: bool = False,
    ) -> dict[str, Any]:
        def inner(client: PlaudClient) -> dict[str, Any]:
            if not action:
                return _error_result(
                    "action is required",
                    error_code="validation",
                    retryable=False,
                )
            if recording_id is not None and recording_ids is not None:
                return _error_result(
                    "pass either recording_id or recording_ids, not both",
                    error_code="validation",
                    retryable=False,
                )
            ids = recording_ids if recording_ids is not None else ([recording_id] if recording_id else None)
            if not ids:
                return _error_result(
                    "recording_id or recording_ids is required",
                    error_code="validation",
                    retryable=False,
                )
            is_batch = recording_ids is not None

            if action in _SHARE_ACTIONS:
                if is_batch:
                    return _error_result(
                        f"action={action} does not support recording_ids (batch); pass a single recording_id",
                        error_code="validation",
                        retryable=False,
                    )
                return _share_action(
                    client,
                    action,
                    ids[0],
                    days=share_days,
                    never_expires=share_never_expires,
                    audio=share_audio,
                    transcript=share_transcript,
                    summary=share_summary,
                    confirm=confirm,
                )

            if action == "rename":
                if is_batch:
                    return _error_result(
                        "action=rename does not support recording_ids (batch); pass a single recording_id",
                        error_code="validation",
                        retryable=False,
                    )
                if not new_name:
                    return _error_result(
                        "new_name required for action=rename",
                        error_code="validation",
                        retryable=False,
                    )
                client.rename_recording(ids[0], new_name)
                return _json_result({"ok": True, "recording_id": ids[0], "new_name": new_name})

            if action == "trim":
                if is_batch:
                    return _error_result(
                        "action=trim does not support recording_ids (batch); pass a single recording_id",
                        error_code="validation",
                        retryable=False,
                    )
                if not keep:
                    return _error_result(
                        "keep is required for action=trim: ranges to keep, e.g. ['0:00-1:00', '5:00-6:30']",
                        error_code="validation",
                        retryable=False,
                    )
                ranges = parse_keep_ranges(keep)
                try:
                    detail = client.trim_recording(
                        ids[0], ranges, timeout_s=_WAIT_TIMEOUT_S, on_progress=current_progress()
                    )
                except PlaudWaitTimeoutError as exc:
                    # Plaud already named the copy, so the handle is its recording
                    # id.  Re-running would trim a second copy.
                    return _still_processing(
                        "trim",
                        exc.task_id or "",
                        "get_recording",
                        "Plaud is still trimming. Do not call mutate_recording(action='trim') again; "
                        "the copy is the job id, check it with get_recording in a minute.",
                        recording_id=ids[0],
                        new_recording_id=exc.task_id or "",
                    )
                return _json_result(
                    {
                        "ok": True,
                        "action": "trim",
                        "recording_id": ids[0],
                        "new_recording_id": detail.id,
                        "title": detail.filename,
                        "duration_minutes": round(detail.duration / 60000, 1),
                    }
                )

            if action == "trash":
                client.move_to_trash(ids)
                if is_batch:
                    return _json_result(
                        {"ok": True, "action": "trash", "recording_ids": ids, "count": len(ids)}
                    )
                return _json_result({"ok": True, "recording_id": ids[0], "action": "trash"})

            if action == "restore":
                client.restore_from_trash(ids)
                if is_batch:
                    return _json_result(
                        {"ok": True, "action": "restore", "recording_ids": ids, "count": len(ids)}
                    )
                return _json_result({"ok": True, "recording_id": ids[0], "action": "restore"})

            if action == "move":
                # #140: folder_id omitted (and clear_folder not set) used to
                # fall through to "clear" and silently unfile the recording.
                # The schema already documents folder_id as required for move
                # unless clear_folder=true — enforce that at runtime too.
                if folder_id is None and not clear_folder:
                    return _error_result(
                        "folder_id is required for action=move unless clear_folder=true",
                        error_code="validation",
                        retryable=False,
                    )
                actual_folder_id = None if (clear_folder or folder_id in ("", "-")) else folder_id
                client.set_recording_folder(ids, actual_folder_id)
                if is_batch:
                    return _json_result(
                        {
                            "ok": True,
                            "action": "move",
                            "recording_ids": ids,
                            "count": len(ids),
                            "folder_id": actual_folder_id,
                        }
                    )
                return _json_result({"ok": True, "recording_id": ids[0], "folder_id": actual_folder_id})

            return _error_result(
                f"unknown action: {action!r}",
                error_code="validation",
                retryable=False,
            )

        return _call(get_client, inner)

    def delete_recording(
        recording_id: str,
        confirm: bool = False,
    ) -> dict[str, Any]:
        # D4: destructive-op confirm gate — the MCP surface cannot show
        # interactive prompts over stdio, so we require the caller to pass
        # confirm=True only after the human has acknowledged the irreversibility.
        # This mirrors the CLI's --yes flag but is enforced server-side so that
        # even clients that ignore ToolAnnotations cannot silently hard-delete.
        if not confirm:
            return _error_result(
                "delete_recording requires explicit confirmation. "
                "Re-invoke with confirm=true only after the human has confirmed "
                "they want to permanently and irreversibly delete this recording.",
                error_code="validation",
                retryable=False,
            )

        def inner(client: PlaudClient) -> dict[str, Any]:
            client.delete_recordings([recording_id])
            return _json_result({"ok": True, "recording_id": recording_id})

        return _call(get_client, inner)

    def edit_transcript(
        recording_id: str,
        action: str,
        original_label: str | None = None,
        new_name: str | None = None,
        find: str | None = None,
        replace: str | None = None,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        def inner(client: PlaudClient) -> dict[str, Any]:
            if action == "rename_speaker":
                if original_label is None or new_name is None:
                    return _error_result(
                        "original_label and new_name are required for action=rename_speaker",
                        error_code="validation",
                        retryable=False,
                    )
                result = client.rename_speaker(recording_id, original_label, new_name)
                return _json_result(
                    {
                        "ok": True,
                        "recording_id": recording_id,
                        "action": "rename_speaker",
                        "segments_updated": result["segments_updated"],
                    }
                )

            if action == "correct":
                if find is None or replace is None:
                    return _error_result(
                        "find and replace are required for action=correct",
                        error_code="validation",
                        retryable=False,
                    )
                if dry_run:
                    matches = client.count_transcript_matches(recording_id, find)
                    return _json_result(
                        {
                            "ok": True,
                            "recording_id": recording_id,
                            "action": "correct",
                            "dry_run": True,
                            "matches": matches,
                        }
                    )
                result = client.correct_transcript(recording_id, find, replace)
                return _json_result(
                    {
                        "ok": True,
                        "recording_id": recording_id,
                        "action": "correct",
                        "replacements": result["replacements"],
                        "segments_changed": result["segments_changed"],
                    }
                )

            return _error_result(
                f"unknown action: {action!r} (expected 'rename_speaker' or 'correct')",
                error_code="validation",
                retryable=False,
            )

        return _call(get_client, inner)

    def upload_recording(
        file_path: str,
        title: str | None = None,
        folder_id: str | None = None,
        start_time: int | str | None = None,
        timezone_offset: float | None = None,
        attach_to: str | None = None,
    ) -> dict[str, Any]:
        def inner(client: PlaudClient) -> dict[str, Any]:
            from ..core.transcode import upload_with_transcode

            path = Path(file_path)
            if attach_to:
                # A document, not audio: it joins that recording's container
                # and the other upload options do not apply.
                if not path.is_file():
                    raise ValueError(f"file not found: {path}")
                attachment = client.attach_document(attach_to, path, title, on_progress=current_progress())
                return _json_result(
                    {
                        "ok": True,
                        "recording_id": attach_to,
                        "attachment": {"id": attachment.id, "name": attachment.filename},
                    }
                )
            rec_title = title or path.stem
            start_ms: int | None = None
            if isinstance(start_time, str):
                start_ms = parse_isoish(start_time, "start_time")
            elif isinstance(start_time, int):
                start_ms = start_time

            # No wait budget here (parts-sent progress goes to the client when
            # it asked): the call returns as soon as Plaud confirms
            # the upload, with the new recording_id.  A failure is a normal
            # error (mapped by _call), never a "still processing" that would
            # invite a duplicate upload.  The folder is set in the same
            # confirm request, so there is no "uploaded but not filed" state.
            outcome = upload_with_transcode(
                client,
                path,
                rec_title,
                start_time=start_ms,
                timezone_offset=timezone_offset,
                folder_id=folder_id,
                on_progress=current_progress(),
            )
            return _json_result(
                {
                    "ok": True,
                    "recording_id": outcome.recording.id,
                    "filename": outcome.recording.filename,
                    "transcoded": outcome.transcoded,
                }
            )

        return _call(get_client, inner)

    def process_recording(
        recording_id: str,
        action: str = "process",
        template_id: str | None = None,
        template_kind: str | None = None,
        note_name: str | None = None,
        language: str | None = None,
        diarization: bool | None = None,
        llm: str | None = None,
        verbosity: str | None = None,
        instruction: str | None = None,
        wait: str = "transcript",
        confirm: bool = False,
    ) -> dict[str, Any]:
        def inner(client: PlaudClient) -> dict[str, Any]:
            if wait not in PROCESS_WAIT_MODES:
                return _error_result(
                    "wait must be one of: none, transcript, summary",
                    error_code="validation",
                    retryable=False,
                )
            problem = _process_option_problem(action, note_name, language, diarization, verbosity, confirm)
            if problem:
                return _error_result(problem, error_code="validation", retryable=False)
            options = {
                "template_id": template_id,
                "template_kind": template_kind,
                "llm": llm,
                "instruction": instruction,
            }
            if action == "add_note":
                job = client.generate_note(recording_id, note_name=note_name, **options)
                fields = {"recording_id": recording_id, "note_id": job.note_id, "name": job.name}
                return _await_job(
                    client,
                    job.task_id,
                    "note",
                    recording_id,
                    wait,
                    fields,
                    "Plaud is still writing the note. Do not call process_recording again (that adds "
                    "a second note); check get_recording in a minute.",
                )
            if action == "resummarize":
                task_id = client.resummarize(recording_id, verbosity=verbosity, **options)
                return _await_job(
                    client,
                    task_id,
                    "summary",
                    recording_id,
                    wait,
                    {"recording_id": recording_id},
                    "Plaud is still rewriting the summary. Do not call process_recording again; "
                    "check get_recording in a minute.",
                )
            started = client.transcribe_and_summarize(
                recording_id,
                language=language,
                diarization=diarization,
                verbosity=verbosity,
                **options,
            )
            if not started:
                # Plaud keeps the existing output and ignores the new options,
                # so say so instead of implying a fresh run happened.
                detail = client.get_recording(recording_id)
                return _json_result(
                    {
                        "ok": True,
                        "recording_id": recording_id,
                        "already_processed": True,
                        "is_trans": detail.is_trans,
                        "is_summary": detail.is_summary,
                        "message": describe_unstarted_process(detail)
                        + " Use get_recording to read it, edit_summary to change the summary, "
                        "action='resummarize' to regenerate it, or action='add_note' for an extra note.",
                    }
                )
            if wait == "none":
                return _json_result(
                    {
                        "recording_id": recording_id,
                        "accepted": True,
                    }
                )
            # One budget for the whole call: the summary wait gets whatever the
            # transcription wait left, so wait="summary" is bounded by
            # _WAIT_TIMEOUT_S in total, not per stage.
            started = time.monotonic()
            deadline = started + _WAIT_TIMEOUT_S
            stages = [("transcription", client.wait_for_transcription)]
            if wait == "summary":
                stages.append(("summary", client.wait_for_summary))
            for kind, wait_fn in stages:
                try:
                    wait_fn(
                        recording_id,
                        timeout_s=max(0.0, deadline - time.monotonic()),
                        on_progress=shifted(current_progress(), time.monotonic() - started),
                    )
                except PlaudApiError as exc:
                    if not exc.is_soft_deadline_timeout():
                        raise
                    return _still_processing(
                        kind,
                        recording_id,
                        "get_recording",
                        f"Plaud is still working on the {kind}. Do not call process_recording "
                        f"again; check get_recording in a minute.",
                        recording_id=recording_id,
                        is_trans=kind == "summary",
                    )
            detail = client.get_recording(recording_id)
            return _json_result(
                {
                    "ok": True,
                    "recording_id": recording_id,
                    "is_trans": detail.is_trans,
                    "is_summary": detail.is_summary,
                }
            )

        return _call(get_client, inner)

    def export_transcript(
        recording_id: str,
        format: str = "json",  # noqa: A002 — the public MCP parameter name
        transcript_block: str = DEFAULT_TRANSCRIPT_BLOCK,
        with_speakers: bool = True,
        with_timestamps: bool = True,
        expected_transcript_fingerprint: str | None = None,
        output_path: str | None = None,
        overwrite: bool = False,
    ) -> dict[str, Any]:
        # The response carries file metadata only: the transcript itself is
        # written to disk in code so it never has to pass through the model.
        def inner(client: PlaudClient) -> dict[str, Any]:
            try:
                result = _export_transcript(
                    client,
                    recording_id,
                    fmt=format,
                    block=transcript_block,
                    with_speakers=with_speakers,
                    with_timestamps=with_timestamps,
                    expected_fingerprint=expected_transcript_fingerprint,
                    output_path=output_path,
                    overwrite=overwrite,
                )
            except TranscriptExportError as exc:
                return _error_result(str(exc), error_code=exc.code, retryable=False)
            return _json_result(result)

        return _call(get_client, inner)

    def list_folders() -> dict[str, Any]:
        def inner(client: PlaudClient) -> dict[str, Any]:
            return _json_result({"folders": [folder_dict(folder) for folder in client.list_folders()]})

        return _call(get_client, inner)

    def search_templates(query: str) -> dict[str, Any]:
        # The model list is included so one call returns everything
        # process_recording's template and llm options need.
        def inner(client: PlaudClient) -> dict[str, Any]:
            return _json_result(
                {
                    "templates": [template_dict(t) for t in client.search_templates(query)],
                    "models": [model_dict(m) for m in client.list_summary_models()],
                }
            )

        return _call(get_client, inner)

    def merge_recordings(
        recording_ids: list[str],
        title: str,
    ) -> dict[str, Any]:
        def inner(client: PlaudClient) -> dict[str, Any]:
            try:
                detail = client.merge_recordings(
                    recording_ids, title, timeout_s=_WAIT_TIMEOUT_S, on_progress=current_progress()
                )
            except PlaudWaitTimeoutError as exc:
                # The combine task keeps running on Plaud and no merged
                # recording id exists until it finishes.  Hand back the task id
                # and say plainly not to re-run: a second call is a second merge.
                return _still_processing(
                    "merge",
                    exc.task_id or "",
                    "browse_recordings",
                    f"Plaud is still merging. Do not call merge_recordings again; the new "
                    f"recording titled {title!r} will appear in browse_recordings when done.",
                    recording_ids=recording_ids,
                    title=title,
                )
            # Slim response: a fresh merge's detail dict is all nulls besides
            # id/filename (no transcript/summary yet), so the full
            # _summarize_detail() shape is dead weight.
            return _json_result({"ok": True, "recording_id": detail.id, "title": detail.filename})

        return _call(get_client, inner)

    def edit_summary(
        recording_id: str,
        action: str,
        find: str | None = None,
        replace: str | None = None,
        content: str | None = None,
        dry_run: bool = False,
        note_id: str | None = None,
    ) -> dict[str, Any]:
        def inner(client: PlaudClient) -> dict[str, Any]:
            if action == "correct":
                if find is None or replace is None:
                    return _error_result(
                        "find and replace are required for action=correct",
                        error_code="validation",
                        retryable=False,
                    )
                if dry_run:
                    matches = client.count_summary_matches(recording_id, find, note_id)
                    return _json_result(
                        {
                            "ok": True,
                            "recording_id": recording_id,
                            "action": "correct",
                            "dry_run": True,
                            "matches": matches,
                        }
                    )
                result = client.correct_summary(recording_id, find, replace, note_id)
                return _json_result(
                    {
                        "ok": True,
                        "recording_id": recording_id,
                        "action": "correct",
                        "replacements": result["replacements"],
                    }
                )

            if action == "replace":
                if content is None:
                    return _error_result(
                        "content is required for action=replace",
                        error_code="validation",
                        retryable=False,
                    )
                client.set_summary(recording_id, content, note_id)
                return _json_result({"ok": True, "recording_id": recording_id, "action": "replace"})

            return _error_result(
                f"unknown action: {action!r} (expected 'correct' or 'replace')",
                error_code="validation",
                retryable=False,
            )

        return _call(get_client, inner)

    def mutate_folder(
        action: str,
        folder_id: str | None = None,
        name: str | None = None,
        color: str | None = None,
        icon: str | None = None,
        parent_id: str | None = None,
        confirm: bool = False,
    ) -> dict[str, Any]:
        def inner(client: PlaudClient) -> dict[str, Any]:
            if action == "create":
                if not name:
                    return _error_result(
                        "name is required for action=create",
                        error_code="validation",
                        retryable=False,
                    )
                folder = client.create_folder(name, color=color, icon=icon, parent_id=parent_id)
                return _json_result(
                    {
                        "ok": True,
                        "action": "create",
                        "folder": folder_dict(folder),
                    }
                )

            if action == "edit":
                if not folder_id:
                    return _error_result(
                        "folder_id is required for action=edit",
                        error_code="validation",
                        retryable=False,
                    )
                if name is None and color is None and icon is None:
                    return _error_result(
                        "action=edit requires at least one of name, color, icon",
                        error_code="validation",
                        retryable=False,
                    )
                folder = client.update_folder(folder_id, name=name, color=color, icon=icon)
                return _json_result(
                    {
                        "ok": True,
                        "action": "edit",
                        "folder": folder_dict(folder),
                    }
                )

            if action == "delete":
                if not folder_id:
                    return _error_result(
                        "folder_id is required for action=delete",
                        error_code="validation",
                        retryable=False,
                    )
                # Deleting a folder is irreversible (the folder is gone; what
                # was inside survives one level up).  Gate it behind an
                # explicit confirm, mirroring delete_recording — the stdio
                # surface can show no interactive prompt.
                if not confirm:
                    return _error_result(
                        "Deleting a folder cannot be undone (what is inside is kept and moves up "
                        "one level). Re-invoke with confirm=true only after the human has "
                        "confirmed they want to delete this folder.",
                        error_code="validation",
                        retryable=False,
                    )
                client.delete_folder(folder_id)
                return _json_result({"ok": True, "action": "delete", "folder_id": folder_id})

            if action == "move":
                # parent_id is required, never defaulted: an omitted target
                # must not silently move a folder to the top level (#140).
                if not folder_id or not parent_id:
                    return _error_result(
                        'folder_id and parent_id are required for action=move (parent_id="root" '
                        "for the top level)",
                        error_code="validation",
                        retryable=False,
                    )
                folder = client.move_folder(folder_id, parent_id)
                return _json_result({"ok": True, "action": "move", "folder": folder_dict(folder)})

            return _error_result(
                f"unknown action: {action!r} (expected 'create', 'edit', 'move', or 'delete')",
                error_code="validation",
                retryable=False,
            )

        return _call(get_client, inner)

    def mutate_vocabulary(
        action: str,
        word: str | None = None,
        heard_as: str | None = None,
        language: str | None = None,
        word_id: str | None = None,
        word_ids: list[str] | None = None,
        confirm: bool = False,
    ) -> dict[str, Any]:
        def inner(client: PlaudClient) -> dict[str, Any]:
            if action == "list":
                entries = client.list_vocabulary()
                return _json_result(
                    {
                        "ok": True,
                        "action": "list",
                        "count": len(entries),
                        "words": [vocabulary_dict(e) for e in entries],
                    }
                )

            if action == "add":
                if not word:
                    return _validation_error("word is required for action=add")
                entry = client.add_vocabulary(word, heard_as=heard_as, language=language or VOCAB_LANGUAGE)
                return _json_result({"ok": True, "action": "add", "word": vocabulary_dict(entry)})

            if action == "edit":
                if not word_id:
                    return _validation_error("word_id is required for action=edit")
                if word is None and heard_as is None and language is None:
                    return _validation_error("action=edit requires at least one of word, heard_as, language")
                entry = client.update_vocabulary(word_id, word=word, heard_as=heard_as, language=language)
                return _json_result({"ok": True, "action": "edit", "word": vocabulary_dict(entry)})

            if action == "delete":
                ids = word_ids or ([word_id] if word_id else [])
                if not ids:
                    return _validation_error("word_ids (or word_id) is required for action=delete")
                if not confirm:
                    return _validation_error(
                        "Deleting vocabulary words cannot be undone. Re-invoke with confirm=true only after "
                        "the human has confirmed they want to delete them."
                    )
                return _json_result(
                    {"ok": True, "action": "delete", "deleted": client.delete_vocabulary(ids)}
                )

            return _validation_error(
                f"unknown action: {action!r} (expected 'list', 'add', 'edit', or 'delete')"
            )

        return _call(get_client, inner)

    return {
        "mutate_vocabulary": mutate_vocabulary,
        "browse_recordings": browse_recordings,
        "search_recordings": search_recordings,
        "get_recording": get_recording,
        "export_transcript": export_transcript,
        "mutate_recording": mutate_recording,
        "delete_recording": delete_recording,
        "edit_transcript": edit_transcript,
        "upload_recording": upload_recording,
        "process_recording": process_recording,
        "list_folders": list_folders,
        "search_templates": search_templates,
        "merge_recordings": merge_recordings,
        "edit_summary": edit_summary,
        "mutate_folder": mutate_folder,
    }
