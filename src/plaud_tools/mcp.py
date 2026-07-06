from __future__ import annotations

import json
import logging
import os
import time
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any

from .appdata import events_path as _events_path
from .client import PlaudClient, PlaudRecordingQuery
from .errors import PlaudApiError, PlaudSessionExpiredError
from .query import BROWSE_PAGE_SIZE, collect_filtered_paged, folder_dict, parse_isoish, summarize_recording
from .session import SessionManager, SessionStore

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


def _diagnose_session_state() -> dict[str, Any]:
    """Best-effort snapshot of how the MCP currently sees the user session.

    Thin wrapper: calls ``SessionManager(SessionStore()).diagnose()`` for the
    session-y fields, then merges in MCP-process-local fields (PID, app version,
    env-token-present flag).  This keeps all JWT introspection in session.py
    while leaving only facade-local metadata here.  See ADR 004.
    """
    # Lazy import to avoid the circular import surfaced by
    # ``plaud_tools/__init__.py`` re-exporting ``build_handlers`` from this module.
    from . import __version__ as _app_version

    diag: dict[str, Any] = {
        "mcp_pid": os.getpid(),
        "mcp_version": _app_version,
        "env_token_present": bool(os.getenv("PLAUD_ACCESS_TOKEN")),
    }
    store = SessionStore()
    manager = SessionManager(store)
    diag.update(manager.diagnose())
    return diag


def _emit_session_expired(reason: str) -> None:
    """Log + write a session_expired event with full diagnostic context."""
    diag = _diagnose_session_state()
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
    # Compact separators (no indent, no spaces after , / :) — every MCP
    # response pays this cost once per tool call; the 20-35% whitespace tax
    # from the previous `indent=2` compounds over a long agent session.
    result: dict[str, Any] = {"content": [{"type": "text", "text": json.dumps(value, separators=(",", ":"))}]}
    if is_error:
        result["isError"] = True
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
        _emit_session_expired("token_expired")
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
            _emit_session_expired("http_401")
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
        # Without this, an OSError propagates past call_tool's TypeError-only
        # guard in server.py and the MCP SDK's generic catch-all reports it as
        # an unstructured plain-text error instead of {error, error_code,
        # retryable}.
        return _error_result(str(exc), error_code="io_error", retryable=False)


def _summarize_detail(detail: Any) -> dict[str, Any]:
    extra = detail.extra_data or {}
    return {
        "id": detail.id,
        "title": detail.filename,
        "date": datetime.fromtimestamp(detail.start_time / 1000).isoformat()[:16],
        "duration_minutes": round(detail.duration / 60000),
        "folder_id": detail.folder_id,
        "is_trash": detail.is_trash,
        "is_trans": detail.is_trans,
        "is_summary": detail.is_summary,
        "headline": (extra.get("aiContentHeader") or {}).get("headline"),
        "language": (extra.get("tranConfig") or {}).get("language"),
        "used_template": extra.get("used_template")
        or (extra.get("aiContentHeader") or {}).get("used_template"),
    }


def _slice_transcript(transcript: str, offset: int, max_chars: int | None) -> tuple[str, bool]:
    """Slice a formatted transcript string for pagination.

    Pure client-side slicing of the transcript text already fetched by
    ``get_recording`` (client.py's ``_fetch_transcript_segments`` /
    ``_format_transcript_from_segments``) — no extra network call. ``offset``
    and ``max_chars`` are validated by the caller before this runs.
    """
    end = len(transcript) if max_chars is None else offset + max_chars
    sliced = transcript[offset:end]
    truncated = offset > 0 or end < len(transcript)
    return sliced, truncated


def _count_transcript_matches(client: PlaudClient, recording_id: str, find: str) -> int:
    """Count literal occurrences of ``find`` in a recording's transcript, read-only.

    Backs ``edit_transcript(action="correct", dry_run=True)``. Mirrors the
    counting client.correct_transcript() does internally (client.py:684-698)
    without mutating anything — it fetches the same formatted transcript text
    `get_recording` already returns and counts on that.
    """
    detail = client.get_recording(recording_id, include_transcript=True)
    if not detail.transcript:
        raise ValueError(f"recording {recording_id} has no transcript yet")
    return detail.transcript.count(find)


def _count_summary_matches(client: PlaudClient, recording_id: str, find: str) -> int:
    """Count literal occurrences of ``find`` in a recording's AI summary, read-only.

    Backs ``edit_summary(action="correct", dry_run=True)``; see
    ``_count_transcript_matches`` for the same rationale applied to summaries.
    """
    detail = client.get_recording(recording_id, include_summary=True)
    if not detail.ai_content:
        raise ValueError(f"recording {recording_id} has no summary text to edit")
    return detail.ai_content.count(find)


