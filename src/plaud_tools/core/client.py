"""Plaud API client, speaking Plaud 4.0's ``/file-app/v4`` protocol.

Plaud 4.0 (accounts migrated from 2026-11) stores everything as a tree of
nodes.  Each recording is a *file node* (``on_f_…`` / ``n_…``) inside its own
*container folder* (``on_w_…`` / ``n_…``), which sits in a user folder
(``on_fd_…`` / ``n_…``) or a system folder (Recordings, Import, Trash).  The
recording itself is addressed by its *file id*: ``of_<hex>`` for recordings
made before migration, ``f_<hex>`` after.  The pre-4.0 bare 32-hex id still
works everywhere here: ``to_file_id`` adds the ``of_`` prefix.

Every write carries ``origin_version`` (the node's or content object's
current ``version_ms``); Plaud rejects a missing or stale one with status
-1800313.  So each write reads the current version immediately before
writing.  See docs/adr/008-plaud-4-protocol.md.

Three pre-4.0 endpoints still answer for migrated accounts and are kept:
``/user/me``, ``/gsearch/v1/search`` (full-text search), and
``/file/document/export`` (TXT/SRT/DOCX/PDF rendering).
"""

from __future__ import annotations

import json
import logging
import random
import re
import time
import uuid
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path
from typing import Any, TypeVar
from urllib.parse import urlencode

