"""Shared query helpers used by both cli.py and mcp.py.

These were previously duplicated across the two modules with slight API
differences; the canonical versions below reconcile those differences.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from datetime import date, datetime
from typing import Any


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


def transcript_fingerprint(segments: list[dict[str, Any]]) -> str:
    """Content fingerprint of a whole transcript block: ``"sha256:<hex>"``.

    Hashes every utterance field as Plaud returned it (text, speakers,
    timings), so any edit — a corrected word, a renamed speaker — changes it,
    and an unchanged block hashes identically on every read. Paging callers
    compare it across pages to detect an edit mid-read and restart instead of
    splicing two versions. This is our hash of the content, not a Plaud
    revision number; Plaud exposes none.
    """
    canonical = json.dumps(segments, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
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
    since_ms: int | None,
    until_ms: int | None,
    query: str | None,
    folder_id: str | None,
    unfiled: bool = False,
) -> list[Any]:
    """Filter and sort a list of Recording objects.

    Reconciliation notes:
    - cli.py accepted an explicit ``unfiled`` boolean kwarg and used an
      ``elif folder_id is not None`` branch so that ``unfiled=True`` took
      priority over any ``folder_id``.
    - mcp.py had no ``unfiled`` kwarg and instead used ``folder_id=""`` as the
      sentinel for "no folder assigned"; it now translates its own
      ``folder=""`` MCP parameter into ``unfiled=True`` before calling here
      (Wave 5, §7.8), so ``unfiled=True`` is the single internal convention.
    - mcp.py did NOT sort; cli.py sorted descending by start_time.  The sort is
      included here so callers get consistent ordering regardless of surface.
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
        filtered = [item for item in filtered if not item.filetag_id_list]
    elif folder_id is not None:
        filtered = [item for item in filtered if folder_id in item.filetag_id_list]
    filtered.sort(key=lambda item: item.start_time, reverse=True)
    return filtered


# Upstream page size for incremental filtered browse (shared by cli.py and mcp.py).
BROWSE_PAGE_SIZE = 200
# Safety stop for exhaustive paging: 1000 pages x 200 = 200k recordings, far
# beyond any real library. Only hit if the upstream ignores ``skip``.
MAX_BROWSE_PAGES = 1000


def collect_filtered_paged(
    fetch_page: Callable[[int, int], list[Any]],
    page_size: int,
    *,
    since_ms: int | None,
    until_ms: int | None,
    query: str | None,
    folder_id: str | None,
    unfiled: bool = False,
    after: int = 0,
    limit: int | None,
) -> tuple[list[Any], bool]:
    """Incrementally fetch upstream pages, filter each one, and stop early.

    ``fetch_page(skip, page_size)`` must return a list of Recording-like objects
    for the given upstream window.  Paging stops when either:
    - ``after + limit + 1`` filtered matches have been collected (enough to
      resolve ``has_more`` without over-fetching), or
    - the upstream returns fewer than ``page_size`` items (list exhausted), or
    - with ``since_ms`` set, a batch reaches back past ``since_ms``.  This
      relies on ``fetch_page`` returning newest-first (``sort_by=start_time``,
      ``is_desc=True``, as both callers request): every later page is older
      still, so none of it can match.  Without it, a narrow date filter on a
      big library scanned the whole library.

    ``limit=None`` means "everything": paging only stops at upstream
    exhaustion (backs CLI ``list --all``), and ``has_more`` is always False.
    Each request is still bounded to ``page_size``; if the upstream never runs
    dry within ``MAX_BROWSE_PAGES`` requests this raises instead of returning a
    list that merely looks complete.

    Returns ``(page, has_more)`` where ``page`` is the slice
    ``matched[after:after+limit]`` and ``has_more`` is True when a subsequent
    page would be non-empty.
    """
    need = None if limit is None else after + limit + 1
    matched: list[Any] = []
    upstream_skip = 0

    while need is None or len(matched) < need:
        if upstream_skip >= MAX_BROWSE_PAGES * page_size:
            raise RuntimeError(
                f"recording list did not end after {MAX_BROWSE_PAGES} pages of {page_size}; "
                "refusing to return a possibly incomplete list"
            )
        batch = fetch_page(upstream_skip, page_size)
        if not batch:
            break
        filtered = filter_recordings(
            batch,
            since_ms=since_ms,
            until_ms=until_ms,
            query=query,
            folder_id=folder_id,
            unfiled=unfiled,
        )
        matched.extend(filtered)
        if len(batch) < page_size:
            break
        if since_ms is not None and min(item.start_time for item in batch) < since_ms:
            break
        upstream_skip += page_size

    if limit is None:
        return matched[after:], False
    page = matched[after : after + limit]
    has_more = len(matched) > after + limit
    return page, has_more


def folder_dict(tag: Any) -> dict[str, Any]:
    """Produce the standard {id, name, color, icon} dict for a FileTag.

    Built independently four times across cli.py (_handle_folders,
    _handle_folder) and mcp.py (list_folders, mutate_folder create/edit)
    before Wave 5's §7.5 consolidation.
    """
    return {"id": tag.id, "name": tag.name, "color": tag.color, "icon": tag.icon}


def detail_summary_dict(detail: Any) -> dict[str, Any]:
    """Produce the base summary dict for a RecordingDetail (a "show" view).

    Shared core of cli.py's ``_handle_show`` (inline dict) and mcp.py's
    ``_summarize_detail`` (near-identical near-twins before Wave 5's §7.5
    consolidation) -- id/title/date/duration_minutes/folder_id/is_trans/
    is_summary/headline. mcp.py's ``get_recording`` handler adds its own
    extra fields (is_trash, language, used_template) on top of this base;
    cli.py's ``show`` command uses the base as-is to keep its existing
    output shape unchanged.
    """
    extra = detail.extra_data or {}
    return {
        "id": detail.id,
        "title": detail.filename,
        "date": datetime.fromtimestamp(detail.start_time / 1000).isoformat()[:16],
        "duration_minutes": round(detail.duration / 60000),
        "folder_id": detail.folder_id,
        "is_trans": detail.is_trans,
        "is_summary": detail.is_summary,
        "headline": (extra.get("aiContentHeader") or {}).get("headline"),
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
        "has_transcript": item.is_trans,
        "has_summary": item.is_summary,
        "folder_id": item.filetag_id_list[0] if item.filetag_id_list else None,
    }
