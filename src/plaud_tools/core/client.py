from __future__ import annotations

import json
import logging
import random
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, TypeVar
from urllib.parse import urlencode

from .errors import PlaudApiError, PlaudSessionExpiredError, PlaudWaitTimeoutError
from .models import (
    BROWSER_USER_AGENT,
    ContentMatch,
    FileTag,
    Recording,
    RecordingDetail,
    TaskStatus,
    base_url,
    redirect_api_domain,
    region_for_api_domain,
)
from .query import content_snippet, format_transcript
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
#   base_delay = _BACKOFF_BASE * (2 ** retry_index)   # 1 s, 2 s
#   actual_delay = jitter(base_delay * 0.75, base_delay * 1.25)
# First retry (retry_index 0)  → base 1 s → actual ∈ [0.75, 1.25] s
# Second retry (retry_index 1) → base 2 s → actual ∈ [1.5, 2.5] s
#
# When Retry-After is present we sleep max(retry_after, computed_backoff).
# Rationale: honour the server's instruction but never sleep *less* than our
# own back-off to avoid hammering a server that forgot the header on a 503.
# ---------------------------------------------------------------------------
_MAX_ATTEMPTS = 3
_BACKOFF_BASE = 1.0  # seconds — see formula above

# Defaults for folder (filetag) creation.  The Plaud web client always sends an
# icon and color when creating a folder; the API appears to require both.  These
# mirror the values Plaud assigns to most existing folders (icon "e627" is the
# common default folder glyph) so a folder created via the API is visually
# indistinguishable from one created in the web app when the caller omits them.
_DEFAULT_FOLDER_ICON = "e627"
_DEFAULT_FOLDER_COLOR = "#4c8eff"

# Transcript blocks Plaud publishes in a recording's ``content_list``, each a
# separate `data_type` with its own `data_link`:
#
#   transaction         raw diarized transcript — speaker + timestamps.  The
#                       only editable block: rename_speaker / correct_transcript
#                       PATCH `trans_result` back, so they must read what they
#                       write.  Always the default.
#   transaction_polish  Plaud's AI-cleaned pass over the same utterances (filler
#                       words dropped, punctuation repaired), same per-utterance
#                       shape, speaker + timestamps preserved.  Read-only here —
#                       editing it is not a flow the web app exposes.
#
# Plaud also publishes an `outline` block, deliberately NOT listed: it is not an
# utterance list, so format_transcript would render it as nonsense.  Supporting
# it means a second parse+format path.
# ponytail: add `outline` when something actually asks for a section outline —
# the shape work is real and nothing needs it today.
TRANSCRIPT_BLOCKS = ("transaction", "transaction_polish")
DEFAULT_TRANSCRIPT_BLOCK = "transaction"

# How long `/file/temp-url/{id}` presigned audio URLs stay valid, from the
# `Expires` parameter observed across captures.  Notably shorter than the 24h
# the official Plaud developer API hands out, which is why nothing caches these.
AUDIO_URL_TTL_S = 3600

# Most recordings one full-text search returns.  Observed, not documented:
# every broad query against a library of hundreds of recordings came back with
# exactly 20, and no paging parameter changed that (see PlaudClient.search_content).
SEARCH_RESULT_CAP = 20


_T = TypeVar("_T")


def _local_utc_offset_hours() -> float:
    """This machine's UTC offset in hours, signed the way Plaud expects.

    Plaud's web app sends the plain UTC offset: US Central (UTC-6) is ``-6``,
    CET (UTC+1) is ``1``, as seen in HAR captures of both upload and
    transcribe requests.  (JavaScript's ``getTimezoneOffset()`` has the
    opposite sign; that is not what goes on the wire.)
    """
    offset = datetime.now().astimezone().utcoffset()
    return offset.total_seconds() / 3600 if offset is not None else 0.0


def count_segment_matches(segments: list[dict[str, Any]], find: str) -> tuple[int, int]:
    """Return ``(occurrences, segments_containing)`` of *find* in segment content.

    Literal, case-sensitive, content only (speaker labels are not searched).
    The single counting rule behind ``correct_transcript`` and its dry run.
    """
    counts = [(segment.get("content") or "").count(find) for segment in segments]
    return sum(counts), sum(1 for count in counts if count)