from .errors import PlaudApiError, PlaudSessionExpiredError, PlaudWaitTimeoutError
from .folders import ColonNestingPlan, parent_id_of
from .models import (
    BROWSER_USER_AGENT,
    AudioLink,
    ContentMatch,
    Folder,
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

_V4 = "/file-app/v4"

# Upload chunk size when Plaud's upload/create reply does not name one.
_CHUNK_SIZE = 5 * 1024 * 1024

# Largest page Plaud's list endpoints accepted when probed (1000 returned a
# whole 806-recording library in one ~0.3 s request).
_LIST_PAGE_MAX = 1000
# Safety stop for cursor walks: far beyond any real library.
_MAX_LIST_PAGES = 200
# Parallel requests per tree level in list_folders(); kept small so a large
# library doesn't hit Plaud's rate limit.
_FOLDER_WALK_WORKERS = 8

# ---------------------------------------------------------------------------
# Injectable sleep/jitter for tests.  Tests monkeypatch these module-level
# names instead of patching time.sleep globally.
# ---------------------------------------------------------------------------
_sleep = time.sleep


def _jitter(lo: float, hi: float) -> float:
    """Return a random float in [lo, hi] — injected by tests via monkeypatch."""
    return random.uniform(lo, hi)


# Retry / backoff for idempotent GETs on 429 / 5xx / network blips:
# 3 attempts, exponential backoff 1 s then 2 s, each with ±25 % jitter,
# never shorter than a server-sent Retry-After.
_MAX_ATTEMPTS = 3
_BACKOFF_BASE = 1.0

# Folder defaults the 4.0 web app sends when creating a folder.
_DEFAULT_FOLDER_ICON = "e627"
_DEFAULT_FOLDER_COLOR = "folder-color-default"

# Plaud's "system folder" types (``system_folder_type`` on a node).  0 is an
# ordinary node; 1 is the default Recordings folder, where "unfiled"
# recordings live.
_SYSTEM_FOLDER_RECORDINGS = 1

# Community summary templates have 32-hex ids; official ones use a code
# such as MEETING.  Summary tasks must say which kind an id is.
_HEX_ID = re.compile(r"[0-9a-f]{32}")

# Plaud status codes worth naming.
_STATUS_FILE_NOT_FOUND = -1800311
_STATUS_MIGRATED = -1800907

# Transcript blocks, by the names this tool has always used, mapped to the
# 4.0 content object that holds each:
#
#   transaction         raw diarized transcript (TRANSCRIPT).  The only
#                       editable block: speaker renames and text corrections
#                       write it back, so they must read what they write.
#   transaction_polish  Plaud's AI-cleaned pass (POLISHED_TRANSCRIPT), same
#                       per-utterance shape.  Read-only here.
TRANSCRIPT_BLOCKS = ("transaction", "transaction_polish")
DEFAULT_TRANSCRIPT_BLOCK = "transaction"
_BLOCK_OBJECT_TYPE = {"transaction": "TRANSCRIPT", "transaction_polish": "POLISHED_TRANSCRIPT"}

# How long presigned content URLs (audio included) stay valid.
AUDIO_URL_TTL_S = 3600

# Most recordings one full-text search returns.  Observed, not documented:
# no paging parameter changed it (see PlaudClient.search_content).
SEARCH_RESULT_CAP = 20

_LEGACY_ID = re.compile(r"[0-9a-f]{32}")

# Upload formats Plaud accepts, as (MIME type, file extension).
_UPLOAD_FORMATS = {"MP3": ("audio/mpeg", "mp3"), "OGG": ("audio/ogg", "ogg"), "OPUS": ("audio/ogg", "opus")}

_T = TypeVar("_T")


def to_file_id(recording_id: str) -> str:
    """Normalize any recording id a caller may hold to a Plaud 4.0 file id.

    Pre-4.0 ids (bare 32 hex, as older exports, notes and the search index
    still carry) become ``of_<hex>``; so do the migrated node ids
    ``on_f_<hex>`` / ``on_w_<hex>``.  Anything else passes through.
    """
    rid = recording_id.strip()
    for prefix in ("on_f_", "on_w_"):
        if rid.startswith(prefix):
            return "of_" + rid[len(prefix) :]
    if _LEGACY_ID.fullmatch(rid):
        return "of_" + rid
    return rid


def _local_timezone_name() -> str:
    """This machine's IANA timezone name (``America/Denver``), or ``UTC``.

    Plaud 4.0 wants the IANA name in an ``x-timezone-iana`` header (upload
    confirm fails without it).  Windows has no IANA name of its own;
    tzlocal maps the Windows zone to one.
    """
    try:
        from tzlocal import get_localzone_name

        return get_localzone_name() or "UTC"
    except Exception:  # noqa: BLE001 — a missing zone must never block a request
        return "UTC"


def _timezone_for_offset(offset_hours: float) -> str:
    """IANA name for a fixed whole-hour UTC offset (``-6`` → ``Etc/GMT+6``).

    The ``Etc/GMT`` zones have the sign inverted by POSIX convention.  Plaud
    4.0 takes only IANA names, so fractional offsets (India's +5.5) cannot be
    expressed this way and are rejected.
    """
    if offset_hours != int(offset_hours):
        raise ValueError("timezone_offset must be a whole number of hours")
    hours = int(offset_hours)
    return "UTC" if hours == 0 else f"Etc/GMT{-hours:+d}"


def count_segment_matches(segments: list[dict[str, Any]], find: str) -> tuple[int, int]:
    """Return ``(occurrences, segments_containing)`` of *find* in segment content.

    Literal, case-sensitive, content only (speaker labels are not searched).
    The single counting rule behind ``correct_transcript`` and its dry run.
    """
    counts = [(segment.get("content") or "").count(find) for segment in segments]
    return sum(counts), sum(1 for count in counts if count)


@dataclass(slots=True)
class PlaudRecordingQuery:
    """A newest-first window of the recording list (``skip`` then ``limit``)."""

    skip: int | None = None
    limit: int | None = None
    is_trash: int | None = None


def describe_unstarted_process(detail: RecordingDetail) -> str:
    """Explain why a process request started no new job, from the real state.

    Used by the MCP and CLI after ``transcribe_and_summarize`` returns False.
    """
    if detail.is_trans and detail.is_summary:
        return (
            "This recording already has a transcript and summary. Plaud kept both and did "
            "not start a new run, so the requested options were not applied."
        )
    return (
        "Plaud is already processing this recording, so no new run was started. Check it "
        "again in a minute instead of re-running."
    )


def _object_done(obj: dict[str, Any] | None) -> bool:
    """True when a content object has finished generating and can be downloaded.

    Migrated objects report ``task_status`` null once done; generated ones 1.
    """
    return bool(obj and obj.get("content_url") and obj.get("task_status") in (None, 1))


def _is_container(node: dict[str, Any]) -> bool:
    """True for a per-recording container folder (as opposed to a user folder).

    Container folders carry no icon; folders a person made always do.
    """
    return node.get("type") == 1 and not node.get("icon") and not node.get("system_folder_type")


def _is_user_folder(node: dict[str, Any]) -> bool:
    return node.get("type") == 1 and bool(node.get("icon")) and not node.get("system_folder_type")


class PlaudClient:
    def __init__(self, session_manager: SessionManager, transport: Transport | None = None) -> None:
        self._session_manager = session_manager
        self._transport = transport or UrllibTransport()
        self._timezone = _local_timezone_name()

    @property
    def session_manager(self) -> SessionManager:
        """The session manager this client authenticates through."""
        return self._session_manager

    # ------------------------------------------------------------------
    # Reading recordings
    # ------------------------------------------------------------------

    def list_recordings(self, query: PlaudRecordingQuery | None = None) -> list[Recording]:
        """Recordings newest first, windowed by ``query.skip`` / ``query.limit``.

        ``is_trash=1`` lists the trash instead.  The 4.0 list pages by cursor,
        not offset, so a window is cut from the front of a cursor walk; at
        ``_LIST_PAGE_MAX`` per page that is one request for most libraries.
        """
        q = query or PlaudRecordingQuery()
        skip = q.skip or 0
        want = None if q.limit is None else skip + q.limit
        if q.is_trash:
            return self.list_trash()[skip:want]
        items = self._walk(f"{_V4}/recordings/all", {"sort_by": "created", "sort_order": "desc"}, want=want)
        return [self._normalize_recording(item) for item in items][skip:want]

    def list_trash(self) -> list[Recording]:
        """Every recording in the trash, newest first.

        The trash holds container folders (and any trashed user folders,
        which are skipped).  A migrated container ``on_w_<hex>`` names its
        recording directly; a newer one needs one lookup for its file.
        """
        items = self._walk(f"{_V4}/folders/content", {"scope": "trash"})
        recordings: list[Recording] = []
        for item in items:
            if item.get("type") == 2 and item.get("file_id"):
                recordings.append(self._normalize_trash_item(item, str(item["file_id"]), ""))
            elif _is_container(item):
                node_id = str(item.get("node_id") or "")
                file_id = (
                    to_file_id(node_id) if node_id.startswith("on_w_") else self._container_file_id(node_id)
                )
                if file_id:
                    recordings.append(self._normalize_trash_item(item, file_id, node_id))
        return recordings

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
        detail = self._normalize_recording_detail(raw)
        if include_transcript:
            detail.transcript_blocks_available = self._available_transcript_blocks(raw)
            segments = self._fetch_segments(self._object(raw, _BLOCK_OBJECT_TYPE[transcript_block]))
            detail.transcript_segments = segments
            detail.speakers = list(
                dict.fromkeys(
                    s.get("speaker") or s.get("original_speaker") or ""
                    for s in segments
                    if s.get("speaker") or s.get("original_speaker")
                )
            )
            detail.transcript = format_transcript(segments)
        if include_summary and detail.is_summary:
            detail.ai_content = self._fetch_text(self._object(raw, "SUMMARY"))
        return detail

    def dump_raw_detail(self, recording_id: str) -> dict[str, Any]:
        """Return the raw ``files/detail`` payload for debugging."""
        return self._get_detail_raw(recording_id)

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
        matching chunk each and ignores every paging parameter we tried, so a
        date window is the only way to reach other matches.  Matching is
        keyword based with stemming.  Trashed recordings are not returned.

        The search index predates 4.0: it names a migrated recording by its
        bare old hex id and a newer one by its container folder, so each hit
        is mapped to the file id every other call takes.
        """
        body = {"query": query, "date_from": since_ms, "date_to": until_ms, "from": ""}
        data = self._request_json("POST", "/gsearch/v1/search", strict=True, body=body).get("data") or {}
        keywords = [str(word) for word in data.get("keywords") or []]
        return [self._normalize_content_match(item, query, keywords) for item in data.get("list") or []]

    def _search_hit_file_id(self, hit_id: str) -> str:
        if hit_id.startswith("n_"):
            return self._container_file_id(hit_id) or hit_id
        return to_file_id(hit_id)

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
            id=self._search_hit_file_id(str(item.get("id") or "")),
            title=str(item.get("title") or ""),
            start_time=int(item.get("start_time") or 0),
            source=source,
            snippet=content_snippet(
                str(chunk.get("content") or ""), query, keywords, mid_text=bool(chunk.get("offset"))
            ),
            start_ms=start_ms,
            raw=item,
        )

    def get_audio_link(self, recording_id: str) -> AudioLink | None:
        """Return a presigned download link for a recording's audio, or None.

        The link is the AUDIO content object's ``content_url``, valid for
        about ``AUDIO_URL_TTL_S``.  Device recordings are often Ogg, not MP3;
        ``AudioLink.extension`` names the right file suffix.  None when Plaud
        has no audio yet (nothing synced from the device).
        """
        audio = self._object(self._get_detail_raw(recording_id), "AUDIO")
        if not audio or not audio.get("content_url"):
            return None
        return AudioLink(url=str(audio["content_url"]), mime_type=str(audio.get("mime_type") or ""))

    def download_audio(self, link: AudioLink, destination: Path) -> Path:
        """Download the audio behind *link* to *destination*, returning the path.

        Reads the whole response into memory before writing: Plaud recordings
        are tens of MB at most.
        """
        response = self._get_presigned(link.url, what="audio download")
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(response.body)
        return destination

    def render_transcript_document(
        self,
        detail: RecordingDetail,
        segments: list[dict[str, Any]],
        to_format: str,
        *,
        with_speaker: bool,
        with_timestamp: bool,
    ) -> bytes:
        """Have Plaud render *segments* as a TXT/SRT/DOCX/PDF file; return its bytes.

        ``POST /file/document/export`` predates 4.0 but still renders for
        migrated accounts.  It carries the utterances themselves
        (``trans_content``) and answers with a presigned link to the file.
        Rendering is read-only toward the recording.
        """
        data = self._request_json(
            "POST",
            "/file/document/export",
            strict=True,
            body={
                "file_id": detail.id,
                "prompt_type": "trans",
                "to_format": to_format,
                "title": detail.filename,
                # Local wall-clock time, formatted the way the web app sends it.
                "create_time": datetime.fromtimestamp(detail.start_time / 1000).strftime("%Y-%m-%d %H:%M:%S"),
                "with_speaker": int(with_speaker),
                "with_timestamp": int(with_timestamp),
                "trans_content": segments,
                "language": "",
            },
        )
        url = data.get("data")
        if not isinstance(url, str) or not url.startswith("https://"):
            raise PlaudApiError("Plaud document export returned no download link")
        return self._get_presigned(url, what="document download").body

    def get_user_info(self) -> dict[str, Any]:
        data = self._request_json("GET", "/user/me", strict=True)
        return data.get("data_user") or data.get("data") or data

    def get_task_status(self, recording_id: str) -> list[TaskStatus]:
        """Plaud jobs still running on a recording (empty when none are)."""
        raw = self._get_detail_raw(recording_id)
        file_id = str((raw.get("meta") or {}).get("file_id") or to_file_id(recording_id))
        tasks = (raw.get("task") or {}).get("in_progress_tasks") or []
        return [
            TaskStatus(
                file_id=file_id,
                task_id=str(task.get("task_id") or ""),
                task_type=str(task.get("task_type") or ""),
                stages=list(task.get("task_stage") or []),
                raw=task,
            )
            for task in tasks
        ]

    # ------------------------------------------------------------------
    # Upload, processing, merge
    # ------------------------------------------------------------------

    def upload_recording(
        self,
        path: Path,
        filename: str,
        file_type: str,
        *,
        start_time: int | None = None,
        timezone_offset: float | None = None,
        folder_id: str | None = None,
    ) -> Recording:
        """Upload an audio file: create → S3 multipart PUT → merge-part → confirm.

        Reads *path* one chunk at a time, so a large recording never sits in
        memory whole.  ``file_type`` must be "MP3", "OPUS", or "OGG"; transcode
        anything else first (``transcode.upload_with_transcode`` does).

        ``start_time`` (ms epoch) is the recording's date, default now.
        ``timezone_offset`` (whole hours) overrides this machine's timezone.
        ``folder_id`` files it straight into that folder; without one Plaud
        puts uploads in its Import folder.  A name already in use gets Plaud's
        "keep both" treatment instead of failing.  Plaud starts transcription
        and a summary on its own once the upload is confirmed.

        There is deliberately no overall deadline: an upload is an active
        transfer, and each S3 PUT has its own 120 s ceiling (see ``_s3_put``).
        """
        if not filename.strip():
            raise ValueError("filename cannot be empty")
        if file_type not in _UPLOAD_FORMATS:
            raise ValueError(f"file_type must be MP3, OPUS, or OGG — got {file_type!r}")
        filesize = path.stat().st_size
        if filesize == 0:
            raise ValueError("data cannot be empty")
        mime_type, extension = _UPLOAD_FORMATS[file_type]
        timezone = self._timezone if timezone_offset is None else _timezone_for_offset(timezone_offset)

        created = (
            self._request_json(
                "POST",
                f"{_V4}/upload/create",
                strict=True,
                body={
                    "file_name": filename,
                    "file_size": filesize,
                    "file_type": mime_type,
                    "file_extension": extension,
                },
            ).get("data")
            or {}
        )
        part_urls = created.get("part_urls")
        upload_id = created.get("upload_id")
        if not isinstance(part_urls, list) or not part_urls or not isinstance(upload_id, str):
            raise PlaudApiError("Plaud upload/create response missing fields")
        chunk_size = int(created.get("chunk_size") or _CHUNK_SIZE)

        parts: list[dict[str, Any]] = []
        with path.open("rb") as fh:
            for i, url in enumerate(part_urls):
                chunk = fh.read(chunk_size)
                if not chunk:
                    raise PlaudApiError(
                        f"Plaud returned {len(part_urls)} part URLs but the file ran out after {i} chunk(s)"
                    )
                etag = self._s3_put(str(url), chunk).headers.get("etag", "").replace('"', "")
                if not etag:
                    raise PlaudApiError(f"S3 upload returned no ETag for part {i + 1}")
                parts.append({"etag": etag, "part_number": i + 1})
            if fh.read(1):
                # Fewer part URLs than chunks: merging now would store a
                # silently truncated recording.
                raise PlaudApiError(f"Plaud returned {len(part_urls)} part URLs but the file has more data")

        self._request_json(
            "POST", f"{_V4}/upload/merge-part", strict=True, body={"upload_id": upload_id, "parts": parts}
        )

        confirm_body: dict[str, Any] = {
            # The client names the new recording's file id.
            "file_id": f"f_{uuid.uuid4().hex}",
            "upload_id": upload_id,
            "file_name": filename,
            "start_time": start_time if start_time is not None else int(time.time() * 1000),
            "scene_source": 101,
            "conflict_strategy": "keep_both",
        }
        if folder_id:
            confirm_body["target_node_id"] = folder_id
        try:
            confirmed = (
                self._request_json(
                    "POST",
                    f"{_V4}/upload/confirm-audio-web",
                    strict=True,
                    body=confirm_body,
                    timezone=timezone,
                ).get("data")
                or {}
            )
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
        return Recording(
            id=str(confirmed.get("file_id") or confirm_body["file_id"]),
            filename=str(confirmed.get("file_name") or confirmed.get("name") or filename),
            start_time=int(confirm_body["start_time"]),
            folder_id=folder_id,
            node_id=str(confirmed.get("node_id") or ""),
            wrapper_id=str(confirmed.get("folder_node_id") or ""),
            version_ms=int(confirmed.get("version_ms") or 0),
            raw=confirmed,
        )

    def _s3_put(self, url: str, chunk: bytes) -> HttpResponse:
        """PUT a chunk to a presigned S3 URL. No Plaud auth — signature is in the URL.

        Uses a dedicated 120 s ceiling: a 5 MiB chunk on a poor link can take
        well over the default 30 s transport budget.
        """
        return self._transport.request(method="PUT", url=url, headers={}, body=chunk, timeout=120.0)

    def transcribe_and_summarize(
        self,
        recording_id: str,
        *,
        template_type: str | None = None,
        language: str | None = None,
        diarization: bool | None = None,
        llm: str | None = None,
    ) -> bool:
        """Start whatever processing a recording is missing. False if nothing to do.

        No transcript yet: transcription plus summary (``tasks/trans-sum``).
        Transcript but no summary: summary only (``tasks/summary``).  Both
        present, or a job already running: nothing starts and this returns
        False.  Never re-transcribes a finished recording, since that wipes
        speaker renames and edits.

        ``template_type`` is a Plaud summary template id; "default" or None
        lets Plaud choose.  Plaud rejects a ``template_id`` sent without its
        kind, so the kind is inferred: official templates use codes such as
        ``MEETING`` or ``SALES-BANT``, community templates use hex ids.
        """
        raw = self._get_detail_raw(recording_id)
        detail = self._normalize_recording_detail(raw)
        if (raw.get("task") or {}).get("in_progress_tasks"):
            return False
        if detail.is_trans and detail.is_summary:
            return False
        body: dict[str, Any] = {
            "file_id": detail.id,
            "post_id": int(time.time() * 1000),
            "ppc_status": 1,
            "summary_verbosity": "medium",
            "summary_language": "auto",
            "summary_ai_model": llm or "auto",
        }
        if template_type and template_type.lower() not in ("default", "auto-select"):
            body["template_id"] = template_type
            body["template_type"] = "community" if _HEX_ID.fullmatch(template_type) else "official"
        if detail.is_trans:
            self._request_json("POST", f"{_V4}/tasks/summary", strict=True, body=body)
            return True
        if language and "-" in language:
            language = language.split("-")[0]
        body.update(
            {
                "language": language or "auto",
                "diarization": 0 if diarization is False else 1,
                "force_reload": False,
            }
        )
        self._request_json("POST", f"{_V4}/tasks/trans-sum", strict=True, body=body)
        return True

    def merge_recordings(
        self,
        ids: list[str],
        filename: str,
        *,
        poll_interval_s: float = 3.0,
        timeout_s: float = 300.0,
    ) -> RecordingDetail:
        """Merge recordings into one new recording (``tasks/audio-merge``) and wait for it.

        Plaud only merges recordings that share a folder; the merged
        recording lands in that folder and the originals are kept.  On
        timeout raises ``PlaudWaitTimeoutError`` carrying Plaud's task id:
        the merge keeps running, so re-running it would create a duplicate.
        """
        if len(ids) < 2:
            raise ValueError("merge requires at least 2 recording IDs")
        if not filename.strip():
            raise ValueError("filename cannot be empty")
        file_ids = [to_file_id(rid) for rid in ids]
        first = self._normalize_recording_detail(self._get_detail_raw(file_ids[0]))
        target = self._folder_node_of(first)
        data = (
            self._request_json(
                "POST",
                f"{_V4}/tasks/audio-merge",
                strict=True,
                body={
                    "source_file_ids": file_ids,
                    "target_node_id": target,
                    "file_name": filename,
                    "preserve_original_summaries": True,
                    "regen_after_merge": False,
                    "ppc_status": 1,
                    "post_id": int(time.time() * 1000),
                },
            ).get("data")
            or {}
        )
        task_id = str(data.get("task_id") or "")
        merged_id = str(data.get("file_id") or data.get("target_file_id") or "")

        def check() -> RecordingDetail | None:
            if merged_id:
                try:
                    detail = self.get_recording(merged_id)
                except PlaudApiError as exc:
                    if exc.plaud_code == _STATUS_FILE_NOT_FOUND:
                        return None
                    raise
                return detail if detail.duration else None
            for item in self._walk(f"{_V4}/folders/content", {"node_id": target}):
                if item.get("name") == filename and _is_container(item):
                    file_id = self._container_file_id(str(item.get("node_id") or ""))
                    if file_id:
                        return self.get_recording(file_id)
            return None

        return self._poll_until(
            check, what="merge", timeout_s=timeout_s, poll_interval_s=poll_interval_s, task_id=task_id or None
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

        *check* always runs at least once, so a job that is already done is
        reported done even with a zero budget.  A transient error (429 / 5xx /
        network blip) counts as a skipped poll; anything else propagates.
        When the budget runs out this raises ``PlaudWaitTimeoutError`` (with
        *task_id*, if any): the job is still running server-side.
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

    # ------------------------------------------------------------------
    # Recording writes
    # ------------------------------------------------------------------

    def rename_recording(self, recording_id: str, filename: str) -> None:
        """Rename a recording: its file node and, to match, its container folder."""
        if not filename.strip():
            raise ValueError("filename cannot be empty")
        detail = self._normalize_recording_detail(self._get_detail_raw(recording_id))
        self._rename_node(detail.node_id, filename, detail.version_ms)
        if detail.wrapper_id:
            container = self._container_node(detail)
            self._rename_node(detail.wrapper_id, filename, int(container.get("version_ms") or 0))

    def set_recording_folder(self, recording_ids: str | list[str], folder_id: str | None) -> None:
        """Move one or more recordings into *folder_id* (``None`` unfiles them).

        Each recording moves as its container folder, keeping its audio,
        transcript and notes together.  "Unfiled" in 4.0 is Plaud's default
        Recordings folder.
        """
        ids = [recording_ids] if isinstance(recording_ids, str) else list(recording_ids)
        if not ids:
            raise ValueError("recording_ids cannot be empty")
        target = folder_id or self._system_folder_id(_SYSTEM_FOLDER_RECORDINGS)
        items = [self._movable_item(rid) for rid in ids]
        self._node_batch("POST", "move", {"target_node_id": target, "items": items})

    def move_to_trash(self, recording_ids: str | list[str]) -> None:
        ids = [recording_ids] if isinstance(recording_ids, str) else list(recording_ids)
        if not ids:
            raise ValueError("recording_ids cannot be empty")
        self._node_batch("DELETE", "trash", {"items": [self._movable_item(rid) for rid in ids]})

    def restore_from_trash(self, recording_ids: str | list[str]) -> None:
        ids = [recording_ids] if isinstance(recording_ids, str) else list(recording_ids)
        if not ids:
            raise ValueError("recording_ids cannot be empty")
        self._node_batch("POST", "restore", {"items": self._trash_items(ids)})

    def delete_recordings(self, recording_ids: str | list[str]) -> None:
        """Permanently delete recordings.  Cannot be undone.

        Plaud deletes only from the trash, so a recording not yet there is
        trashed first.
        """
        ids = [recording_ids] if isinstance(recording_ids, str) else list(recording_ids)
        if not ids:
            raise ValueError("recording_ids cannot be empty")
        trashed = {rec.id for rec in self.list_trash()}
        live = [rid for rid in ids if to_file_id(rid) not in trashed]
        if live:
            self.move_to_trash(live)
        self._node_batch("DELETE", "delete", {"items": self._trash_items(ids)})

    # ------------------------------------------------------------------
    # Transcript and summary edits
    # ------------------------------------------------------------------

    def rename_speaker(self, recording_id: str, original_label: str, new_name: str) -> dict[str, int]:
        if not original_label.strip():
            raise ValueError("original_label cannot be empty")
        if not new_name.strip():
            raise ValueError("new_name cannot be empty")

        obj, segments = self._editable_transcript(recording_id)

        # Match the label against BOTH the displayed `speaker` and the
        # `original_speaker` fields.  Plaud auto-resolves enrolled voices, so a
        # never-renamed segment can already show `speaker="Kadin Bullock"` while
        # `original_speaker="Speaker 1"`.  Callers pass whatever label they see
        # in the transcript, so either field may be the one that matches.
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

        self._update_content(recording_id, obj, json.dumps(next_segments, ensure_ascii=False), speakers=True)
        return {"segments_updated": updated}

    def correct_transcript(self, recording_id: str, find: str, replace: str) -> dict[str, int]:
        """Find-and-replace literal text across all transcript segment content.

        Matching is literal (not regex) and case-sensitive, mirroring the web
        client.  Speaker labels are left untouched — use ``rename_speaker``.
        Returns the occurrences replaced and the segments that changed.
        """
        if not find:
            raise ValueError("find text cannot be empty")

        obj, segments = self._editable_transcript(recording_id)

        replacements, segments_changed = count_segment_matches(segments, find)
        if replacements == 0:
            raise ValueError(f'no occurrences of "{find}" found in transcript')

        next_segments = [
            {**segment, "content": segment["content"].replace(find, replace)}
            if find in (segment.get("content") or "")
            else segment
            for segment in segments
        ]
        self._update_content(recording_id, obj, json.dumps(next_segments, ensure_ascii=False))
        return {"replacements": replacements, "segments_changed": segments_changed}

    def count_transcript_matches(self, recording_id: str, find: str) -> int:
        """Count what ``correct_transcript`` would replace, without editing."""
        if not find:
            raise ValueError("find text cannot be empty")
        _obj, segments = self._editable_transcript(recording_id)
        replacements, _ = count_segment_matches(segments, find)
        return replacements

    def set_summary(self, recording_id: str, content: str) -> None:
        """Overwrite a recording's AI summary with ``content`` (full replace).

        The recording must already have a summary — this edits it, it does
        not create one (``transcribe_and_summarize`` generates one).
        """
        if not content.strip():
            raise ValueError("summary content cannot be empty")
        obj, _current = self._summary(recording_id)
        self._update_content(recording_id, obj, content)

    def correct_summary(self, recording_id: str, find: str, replace: str) -> dict[str, int]:
        """Find-and-replace literal text in a recording's AI summary.

        Literal, case-sensitive.  Returns the occurrences replaced; raises
        ``ValueError`` if the text is not found.
        """
        if not find:
            raise ValueError("find text cannot be empty")
        obj, content = self._summary(recording_id)
        if not content:
            raise ValueError(f"recording {recording_id} has no summary text to edit")
        count = content.count(find)
        if count == 0:
            raise ValueError(f'no occurrences of "{find}" found in summary')
        self._update_content(recording_id, obj, content.replace(find, replace))
        return {"replacements": count}

    def _editable_transcript(self, recording_id: str) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        """The editable transcript's content object and segments; ValueError if none."""
        obj = self._object(self._get_detail_raw(recording_id), "TRANSCRIPT")
        segments = self._fetch_segments(obj)
        if obj is None or not segments:
            raise ValueError(f"recording {recording_id} has no transcript yet")
        return obj, segments

    def _summary(self, recording_id: str) -> tuple[dict[str, Any], str]:
        """The summary's content object and current text; ValueError if none."""
        obj = self._object(self._get_detail_raw(recording_id), "SUMMARY")
        if not _object_done(obj):
            raise ValueError(f"recording {recording_id} has no summary yet")
        assert obj is not None
        return obj, self._fetch_text(obj) or ""

    def _update_content(
        self, recording_id: str, obj: dict[str, Any], content: str, *, speakers: bool = False
    ) -> None:
        """Replace one content object's whole body (``files/update/content``).

        There is no partial patch: the web app sends the complete text every
        time.  ``speakers`` flags a speaker rename the way the web app does.
        """
        body: dict[str, Any] = {
            "object_id": obj.get("object_id"),
            "content_id": obj.get("content_id"),
            "content": content,
            "origin_version": obj.get("version_ms"),
        }
        if speakers:
            body["has_replaced_speaker"] = True
        self._request_json(
            "PATCH", f"{_V4}/files/update/content/{to_file_id(recording_id)}", strict=True, body=body
        )

    # ------------------------------------------------------------------
    # Folders
    # ------------------------------------------------------------------

    def list_folders(self) -> list[Folder]:
        """Every user folder, top level first, nested folders after their parent.

        Walks the tree from the root: user folders can sit at the top level
        or inside a system folder (Recordings, Import) or another folder.
        Per-recording container folders are not user folders and are not
        descended into.  Plaud has no tree endpoint, so this takes one request
        per folder; each level of the tree is fetched in parallel, so the
        cost is one round trip per level (about 1 s for a flat library of 16
        folders instead of 4 s one at a time).
        """
        return self._folder_tree("root")

    def subfolders(self, folder_id: str) -> list[Folder]:
        """Every folder inside *folder_id*, at any depth (not the folder itself).

        Backs the folder filter of ``query_recordings``, which counts
        recordings in subfolders as part of the folder.  Costs one request
        per level below the folder; a folder with no subfolders is one.
        """
        if not folder_id:
            raise ValueError("folder_id cannot be empty")
        return self._folder_tree(folder_id)

    def _folder_tree(self, start: str) -> list[Folder]:
        """User folders below *start*, breadth first, one parallel round trip per level.

        From the root this also descends into the system folders (Recordings,
        Import), where user folders can sit too.
        """
        folders: list[Folder] = []
        level = [start]
        with ThreadPoolExecutor(max_workers=_FOLDER_WALK_WORKERS) as pool:
            while level:
                children = pool.map(
                    lambda parent: self._walk(f"{_V4}/folders/children", {"node_id": parent}), level
                )
                next_level: list[str] = []
                for parent, items in zip(level, children, strict=True):
                    for item in items:
                        if _is_user_folder(item):
                            folders.append(self._normalize_folder(item))
                            next_level.append(str(item["node_id"]))
                        elif item.get("type") == 1 and item.get("system_folder_type") and parent == "root":
                            next_level.append(str(item["node_id"]))
                level = next_level
        return folders

    def create_folder(
        self,
        name: str,
        *,
        color: str | None = None,
        icon: str | None = None,
        parent_id: str | None = None,
    ) -> Folder:
        """Create a folder (at the top level unless *parent_id* names a folder)."""
        if not name.strip():
            raise ValueError("folder name cannot be empty")
        data = (
            self._request_json(
                "POST",
                f"{_V4}/nodes/folder",
                strict=True,
                body={
                    "parent_node_id": parent_id or "root",
                    "name": name,
                    "icon": icon or _DEFAULT_FOLDER_ICON,
                    "color": color or _DEFAULT_FOLDER_COLOR,
                },
            ).get("data")
            or {}
        )
        items = data.get("items") or [data]
        return self._normalize_folder(items[0])

    def update_folder(
        self,
        folder_id: str,
        *,
        name: str | None = None,
        color: str | None = None,
        icon: str | None = None,
    ) -> Folder:
        """Edit a folder's name, color, and/or icon; at least one is required."""
        if not folder_id:
            raise ValueError("folder_id cannot be empty")
        if name is not None and not name.strip():
            raise ValueError("folder name cannot be empty")
        if name is None and color is None and icon is None:
            raise ValueError("update_folder requires at least one of name, color, icon")
        return self._rename_folder(self._find_folder(folder_id), name=name, color=color, icon=icon)

    def move_folder(self, folder_id: str, parent_id: str) -> Folder:
        """Move a folder, with everything inside it, into *parent_id* ("root" = top level).

        Folders move with the same ``nodes/move`` call as recordings.  Plaud
        refuses a move into the folder itself or one of its own subfolders
        (-1800308) and a name already taken in the target (-1800306).
        """
        if not folder_id:
            raise ValueError("folder_id cannot be empty")
        if not parent_id:
            raise ValueError('parent_id cannot be empty; pass "root" for the top level')
        return self._move_folder_node(self._find_folder(folder_id), parent_id)

    def nest_colon_folders(self, plan: ColonNestingPlan) -> None:
        """Carry out a ``plan_colon_nesting`` plan: real subfolders for colon names.

        Missing parents are created first, shallowest first.  Then each
        colon-named folder moves under its parent and takes its last name
        part, keeping its id, color, icon and recordings.  Moving before
        renaming means the short name is only ever used inside the new
        parent, where the plan already checked it is free.  The versions come
        from the plan's folder listing, so run this right after planning.  A
        run that stops partway can be planned and run again: converted folders
        have no colon left and created parents are reused.
        """
        ids = dict(plan.owners)
        for path in plan.creates:
            ids[path] = self.create_folder(path[-1], parent_id=parent_id_of(path, ids)).id
        for nesting in plan.moves:
            moved = self._move_folder_node(nesting.folder, parent_id_of(nesting.path, ids))
            self._rename_folder(moved, name=nesting.path[-1])

    def _move_folder_node(self, folder: Folder, parent_id: str) -> Folder:
        """``nodes/move`` one folder; returns it with its new parent and version.

        The move reply's item carries the new ``version_ms`` that a follow-up
        rename must send, so no re-listing (which can lag a write) is needed.
        """
        moved = self._node_batch(
            "POST",
            "move",
            {
                "target_node_id": parent_id,
                "items": [{"node_id": folder.id, "origin_version": folder.version_ms}],
            },
        )
        version = int(moved[0].get("version_ms") or 0) if moved else 0
        if not version:
            return self._find_folder(folder.id)
        return replace(folder, parent_id=parent_id, version_ms=version, raw=moved[0])

    def _rename_folder(
        self, current: Folder, *, name: str | None = None, color: str | None = None, icon: str | None = None
    ) -> Folder:
        """PATCH ``nodes/rename`` with *current*'s version; unchanged fields keep their value."""
        data = (
            self._request_json(
                "PATCH",
                f"{_V4}/nodes/rename/{current.id}",
                strict=True,
                body={
                    "name": name if name is not None else current.name,
                    "color": color if color is not None else current.color,
                    "icon": icon if icon is not None else current.icon,
                    "origin_version": current.version_ms,
                },
            ).get("data")
            or {}
        )
        items = data.get("items") or []
        return self._normalize_folder(items[0]) if items else self._find_folder(current.id)

    def delete_folder(self, folder_id: str) -> None:
        """Delete a folder, keeping what was inside it.  Cannot be undone.

        Plaud's own delete takes a folder's contents with it, so everything
        inside first moves up one level: into the parent folder for a
        subfolder.  For a top-level folder, subfolders move to the top level
        and recordings to the Recordings folder (unfiled).  Then the empty
        folder is trashed and permanently deleted.
        """
        if not folder_id:
            raise ValueError("folder_id cannot be empty")
        folder = self._find_folder(folder_id)
        version = folder.version_ms
        children = self._walk(f"{_V4}/folders/children", {"node_id": folder_id})
        if folder.parent_id not in ("", "root"):
            moves = [(folder.parent_id, children)]
        else:
            subfolders = [c for c in children if _is_user_folder(c)]
            recordings = [c for c in children if not _is_user_folder(c)]
            moves = [("root", subfolders)]
            if recordings:
                moves.append((self._system_folder_id(_SYSTEM_FOLDER_RECORDINGS), recordings))
        for target, nodes in moves:
            if not nodes:
                continue
            moved = self._node_batch(
                "POST",
                "move",
                {
                    "target_node_id": target,
                    "items": [
                        {"node_id": c["node_id"], "origin_version": c.get("version_ms")} for c in nodes
                    ],
                },
            )
            # Moving children out bumps the folder's own version to the move's
            # timestamp, which a listing read straight after may not show yet.
            version = max([version, *(int(item.get("version_ms") or 0) for item in moved)])
        trashed = self._node_batch(
            "DELETE", "trash", {"items": [{"node_id": folder_id, "origin_version": version}]}
        )
        version = next(
            (int(item.get("version_ms") or 0) for item in trashed if item.get("node_id") == folder_id),
            version,
        )
        self._node_batch("DELETE", "delete", {"items": [{"node_id": folder_id, "origin_version": version}]})

    def _find_folder(self, folder_id: str) -> Folder:
        # Most folders are top level, and the root is one request where the
        # full tree walk is one per folder.
        for item in self._walk(f"{_V4}/folders/children", {"node_id": "root"}):
            if item.get("node_id") == folder_id and _is_user_folder(item):
                return self._normalize_folder(item)
        for folder in self.list_folders():
            if folder.id == folder_id:
                return folder
        raise ValueError(f"no folder with id {folder_id!r}; list_folders shows the valid ids")

    def _system_folder_id(self, system_type: int) -> str:
        for item in self._walk(f"{_V4}/folders/children", {"node_id": "root"}):
            if item.get("system_folder_type") == system_type:
                return str(item["node_id"])
        raise PlaudApiError(f"Plaud has no system folder of type {system_type}")

    # ------------------------------------------------------------------
    # Node plumbing
    # ------------------------------------------------------------------

    def _get_detail_raw(self, recording_id: str) -> dict[str, Any]:
        """GET ``files/detail/{file_id}`` and unwrap Plaud's ``data`` envelope."""
        data = self._request_json("GET", f"{_V4}/files/detail/{to_file_id(recording_id)}", strict=True)
        raw = data.get("data")
        if not isinstance(raw, dict):
            raise PlaudApiError("Plaud recording detail response had no data")
        return raw

    def _walk(self, path: str, params: dict[str, Any], *, want: int | None = None) -> list[dict[str, Any]]:
        """Follow a 4.0 list endpoint's ``next_cursor`` until done or *want* items."""
        items: list[dict[str, Any]] = []
        cursor: str | None = None
        for _ in range(_MAX_LIST_PAGES):
            page_size = _LIST_PAGE_MAX if want is None else max(1, min(_LIST_PAGE_MAX, want - len(items)))
            query = {**params, "page_size": page_size, **({"cursor": cursor} if cursor else {})}
            data = self._request_json("GET", f"{path}?{urlencode(query)}", strict=True).get("data") or {}
            items.extend(data.get("items") or [])
            cursor = data.get("next_cursor") or None
            if not cursor or (want is not None and len(items) >= want):
                return items
        raise RuntimeError(
            f"{path} did not end after {_MAX_LIST_PAGES} pages; refusing a possibly partial list"
        )

    def _container_file_id(self, container_id: str) -> str:
        """The recording's file id inside its container folder ('' if none).

        A container can also hold notes (``MD`` files) and attached
        documents next to the recording, in any order, so the ``REC`` file
        wins.  A container with no ``REC`` file falls back to its first
        non-note file.
        """
        files = [
            item
            for item in self._walk(f"{_V4}/folders/children", {"node_id": container_id})
            if item.get("file_id")
        ]
        recordings = [f for f in files if f.get("file_type") == "REC"]
        not_notes = [f for f in files if f.get("file_type") != "MD"]
        return str((recordings or not_notes or [{}])[0].get("file_id") or "")

    def _container_node(self, detail: RecordingDetail) -> dict[str, Any]:
        """The container folder's node (for its version), via ``nodes/ancestor-items``.

        That endpoint returns the recording's breadcrumb plus a window of the
        container's siblings, the container included, in one request.
        """
        data = (
            self._request_json(
                "GET", f"{_V4}/nodes/ancestor-items?{urlencode({'file_id': detail.id})}", strict=True
            ).get("data")
            or {}
        )
        for item in data.get("flat_items") or []:
            if item.get("node_id") == detail.wrapper_id:
                found: dict[str, Any] = item
                return found
        parent = self._folder_node_of(detail)
        for item in self._walk(f"{_V4}/folders/children", {"node_id": parent}):
            if item.get("node_id") == detail.wrapper_id:
                return item
        raise PlaudApiError(f"could not find the container folder of recording {detail.id}")

    def _movable_item(self, recording_id: str) -> dict[str, Any]:
        """``{node_id, origin_version}`` for the node that moves as the recording."""
        detail = self._normalize_recording_detail(self._get_detail_raw(recording_id))
        if detail.wrapper_id:
            container = self._container_node(detail)
            return {"node_id": detail.wrapper_id, "origin_version": container.get("version_ms")}
        return {"node_id": detail.node_id, "origin_version": detail.version_ms}

    def _node_batch(self, method: str, action: str, body: dict[str, Any]) -> list[dict[str, Any]]:
        """Run a batch node operation (move/trash/restore/delete); return its items.

        These endpoints answer status 0 even when some items fail, listing
        them under ``data.failed``, so each reply is checked item by item.
        """
        data = self._request_json(method, f"{_V4}/nodes/{action}", strict=True, body=body).get("data") or {}
        failed = data.get("failed") or []
        if failed:
            reasons = "; ".join(
                f"{f.get('name') or f.get('node_id')}: {f.get('error_message') or f.get('error_code')}"
                for f in failed
            )
            code = str(failed[0].get("error_code") or "")
            raise PlaudApiError(
                f"Plaud API error: {action} failed for {len(failed)} item(s): {reasons}",
                plaud_code=int(code) if code.lstrip("-").isdigit() else code or None,
                plaud_msg=str(failed[0].get("error_message") or ""),
            )
        return [item for item in data.get("items") or [] if isinstance(item, dict)]

    def _trash_items(self, recording_ids: list[str]) -> list[dict[str, Any]]:
        """``{node_id, origin_version}`` for each recording, as it sits in the trash."""
        by_id = {rec.id: rec for rec in self.list_trash()}
        items = []
        for rid in recording_ids:
            rec = by_id.get(to_file_id(rid))
            if rec is None:
                raise ValueError(f"recording {rid} is not in the trash")
            items.append({"node_id": rec.wrapper_id or rec.node_id, "origin_version": rec.version_ms})
        return items

    def _rename_node(self, node_id: str, name: str, version: int) -> None:
        self._request_json(
            "PATCH",
            f"{_V4}/nodes/rename/{node_id}",
            strict=True,
            body={"name": name, "origin_version": version},
        )

    @staticmethod
    def _folder_node_of(detail: RecordingDetail) -> str:
        """The folder (user or system) a recording's container sits in."""
        node = detail.raw.get("node") or {}
        return str((node.get("display_folder") or {}).get("folder_id") or "")

    @staticmethod
    def _object(raw: dict[str, Any], object_type: str) -> dict[str, Any] | None:
        for obj in raw.get("objects") or []:
            if isinstance(obj, dict) and obj.get("object_type") == object_type:
                return obj
        return None

    def _available_transcript_blocks(self, raw: dict[str, Any]) -> list[str]:
        """Which blocks in ``TRANSCRIPT_BLOCKS`` are finished, in that order."""
        return [
            block for block in TRANSCRIPT_BLOCKS if _object_done(self._object(raw, _BLOCK_OBJECT_TYPE[block]))
        ]

    def _get_presigned(self, url: str, *, what: str) -> HttpResponse:
        """GET a presigned S3 URL: no Plaud auth header, the signature is in the URL."""
        response = self._transport.request(method="GET", url=url, headers={"User-Agent": BROWSER_USER_AGENT})
        if response.status_code < 200 or response.status_code >= 300:
            raise PlaudApiError(
                f"{what} failed (HTTP {response.status_code})", http_status=response.status_code
            )
        return response

    def _fetch_text(self, obj: dict[str, Any] | None) -> str | None:
        """A finished content object's body as text (S3 serves it gzip-encoded)."""
        if not _object_done(obj):
            return None
        assert obj is not None
        return self._get_presigned(str(obj["content_url"]), what="content download").text() or None

    def _fetch_segments(self, obj: dict[str, Any] | None) -> list[dict[str, Any]]:
        """A finished transcript object's utterances; ``[]`` when not finished.

        A finished object whose download fails raises ``PlaudApiError`` rather
        than reading as an empty transcript (#210).
        """
        text = self._fetch_text(obj)
        if text is None:
            return []
        try:
            body = json.loads(text)
        except ValueError as exc:
            raise PlaudApiError("Plaud transcript content was not JSON") from exc
        if isinstance(body, dict):
            body = body.get("trans_result") or body.get("segments") or []
        return [s for s in body if isinstance(s, dict)] if isinstance(body, list) else []

    # ------------------------------------------------------------------
    # Normalization
    # ------------------------------------------------------------------

    def _normalize_recording(self, raw: dict[str, Any]) -> Recording:
        """One ``recordings/all`` item.  ``parent_folder`` skips the container."""
        parent_folder = raw.get("parent_folder") or {}
        folder_id = parent_folder.get("folder_id") if not parent_folder.get("system_folder_type") else None
        parent_id = str(raw.get("parent_id") or "")
        return Recording(
            id=str(raw.get("file_id") or ""),
            filename=str(raw.get("name") or ""),
            start_time=int(raw.get("created_at_show_ms") or 0),
            duration=int(raw.get("duration_ms") or 0),
            folder_id=str(folder_id) if folder_id else None,
            node_id=str(raw.get("node_id") or ""),
            wrapper_id=parent_id if parent_id != parent_folder.get("folder_id") else "",
            version_ms=int(raw.get("version_ms") or 0),
            raw=raw,
        )

    @staticmethod
    def _normalize_trash_item(item: dict[str, Any], file_id: str, wrapper_id: str) -> Recording:
        return Recording(
            id=file_id,
            filename=str(item.get("name") or ""),
            start_time=int(item.get("created_at_show_ms") or 0),
            duration=int(item.get("duration_ms") or 0),
            node_id="" if wrapper_id else str(item.get("node_id") or ""),
            wrapper_id=wrapper_id,
            version_ms=int(item.get("version_ms") or 0),
            raw=item,
        )

    def _normalize_recording_detail(self, raw: dict[str, Any]) -> RecordingDetail:
        node = raw.get("node") or {}
        meta = raw.get("meta") or {}
        folder = node.get("folder") or {}
        display = node.get("display_folder") or {}
        # The container is the node's direct parent folder when that differs
        # from the folder the web app displays it in.
        wrapper_id = folder.get("folder_id") if folder.get("folder_id") != display.get("folder_id") else ""
        transcript = self._object(raw, "TRANSCRIPT")
        return RecordingDetail(
            id=str(meta.get("file_id") or ""),
            filename=str(node.get("name") or ""),
            start_time=int(meta.get("start_time") or 0),
            duration=int(meta.get("duration") or 0),
            folder_id=str(display["folder_id"])
            if display.get("folder_id") and not display.get("system_folder_type")
            else None,
            is_trash=bool(node.get("trash_info")),
            is_trans=_object_done(transcript),
            is_summary=_object_done(self._object(raw, "SUMMARY")),
            language=(transcript or {}).get("language"),
            node_id=str(node.get("node_id") or ""),
            wrapper_id=str(wrapper_id or ""),
            version_ms=int(node.get("version_ms") or 0),
            raw=raw,
        )

    @staticmethod
    def _normalize_folder(item: dict[str, Any]) -> Folder:
        return Folder(
            id=str(item.get("node_id") or ""),
            name=str(item.get("name") or ""),
            color=str(item.get("color") or ""),
            icon=str(item.get("icon") or ""),
            parent_id=str(item.get("parent_id") or ""),
            version_ms=int(item.get("version_ms") or 0),
            raw=item,
        )

    # ------------------------------------------------------------------
    # HTTP
    # ------------------------------------------------------------------

    def _request_json(
        self,
        method: str,
        path: str,
        strict: bool,
        body: dict[str, Any] | list[Any] | None = None,
        *,
        timezone: str | None = None,
        _redirected: bool = False,
    ) -> dict[str, Any]:
        """Issue a Plaud API request and return the parsed JSON payload.

        Retry / backoff: an idempotent GET that hits HTTP 429, 5xx or a
        network blip is retried up to ``_MAX_ATTEMPTS - 1`` more times with
        exponential backoff and jitter, never sleeping less than a
        ``Retry-After``.  Other methods get exactly one try, since replaying a
        write risks doing it twice (#147).

        Region redirect: a ``status == -302`` payload updates the stored
        region once and retries immediately; a second redirect raises.

        ``timezone`` overrides the ``x-timezone-iana`` header for this call.
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
            "x-timezone-iana": timezone or self._timezone,
        }
        encoded_body = json.dumps(body).encode("utf-8") if body is not None else None

        last_error: PlaudApiError | None = None
        for attempt in range(_MAX_ATTEMPTS):
            if attempt > 0:
                base_delay = _BACKOFF_BASE * (2 ** (attempt - 1))
                computed_delay = _jitter(base_delay * 0.75, base_delay * 1.25)
                retry_after = last_error.retry_after if last_error is not None else None
                sleep_s = max(computed_delay, retry_after) if retry_after is not None else computed_delay
                _log.debug(
                    "Plaud API retry %d/%d for %s %s — sleeping %.2fs",
                    attempt,
                    _MAX_ATTEMPTS - 1,
                    method,
                    path,
                    sleep_s,
                )
                _sleep(sleep_s)

            try:
                response = self._transport.request(method=method, url=url, headers=headers, body=encoded_body)
            except PlaudApiError as exc:
                _code, retryable = exc.classify()
                if retryable and method == "GET" and attempt < _MAX_ATTEMPTS - 1:
                    last_error = exc
                    continue
                raise

            try:
                payload = response.json()
            except ValueError as exc:
                # #146: a 2xx with a non-JSON body is a Plaud-side problem,
                # not a caller input error.
                raise PlaudApiError(
                    f"Plaud API returned a non-JSON body (HTTP {response.status_code})",
                    http_status=response.status_code,
                ) from exc
            if not isinstance(payload, dict):
                raise PlaudApiError("Plaud API returned a non-object payload.")

            status = payload.get("status")
            if status == -302:
                if _redirected:
                    raise PlaudApiError("region redirect loop")
                next_region = region_for_api_domain(redirect_api_domain(payload))
                if next_region is None:
                    raise PlaudApiError("region redirect to an unrecognized API host")
                self._session_manager.update_region(next_region)
                return self._request_json(
                    method, path, strict=strict, body=body, timezone=timezone, _redirected=True
                )

            if strict and status != 0:
                msg = str(payload.get("msg") or f"status {status}")
                if status == _STATUS_MIGRATED:
                    msg += " (this plaud-tools version predates Plaud 4.0; update plaud-tools)"
                raise PlaudApiError(
                    f"Plaud API error: {msg} (status {status}, {method} {path.split('?', 1)[0]})",
                    plaud_code=status,
                    plaud_msg=str(payload.get("msg") or ""),
                )
            return payload

        assert last_error is not None
        raise last_error
