from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from .client import PlaudClient, PlaudRecordingQuery
from .errors import PlaudApiError, PlaudSessionExpiredError


def _json_result(value: Any, is_error: bool = False) -> dict[str, Any]:
    result = {"content": [{"type": "text", "text": json.dumps(value, indent=2)}]}
    if is_error:
        result["isError"] = True
    return result


def _expired_result(message: str) -> dict[str, Any]:
    return _json_result({"error": message}, is_error=True)


def _call(get_client: Callable[[], PlaudClient | None], fn: Callable[[PlaudClient], Any]) -> Any:
    client = get_client()
    if client is None:
        return _expired_result("No Plaud session.")
    try:
        return fn(client)
    except PlaudSessionExpiredError as exc:
        return _expired_result(str(exc))
    except (PlaudApiError, ValueError, RuntimeError) as exc:
        return _json_result({"error": str(exc)}, is_error=True)


def _parse_isoish(value: str, field_name: str, *, end_of_day: bool = False) -> int:
    try:
        normalized = value.replace("Z", "+00:00")
        dt = datetime.fromisoformat(normalized)
        if end_of_day and "T" not in value:
            dt = dt.replace(hour=23, minute=59, second=59, microsecond=999999)
        return int(dt.timestamp() * 1000)
    except ValueError as exc:
        raise ValueError(f"Invalid {field_name} value: {value}") from exc


def _filter_recordings(items: list[Any], *, since_ms, until_ms, query, folder_id):
    filtered = list(items)
    if since_ms is not None:
        filtered = [item for item in filtered if item.start_time >= since_ms]
    if until_ms is not None:
        filtered = [item for item in filtered if item.start_time <= until_ms]
    if query:
        query_lower = query.lower()
        filtered = [item for item in filtered if query_lower in item.filename.lower()]
    if folder_id is None:
        return filtered
    if folder_id == "":
        return [item for item in filtered if not item.filetag_id_list]
    return [item for item in filtered if folder_id in item.filetag_id_list]


def _summarize_recording(item: Any) -> dict[str, Any]:
    return {
        "id": item.id,
        "title": item.filename,
        "date": datetime.fromtimestamp(item.start_time / 1000).isoformat()[:16],
        "duration_minutes": round(item.duration / 60000),
        "has_transcript": item.is_trans,
        "folder_id": item.filetag_id_list[0] if item.filetag_id_list else None,
    }


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
        "used_template": extra.get("used_template") or (extra.get("aiContentHeader") or {}).get("used_template"),
    }


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
            since_ms = _parse_isoish(since, "since") if since else None
            until_ms = _parse_isoish(until, "until", end_of_day=True) if until else None
            has_filters = any(value is not None for value in (since, until, query, folder))
            if has_filters:
                items = client.list_recordings()
                items = _filter_recordings(
                    items,
                    since_ms=since_ms,
                    until_ms=until_ms,
                    query=query,
                    folder_id=folder,
                )
                items = sorted(items, key=lambda item: item.start_time, reverse=True)
                items = items[after:after + limit]
            else:
                items = client.list_recordings(
                    PlaudRecordingQuery(
                        skip=after if after else None,
                        limit=limit,
                        is_trash=0,
                        sort_by="start_time",
                        is_desc=True,
                    )
                )
            return _json_result([_summarize_recording(item) for item in items])

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
        original_label: str | None = None,
    ) -> dict[str, Any]:
        def inner(client: PlaudClient) -> dict[str, Any]:
            if mutation == "rename":
                if not new_name:
                    return _json_result({"error": "new_name required for rename"}, is_error=True)
                client.rename_recording(recording_id, new_name)
                return _json_result({"ok": True, "recording_id": recording_id, "new_name": new_name})

            if mutation == "trash":
                client.move_to_trash([recording_id])
                return _json_result({"ok": True, "recording_id": recording_id, "mutation": "trash"})

            if mutation == "restore":
                client.restore_from_trash([recording_id])
                return _json_result({"ok": True, "recording_id": recording_id, "mutation": "restore"})

            if mutation == "delete":
                client.delete_recordings([recording_id])
                return _json_result({"ok": True, "recording_id": recording_id, "mutation": "delete"})

            if mutation == "move":
                actual_folder_id = None if (folder_id is None or folder_id in ("", "-")) else folder_id
                client.set_recording_folder(recording_id, actual_folder_id)
                return _json_result({"ok": True, "recording_id": recording_id, "folder_id": actual_folder_id})

            if mutation == "rename_speaker":
                if not original_label or not new_name:
                    return _json_result(
                        {"error": "original_label and new_name required for rename_speaker"}, is_error=True
                    )
                result = client.rename_speaker(recording_id, original_label, new_name)
                return _json_result({
                    "ok": True,
                    "recording_id": recording_id,
                    "original_label": original_label,
                    "new_name": new_name,
                    "segments_updated": result["segments_updated"],
                })

            return _json_result({"error": f"unknown mutation: {mutation!r}"}, is_error=True)

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
                return _json_result({"error": f"file not found: {file_path}"}, is_error=True)
            try:
                file_type, needs_transcode = get_file_type(path)
            except ValueError as exc:
                return _json_result({"error": str(exc)}, is_error=True)
            raw_bytes = path.read_bytes()
            audio_data = transcode_to_mp3(raw_bytes, path.suffix) if needs_transcode else raw_bytes
            rec_title = title or path.stem
            start_ms: int | None = None
            if isinstance(start_time, str):
                start_ms = _parse_isoish(start_time, "start_time")
            elif isinstance(start_time, int):
                start_ms = start_time
            recording = client.upload_recording(audio_data, rec_title, file_type, start_time=start_ms, timezone_offset=timezone_offset)
            if folder_id:
                client.set_recording_folder(recording.id, folder_id)
            return _json_result({
                "ok": True,
                "recording_id": recording.id,
                "filename": recording.filename,
                "transcoded": needs_transcode,
            })

        return _call(get_client, inner)

    def process_recording(
        recording_id: str,
        template_type: str | None = None,
        language: str | None = None,
        diarization: bool | None = None,
        llm: str | None = None,
    ) -> dict[str, Any]:
        def inner(client: PlaudClient) -> dict[str, Any]:
            client.transcribe_and_summarize(
                recording_id,
                template_type=template_type,
                language=language,
                diarization=diarization,
                llm=llm,
            )
            client.wait_for_transcription(recording_id)
            client.wait_for_summary(recording_id)
            detail = client.get_recording(recording_id)
            return _json_result({
                "ok": True,
                "recording_id": recording_id,
                "is_trans": detail.is_trans,
                "is_summary": detail.is_summary,
            })

        return _call(get_client, inner)

    def list_folders() -> dict[str, Any]:
        def inner(client: PlaudClient) -> dict[str, Any]:
            tags = client.list_file_tags()
            return _json_result([
                {"id": tag.id, "name": tag.name, "color": tag.color, "icon": tag.icon}
                for tag in tags
            ])

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
        "upload_recording": upload_recording,
        "process_recording": process_recording,
        "list_folders": list_folders,
        "merge_recordings": merge_recordings,
    }


# Keep old name as alias during transition
def build_read_handlers(get_client: Callable[[], PlaudClient | None]) -> dict[str, Callable[..., dict[str, Any]]]:
    return build_handlers(get_client)
