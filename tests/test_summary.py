"""Rendering Plaud summary markdown for reading (citations as timestamps, no card markup)."""

from __future__ import annotations

from plaud_tools.core.summary import format_clock, render_summary


def test_source_links_become_timestamps():
    text = (
        "Agreed [](plaud://source?type=summary&fileId=f_1&start_time=125370&end_time=131290&displayLabel=1)"
    )
    assert render_summary(text) == "Agreed [2:05]"


def test_timestamps_over_an_hour_include_hours():
    assert format_clock(3_725_000) == "1:02:05"
    assert format_clock(0) == "0:00"


def test_card_lines_are_dropped_and_todos_kept():
    cards = "[](plaud://image?type=summaryCard&id=oc_1)\n[](plaud://highlight-card?title=A&detail=B)\n"
    text = f"## Notes\n{cards}- [ ] Send\n- [x] Done\n"
    assert render_summary(text) == "## Notes\n- [ ] Send\n- [x] Done\n"


def test_missing_text_stays_missing():
    assert render_summary(None) is None
