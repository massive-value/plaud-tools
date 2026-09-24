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
    TRANSCRIPT_BLOCKS,
    PlaudClient,
    PlaudRecordingQuery,
)
from ..core.errors import PlaudApiError, PlaudSessionExpiredError, PlaudWaitTimeoutError
from ..core.query import (
    BROWSE_PAGE_SIZE,
    collect_filtered_paged,
    detail_summary_dict,
    folder_dict,
    format_transcript,
    parse_isoish,
    structured_segments,
    summarize_recording,
    transcript_fingerprint,
)
from ..core.session import SessionManager, SessionStore

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
    fields plus the MCP-specific extras (is_trash, language, used_template)."""
    extra = detail.extra_data or {}
    output = detail_summary_dict(detail)
    output["is_trash"] = detail.is_trash
    output["language"] = (extra.get("tranConfig") or {}).get("language")
    output["used_template"] = extra.get("used_template") or (extra.get("aiContentHeader") or {}).get(
        "used_template"
    )
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


def _count_summary_matches(client: PlaudClient, recording_id: str, find: str) -> int:
    """Count literal occurrences of ``find`` in a recording's AI summary, read-only.

    Backs ``edit_summary(action="correct", dry_run=True)``.  The transcript
    equivalent is ``PlaudClient.count_transcript_matches``.
    """
    detail = client.get_recording(recording_id, include_summary=True)
    if not detail.ai_content:
        raise ValueError(f"recording {recording_id} has no summary text to edit")
    return detail.ai_content.count(find)


PROCESS_WAIT_MODES = {"none", "transcript", "summary"}

# Largest browse page.  Matches the upstream page size, so one browse call is
# at most one Plaud request plus a look-ahead item.
MAX_BROWSE_LIMIT = 200

# How long one tool call may block waiting on a Plaud job (#151).  Most MCP
# clients give up on a call after 60-120 s, so waiting longer server-side only
# orphans the handler.  process_recording spends this once across both of its
# waits; merge_recordings spends it on the combine poll.  upload_recording is
# not bounded: an upload is an active transfer, and stopping it midway leaves
# nothing to check back on.  See ADR 007 for the job-handle response shape.
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
            since_ms = parse_isoish(since, "since") if since else None
            until_ms = parse_isoish(until, "until", end_of_day=True) if until else None
            has_filters = any(value is not None for value in (since, until, query, folder))
            # trash=True lists client.list_trash()'s underlying query
            # (is_trash=1) instead of active recordings (is_trash=0) — a real
            # gap the MCP had no way to discover trashed IDs to restore.
            is_trash_flag = 1 if trash else 0
            # The MCP `folder` parameter documents "" as the unfiled sentinel
            # (see server.py's tool schema); translate it into query.py's
            # single internal `unfiled` convention here rather than relying
            # on filter_recordings' own folder_id=="" special case (§7.8).
            is_unfiled = folder == ""
            if has_filters:
                page, has_more = collect_filtered_paged(
                    lambda skip, page_size: client.list_recordings(
                        PlaudRecordingQuery(
                            skip=skip,
                            limit=page_size,
                            is_trash=is_trash_flag,
                            sort_by="start_time",
                            is_desc=True,
                        )
                    ),
                    BROWSE_PAGE_SIZE,
                    since_ms=since_ms,
                    until_ms=until_ms,
                    query=query,
                    folder_id=None if is_unfiled else folder,
                    unfiled=is_unfiled,
                    after=after,
                    limit=limit,
                )
            else:
                # Ask for one extra item: a full page alone can't tell "more
                # exist" from "the library is an exact multiple of limit".
                fetched = client.list_recordings(
                    PlaudRecordingQuery(
                        skip=after if after else None,
                        limit=limit + 1,
                        is_trash=is_trash_flag,
                        sort_by="start_time",
                        is_desc=True,
                    )
                )
                page = fetched[:limit]
                has_more = len(fetched) > limit
            next_after = after + len(page) if has_more else None
            return _json_result(
                {
                    "items": [summarize_recording(item) for item in page],
                    "next_after": next_after,
                }
            )

        return _call(get_client, inner)

    def get_recording(
        recording_id: str,
        include: list[str] | None = None,
        transcript_after: int = 0,
        transcript_limit: int = DEFAULT_TRANSCRIPT_UTTERANCES,
        transcript_block: str = DEFAULT_TRANSCRIPT_BLOCK,
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
            if "audio_url" in include_set:
                url = client.get_audio_url(recording_id)
                output["audio_url"] = url
                if url is None:
                    notes.append(
                        "No audio is available for this recording — it may not have finished "
                        "syncing from the device yet."
                    )
                else:
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
                    transcript_fingerprint(segments) if block_available else None
                )
                note = _transcript_unavailable_note(detail, transcript_block)
                if note is not None:
                    notes.append(note)
            if "summary" in include_set:
                if detail.ai_content is None and detail.is_summary:
                    output["summary"] = None
                    # Don't dead-end the caller: Plaud says a summary exists but
                    # the data_link fetch came back empty, which is transient
                    # far more often than not.  Name the retry explicitly.
                    notes.append(
                        "Plaud reports a summary exists for this recording but its content could "
                        "not be fetched. This is usually transient — retry get_recording in a "
                        "few moments."
                    )
                else:
                    output["summary"] = detail.ai_content
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
    ) -> dict[str, Any]:
        def inner(client: PlaudClient) -> dict[str, Any]:
            from ..core.transcode import upload_with_transcode

            path = Path(file_path)
            rec_title = title or path.stem
            start_ms: int | None = None
            if isinstance(start_time, str):
                start_ms = parse_isoish(start_time, "start_time")
            elif isinstance(start_time, int):
                start_ms = start_time

            # No wait budget here: the call returns as soon as Plaud confirms
            # the upload, with the new recording_id.  A failure is a normal
            # error (mapped by _call), never a "still processing" that would
            # invite a duplicate upload.
            outcome = upload_with_transcode(
                client,
                path,
                rec_title,
                start_time=start_ms,
                timezone_offset=timezone_offset,
                folder_id=folder_id,
            )
            payload: dict[str, Any] = {
                "ok": True,
                "recording_id": outcome.recording.id,
                "filename": outcome.recording.filename,
                "transcoded": outcome.transcoded,
            }
            if outcome.folder_error is not None:
                # #149: the recording was created successfully but the
                # post-upload folder move failed — surface both so the caller
                # can retry the move instead of re-uploading the file.
                payload["folder_error"] = outcome.folder_error
            return _json_result(payload)

        return _call(get_client, inner)

    def process_recording(
        recording_id: str,
        template_type: str | None = None,
        language: str | None = None,
        diarization: bool | None = None,
        llm: str | None = None,
        wait: str = "transcript",
    ) -> dict[str, Any]:
        def inner(client: PlaudClient) -> dict[str, Any]:
            if wait not in PROCESS_WAIT_MODES:
                return _error_result(
                    "wait must be one of: none, transcript, summary",
                    error_code="validation",
                    retryable=False,
                )
            client.transcribe_and_summarize(
                recording_id,
                template_type=template_type,
                language=language,
                diarization=diarization,
                llm=llm,
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
            deadline = time.monotonic() + _WAIT_TIMEOUT_S
            stages = [("transcription", client.wait_for_transcription)]
            if wait == "summary":
                stages.append(("summary", client.wait_for_summary))
            for kind, wait_fn in stages:
                try:
                    wait_fn(recording_id, timeout_s=max(0.0, deadline - time.monotonic()))
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

    def list_folders() -> dict[str, Any]:
        def inner(client: PlaudClient) -> dict[str, Any]:
            tags = client.list_file_tags()
            return _json_result({"folders": [folder_dict(tag) for tag in tags]})

        return _call(get_client, inner)

    def merge_recordings(
        recording_ids: list[str],
        title: str,
    ) -> dict[str, Any]:
        def inner(client: PlaudClient) -> dict[str, Any]:
            try:
                detail = client.merge_recordings(recording_ids, title, timeout_s=_WAIT_TIMEOUT_S)
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
                    matches = _count_summary_matches(client, recording_id, find)
                    return _json_result(
                        {
                            "ok": True,
                            "recording_id": recording_id,
                            "action": "correct",
                            "dry_run": True,
                            "matches": matches,
                        }
                    )
                result = client.correct_summary(recording_id, find, replace)
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
                client.set_summary(recording_id, content)
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
                tag = client.create_folder(name, color=color, icon=icon)
                return _json_result(
                    {
                        "ok": True,
                        "action": "create",
                        "folder": folder_dict(tag),
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
                tag = client.update_folder(folder_id, name=name, color=color, icon=icon)
                return _json_result(
                    {
                        "ok": True,
                        "action": "edit",
                        "folder": folder_dict(tag),
                    }
                )

            if action == "delete":
                if not folder_id:
                    return _error_result(
                        "folder_id is required for action=delete",
                        error_code="validation",
                        retryable=False,
                    )
                # Deleting a folder is irreversible (the folder is gone; the
                # recordings inside survive but become unfiled).  Gate it behind
                # an explicit confirm, mirroring delete_recording — the stdio
                # surface can show no interactive prompt.
                if not confirm:
                    return _error_result(
                        "Deleting a folder cannot be undone (recordings inside are kept but "
                        "become unfiled). Re-invoke with confirm=true only after the human has "
                        "confirmed they want to delete this folder.",
                        error_code="validation",
                        retryable=False,
                    )
                client.delete_folder(folder_id)
                return _json_result({"ok": True, "action": "delete", "folder_id": folder_id})

            return _error_result(
                f"unknown action: {action!r} (expected 'create', 'edit', or 'delete')",
                error_code="validation",
                retryable=False,
            )

        return _call(get_client, inner)

    return {
        "browse_recordings": browse_recordings,
        "get_recording": get_recording,
        "mutate_recording": mutate_recording,
        "delete_recording": delete_recording,
        "edit_transcript": edit_transcript,
        "upload_recording": upload_recording,
        "process_recording": process_recording,
        "list_folders": list_folders,
        "merge_recordings": merge_recordings,
        "edit_summary": edit_summary,
        "mutate_folder": mutate_folder,
    }