PROCESS_WAIT_MODES = {"none", "transcript", "summary"}

# #151: client.wait_for_transcription/wait_for_summary poll for up to 600s
# (10 min) each by default — a wait="summary" call can block a handler for
# ~20 min total, long enough that a disconnected MCP client orphans the
# process holding the exe lock the updater fights.  Most MCP clients time out
# long before that (60-120s).  Bounding the wait here to a soft deadline and
# reporting "still_processing" on timeout is the minimum viable fix; true
# cancellation on client disconnect is a deeper change tracked as follow-up.
_WAIT_TIMEOUT_S = 90.0


def _wait_or_still_processing(wait_fn: Callable[..., None], recording_id: str) -> bool:
    """Call a client wait_for_* method bounded by ``_WAIT_TIMEOUT_S``.

    Returns True if the wait completed normally, False if it hit the soft
    deadline (surfaced by the client as a ``PlaudApiError`` with no
    http_status whose message ends "timed out after Ns").  Any other error
    (auth failure, 404, non-retryable API error) propagates unchanged.
    """
    try:
        wait_fn(recording_id, timeout_s=_WAIT_TIMEOUT_S)
    except PlaudApiError as exc:
        if exc.http_status is None and "timed out" in str(exc):
            return False
        raise
    return True


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
        if limit <= 0:
            return _error_result(
                "limit must be a positive integer (> 0)",
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
                page = client.list_recordings(
                    PlaudRecordingQuery(
                        skip=after if after else None,
                        limit=limit,
                        is_trash=is_trash_flag,
                        sort_by="start_time",
                        is_desc=True,
                    )
                )
                has_more = len(page) == limit
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
        transcript_offset: int = 0,
        transcript_max_chars: int | None = None,
    ) -> dict[str, Any]:
        if transcript_offset < 0:
            return _error_result(
                "transcript_offset must be a non-negative integer (>= 0)",
                error_code="validation",
                retryable=False,
            )
        if transcript_max_chars is not None and transcript_max_chars <= 0:
            return _error_result(
                "transcript_max_chars must be a positive integer (> 0)",
                error_code="validation",
                retryable=False,
            )

        def inner(client: PlaudClient) -> dict[str, Any]:
            include_set = set(include or [])
            need_transcript = bool(include_set & {"transcript", "speakers"})
            need_summary = "summary" in include_set
            detail = client.get_recording(
                recording_id,
                include_transcript=need_transcript,
                include_summary=need_summary,
            )
            output = _summarize_detail(detail)
            if "speakers" in include_set:
                output["speakers"] = detail.speakers
            if "transcript" in include_set:
                sliced, truncated = _slice_transcript(
                    detail.transcript or "", transcript_offset, transcript_max_chars
                )
                output["transcript"] = sliced
                output["transcript_truncated"] = truncated
            if "summary" in include_set:
                if detail.ai_content is None and detail.is_summary:
                    output["summary"] = "(summary exists on Plaud but could not be fetched)"
                else:
                    output["summary"] = detail.ai_content
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
                for rid in ids:
                    client.set_recording_folder(rid, actual_folder_id)
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
                    matches = _count_transcript_matches(client, recording_id, find)
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
            from .transcode import upload_with_transcode

            path = Path(file_path)
            rec_title = title or path.stem
            start_ms: int | None = None
            if isinstance(start_time, str):
                start_ms = parse_isoish(start_time, "start_time")
            elif isinstance(start_time, int):
                start_ms = start_time

            # ValueError (missing file / unsupported format) and RuntimeError
            # (ffmpeg failure) are intentionally not caught here — they
            # propagate to _call's except clauses, which already map them to
            # the correct structured {error_code, retryable} shape (#150).
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
            if not _wait_or_still_processing(client.wait_for_transcription, recording_id):
                return _json_result({"recording_id": recording_id, "status": "still_processing"})
            if wait == "summary":
                if not _wait_or_still_processing(client.wait_for_summary, recording_id):
                    return _json_result(
                        {"recording_id": recording_id, "status": "still_processing", "is_trans": True}
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
            return _json_result([folder_dict(tag) for tag in tags])

        return _call(get_client, inner)

    def merge_recordings(
        recording_ids: list[str],
        title: str,
    ) -> dict[str, Any]:
        def inner(client: PlaudClient) -> dict[str, Any]:
            detail = client.merge_recordings(recording_ids, title)
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
