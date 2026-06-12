from __future__ import annotations

import json
import logging
import random
import time
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, overload
from urllib.parse import urlencode

from .errors import PlaudApiError, PlaudSessionExpiredError
from .models import BASE_URLS, BROWSER_USER_AGENT, FileTag, Recording, RecordingDetail, TaskStatus
from .query import summarize_recording as _summarize_recording_impl
from .session import SessionManager
from .transport import HttpResponse, Transport, UrllibTransport

_log = logging.getLogger(__name__)

_CHUNK_SIZE = 5 * 1024 * 1024  # 5 MiB — matches Plaud web client chunk strategy

# ---------------------------------------------------------------------------
# Injectable sleep/jitter for tests (Wave 2 / C5 — polite retry)
#
# Tests monkeypatch these module-level names instead of patching time.sleep
# globally, so the test harness can assert call counts and values without
# wall-clock cost.  Production code calls these the same way it would call
# time.sleep and random.uniform; the indirection is intentionally minimal.
# ---------------------------------------------------------------------------
_sleep = time.sleep  # replaced in tests: monkeypatch.setattr(client, "_sleep", lambda s: None)


def _jitter(lo: float, hi: float) -> float:
    """Return a random float in [lo, hi] — injected by tests via monkeypatch."""
    return random.uniform(lo, hi)


# ---------------------------------------------------------------------------
# Retry / backoff constants (Wave 2 / C5)
#
# _MAX_ATTEMPTS = 3 means 1 original attempt + 2 retries.
#
# Backoff formula (exponential with ±25 % full jitter):
#   base_delay = _BACKOFF_BASE * (2 ** attempt_index)   # 1 s, 3 s
#   actual_delay = jitter(base_delay * 0.75, base_delay * 1.25)
# On attempt_index 0 (first retry) → base ≈ 1 s → actual ∈ [0.75, 1.25] s
# On attempt_index 1 (second retry) → base ≈ 3 s → actual ∈ [2.25, 3.75] s
#
# When Retry-After is present we sleep max(retry_after, computed_backoff).
# Rationale: honour the server's instruction but never sleep *less* than our
# own back-off to avoid hammering a server that forgot the header on a 503.
# ---------------------------------------------------------------------------
_MAX_ATTEMPTS = 3
_BACKOFF_BASE = 1.0  # seconds — see formula above


@dataclass(slots=True)
class PlaudRecordingQuery:
    skip: int | None = None
    limit: int | None = None
    is_trash: int | None = None
    sort_by: str | None = None
    is_desc: bool | None = None


