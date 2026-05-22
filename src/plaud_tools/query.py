"""Shared query helpers used by both cli.py and mcp.py.

These were previously duplicated across the two modules with slight API
differences; the canonical versions below reconcile those differences.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any


def parse_isoish(value: str, field_name: str, *, end_of_day: bool = False) -> int:
    """Parse an ISO 8601 date/datetime string (or 'Z'-suffixed variant) to ms epoch.

    Reconciliation note: cli.py called the parameter ``flag`` while mcp.py used
    ``field_name``; both produced the same error message pattern so ``field_name``
    is kept as the canonical name.
    """
    try:
        normalized = value.replace("Z", "+00:00")
        dt = datetime.fromisoformat(normalized)
        if end_of_day and "T" not in value:
            dt = dt.replace(hour=23, minute=59, second=59, microsecond=999999)
        return int(dt.timestamp() * 1000)
    except ValueError as exc:
        raise ValueError(f"Invalid {field_name} value: {value}") from exc


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
      sentinel for "no folder assigned".
    - The canonical version supports both conventions: ``unfiled=True`` OR
      ``folder_id=""`` each trigger the "no filetag" filter so that neither
      caller needs to change its existing calling pattern.
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
    if unfiled or folder_id == "":
        filtered = [item for item in filtered if not item.filetag_id_list]
    elif folder_id is not None:
        filtered = [item for item in filtered if folder_id in item.filetag_id_list]
    filtered.sort(key=lambda item: item.start_time, reverse=True)
    return filtered


def summarize_recording(item: Any) -> dict[str, Any]:
    """Produce the standard summary dict for a Recording.

    Reconciliation note: mcp.py defined ``_summarize_recording`` locally;
    client.py exported an identical ``summarize_recording_for_cli`` used by
    cli.py.  Both functions produced the same output; this is the single
    canonical version.  ``summarize_recording_for_cli`` in client.py is kept
    as a thin re-export for any external callers that may depend on it.
    """
    return {
        "id": item.id,
        "title": item.filename,
        "date": datetime.fromtimestamp(item.start_time / 1000).isoformat()[:16],
        "duration_minutes": round(item.duration / 60000),
        "has_transcript": item.is_trans,
        "folder_id": item.filetag_id_list[0] if item.filetag_id_list else None,
    }
