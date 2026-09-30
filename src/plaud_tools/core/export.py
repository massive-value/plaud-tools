"""Whole-transcript export: one recording's transcript block saved as a file.

Backs the MCP ``export_transcript`` tool and the CLI ``export`` command.  The
transcript is fetched and turned into a file in code, then written to disk;
the caller gets the file's path plus small metadata (fingerprint, byte size,
SHA-256), never the transcript text.  That keeps an archived transcript from
ever passing through a language model, which could drop, merge or reword
utterances while "copying" them.

Formats:

    json  built here, to the version 1 contract SWIRL ingests (below).
    txt, srt, docx, pdf
          rendered by Plaud itself, the same files the web app's Export
          button produces (PlaudClient.render_transcript_document).  We send
          Plaud the utterances we just fetched, so the file matches the
          fingerprint we report.  Speaker labels and timestamps are optional
          for these; SRT always gets timestamps, because Plaud writes every
          cue as 00:00:00,000 --> 00:00:00,000 without them.

Plaud serves a transcript block as a single download, so an export reads one
consistent revision in one request: there are no pages to stitch together and
no way for two revisions to mix.  The paging in ``get_recording`` is ours, for
a model reading along; it plays no part here.

JSON contract (agreed with SWIRL's version 1 ingestion parser):

    {
      "schema_version": 1,
      "source": "plaud",
      "recording_id": "<Plaud file id>",
      "portion_id": "full",
      "title": "<recording title>",           # omitted when Plaud has none
      "recorded_at": "2026-09-30T15:00:00Z",  # omitted when Plaud has none
      "transcript_block": "transaction" | "transaction_polish",
      "transcript_fingerprint": "sha256:<hex>",  # see query.transcript_fingerprint
      "segments": [
        {"index": 0, "speaker": "Speaker 1" | null, "speaker_id": "Speaker 1" | null,
         "text": "<verbatim>", "start_ms": 1000 | null, "end_ms": 6500 | null},
        ...
      ]
    }

``speaker`` is the label Plaud shows (an enrolled name like "Jane Doe" or a
diarization label like "Speaker 1"); a label is not proof of identity.
``speaker_id`` is Plaud's ``original_speaker``, the diarization label the
recording started with, kept when Plaud sends it.  It is only meaningful
within one recording; Plaud has no cross-recording speaker identifier.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from . import appdata
from .client import TRANSCRIPT_BLOCKS, PlaudClient
from .models import RecordingDetail
from .query import transcript_fingerprint

SCHEMA_VERSION = 1
FULL_PORTION = "full"

# format -> MIME type.  The format is also the file extension.
EXPORT_FORMATS: dict[str, str] = {
    "json": "application/json",
    "txt": "text/plain",
    "srt": "application/x-subrip",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "pdf": "application/pdf",
}

# First bytes of a real file, so an error page saved as "meeting.pdf" is caught.
_MAGIC_BYTES = {"docx": b"PK\x03\x04", "pdf": b"%PDF"}

# SWIRL's limits.  Recording IDs also become file names, so the pattern keeps
# them to characters that are safe in a path on every OS.
_RECORDING_ID_PATTERN = re.compile(r"[A-Za-z0-9_-]{1,128}")
_MAX_SPEAKER_CHARS = 200


class TranscriptExportError(Exception):
    """An export that must not produce a file.

    ``code`` is the MCP ``error_code``: ``transcript_unavailable`` (untranscribed,
    missing block, or empty block), ``revision_conflict`` (the transcript no
    longer matches the fingerprint the caller reviewed), ``file_exists`` (the
    chosen output file is already there and ``overwrite`` was not set) or
    ``invalid_source`` (Plaud sent something that breaks the file contract,
    e.g. negative timing, or a rendered file that is not the format asked for).
    Messages name the problem and the utterance index, never transcript text.
    """

    def __init__(self, message: str, code: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class ExportedFile:
    """One file written by an export, described well enough to verify its bytes."""

    path: Path
    mime_type: str
    byte_size: int
    sha256: str

    @classmethod
    def write(cls, path: Path, data: bytes, mime_type: str) -> ExportedFile:
        """Write *data* to *path* atomically and describe the result.

        The bytes go to a temporary file in the same folder first and are
        renamed into place only once fully written, so a crash or full disk
        never leaves a truncated file under the final name.
        """
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, temp_name = tempfile.mkstemp(dir=path.parent, prefix=".export-", suffix=".tmp")
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(data)
            os.replace(temp_name, path)
        except BaseException:
            Path(temp_name).unlink(missing_ok=True)
            raise
        return cls(path, mime_type, len(data), hashlib.sha256(data).hexdigest())

    def to_dict(self) -> dict[str, Any]:
        resolved = self.path.resolve()
        return {
            "path": str(resolved),
            "uri": resolved.as_uri(),
            "mime_type": self.mime_type,
            "byte_size": self.byte_size,
            "sha256": self.sha256,
        }


class TranscriptExport:
    """A validated version 1 JSON document for one transcript block.

    Build one with ``from_source`` (pure: no network, no disk).
    """

    def __init__(self, document: dict[str, Any]) -> None:
        self.document = document

    @classmethod
    def from_source(
        cls, detail: RecordingDetail, block: str, segments: list[dict[str, Any]]
    ) -> TranscriptExport:
        """Map Plaud's raw utterances onto the file contract, validating each one."""
        document: dict[str, Any] = {
            "schema_version": SCHEMA_VERSION,
            "source": "plaud",
            "recording_id": detail.id,
            "portion_id": FULL_PORTION,
        }
        if detail.filename:
            document["title"] = detail.filename
        if detail.start_time:
            document["recorded_at"] = _utc_iso(detail.start_time)
        document["transcript_block"] = block
        document["transcript_fingerprint"] = transcript_fingerprint(segments, block)
        document["segments"] = [_export_segment(index, segment) for index, segment in enumerate(segments)]
        return cls(document)

    def json_bytes(self) -> bytes:
        """The file's bytes.  Deterministic: an unchanged source re-exports byte-identically."""
        return (json.dumps(self.document, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def export_transcript(
    client: PlaudClient,
    recording_id: str,
    *,
    fmt: str = "json",
    block: str = "transaction",
    with_speakers: bool = True,
    with_timestamps: bool = True,
    expected_fingerprint: str | None = None,
    output_path: str | None = None,
    overwrite: bool = False,
) -> dict[str, Any]:
    """Export a recording's whole transcript block to a file; return metadata only.

    *output_path* is a file path, or a folder (existing, or written with a
    trailing slash) to save ``<recording_id>.<block>.<fmt>`` in.  Without
    it the file goes to the app's exports folder, where the next export of
    the same recording, block and format replaces it.  A file at an explicit
    path is never replaced unless *overwrite* is true: that path may be a
    synced SharePoint folder.

    Raises ``ValueError`` for bad arguments and ``TranscriptExportError`` when
    no file may be produced.  Nothing is written unless the whole transcript
    passes, and a refused export never falls back to the other block.
    Read-only toward Plaud.
    """
    if not _RECORDING_ID_PATTERN.fullmatch(recording_id):
        raise ValueError("recording_id must be a Plaud recording ID (up to 128 letters, digits, '-' or '_')")
    if block not in TRANSCRIPT_BLOCKS:
        raise ValueError(f"transcript_block must be one of: {', '.join(TRANSCRIPT_BLOCKS)}")
    if fmt not in EXPORT_FORMATS:
        raise ValueError(f"format must be one of: {', '.join(EXPORT_FORMATS)}")
    destination = _destination(output_path, f"{recording_id}.{block}.{fmt}")
    if output_path is not None and destination.exists() and not overwrite:
        raise TranscriptExportError(
            f"{destination} already exists. Pick another path, or pass overwrite to replace it.",
            "file_exists",
        )

    detail = client.get_recording(recording_id, include_transcript=True, transcript_block=block)
    available = detail.transcript_blocks_available or []
    if not available:
        raise TranscriptExportError(
            f"Recording {recording_id} has not been transcribed yet.", "transcript_unavailable"
        )
    if block not in available:
        raise TranscriptExportError(
            f"Recording {recording_id} has no {block!r} transcript. Available: {', '.join(available)}.",
            "transcript_unavailable",
        )
    segments = detail.transcript_segments
    if not segments:
        raise TranscriptExportError(
            f"The {block!r} transcript for recording {recording_id} has no utterances.",
            "transcript_unavailable",
        )
    fingerprint = transcript_fingerprint(segments, block)
    if expected_fingerprint is not None and fingerprint != expected_fingerprint:
        raise TranscriptExportError(
            f"The transcript changed since it was reviewed (expected {expected_fingerprint}, "
            f"now {fingerprint}). Review the current transcript again before exporting.",
            "revision_conflict",
        )

    if fmt == "json":
        data = TranscriptExport.from_source(detail, block, segments).json_bytes()
    else:
        data = client.render_transcript_document(
            detail,
            segments,
            fmt.upper(),
            with_speaker=with_speakers,
            with_timestamp=with_timestamps or fmt == "srt",
        )
        if not data or not data.startswith(_MAGIC_BYTES.get(fmt, b"")):
            raise TranscriptExportError(f"Plaud did not return a valid {fmt} file.", "invalid_source")

    written = ExportedFile.write(destination, data, EXPORT_FORMATS[fmt])
    return {
        "recording_id": recording_id,
        "portion_id": FULL_PORTION,
        "format": fmt,
        "transcript_block": block,
        "transcript_fingerprint": fingerprint,
        "segment_count": len(segments),
        "title": detail.filename or None,
        "recorded_at": _utc_iso(detail.start_time) if detail.start_time else None,
        "file": written.to_dict(),
    }


def _destination(output_path: str | None, default_name: str) -> Path:
    """Where to write: the exports folder, a folder the caller named, or their exact file path."""
    if output_path is None:
        return appdata.exports_dir() / default_name
    path = Path(output_path).expanduser()
    # A trailing separator means "this is a folder" even before it exists.
    if path.is_dir() or output_path.endswith(("/", "\\")):
        return path / default_name
    return path


def _utc_iso(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def _export_segment(index: int, segment: dict[str, Any]) -> dict[str, Any]:
    """One raw Plaud utterance in contract shape; raises on anything that breaks the contract."""
    text = segment.get("content", "")
    if not isinstance(text, str):
        raise TranscriptExportError(f"Utterance {index} has non-text content.", "invalid_source")
    speaker = _optional_label(segment.get("speaker")) or _optional_label(segment.get("original_speaker"))
    if speaker is not None and len(speaker) > _MAX_SPEAKER_CHARS:
        raise TranscriptExportError(
            f"Utterance {index} has a speaker label over {_MAX_SPEAKER_CHARS} characters.", "invalid_source"
        )
    start_ms, end_ms = _timing(index, segment.get("start_time"), segment.get("end_time"))
    return {
        "index": index,
        "speaker": speaker,
        "speaker_id": _optional_label(segment.get("original_speaker")),
        "text": text,
        "start_ms": start_ms,
        "end_ms": end_ms,
    }


def _optional_label(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _timing(index: int, start: Any, end: Any) -> tuple[int | None, int | None]:
    """Validate utterance timing: both whole, non-negative ms with end >= start, or both absent."""
    if start is None and end is None:
        return None, None
    values = []
    for value in (start, end):
        if isinstance(value, float) and value.is_integer():
            value = int(value)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise TranscriptExportError(
                f"Utterance {index} has missing or invalid timing (start={start!r}, end={end!r}).",
                "invalid_source",
            )
        values.append(value)
    if values[1] < values[0]:
        raise TranscriptExportError(f"Utterance {index} ends before it starts.", "invalid_source")
    return values[0], values[1]