@dataclass(slots=True)
class PlaudRecordingQuery:
    skip: int | None = None
    limit: int | None = None
    is_trash: int | None = None
    sort_by: str | None = None
    is_desc: bool | None = None


def describe_unstarted_process(detail: RecordingDetail) -> str:
    """Explain why a process request started no new job, from the real state.

    Used by the MCP and CLI after ``transcribe_and_summarize`` returns False.
    Plaud's status-1 reply was only observed on a fully processed recording,
    so the wording comes from the recording's current state, not the reply.
    """
    if detail.is_trans and detail.is_summary:
        return (
            "This recording already has a transcript and summary. Plaud kept both and did "
            "not start a new run, so the requested options were not applied."
        )
    if detail.is_trans:
        return (
            "This recording already has a transcript but no finished summary (it may still "
            "be generating). Plaud did not start a new run, so the requested options were "
            "not applied."
        )
    return (
        "Plaud did not start a new run and no transcript is ready yet, so the recording is "
        "probably still processing. Check it again in a minute instead of re-running."
    )


class PlaudClient:
    def __init__(self, session_manager: SessionManager, transport: Transport | None = None) -> None:
        self._session_manager = session_manager
        self._transport = transport or UrllibTransport()

    @property
    def session_manager(self) -> SessionManager:
        """The session manager this client authenticates through."""
        return self._session_manager

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
        transcript_block: str = DEFAULT_TRANSCRIPT_BLOCK,
    ) -> RecordingDetail:
        if transcript_block not in TRANSCRIPT_BLOCKS:
            raise ValueError(
                f"transcript_block must be one of {', '.join(TRANSCRIPT_BLOCKS)}; got {transcript_block!r}"
            )
        raw = self._get_detail_raw(recording_id)
        detail = self._normalize_recording_detail(raw, recording_id)
        if include_transcript:
            detail.transcript_blocks_available = self._available_transcript_blocks(raw)
            segments = self._fetch_transcript_segments(raw, transcript_block)
            detail.transcript_segments = segments
            detail.speakers = list(
                dict.fromkeys(
                    s.get("speaker") or s.get("original_speaker") or ""
                    for s in segments
                    if s.get("speaker") or s.get("original_speaker")
                )
            )
            detail.transcript = format_transcript(segments)
        if include_summary and detail.is_summary and not detail.ai_content:
            detail.ai_content = self._fetch_summary_from_data_link(raw)
        return detail

    def search_content(
        self,
        query: str,
        *,
        since_ms: int | None = None,
        until_ms: int | None = None,
    ) -> list[ContentMatch]:
        """Full-text search over transcripts and summaries, best match first.

        ``POST /gsearch/v1/search`` is the web app's global search box.  It
        answers the ``SEARCH_RESULT_CAP`` most relevant recordings with one
        matching chunk each and ignores every paging parameter we tried
        (limit, size, page, skip, offset), so the cap is all there is; a date
        window is the only way to reach other matches.  Matching is keyword
        based with stemming, not an exact phrase.  Trashed recordings are not
        returned.  ``from`` is a recording-source filter in the web app; empty
        means all sources.
        """
        body = {"query": query, "date_from": since_ms, "date_to": until_ms, "from": ""}
        data = self._request_json("POST", "/gsearch/v1/search", strict=True, body=body).get("data") or {}
        keywords = [str(word) for word in data.get("keywords") or []]
        return [self._normalize_content_match(item, query, keywords) for item in data.get("list") or []]

    def _normalize_content_match(self, item: dict[str, Any], query: str, keywords: list[str]) -> ContentMatch:
        """Flatten one search hit; transcript hits carry ``trans_chunks``, summary hits ``notes``."""
        if item.get("trans_chunks"):
            source = "transcript"
            chunk = item["trans_chunks"][0]
            starts = [
                s["start_time"] for s in chunk.get("speakers") or [] if isinstance(s.get("start_time"), int)
            ]
            start_ms = min(starts) if starts else None
        else:
            source = "summary"
            chunks = [c for note in item.get("notes") or [] for c in note.get("chunks") or []]
            chunk = chunks[0] if chunks else {}
            start_ms = None
        return ContentMatch(
            id=str(item.get("id") or ""),
            title=str(item.get("title") or ""),
            start_time=int(item.get("start_time") or 0),
            source=source,
            snippet=content_snippet(
                str(chunk.get("content") or ""), query, keywords, mid_text=bool(chunk.get("offset"))
            ),
            start_ms=start_ms,
            raw=item,
        )

    def get_audio_url(self, recording_id: str) -> str | None:
        """Return a temporary download URL for a recording's audio, or None.

        ``GET /file/temp-url/{id}`` answers ``{"status": 0, "temp_url": ...,
        "temp_url_opus": ...}``.  The URL is a presigned S3 link to
        ``audiofiles/{id}.mp3`` valid for ``AUDIO_URL_TTL_S`` — an hour, so it
        is not worth caching or persisting anywhere; fetch it when needed.

        ``temp_url_opus`` was null for every recording observed (device-recorded
        and uploaded alike), so it is treated as a fallback rather than a
        separate format choice.  Returns None when Plaud has no audio for the
        recording (nothing synced from the device yet).
        """
        data = self._request_json("GET", f"/file/temp-url/{recording_id}", strict=True)
        url = data.get("temp_url") or data.get("temp_url_opus")
        return str(url) if url else None

    def download_audio(self, recording_id: str, destination: Path) -> Path:
        """Download a recording's audio to *destination*, returning the path.

        Reads the whole response into memory before writing.  Plaud recordings
        are mp3s of a meeting — single-digit MB in the captures — so streaming
        to disk in chunks would add a code path for no practical gain.
        ponytail: switch to chunked writes if multi-hour recordings turn out to
        be hundreds of MB.
        """
        url = self.get_audio_url(recording_id)
        if url is None:
            raise ValueError(f"recording {recording_id} has no downloadable audio")
        # No Plaud auth header: the signature lives in the URL, same as the
        # transcript data_link fetches and the multipart upload PUTs.
        response = self._transport.request(
            method="GET",
            url=url,
            headers={"User-Agent": BROWSER_USER_AGENT},
        )
        if response.status_code < 200 or response.status_code >= 300:
            raise PlaudApiError(
                f"audio download failed (HTTP {response.status_code})",
                http_status=response.status_code,
            )
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(response.body)
        return destination

    def get_user_info(self) -> dict[str, Any]:
        data = self._request_json("GET", "/user/me", strict=True)
        return data.get("data_user") or data.get("data") or data

    def upload_recording(
        self,
        path: Path,
        filename: str,
        file_type: str,
        *,
        start_time: int | None = None,
        timezone_offset: float | None = None,
    ) -> Recording:
        """4-step upload: presign → S3 multipart PUT → merge_multipart → confirm_upload.

        Reads *path* 5 MiB at a time, one chunk per presigned part URL, so a
        large recording never sits in memory whole.

        file_type must be "MP3", "OPUS", or "OGG". For other audio formats,
        transcode to MP3 first using plaud_tools.transcode.transcode_to_mp3_path()
        and pass the resulting path here.

        start_time: millisecond epoch for the recording's date. Defaults to now.
        Plaud respects whatever value the client sends — pass the original
        recording's timestamp to preserve the date after re-upload.

        There is deliberately no overall deadline: an upload is an active
        transfer, not a poll, and abandoning it midway leaves nothing to check
        back on.  Each S3 PUT has its own 120 s ceiling (see ``_s3_put``).
        """
        if not filename.strip():
            raise ValueError("filename cannot be empty")
        if file_type not in ("MP3", "OPUS", "OGG"):
            raise ValueError(f"file_type must be MP3, OPUS, or OGG — got {file_type!r}")
        filesize = path.stat().st_size
        if filesize == 0:
            raise ValueError("data cannot be empty")

        start_time_ms = start_time if start_time is not None else int(time.time() * 1000)
        tz = _local_utc_offset_hours() if timezone_offset is None else timezone_offset

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
        parts: list[dict[str, Any]] = []
        with path.open("rb") as fh:
            for i, url in enumerate(part_urls):
                chunk = fh.read(_CHUNK_SIZE)
                if not chunk:
                    # Presign returned more part URLs than the file has
                    # chunks — treat as a protocol error rather than
                    # silently uploading an empty part.
                    raise PlaudApiError(
                        f"Presign returned {len(part_urls)} part URLs but file exhausted after {i} chunk(s)"
                    )
                response = self._s3_put(str(url), chunk)
                etag = response.headers.get("etag", "").replace('"', "")
                if not etag:
                    raise PlaudApiError(f"S3 upload returned no ETag for part {i + 1}")
                parts.append({"Etag": etag, "PartNumber": i + 1})
            if fh.read(1):
                # The opposite mismatch: fewer part URLs than chunks.  Merging
                # now would store a silently truncated recording.
                raise PlaudApiError(f"Presign returned {len(part_urls)} part URLs but the file has more data")

        self._request_json(
            "POST",
            "/file/merge_multipart",
            strict=True,
            body={"upload_id": upload_id, "object_name": object_name, "parts": parts},
        )

        confirm_body = {
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
        }
        try:
            confirm = self._request_json("POST", "/file/confirm_upload", strict=True, body=confirm_body)
        except PlaudApiError as exc:
            if not exc.network_error:
                raise
            # The request may have reached Plaud before the connection died,
            # in which case the recording exists.  Report it as non-retryable
            # so an agent checks first instead of uploading a duplicate.
            raise PlaudApiError(
                f"{exc}. The upload may have completed anyway; check browse_recordings for "
                f"{filename!r} before uploading again."
            ) from exc
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
        """Merge recordings via /file/combine and poll /file/combine-tasks until done.

        On timeout raises ``PlaudWaitTimeoutError`` carrying the combine
        ``task_id``: the merge keeps running on Plaud, so re-running it would
        create a duplicate.
        """
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

        def check() -> RecordingDetail | None:
            poll = self._request_json("GET", f"/file/combine-tasks/{task_id}", strict=False)
            task = poll.get("data") or {}
            if task.get("status") == "success":
                file_raw = task.get("file") or {}
                return self._normalize_recording_detail(file_raw, str(file_raw.get("file_id") or ""))
            if task.get("status") == "error":
                raise PlaudApiError(f"merge failed: {task.get('error_message') or 'unknown error'}")
            return None

        return self._poll_until(
            check, what="merge", timeout_s=timeout_s, poll_interval_s=poll_interval_s, task_id=task_id
        )

    def wait_for_transcription(
        self,
        recording_id: str,
        *,
        timeout_s: float = 600.0,
        poll_interval_s: float = 5.0,
    ) -> None:
        """Poll get_recording() until is_trans is True or timeout elapses."""
        self._poll_until(
            lambda: True if self.get_recording(recording_id).is_trans else None,
            what="transcription",
            timeout_s=timeout_s,
            poll_interval_s=poll_interval_s,
        )

    def wait_for_summary(
        self,
        recording_id: str,
        *,
        timeout_s: float = 600.0,
        poll_interval_s: float = 5.0,
    ) -> None:
        """Poll get_recording() until is_summary is True or timeout elapses."""
        self._poll_until(
            lambda: True if self.get_recording(recording_id).is_summary else None,
            what="summary",
            timeout_s=timeout_s,
            poll_interval_s=poll_interval_s,
        )

    def _poll_until(
        self,
        check: Callable[[], _T | None],
        *,
        what: str,
        timeout_s: float,
        poll_interval_s: float,
        task_id: str | None = None,
    ) -> _T:
        """Call *check* until it returns non-None, sleeping between polls.

        Shared by the transcription, summary, and merge waits.  *check* always
        runs at least once, so a job that is already done is reported done
        even with a zero budget.  A transient error (429 / 5xx / network blip)
        counts as a skipped poll; anything else propagates.  When the budget
        runs out this raises ``PlaudWaitTimeoutError`` (with *task_id*, if
        any): the job is still running server-side.
        """
        deadline = time.monotonic() + timeout_s
        while True:
            try:
                result = check()
            except PlaudApiError as exc:
                _code, retryable = exc.classify()
                if not retryable:
                    raise
                _log.info("%s poll transient error (%s); continuing until deadline", what, exc)
            else:
                if result is not None:
                    return result
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise PlaudWaitTimeoutError(f"{what} timed out after {int(timeout_s)}s", task_id=task_id)
            _sleep(min(poll_interval_s, remaining))

    def dump_raw_detail(self, recording_id: str) -> dict[str, Any]:
        """Return the raw /file/detail payload for debugging."""
        return self._get_detail_raw(recording_id)

    def _get_detail_raw(self, recording_id: str) -> dict[str, Any]:
        """GET /file/detail/{id} and unwrap Plaud's ``data`` envelope."""
        data = self._request_json("GET", f"/file/detail/{recording_id}", strict=True)
        raw = data.get("data", data)
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

    def _normalize_file_tag(self, item: dict[str, Any]) -> FileTag:
        return FileTag(
            id=str(item.get("id") or item.get("filetag_id") or ""),
            name=str(item.get("name") or ""),
            color=str(item.get("color") or ""),
            icon=str(item.get("icon") or ""),
            raw=item,
        )

    def list_file_tags(self) -> list[FileTag]:
        data = self._request_json("GET", "/filetag/", strict=True)
        items = data.get("data_filetag_list") or data.get("data") or data.get("filetags") or []
        return [self._normalize_file_tag(item) for item in items]

    def create_folder(
        self,
        name: str,
        *,
        color: str | None = None,
        icon: str | None = None,
    ) -> FileTag:
        """Create a new folder (filetag) and return it.

        Mirrors the web client's ``POST /filetag/`` call.  ``color`` and ``icon``
        default to Plaud's common folder defaults when omitted (see
        ``_DEFAULT_FOLDER_*``).  A duplicate name is rejected by Plaud with
        ``status:-2 msg:"filetag name existed"``, which surfaces here as a
        ``PlaudApiError`` via the strict-status check in ``_request_json``.
        """
        if not name.strip():
            raise ValueError("folder name cannot be empty")
        data = self._request_json(
            "POST",
            "/filetag/",
            strict=True,
            body={
                "name": name,
                "icon": icon or _DEFAULT_FOLDER_ICON,
                "color": color or _DEFAULT_FOLDER_COLOR,
            },
        )
        return self._normalize_file_tag(data.get("data_filetag") or {})

    def update_folder(
        self,
        folder_id: str,
        *,
        name: str | None = None,
        color: str | None = None,
        icon: str | None = None,
    ) -> FileTag:
        """Edit an existing folder's name, color, and/or icon (``PATCH /filetag/{id}``).

        Only the supplied fields are sent; at least one of name/color/icon is
        required.  Returns the updated folder as echoed back by Plaud.
        """
        if not folder_id:
            raise ValueError("folder_id cannot be empty")
        body: dict[str, Any] = {}
        if name is not None:
            if not name.strip():
                raise ValueError("folder name cannot be empty")
            body["name"] = name
        if color is not None:
            body["color"] = color
        if icon is not None:
            body["icon"] = icon
        if not body:
            raise ValueError("update_folder requires at least one of name, color, icon")
        data = self._request_json("PATCH", f"/filetag/{folder_id}", strict=True, body=body)
        return self._normalize_file_tag(data.get("data_filetag") or {})

    def delete_folder(self, folder_id: str) -> None:
        """Delete a folder (``DELETE /filetag/{id}``).

        Deletes only the folder itself — recordings inside it are not deleted;
        they lose the folder association and become unfiled.  There is no undo
        for the folder, so callers should gate this behind an explicit
        confirmation (the CLI ``--yes`` flag / the MCP ``confirm`` parameter).
        """
        if not folder_id:
            raise ValueError("folder_id cannot be empty")
        self._request_json("DELETE", f"/filetag/{folder_id}", strict=True)

    def list_trash(self) -> list[Recording]:
        return self.list_recordings(PlaudRecordingQuery(is_trash=1))

    def set_recording_folder(self, recording_ids: str | list[str], folder_id: str | None) -> None:
        """Move one or more recordings into *folder_id* (``None`` unfiles them).

        ``/file/update-tags`` takes a ``file_id_list``, so a batch is one request.
        """
        ids = [recording_ids] if isinstance(recording_ids, str) else list(recording_ids)
        if not ids:
            raise ValueError("recording_ids cannot be empty")
        self._request_json(
            "POST",
            "/file/update-tags",
            strict=True,
            body={
                "file_id_list": ids,
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
    ) -> bool:
        """Start transcription + summary. Returns False if already processed.

        Plaud answers ``is_reload=0`` on a recording that already has a
        transcript with ``status: 1, msg: "success"`` and the EXISTING
        transcript/summary: no new job starts and the template is ignored
        (verified live 2026-09-24). ``is_reload=1`` would re-transcribe and
        replace the summary, wiping speaker renames and edits, so it is not
        exposed.
        """
        if template_type and template_type.lower() == "default":
            template_type = "AUTO-SELECT"
        if language and "-" in language:
            language = language.split("-")[0]
        info = json.dumps(
            {
                "language": language or "auto",
                "timezone": _local_utc_offset_hours(),
                "diarization": 0 if diarization is False else 1,
                "llm": llm or "auto",
            }
        )
        payload = self._request_json(
            "POST",
            f"/ai/transsumm/{recording_id}",
            strict=False,
            body={
                "is_reload": 0,
                "summ_type": template_type or "AUTO-SELECT",
                "summ_type_type": "system",
                "info": info,
                "support_mul_summ": True,
            },
        )
        status = payload.get("status")
        if status == 1:
            return False
        if status != 0:
            raise PlaudApiError(f"Plaud API error: {payload.get('msg') or f'status {status}'}")
        return True

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

        segments = self._editable_segments(recording_id)

        # Match the label against BOTH the displayed `speaker` and the
        # `original_speaker` fields.  Plaud auto-resolves enrolled voices, so a
        # never-renamed segment can already show `speaker="Kadin Bullock"` while
        # `original_speaker="Speaker 1"`.  Callers naturally pass whatever label
        # they see in the transcript — the display name — so matching only
        # `original_speaker` silently failed for every recording that had an
        # enrolled or previously-renamed speaker.  Matching either field lets a
        # caller rename by the current display name ("Benjamin Everitt") or by
        # the generic original ("Speaker 1") interchangeably.
        updated = 0
        next_segments: list[dict[str, Any]] = []
        for segment in segments:
            if original_label in (segment.get("speaker"), segment.get("original_speaker")):
                updated += 1
                next_segments.append({**segment, "speaker": new_name})
            else:
                next_segments.append(segment)

        if updated == 0:
            raise ValueError(f'no segments found for speaker "{original_label}"')

        self.edit_transcript(recording_id, next_segments)
        return {"segments_updated": updated}

    def correct_transcript(self, recording_id: str, find: str, replace: str) -> dict[str, int]:
        """Find-and-replace literal text across all transcript segment content.

        This is the same operation the Plaud web app performs for transcript
        text corrections: it rewrites the `content` of every segment that
        contains *find* and PATCHes the full ``trans_result`` back (see
        ``edit_transcript``).  Matching is literal (not regex) and
        case-sensitive, mirroring the web client.  Speaker labels are left
        untouched — use ``rename_speaker`` for those.

        Returns the number of individual occurrences replaced and the number of
        segments that changed.
        """
        if not find:
            raise ValueError("find text cannot be empty")

        segments = self._editable_segments(recording_id)

        replacements, segments_changed = count_segment_matches(segments, find)
        if replacements == 0:
            raise ValueError(f'no occurrences of "{find}" found in transcript')

        next_segments = [
            {**segment, "content": segment["content"].replace(find, replace)}
            if find in (segment.get("content") or "")
            else segment
            for segment in segments
        ]
        self.edit_transcript(recording_id, next_segments)
        return {"replacements": replacements, "segments_changed": segments_changed}

    def count_transcript_matches(self, recording_id: str, find: str) -> int:
        """Count what ``correct_transcript`` would replace, without editing.

        Same block, same field (segment ``content`` only, never speaker
        labels), same counting as the real edit, so a dry run's number
        matches the real run's ``replacements``.
        """
        if not find:
            raise ValueError("find text cannot be empty")
        replacements, _ = count_segment_matches(self._editable_segments(recording_id), find)
        return replacements

    def _editable_segments(self, recording_id: str) -> list[dict[str, Any]]:
        """The editable (``transaction``) transcript segments; ValueError if none."""
        segments = self._fetch_transcript_segments(self._get_detail_raw(recording_id))
        if not segments:
            raise ValueError(f"recording {recording_id} has no transcript yet")
        return segments

    def _get_summary_note(self, recording_id: str) -> tuple[str, str]:
        """Return ``(note_id, current_content)`` for a recording's AI summary.

        The ``note_id`` is the ``data_id`` of the completed ``auto_sum_note``
        entry in the detail's ``content_list`` — exactly what
        ``/ai/update_note_info`` expects.  The current content is read inline
        when present and otherwise fetched from the summary's ``data_link``.

        Raises ``ValueError`` if the recording has no completed summary.
        """
        raw = self._get_detail_raw(recording_id)
        note_id: str | None = None
        for item in raw.get("content_list") or []:
            if item.get("data_type") == "auto_sum_note" and item.get("task_status") == 1:
                note_id = item.get("data_id")
                break
        if not note_id:
            raise ValueError(f"recording {recording_id} has no summary yet")
        content = self._extract_inline_summary(raw, note_id)
        if content is None:
            content = self._fetch_summary_from_data_link(raw)
        return note_id, content or ""

    def _update_summary_note(self, recording_id: str, note_id: str, content: str) -> None:
        """POST the full summary body back to Plaud (``/ai/update_note_info``).

        The web app replaces the entire ``note_content`` on every edit — there
        is no partial-patch endpoint — so callers pass the complete new text.
        """
        self._request_json(
            "POST",
            "/ai/update_note_info",
            strict=True,
            body={
                "file_id": recording_id,
                "note_id": note_id,
                "note_type": "auto_sum_note",
                "note_content": content,
            },
        )

    def set_summary(self, recording_id: str, content: str) -> None:
        """Overwrite a recording's AI summary with ``content`` (full replace).

        The recording must already have a generated summary — this edits the
        existing note, it does not create one (use ``transcribe_and_summarize``
        to generate a summary first).
        """
        if not content.strip():
            raise ValueError("summary content cannot be empty")
        note_id, _ = self._get_summary_note(recording_id)
        self._update_summary_note(recording_id, note_id, content)

    def correct_summary(self, recording_id: str, find: str, replace: str) -> dict[str, int]:
        """Find-and-replace literal text in a recording's AI summary.

        The summary equivalent of ``correct_transcript`` — the intended use is
        fixing a misspelled name or term in an otherwise-good summary.  Matching
        is literal (not regex) and case-sensitive.  Returns the number of
        occurrences replaced.  Raises ``ValueError`` if the text is not found.
        """
        if not find:
            raise ValueError("find text cannot be empty")
        note_id, content = self._get_summary_note(recording_id)
        if not content:
            raise ValueError(f"recording {recording_id} has no summary text to edit")
        count = content.count(find)
        if count == 0:
            raise ValueError(f'no occurrences of "{find}" found in summary')
        self._update_summary_note(recording_id, note_id, content.replace(find, replace))
        return {"replacements": count}

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

        url = f"{base_url(session.region)}{path}"
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
                # Exponential backoff with ±25 % jitter: 1 s before the first
                # retry, 2 s before the second (see the module-level formula).
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
                # #147: only auto-retry idempotent GETs. Retrying a
                # non-idempotent POST/PATCH/DELETE on a transient 429/5xx (or
                # a network blip) risks duplicating a side effect the caller
                # never asked for — e.g. a second /file/combine merge running
                # to completion, or a folder-create that "fails" with a false
                # "already exists" after the first attempt actually succeeded
                # server-side. A GET can be safely replayed, so it keeps the
                # full retry budget; every other method gets exactly one try
                # here (callers that need resilience for a specific mutation,
                # e.g. merge/transcription polling, already retry at the
                # application layer using idempotent GET polls).
                if retryable and method == "GET" and attempt < _MAX_ATTEMPTS - 1:
                    last_error = exc
                    continue
                raise

            try:
                payload = response.json()
            except ValueError as exc:
                # #146: a 2xx response with a non-JSON (or undecodable) body
                # is a Plaud-side/upstream problem, not a caller input error.
                # Left unguarded, json.JSONDecodeError (a ValueError subclass)
                # escaped all the way to the MCP facade's `except ValueError`
                # branch, which reports error_code="validation" — misleadingly
                # blaming the caller for what is actually a server outage.
                raise PlaudApiError(
                    f"Plaud API returned a non-JSON body (HTTP {response.status_code})",
                    http_status=response.status_code,
                ) from exc
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
                next_region = region_for_api_domain(redirect_api_domain(payload))
                if next_region is None:
                    raise PlaudApiError("region redirect to an unrecognized API host")
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

    def _available_transcript_blocks(self, raw: dict[str, Any]) -> list[str]:
        """Which blocks in ``TRANSCRIPT_BLOCKS`` are finished for this recording.

        Returned in ``TRANSCRIPT_BLOCKS`` order (not Plaud's ``content_list``
        order) so the value is stable across responses.
        """
        done = {
            item.get("data_type")
            for item in raw.get("content_list") or []
            if item.get("task_status") == 1 and item.get("data_link")
        }
        return [block for block in TRANSCRIPT_BLOCKS if block in done]

    def _fetch_transcript_segments(
        self, raw: dict[str, Any], block: str = DEFAULT_TRANSCRIPT_BLOCK
    ) -> list[dict[str, Any]]:
        """Fetch and parse one transcript block's segments.

        *block* defaults to ``"transaction"`` — the raw transcript, and the only
        block the edit paths may touch (``rename_speaker`` /
        ``correct_transcript`` PATCH ``trans_result`` back, so they must read
        the same block they write). Read-only callers may request any block in
        ``TRANSCRIPT_BLOCKS``. Returns ``[]`` when the block is absent or
        unfinished; callers distinguish that from "no transcript at all" via
        ``_available_transcript_blocks``. A finished block whose download fails
        raises ``PlaudApiError`` rather than reading as empty.
        """
        transcript_item = None
        for item in raw.get("content_list") or []:
            if item.get("data_type") == block and item.get("task_status") == 1:
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
            # Plaud says this block is finished, so a failed download is an
            # upstream error — returning [] here would pass it off as a
            # genuinely empty transcript (#210).
            raise PlaudApiError(
                f"Plaud transcript link returned HTTP {response.status_code}",
                http_status=response.status_code,
            )
        try:
            body = response.json()
        except ValueError as exc:
            # #146: same rationale as _request_json — a 2xx with a non-JSON
            # body is an upstream problem, not a caller input error. Without
            # this, json.JSONDecodeError escaped as-is and calling code (e.g.
            # rename_speaker/correct_transcript's "no transcript yet" check)
            # misreported the resulting empty segment list as a validation
            # error instead of the server-side failure it actually was.
            raise PlaudApiError(
                f"Plaud transcript link returned a non-JSON body (HTTP {response.status_code})",
                http_status=response.status_code,
            ) from exc
        if isinstance(body, list):
            return body
        if isinstance(body, dict):
            segments = body.get("trans_result")
            return segments if isinstance(segments, list) else []
        return []