class PlaudClient:
    def __init__(self, session_manager: SessionManager, transport: Transport | None = None) -> None:
        self._session_manager = session_manager
        self._transport = transport or UrllibTransport()

    def list_recordings(self, query: PlaudRecordingQuery | None = None) -> list[Recording]:
        params: dict[str, str] = {}
        if query:
            if query.skip is not None:
                params["skip"] = str(query.skip)
            if query.limit is not None:
                params["limit"] = str(query.limit)
            if query.is_trash is not None:
                params["is_trash"] = str(query.is_trash)
            if query.sort_by is not None:
                params["sort_by"] = query.sort_by
            if query.is_desc is not None:
                params["is_desc"] = str(query.is_desc).lower()

        path = "/file/simple/web"
        if params:
            path = f"{path}?{urlencode(params)}"
        data = self._request_json("GET", path, strict=True)
        items = data.get("data_file_list") or data.get("data") or []
        records = [self._normalize_recording(item) for item in items]
        if query is None or query.is_trash is None:
            return [record for record in records if not record.is_trash]
        return records

    def get_recording(
        self,
        recording_id: str,
        include_transcript: bool = False,
        include_summary: bool = False,
    ) -> RecordingDetail:
        data = self._request_json("GET", f"/file/detail/{recording_id}", strict=True)
        raw = data.get("data", data)
        detail = self._normalize_recording_detail(raw, recording_id)
        if include_transcript:
            segments = self._fetch_transcript_segments(raw)
            detail.speakers = list(
                dict.fromkeys(
                    s.get("speaker") or s.get("original_speaker") or ""
                    for s in segments
                    if s.get("speaker") or s.get("original_speaker")
                )
            )
            detail.transcript = self._format_transcript_from_segments(segments)
        if include_summary and detail.is_summary and not detail.ai_content:
            detail.ai_content = self._fetch_summary_from_data_link(raw)
        return detail

    def fetch_transcript(self, recording_id: str) -> str:
        return self.get_recording(recording_id, include_transcript=True).transcript

    def get_user_info(self) -> dict[str, Any]:
        data = self._request_json("GET", "/user/me", strict=True)
        return data.get("data_user") or data.get("data") or data

    @overload
    def upload_recording(
        self,
        data: Path,
        filename: str,
        file_type: str,
        *,
        start_time: int | None = ...,
        timezone_offset: float | None = ...,
    ) -> Recording: ...

    @overload
    def upload_recording(
        self,
        data: bytes,
        filename: str,
        file_type: str,
        *,
        start_time: int | None = ...,
        timezone_offset: float | None = ...,
    ) -> Recording: ...

    def upload_recording(
        self,
        data: Path | bytes,
        filename: str,
        file_type: str,
        *,
        start_time: int | None = None,
        timezone_offset: float | None = None,
    ) -> Recording:
        """4-step upload: presign → S3 multipart PUT → merge_multipart → confirm_upload.

        *data* may be either a ``Path`` pointing to the audio file on disk, or
        a ``bytes`` buffer (kept for backward compatibility).  The ``Path``
        variant is preferred for large files: it reads 5 MiB chunks directly
        from disk rather than holding the entire file in memory.

        file_type must be "MP3", "OPUS", or "OGG". For other audio formats,
        transcode to MP3 first using plaud_tools.transcode.transcode_to_mp3_path()
        and pass the resulting path here.

        start_time_ms: millisecond epoch for the recording's date. Defaults to now.
        Plaud respects whatever value the client sends — pass the original
        recording's timestamp to preserve the date after re-upload.
        """
        if not filename.strip():
            raise ValueError("filename cannot be empty")
        if file_type not in ("MP3", "OPUS", "OGG"):
            raise ValueError(f"file_type must be MP3, OPUS, or OGG — got {file_type!r}")

        # Resolve filesize without loading the entire file into memory when a
        # Path is supplied.  For the bytes overload we keep the existing len()
        # behaviour so the presign filesize matches what we actually upload.
        if isinstance(data, Path):
            filesize = data.stat().st_size
            if filesize == 0:
                raise ValueError("data cannot be empty")
        else:
            filesize = len(data)
            if filesize == 0:
                raise ValueError("data cannot be empty")

        start_time_ms = start_time if start_time is not None else int(time.time() * 1000)
        if timezone_offset is None:
            offset = datetime.now().astimezone().utcoffset()
            tz = -offset.total_seconds() / 3600 if offset is not None else 0.0
        else:
            tz = timezone_offset

        presign = self._request_json(
            "POST",
            "/file/get_upload_presigned_url",
            strict=True,
            body={"filesize": filesize, "file_type": file_type},
        )
        presign_data = presign.get("data") or {}
        part_urls = presign_data.get("part_urls")
        upload_id = presign_data.get("upload_id")
        object_name = presign_data.get("object_name")
        if (
            not isinstance(part_urls, list)
            or not part_urls
            or not isinstance(upload_id, str)
            or not isinstance(object_name, str)
        ):
            raise PlaudApiError("Plaud presign response missing fields")

        # Upload chunks to S3. Content-Type matches the web client exactly —
        # the presigned signature does not bind Content-Type, but mimicking
        # the browser shields against any future tightening.
        #
        # For the Path variant we open the file once and read _CHUNK_SIZE bytes
        # per part, avoiding a full in-memory buffer.  For the bytes variant we
        # slice the existing buffer as before (no behaviour change for callers
        # that already have bytes in hand).
        parts: list[dict[str, Any]] = []
        if isinstance(data, Path):
            with data.open("rb") as fh:
                for i, url in enumerate(part_urls):
                    chunk = fh.read(_CHUNK_SIZE)
                    if not chunk:
                        # Presign returned more part URLs than the file has
                        # chunks — treat as a protocol error rather than
                        # silently uploading an empty part.
                        raise PlaudApiError(
                            f"Presign returned {len(part_urls)} part URLs but "
                            f"file exhausted after {i} chunk(s)"
                        )
                    response = self._s3_put(str(url), chunk)
                    etag = response.headers.get("etag", "").replace('"', "")
                    if not etag:
                        raise PlaudApiError(f"S3 upload returned no ETag for part {i + 1}")
                    parts.append({"Etag": etag, "PartNumber": i + 1})
        else:
            for i, url in enumerate(part_urls):
                start_byte = i * _CHUNK_SIZE
                end_byte = min(start_byte + _CHUNK_SIZE, len(data))
                chunk = data[start_byte:end_byte]
                response = self._s3_put(str(url), chunk)
                etag = response.headers.get("etag", "").replace('"', "")
                if not etag:
                    raise PlaudApiError(f"S3 upload returned no ETag for part {i + 1}")
                parts.append({"Etag": etag, "PartNumber": i + 1})

        self._request_json(
            "POST",
            "/file/merge_multipart",
            strict=True,
            body={"upload_id": upload_id, "object_name": object_name, "parts": parts},
        )

        confirm = self._request_json(
            "POST",
            "/file/confirm_upload",
            strict=True,
            body={
                "upload_id": upload_id,
                "object_name": object_name,
                "scene": 101,
                "is_tmp": 0,
                "support_mul_summ": True,
                "file_type": file_type,
                "filename": filename,
                "start_time": start_time_ms,
                "session_id": start_time_ms // 1000,
                "serial_number": str(uuid.uuid4()),
                "timezone": tz,
            },
        )
        return self._normalize_recording(confirm.get("data") or {})

    def _s3_put(self, url: str, chunk: bytes) -> HttpResponse:
        """PUT a chunk to a presigned S3 URL. No Plaud auth — signature is in the URL.

        S3 multipart chunks are up to 5 MiB (see _CHUNK_SIZE) and must fully
        transfer before the presigned URL expires.  On a poor link this can
        take well over the default 30 s transport budget, so we use a
        dedicated 120 s ceiling here.  The longer timeout is scoped to this
        call only — all other Plaud API traffic keeps the 30 s default.
        """
        return self._transport.request(
            method="PUT",
            url=url,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            body=chunk,
            timeout=120.0,
        )

    def merge_recordings(
        self,
        ids: list[str],
        filename: str,
        *,
        poll_interval_s: float = 3.0,
        timeout_s: float = 300.0,
    ) -> RecordingDetail:
        """Merge recordings via /file/combine and poll /file/combine-tasks until done."""
        if len(ids) < 2:
            raise ValueError("merge requires at least 2 recording IDs")
        if not filename.strip():
            raise ValueError("filename cannot be empty")

        start = self._request_json(
            "POST",
            "/file/combine",
            strict=True,
            body={"file_ids": ids, "filename": filename},
        )
        task_id = str(start.get("task_id") or "")
        if not task_id:
            raise PlaudApiError("Plaud combine response missing task_id")

        deadline = time.time() + timeout_s
        while True:
            if time.time() >= deadline:
                raise PlaudApiError(f"merge timed out after {int(timeout_s)}s")
            _sleep(poll_interval_s)
            try:
                poll = self._request_json("GET", f"/file/combine-tasks/{task_id}", strict=False)
            except PlaudApiError as exc:
                # Treat transient errors (429/5xx) as skipped polls so a
                # brief server hiccup during a long merge doesn't abort the
                # whole wait.  Non-transient errors (auth, 404, …) propagate
                # immediately — they signal a structural problem, not a blip.
                _code, retryable = exc.classify()
                if retryable:
                    _log.info(
                        "merge poll transient error (%s) — continuing until deadline",
                        exc,
                    )
                    continue
                raise
            task = poll.get("data") or {}
            if task.get("status") == "success":
                file_raw = task.get("file") or {}
                return self._normalize_recording_detail(file_raw, str(file_raw.get("file_id") or ""))
            if task.get("status") == "error":
                raise PlaudApiError(f"merge failed: {task.get('error_message') or 'unknown error'}")

    def wait_for_transcription(
        self,
        recording_id: str,
        *,
        timeout_s: float = 600.0,
        poll_interval_s: float = 5.0,
    ) -> None:
        """Poll get_recording() until is_trans is True or timeout elapses.

        A transient error (429 / 5xx) during a poll is treated as a skipped
        poll: we log and continue until the deadline.  Non-transient errors
        (e.g. 404, auth failure) propagate immediately.
        """
        deadline = time.time() + timeout_s
        while True:
            if time.time() >= deadline:
                raise PlaudApiError(f"transcription timed out after {int(timeout_s)}s")
            try:
                detail = self.get_recording(recording_id)
            except PlaudApiError as exc:
                _code, retryable = exc.classify()
                if retryable:
                    _log.info(
                        "wait_for_transcription transient error (%s) — continuing until deadline",
                        exc,
                    )
                    _sleep(poll_interval_s)
                    continue
                raise
            if detail.is_trans:
                return
            _sleep(poll_interval_s)

    def wait_for_summary(
        self,
        recording_id: str,
        *,
        timeout_s: float = 600.0,
        poll_interval_s: float = 5.0,
    ) -> None:
        """Poll get_recording() until is_summary is True or timeout elapses.

        A transient error (429 / 5xx) during a poll is treated as a skipped
        poll: we log and continue until the deadline.  Non-transient errors
        (e.g. 404, auth failure) propagate immediately.
        """
        deadline = time.time() + timeout_s
        while True:
            if time.time() >= deadline:
                raise PlaudApiError(f"summary timed out after {int(timeout_s)}s")
            try:
                detail = self.get_recording(recording_id)
            except PlaudApiError as exc:
                _code, retryable = exc.classify()
                if retryable:
                    _log.info(
                        "wait_for_summary transient error (%s) — continuing until deadline",
                        exc,
                    )
                    _sleep(poll_interval_s)
                    continue
                raise
            if detail.is_summary:
                return
            _sleep(poll_interval_s)

    def dump_raw_detail(self, recording_id: str) -> dict[str, Any]:
        """Return the raw /file/detail payload for debugging."""
        data = self._request_json("GET", f"/file/detail/{recording_id}", strict=True)
        raw = data.get("data", data)
        # .get() returns Any (the dict value type); the Plaud API always returns
        # a dict here but the annotation is Any — cast to satisfy warn_return_any.
        return raw if isinstance(raw, dict) else data

    def edit_transcript(self, recording_id: str, segments: list[dict[str, Any]]) -> None:
        self._request_json(
            "PATCH",
            f"/file/{recording_id}",
            strict=True,
            body={
                "trans_result": segments,
                "support_mul_summ": True,
            },
        )

    def rename_recording(self, recording_id: str, filename: str) -> None:
        if not filename.strip():
            raise ValueError("filename cannot be empty")
        self._request_json(
            "PATCH",
            f"/file/{recording_id}",
            strict=True,
            body={"filename": filename},
        )

    def list_file_tags(self) -> list[FileTag]:
        data = self._request_json("GET", "/filetag/", strict=True)
        items = data.get("data_filetag_list") or data.get("data") or data.get("filetags") or []
        return [
            FileTag(
                id=str(item.get("id") or item.get("filetag_id") or ""),
                name=str(item.get("name") or ""),
                color=str(item.get("color") or ""),
                icon=str(item.get("icon") or ""),
                raw=item,
            )
            for item in items
        ]

    def list_trash(self) -> list[Recording]:
        return self.list_recordings(PlaudRecordingQuery(is_trash=1))

    def set_recording_folder(self, recording_id: str, folder_id: str | None) -> None:
        self._request_json(
            "POST",
            "/file/update-tags",
            strict=True,
            body={
                "file_id_list": [recording_id],
                "filetag_id": folder_id or "",
            },
        )

    def move_to_trash(self, recording_ids: str | list[str]) -> None:
        ids = [recording_ids] if isinstance(recording_ids, str) else list(recording_ids)
        if not ids:
            raise ValueError("recording_ids cannot be empty")
        self._request_json("POST", "/file/trash/", strict=True, body=ids)

    def restore_from_trash(self, recording_ids: str | list[str]) -> None:
        ids = [recording_ids] if isinstance(recording_ids, str) else list(recording_ids)
        if not ids:
            raise ValueError("recording_ids cannot be empty")
        self._request_json("POST", "/file/untrash/", strict=True, body=ids)

    def delete_recordings(self, recording_ids: str | list[str]) -> None:
        ids = [recording_ids] if isinstance(recording_ids, str) else list(recording_ids)
        if not ids:
            raise ValueError("recording_ids cannot be empty")
        self._request_json("DELETE", "/file/", strict=True, body=ids)

    def transcribe_and_summarize(
        self,
        recording_id: str,
        *,
        template_type: str | None = None,
        language: str | None = None,
        diarization: bool | None = None,
        llm: str | None = None,
    ) -> None:
        if template_type and template_type.lower() == "default":
            template_type = "AUTO-SELECT"
        if language and "-" in language:
            language = language.split("-")[0]
        utcoffset = datetime.now().astimezone().utcoffset()
        info = json.dumps(
            {
                "language": language or "auto",
                "timezone": -utcoffset.total_seconds() / 3600 if utcoffset is not None else 0,
                "diarization": 0 if diarization is False else 1,
                "llm": llm or "auto",
            }
        )
        self._request_json(
            "POST",
            f"/ai/transsumm/{recording_id}",
            strict=True,
            body={
                "is_reload": 0,
                "summ_type": template_type or "AUTO-SELECT",
                "summ_type_type": "system",
                "info": info,
                "support_mul_summ": True,
            },
        )

    def get_task_status(self, recording_id: str | None = None) -> list[TaskStatus]:
        data = self._request_json("GET", "/ai/file-task-status", strict=True)
        raw = (data.get("data") or {}).get("file_status_list")
        items = raw if isinstance(raw, list) else []
        tasks = [
            TaskStatus(
                file_id=str(item.get("file_id") or ""),
                task_id=str(item.get("task_id") or ""),
                task_type=str(item.get("task_type") or ""),
                task_status=int(item.get("task_status") or 0),
                is_complete=int(item.get("task_status") or 0) == 1,
                sum_type=str(item.get("sum_type") or ""),
                sum_type_type=str(item.get("sum_type_type") or ""),
                post_id=int(item.get("post_id") or 0),
                ppc_status=int(item.get("ppc_status") or 0),
                is_chatllm=bool(item.get("is_chatllm")),
                auto_save=bool(item.get("auto_save")),
                raw=item,
            )
            for item in items
        ]
        if recording_id is not None:
            return [task for task in tasks if task.file_id == recording_id]
        return tasks

    def rename_speaker(self, recording_id: str, original_label: str, new_name: str) -> dict[str, int]:
        if not original_label.strip():
            raise ValueError("original_label cannot be empty")
        if not new_name.strip():
            raise ValueError("new_name cannot be empty")

        data = self._request_json("GET", f"/file/detail/{recording_id}", strict=True)
        raw = data.get("data", data)
        segments = self._fetch_transcript_segments(raw)
        if not segments:
            raise ValueError(f"recording {recording_id} has no transcript yet")

        updated = 0
        next_segments: list[dict[str, Any]] = []
        for segment in segments:
            if segment.get("original_speaker") == original_label:
                updated += 1
                next_segments.append({**segment, "speaker": new_name})
            else:
                next_segments.append(segment)

        if updated == 0:
            raise ValueError(f'no segments found with original_speaker "{original_label}"')

        self.edit_transcript(recording_id, next_segments)
        return {"segments_updated": updated}

    def _request_json(
        self,
        method: str,
        path: str,
        strict: bool,
        body: dict[str, Any] | list[Any] | None = None,
        *,
        _redirected: bool = False,
    ) -> dict[str, Any]:
        """Issue a Plaud API request and return the parsed JSON payload.

        Retry / backoff (Wave 2 / C5):
            On HTTP 429 or HTTP 5xx the request is retried up to
            ``_MAX_ATTEMPTS - 1`` additional times (3 total) with exponential
            backoff + ±25 % full jitter.  When the server supplies a
            ``Retry-After`` header we sleep the *larger* of Retry-After and the
            computed back-off delay to respect the server's instruction while
            avoiding hammering.

        Region-redirect (Wave 0 / A2):
            A ``status == -302`` payload triggers a single region update and one
            immediate retry (no delay).  The ``_redirected`` flag bounds this to
            one hop — a redirect on every call raises ``PlaudApiError('region
            redirect loop')`` rather than recursing indefinitely.

        The two mechanisms compose cleanly: retry/backoff wraps the transport
        call; region-redirect is a recursive call at the application layer after
        the region has been persisted.  The ``_redirected`` flag is never
        propagated through the retry loop so a 429 retry cannot accidentally
        suppress the redirect guard.
        """
        try:
            session = self._session_manager.require()
        except PlaudSessionExpiredError:
            # Cache may be stale — discard it and let the error propagate.
            self._session_manager.invalidate_cache()
            raise

        url = f"{BASE_URLS.get(session.region, BASE_URLS['us'])}{path}"
        headers = {
            "Authorization": f"Bearer {session.access_token}",
            "Content-Type": "application/json",
            "User-Agent": BROWSER_USER_AGENT,
            "app-platform": "web",
            "edit-from": "web",
        }
        encoded_body = json.dumps(body).encode("utf-8") if body is not None else None

        last_error: PlaudApiError | None = None
        for attempt in range(_MAX_ATTEMPTS):
            if attempt > 0:
                # Exponential backoff with ±25 % full jitter.
                # base_delay doubles each retry: 1 s → 3 s (base * 2^attempt_index
                # where attempt_index = attempt - 1 for readability, but since
                # attempt starts at 1 here, base * 2^(attempt-1) gives 1 s, 2 s;
                # we use _BACKOFF_BASE * (2 ** attempt) which gives 2 s, 4 s —
                # but we want ~1 s and ~3 s so we use attempt directly:
                #   attempt=1 → _BACKOFF_BASE * 2^0 = 1 s
                #   attempt=2 → _BACKOFF_BASE * 2^1 = 2 s  (jittered to ~[1.5, 2.5])
                # This is intentionally kept simple.  See module-level doc for formula.
                base_delay = _BACKOFF_BASE * (2 ** (attempt - 1))
                computed_delay = _jitter(base_delay * 0.75, base_delay * 1.25)

                # Honour Retry-After: sleep the larger of the server's hint and
                # our computed back-off.  Never sleep *less* than the computed
                # delay — a server that forgets the header on a 503 should still
                # get back-off respect.
                retry_after = last_error.retry_after if last_error is not None else None
                sleep_s = max(computed_delay, retry_after) if retry_after is not None else computed_delay

                _log.debug(
                    "Plaud API retry %d/%d for %s %s — sleeping %.2fs (computed=%.2fs, retry_after=%s)",
                    attempt,
                    _MAX_ATTEMPTS - 1,
                    method,
                    path,
                    sleep_s,
                    computed_delay,
                    retry_after,
                )
                _sleep(sleep_s)

            try:
                response = self._transport.request(
                    method=method,
                    url=url,
                    headers=headers,
                    body=encoded_body,
                )
            except PlaudApiError as exc:
                _code, retryable = exc.classify()
                if retryable and attempt < _MAX_ATTEMPTS - 1:
                    last_error = exc
                    continue
                raise

            payload = response.json()
            if not isinstance(payload, dict):
                raise PlaudApiError("Plaud API returned a non-object payload.")

            if payload.get("status") == -302:
                # Guard against a server that returns -302 on every call — we
                # allow at most one region redirect per outbound request.  The
                # update_region persistence is load-bearing (fragile Plaud
                # protocol): it must run before any retry so the next request
                # hits the correct base URL.
                if _redirected:
                    raise PlaudApiError("region redirect loop")
                domain = ((payload.get("data") or {}).get("domains") or {}).get("api", "")
                next_region = "eu" if "euc1" in domain else "us"
                self._session_manager.update_region(next_region)
                return self._request_json(method, path, strict=strict, body=body, _redirected=True)

            if strict and payload.get("status") != 0:
                msg = payload.get("msg") or f"status {payload.get('status')}"
                raise PlaudApiError(f"Plaud API error: {msg}")
            return payload

        # Should be unreachable — loop always returns or raises — but the type
        # checker cannot prove it.  Raise the last captured error.
        assert last_error is not None
        raise last_error

    def _normalize_recording(self, raw: dict[str, Any]) -> Recording:
        return Recording(
            id=str(raw.get("id") or raw.get("file_id") or ""),
            filename=str(raw.get("filename") or raw.get("file_name") or ""),
            start_time=int(raw.get("start_time") or 0),
            duration=int(raw.get("duration") or 0),
            is_trash=bool(raw.get("is_trash")),
            is_trans=bool(raw.get("is_trans")),
            is_summary=bool(raw.get("is_summary")),
            filetag_id_list=list(raw.get("filetag_id_list") or []),
            raw=raw,
        )

    def _normalize_recording_detail(self, raw: dict[str, Any], fallback_id: str) -> RecordingDetail:
        content_list = raw.get("content_list") or []

        def find_item(data_type: str) -> dict[str, Any] | None:
            for item in content_list:
                if item.get("data_type") == data_type:
                    # item comes from raw["content_list"] which is list[Any];
                    # return type is narrowed here — the Plaud API always
                    # returns dicts in this list.
                    return item if isinstance(item, dict) else None
            return None

        def is_complete(data_type: str) -> bool:
            item = find_item(data_type)
            return bool(item and item.get("task_status") == 1)

        auto_sum_item = find_item("auto_sum_note")
        return RecordingDetail(
            id=str(raw.get("file_id") or raw.get("id") or fallback_id),
            filename=str(raw.get("file_name") or raw.get("filename") or fallback_id),
            start_time=int(raw.get("start_time") or 0),
            duration=int(raw.get("duration") or 0),
            folder_id=(raw.get("filetag_id_list") or [None])[0],
            is_trash=bool(raw.get("is_trash")),
            is_trans=is_complete("transaction"),
            is_summary=is_complete("auto_sum_note"),
            scene=raw.get("scene"),
            transcript="",
            ai_content=self._extract_inline_summary(
                raw, auto_sum_item.get("data_id") if auto_sum_item else None
            ),
            extra_data=raw.get("extra_data") or {},
            raw=raw,
        )

    def _extract_inline_summary(self, raw: dict[str, Any], auto_sum_data_id: str | None) -> str | None:
        def _parse_summary_obj(obj: dict[str, Any]) -> str | None:
            for key in ("ai_content", "content", "text", "markdown"):
                val = obj.get(key)
                if isinstance(val, str) and val:
                    return val
            return None

        def _try_item(item: dict[str, Any]) -> str | None:
            data_content = item.get("data_content")
            if isinstance(data_content, dict):
                return _parse_summary_obj(data_content)
            if isinstance(data_content, str) and data_content:
                try:
                    parsed = json.loads(data_content)
                    if isinstance(parsed, dict):
                        return _parse_summary_obj(parsed)
                    if isinstance(parsed, str):
                        return parsed or None
                except json.JSONDecodeError:
                    return data_content or None
            return None

        pre_list = raw.get("pre_download_content_list") or []
        # Primary: match by data_id
        if auto_sum_data_id:
            for item in pre_list:
                if item.get("data_id") == auto_sum_data_id:
                    return _try_item(item)
        # Fallback: match by data_type when data_id didn't match (or wasn't available)
        for item in pre_list:
            if item.get("data_type") == "auto_sum_note":
                result = _try_item(item)
                if result is not None:
                    return result
        return None

    def _fetch_summary_from_data_link(self, raw: dict[str, Any]) -> str | None:
        summary_item = None
        for item in raw.get("content_list") or []:
            if item.get("data_type") == "auto_sum_note" and item.get("task_status") == 1:
                summary_item = item
                break
        if not summary_item or not summary_item.get("data_link"):
            return None
        response = self._transport.request(
            method="GET",
            url=str(summary_item["data_link"]),
            headers={"User-Agent": BROWSER_USER_AGENT},
        )
        if response.status_code < 200 or response.status_code >= 300:
            return None
        try:
            body = response.json()
        except (ValueError, json.JSONDecodeError):
            text = response.text()
            return text or None
        if isinstance(body, str):
            return body or None
        if isinstance(body, dict):
            for key in ("ai_content", "content", "text", "markdown"):
                val = body.get(key)
                if isinstance(val, str) and val:
                    return val
        return None

    def _fetch_transcript_segments(self, raw: dict[str, Any]) -> list[dict[str, Any]]:
        transcript_item = None
        for item in raw.get("content_list") or []:
            if item.get("data_type") == "transaction" and item.get("task_status") == 1:
                transcript_item = item
                break
        if not transcript_item or not transcript_item.get("data_link"):
            return []
        response = self._transport.request(
            method="GET",
            url=str(transcript_item["data_link"]),
            headers={"User-Agent": BROWSER_USER_AGENT},
        )
        if response.status_code < 200 or response.status_code >= 300:
            return []
        body = response.json()
        if isinstance(body, list):
            return body
        if isinstance(body, dict):
            segments = body.get("trans_result")
            return segments if isinstance(segments, list) else []
        return []

    def _format_transcript_from_segments(self, segments: list[dict[str, Any]]) -> str:
        parts: list[str] = []
        for segment in segments:
            speaker = segment.get("speaker") or segment.get("original_speaker") or ""
            content = segment.get("content") or ""
            parts.append(f"{speaker}: {content}" if speaker else content)
        return "\n\n".join(parts)


def summarize_recording_for_cli(recording: Recording) -> dict[str, Any]:
    """Re-export shim — canonical implementation lives in query.summarize_recording."""
    return _summarize_recording_impl(recording)
