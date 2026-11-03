"""Reading Plaud summary and note markdown.

Plaud 4.0 summaries embed invisible ``plaud://`` links: citations back into
the transcript (``plaud://source?...&start_time=125370``) and markup for the
summary card and highlight card images.  ``render_summary`` turns the
citations into ``[2:05]`` and drops the card markup so people and agents read
prose.  It is for display only: edits always work on the raw markdown, so every
link survives a round trip.
"""

from __future__ import annotations

import re
from urllib.parse import parse_qs, urlsplit

from .models import RecordingNote

# ``[](plaud://source?...)``: a citation, an empty link Plaud's web app shows as a chip.
_SOURCE_LINK = re.compile(r"\[\]\((plaud://source\?[^)]*)\)")
# Image and highlight cards: empty links with nothing readable in them.
_CARD_LINK = re.compile(r"\[\]\(plaud://(?:image|highlight-card)[^)]*\)")
_CARD_LINE = re.compile(rf"^[ \t]*{_CARD_LINK.pattern}[ \t]*\n?", re.MULTILINE)


def format_clock(ms: int) -> str:
    """Milliseconds as ``m:ss``, or ``h:mm:ss`` from the first hour on."""
    minutes, seconds = divmod(max(ms, 0) // 1000, 60)
    hours, minutes = divmod(minutes, 60)
    return f"{hours}:{minutes:02d}:{seconds:02d}" if hours else f"{minutes}:{seconds:02d}"


def _citation(match: re.Match[str]) -> str:
    start = parse_qs(urlsplit(match.group(1)).query).get("start_time", [""])[0]
    return f"[{format_clock(int(start))}]" if start.isdigit() else ""


def render_summary(markdown: str | None) -> str | None:
    """Summary or note markdown with citations as ``[m:ss]`` and card markup removed.

    To-do checkboxes and everything else are left exactly as Plaud wrote them.
    """
    if markdown is None:
        return None
    text = _SOURCE_LINK.sub(_citation, markdown)
    text = _CARD_LINE.sub("", text)
    return _CARD_LINK.sub("", text)


def notes_payload(notes: list[RecordingNote], *, raw: bool = False) -> list[dict[str, str | None]]:
    """A recording's notes as ``{note_id, name, content}`` dicts for MCP and CLI output.

    A note that failed to download also carries ``error``.
    """
    payload: list[dict[str, str | None]] = []
    for n in notes:
        entry = {
            "note_id": n.note_id,
            "name": n.name,
            "content": n.content if raw else render_summary(n.content),
        }
        if n.error:
            entry["error"] = n.error
        payload.append(entry)
    return payload
