"""Shared query helpers used by both cli.py and mcp.py.

These were previously duplicated across the two modules with slight API
differences; the canonical versions below reconcile those differences.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Collection
from datetime import date, datetime
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    # client.py imports this module, so these are for type hints only.
    from .client import PlaudClient
    from .models import Recording


def format_transcript(segments: list[dict[str, Any]]) -> str:
    """Render utterance dicts as "Speaker: content" blocks.

    A pure function of the segments, so it lives here rather than on
    ``PlaudClient``: the client formats a whole transcript while the MCP facade
    formats a single *page* of utterances, and neither needs a session to do it.
    """
    parts: list[str] = []
    for segment in segments:
        speaker = segment.get("speaker") or segment.get("original_speaker") or ""
        content = segment.get("content") or ""
        parts.append(f"{speaker}: {content}" if speaker else content)
    return "\n\n".join(parts)


def _timing(value: Any) -> int | float | None:
    """Upstream timing as-is when numeric; ``None`` when missing or malformed."""
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return value


def structured_segments(segments: list[dict[str, Any]], start: int = 0) -> list[dict[str, Any]]:
    """Render utterance dicts as source-location records for export/citation.

    ``start`` is the absolute index of ``segments[0]`` within the full block, so
    a page of utterances keeps the same ``index`` values as the whole
    transcript. ``start_ms``/``end_ms`` are Plaud's per-utterance timings —
    milliseconds from the start of the recording — passed through verbatim, or
    ``None`` when Plaud did not send them. ``speaker`` matches the label
    ``format_transcript`` prints.

    Shared by the CLI ``transcript --segments`` export and MCP
    ``get_recording(include=["segments"])`` so both surfaces cite identical
    locations.
    """
    return [
        {
            "index": start + offset,
            "speaker": segment.get("speaker") or segment.get("original_speaker") or "",
            "text": segment.get("content") or "",
            "start_ms": _timing(segment.get("start_time")),
            "end_ms": _timing(segment.get("end_time")),
        }
        for offset, segment in enumerate(segments)
    ]


def transcript_fingerprint(segments: list[dict[str, Any]], block: str) -> str:
    """Content fingerprint of a whole transcript block: ``"sha256:<hex>"``.

    Hashes the block name plus every utterance field as Plaud returned it
    (text, speakers, timings), in order, so any edit — a corrected word, a
    renamed speaker — changes it, and an unchanged block hashes identically
    on every read. The block name is in the input so a raw and a polished
    transcript that happen to match never share a fingerprint. Paging
    callers compare it across pages to detect an edit mid-read and restart
    instead of splicing two versions; ``export_transcript`` stamps it into
    the exported file. This is our hash of the content, not a Plaud revision
    number; Plaud exposes none.

    Algorithm: SHA-256 of the UTF-8 bytes of
    ``json.dumps({"block": block, "segments": segments}, sort_keys=True,
    ensure_ascii=False, separators=(",", ":"))``.
    """
    canonical = json.dumps(
        {"block": block, "segments": segments}, sort_keys=True, ensure_ascii=False, separators=(",", ":")
    )
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def parse_isoish(value: str, field_name: str, *, end_of_day: bool = False) -> int:
    """Parse an ISO 8601 date/datetime string (or 'Z'-suffixed variant) to ms epoch.

    Reconciliation note: cli.py called the parameter ``flag`` while mcp.py used
    ``field_name``; both produced the same error message pattern so ``field_name``
    is kept as the canonical name.

    ``end_of_day`` applies only to date-only input ("2026-01-05" means through
    23:59:59.999 that day); an explicit time such as "2026-01-05 10:00" is
    taken as given.
    """
    try:
        normalized = value.replace("Z", "+00:00")
        dt = datetime.fromisoformat(normalized)
        if end_of_day and _is_date_only(value):
            dt = dt.replace(hour=23, minute=59, second=59, microsecond=999999)
        return int(dt.timestamp() * 1000)
    except ValueError as exc:
        raise ValueError(f"Invalid {field_name} value: {value}") from exc


def parse_keep_ranges(specs: list[str]) -> list[tuple[int, int]]:
    """Turn ``"mm:ss-mm:ss"`` keep ranges into ``(start_ms, end_ms)`` pairs.

    Each side is ``ss``, ``mm:ss`` or ``h:mm:ss`` (seconds may carry a
    fraction), e.g. ``"0:00-1:00"`` or ``"1:05:30-1:10:00"``.  Raises
    ``ValueError`` naming the bad range.
    """
    ranges: list[tuple[int, int]] = []
    for spec in specs:
        start_text, dash, end_text = spec.strip().partition("-")
        if not dash:
            raise ValueError(f"invalid range {spec!r}: use start-end, e.g. 0:00-1:00")
        ranges.append((_clock_to_ms(start_text, spec), _clock_to_ms(end_text, spec)))
    return ranges


def _clock_to_ms(text: str, spec: str) -> int:
    """``ss``, ``mm:ss`` or ``h:mm:ss`` to milliseconds; *spec* is the range for error messages."""
    parts = text.strip().split(":")
    try:
        if not 1 <= len(parts) <= 3 or any(not part.strip() for part in parts):
            raise ValueError
        seconds = 0.0
        for part in parts:
            value = float(part)
            if value < 0:
                raise ValueError
            seconds = seconds * 60 + value
    except ValueError:
        raise ValueError(f"invalid time {text.strip()!r} in range {spec!r}: use mm:ss or h:mm:ss") from None
    return round(seconds * 1000)


def _is_date_only(value: str) -> bool:
    """True when *value* is a bare ISO date with no time part."""
    try:
        date.fromisoformat(value)
    except ValueError:
        return False
    return True


def filter_recordings(
    items: list[Any],
    *,
    since_ms: int | None = None,
    until_ms: int | None = None,
    query: str | None = None,
    folder_ids: Collection[str] | None = None,
    unfiled: bool = False,
) -> list[Any]:
    """Filter Recording objects in memory and sort them newest first.

    ``query`` is a case-insensitive title substring.  ``folder_ids`` keeps
    recordings filed in any of those folders; ``unfiled=True`` keeps only
    recordings in no folder and wins over ``folder_ids``.  The MCP surface
    passes ``folder=""`` for "unfiled" and translates it to ``unfiled=True``
    before calling here, so ``unfiled`` is the one internal convention.
    """
    filtered = list(items)
    if since_ms is not None:
        filtered = [item for item in filtered if item.start_time >= since_ms]
    if until_ms is not None:
        filtered = [item for item in filtered if item.start_time <= until_ms]
    if query:
        query_lower = query.lower()
        filtered = [item for item in filtered if query_lower in item.filename.lower()]
    if unfiled:
        filtered = [item for item in filtered if not item.folder_id]
    elif folder_ids is not None:
        filtered = [item for item in filtered if item.folder_id in folder_ids]
    filtered.sort(key=lambda item: item.start_time, reverse=True)
    return filtered


def query_recordings(
    client: PlaudClient,
    *,
    trash: bool = False,
    since_ms: int | None = None,
    until_ms: int | None = None,
    query: str | None = None,
    folder_id: str | None = None,
    unfiled: bool = False,
    after: int = 0,
    limit: int | None = None,
) -> tuple[list[Recording], bool]:
    """One page of recordings matching the filters, and whether more follow.

    The one list query behind CLI ``list``/``search``/``trash --list`` and MCP
    ``browse_recordings``.  Plaud 4.0 returns a whole library in one request
    (about 0.3 s for 800 recordings), so this fetches the full list once
    (active recordings, or the trash), filters it in memory, and slices
    ``[after : after + limit]``.  ``limit=None`` returns every match from
    ``after`` on, with ``has_more`` False.

    ``folder_id`` matches recordings in that folder *and its subfolders*, the
    way a person means "what's in Client Meetings" once its old colon-named
    folders are real subfolders.  Each recording's own ``folder_id`` still
    says exactly where it sits.  Finding the subfolders costs one request
    per level below the folder (one for a folder with none).

    Trashed recordings carry no folder in Plaud 4.0, so a folder or unfiled
    filter on the trash is refused rather than answered with nothing.
    """
    if trash and (folder_id is not None or unfiled):
        raise ValueError("trashed recordings have no folder, so the trash cannot be filtered by folder")
    items = client.list_trash() if trash else client.list_recordings()
    folder_ids = None
    if folder_id is not None and not unfiled:
        folder_ids = {folder_id, *(folder.id for folder in client.subfolders(folder_id))}
    matched = filter_recordings(
        items,
        since_ms=since_ms,
        until_ms=until_ms,
        query=query,
        folder_ids=folder_ids,
        unfiled=unfiled,
    )
    if limit is None:
        return matched[after:], False
    return matched[after : after + limit], len(matched) > after + limit


def folder_dict(folder: Any) -> dict[str, Any]:
    """The standard folder dict (CLI and MCP folder listings and edits).

    ``parent_id`` is "root" for a top-level folder, otherwise the folder (or
    system folder) it sits in.
    """
    return {
        "id": folder.id,
        "name": folder.name,
        "color": folder.color,
        "icon": folder.icon,
        "parent_id": folder.parent_id,
    }


# Said whenever a share link is revoked: Plaud keeps the URL, so a later share revives it.
SHARE_REVOKE_NOTE = (
    "Revoked. Plaud does not rotate the URL: sharing this recording again brings back the same URL."
)


def vocabulary_dict(entry: Any) -> dict[str, Any]:
    """The standard vocabulary-entry dict (CLI and MCP vocabulary listings and edits)."""
    return {
        "id": entry.id,
        "word": entry.word,
        "heard_as": entry.heard_as,
        "language": entry.language,
        "correction_count": entry.correction_count,
    }


def share_dict(link: Any) -> dict[str, Any]:
    """The standard share-link dict (CLI and MCP).  ``expires_at`` is null for a link that never expires."""
    return {
        "url": link.url,
        "expires_at": link.expires_at,
        "includes": {"audio": link.audio, "transcript": link.transcript, "summary": link.summary},
    }


def detail_summary_dict(detail: Any) -> dict[str, Any]:
    """Produce the base summary dict for a RecordingDetail (a "show" view).

    Shared core of cli.py's ``_handle_show`` (inline dict) and mcp.py's
    ``_summarize_detail`` (near-identical near-twins before Wave 5's §7.5
    consolidation) -- id/title/date/duration_minutes/folder_id/is_trans/
    is_summary. mcp.py's ``get_recording`` handler adds its own extra fields
    (is_trash, language) on top of this base; cli.py's ``show`` command uses
    the base as-is.
    """
    return {
        "id": detail.id,
        "title": detail.filename,
        "date": datetime.fromtimestamp(detail.start_time / 1000).isoformat()[:16],
        "duration_minutes": round(detail.duration / 60000),
        "folder_id": detail.folder_id,
        "is_trans": detail.is_trans,
        "is_summary": detail.is_summary,
    }


SNIPPET_CHARS = 240


def content_snippet(
    text: str,
    query: str,
    keywords: list[str],
    *,
    mid_text: bool = False,
    width: int = SNIPPET_CHARS,
) -> str:
    """Cut a short, whitespace-collapsed window of *text* around the first hit.

    Plaud's search chunks run 1-2k characters, far more than a caller needs
    to judge a match.  The hit is located by the caller's own query words
    first, then by Plaud's stemmed ``keywords`` (``"retir"`` for
    "retirement"), because a short stem can also match inside an unrelated
    word.  With no hit the window starts at the top of the chunk.  Cuts land
    on word boundaries and are marked with an ellipsis.  ``mid_text`` says the
    chunk itself begins partway through the document (Plaud sends an
    ``offset``), often mid-word, so its start is treated as a cut too.
    """
    flat = " ".join(text.split())
    lower = flat.lower()
    hit = 0
    for terms in (query.split(), keywords):
        positions = [lower.find(term.lower()) for term in terms if term]
        found = [pos for pos in positions if pos >= 0]
        if found:
            hit = min(found)
            break
    start = max(0, hit - width // 3)
    end = min(len(flat), start + width)
    if start > 0 or mid_text:
        space = flat.find(" ", start, hit)
        if space != -1:
            start = space + 1
    if end < len(flat):
        space = flat.rfind(" ", hit, end)
        if space != -1:
            end = space
    snippet = flat[start:end]
    return f"{'…' if start > 0 or mid_text else ''}{snippet}{'…' if end < len(flat) else ''}"


def summarize_match(match: Any) -> dict[str, Any]:
    """Produce the standard dict for a ContentMatch (CLI and MCP content search)."""
    return {
        "id": match.id,
        "title": match.title,
        "date": datetime.fromtimestamp(match.start_time / 1000).isoformat()[:16],
        "source": match.source,
        "snippet": match.snippet,
        "start_ms": match.start_ms,
    }


def summarize_recording(item: Any) -> dict[str, Any]:
    """Produce the standard summary dict for a Recording.

    Reconciliation note: mcp.py defined ``_summarize_recording`` locally;
    client.py exported an identical ``summarize_recording_for_cli`` used by
    cli.py.  Both functions produced the same output; this is the single
    canonical version.  The client.py re-export had no remaining callers and
    was deleted in Wave 5 (2026-07-06 audit, §7.4).
    """
    return {
        "id": item.id,
        "title": item.filename,
        "date": datetime.fromtimestamp(item.start_time / 1000).isoformat()[:16],
        "duration_minutes": round(item.duration / 60000),
        "folder_id": item.folder_id,
    }
