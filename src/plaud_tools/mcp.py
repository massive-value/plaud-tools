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
from .query import filter_recordings, parse_isoish, summarize_recording
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


def _json_result(value: Any, is_error: bool = False) -> dict[str, Any]:
    result: dict[str, Any] = {"content": [{"type": "text", "text": json.dumps(value, indent=2)}]}
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
            "No Plaud session.",
            error_code="session_expired",
            retryable=False,
        )
    try:
        return fn(client)
    except PlaudSessionExpiredError as exc:
        _emit_session_expired("token_expired")
        return _error_result(
            str(exc),
            error_code="session_expired",
            retryable=False,
        )
    except PlaudApiError as exc:
        code, retryable = exc.classify()
        return _error_result(
            str(exc),
            error_code=code,
            retryable=retryable,
            http_status=exc.http_status,
        )
    except ValueError as exc:
        return _error_result(str(exc), error_code="validation", retryable=False)
    except RuntimeError as exc:
        return _error_result(str(exc), error_code="api_error", retryable=False)


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


PROCESS_WAIT_MODES = {"none", "transcript", "summary"}


def build_handlers(get_client: Callable[[], PlaudClient | None]) -> dict[str, Callable[..., dict[str, Any]]]:
    def browse_recordings(
        limit: int = 50,
        since: str | None = None,
        until: str | None = None,
        query: str | None = None,
        folder: str | None = None,
        after: int = 0,
    ) -> dict[str, Any]:
        def inner(client: PlaudClient) -> dict[str, Any]:
            since_ms = parse_isoish(since, "since") if since else None
            until_ms = parse_isoish(until, "until", end_of_day=True) if until else None
            has_filters = any(value is not None for value in (since, until, query, folder))
            if has_filters:
                all_items = client.list_recordings()
                all_items = filter_recordings(
                    all_items,
                    since_ms=since_ms,
                    until_ms=until_ms,
                    query=query,
                    folder_id=folder,
                )
                page = all_items[after : after + limit]
                has_more = len(all_items) > after + limit
            else:
                page = client.list_recordings(
                    PlaudRecordingQuery(
                        skip=after if after else None,
                        limit=limit,
                        is_trash=0,
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
    ) -> dict[str, Any]:
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
                output["transcript"] = detail.transcript
            if "summary" in include_set:
                if detail.ai_content is None and detail.is_summary:
                    output["summary"] = "(summary exists on Plaud but could not be fetched)"
                else:
                    output["summary"] = detail.ai_content
            return _json_result(output)

        return _call(get_client, inner)

    def mutate_recording(
        recording_id: str,
        mutation: str,
        new_name: str | None = None,
        folder_id: str | None = None,
        clear_folder: bool = False,
    ) -> dict[str, Any]:
        def inner(client: PlaudClient) -> dict[str, Any]:
            if mutation == "rename":
                if not new_name:
                    return _error_result(
                        "new_name required for rename",
                        error_code="validation",
                        retryable=False,
                    )
                client.rename_recording(recording_id, new_name)
                return _json_result({"ok": True, "recording_id": recording_id, "new_name": new_name})

            if mutation == "trash":
                client.move_to_trash([recording_id])
                return _json_result({"ok": True, "recording_id": recording_id, "mutation": "trash"})

            if mutation == "restore":
                client.restore_from_trash([recording_id])
                return _json_result({"ok": True, "recording_id": recording_id, "mutation": "restore"})

            if mutation == "move":
                actual_folder_id = (
                    None if (clear_folder or folder_id is None or folder_id in ("", "-")) else folder_id
                )
                client.set_recording_folder(recording_id, actual_folder_id)
                return _json_result({"ok": True, "recording_id": recording_id, "folder_id": actual_folder_id})

            return _error_result(
                f"unknown mutation: {mutation!r}",
                error_code="validation",
                retryable=False,
            )

        return _call(get_client, inner)

    def delete_recording(
        recording_id: str,
    ) -> dict[str, Any]:
        def inner(client: PlaudClient) -> dict[str, Any]:
            client.delete_recordings([recording_id])
            return _json_result({"ok": True, "recording_id": recording_id})

        return _call(get_client, inner)

    def rename_speaker(
        recording_id: str,
        original_label: str,
        new_name: str,
    ) -> dict[str, Any]:
        def inner(client: PlaudClient) -> dict[str, Any]:
            result = client.rename_speaker(recording_id, original_label, new_name)
            return _json_result(
                {
                    "ok": True,
                    "recording_id": recording_id,
                    "original_label": original_label,
                    "new_name": new_name,
                    "segments_updated": result["segments_updated"],
                }
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
            from .transcode import get_file_type, transcode_to_mp3

            path = Path(file_path)
            if not path.exists():
                return _error_result(
                    f"file not found: {file_path}",
                    error_code="validation",
                    retryable=False,
                )
            try:
                file_type, needs_transcode = get_file_type(path)
            except ValueError as exc:
                return _error_result(str(exc), error_code="validation", retryable=False)
            raw_bytes = path.read_bytes()
            try:
                audio_data = transcode_to_mp3(raw_bytes, path.suffix) if needs_transcode else raw_bytes
            except RuntimeError as exc:
                # transcode_to_mp3 raises RuntimeError when ffmpeg fails.
                return _json_result({"error": str(exc)}, is_error=True)
            rec_title = title or path.stem
            start_ms: int | None = None
            if isinstance(start_time, str):
                start_ms = parse_isoish(start_time, "start_time")
            elif isinstance(start_time, int):
                start_ms = start_time
            recording = client.upload_recording(
                audio_data, rec_title, file_type, start_time=start_ms, timezone_offset=timezone_offset
            )
            if folder_id:
                client.set_recording_folder(recording.id, folder_id)
            return _json_result(
                {
                    "ok": True,
                    "recording_id": recording.id,
                    "filename": recording.filename,
                    "transcoded": needs_transcode,
                }
            )

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
            client.wait_for_transcription(recording_id)
            if wait == "summary":
                client.wait_for_summary(recording_id)
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
            return _json_result(
                [{"id": tag.id, "name": tag.name, "color": tag.color, "icon": tag.icon} for tag in tags]
            )

        return _call(get_client, inner)

    def merge_recordings(
        recording_ids: list[str],
        title: str,
    ) -> dict[str, Any]:
        def inner(client: PlaudClient) -> dict[str, Any]:
            detail = client.merge_recordings(recording_ids, title)
            return _json_result(_summarize_detail(detail))

        return _call(get_client, inner)

    return {
        "browse_recordings": browse_recordings,
        "get_recording": get_recording,
        "mutate_recording": mutate_recording,
        "delete_recording": delete_recording,
        "rename_speaker": rename_speaker,
        "upload_recording": upload_recording,
        "process_recording": process_recording,
        "list_folders": list_folders,
        "merge_recordings": merge_recordings,
    }
