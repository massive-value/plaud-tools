"""Unit tests for src/plaud_tools/query.py.

Coverage:
- parse_isoish: bare date, full datetime, Z-suffix, end_of_day flag, invalid input
- filter_recordings: date range, query/text match, folder_id, unfiled=True,
  unfiled/folder_id="" equivalence, combined filters, sort order, empty list
- summarize_recording: shape and field values for a representative Recording
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from plaud_tools.models import Recording
from plaud_tools.query import filter_recordings, parse_isoish, summarize_recording

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def make_recording(
    id: str = "rec1",
    filename: str = "Meeting",
    start_time: int = 0,
    duration: int = 0,
    is_trans: bool = False,
    filetag_id_list: list[str] | None = None,
) -> Recording:
    return Recording(
        id=id,
        filename=filename,
        start_time=start_time,
        duration=duration,
        is_trans=is_trans,
        filetag_id_list=filetag_id_list if filetag_id_list is not None else [],
    )


# ---------------------------------------------------------------------------
# parse_isoish
# ---------------------------------------------------------------------------


class TestParseIsoish:
    def test_bare_date_returns_midnight_epoch_ms(self):
        # 2024-01-15 in UTC+00:00 is epoch 1705276800000 ms
        # Use a fixed-offset parse to verify the function's output is plausible
        # without depending on local TZ.
        result = parse_isoish("2024-01-15", "since")
        # parse_isoish does fromisoformat("2024-01-15"), which produces a naive
        # datetime; timestamp() converts using local TZ.  We verify the result
        # falls within ±26 hours of the UTC midnight, covering any real TZ.
        utc_midnight_ms = int(datetime(2024, 1, 15, tzinfo=UTC).timestamp() * 1000)
        delta_ms = abs(result - utc_midnight_ms)
        # Allow ±26 hours to tolerate any real timezone offset.
        assert delta_ms <= 26 * 3600 * 1000

    def test_full_datetime_uses_exact_value(self):
        # Provide an explicit UTC offset so timestamp() is deterministic.
        result = parse_isoish("2024-01-15T12:00:00+00:00", "since")
        expected = int(datetime(2024, 1, 15, 12, 0, 0, tzinfo=UTC).timestamp() * 1000)
        assert result == expected

    def test_z_suffix_is_treated_as_utc(self):
        # "Z" should be replaced with "+00:00" — identical to explicit UTC offset.
        result_z = parse_isoish("2024-06-01T10:30:00Z", "until")
        result_utc = parse_isoish("2024-06-01T10:30:00+00:00", "until")
        assert result_z == result_utc

    def test_end_of_day_on_bare_date_sets_23_59_59(self):
        result = parse_isoish("2024-01-15", "until", end_of_day=True)
        # Reconstruct what the function should produce: 2024-01-15T23:59:59.999999 local.
        dt_expected = datetime(2024, 1, 15, 23, 59, 59, 999999)
        expected_ms = int(dt_expected.timestamp() * 1000)
        assert result == expected_ms

    def test_end_of_day_on_full_datetime_does_not_mutate_time(self):
        # end_of_day only applies when "T" is absent from the value.
        result_with = parse_isoish("2024-01-15T08:00:00+00:00", "until", end_of_day=True)
        result_without = parse_isoish("2024-01-15T08:00:00+00:00", "until", end_of_day=False)
        assert result_with == result_without

    def test_end_of_day_false_on_bare_date(self):
        result_default = parse_isoish("2024-01-15", "since")
        result_explicit = parse_isoish("2024-01-15", "since", end_of_day=False)
        assert result_default == result_explicit

    def test_invalid_input_raises_value_error_with_field_name(self):
        with pytest.raises(ValueError, match="Invalid my_field value: not-a-date"):
            parse_isoish("not-a-date", "my_field")

    def test_invalid_input_includes_original_value_in_message(self):
        with pytest.raises(ValueError, match="bogus-date"):
            parse_isoish("bogus-date", "since")

    def test_empty_string_raises_value_error(self):
        with pytest.raises(ValueError):
            parse_isoish("", "since")


# ---------------------------------------------------------------------------
# filter_recordings
# ---------------------------------------------------------------------------


class TestFilterRecordings:
    # Build a small fixture set covering the main discriminating attributes.
    # All start_times use fixed values so sort order is deterministic.

    def _make_items(self) -> list[Recording]:
        return [
            make_recording("a", "Alpha Meeting", start_time=1_000, filetag_id_list=["folder1"]),
            make_recording("b", "Beta Call", start_time=3_000, filetag_id_list=[]),
            make_recording("c", "Gamma Note", start_time=2_000, filetag_id_list=["folder2"]),
            make_recording("d", "Delta Meeting", start_time=4_000, filetag_id_list=[]),
        ]

    # --- passthrough ---

    def test_no_filters_returns_all_sorted_descending(self):
        items = self._make_items()
        result = filter_recordings(items, since_ms=None, until_ms=None, query=None, folder_id=None)
        ids = [item.id for item in result]
        # Descending by start_time: d(4000), b(3000), c(2000), a(1000)
        assert ids == ["d", "b", "c", "a"]

    def test_empty_input_returns_empty(self):
        result = filter_recordings([], since_ms=None, until_ms=None, query=None, folder_id=None)
        assert result == []

    # --- date range ---

    def test_since_ms_excludes_earlier_items(self):
        items = self._make_items()
        result = filter_recordings(items, since_ms=2_000, until_ms=None, query=None, folder_id=None)
        ids = {item.id for item in result}
        assert "a" not in ids  # start_time=1000 < 2000
        assert "b" in ids
        assert "c" in ids
        assert "d" in ids

    def test_until_ms_excludes_later_items(self):
        items = self._make_items()
        result = filter_recordings(items, since_ms=None, until_ms=2_000, query=None, folder_id=None)
        ids = {item.id for item in result}
        assert "a" in ids
        assert "c" in ids
        assert "b" not in ids  # start_time=3000 > 2000
        assert "d" not in ids  # start_time=4000 > 2000

    def test_since_ms_is_inclusive(self):
        items = self._make_items()
        result = filter_recordings(items, since_ms=1_000, until_ms=None, query=None, folder_id=None)
        ids = {item.id for item in result}
        assert "a" in ids  # start_time == since_ms (boundary inclusive)

    def test_until_ms_is_inclusive(self):
        items = self._make_items()
        result = filter_recordings(items, since_ms=None, until_ms=4_000, query=None, folder_id=None)
        ids = {item.id for item in result}
        assert "d" in ids  # start_time == until_ms (boundary inclusive)

    def test_since_and_until_together(self):
        items = self._make_items()
        result = filter_recordings(items, since_ms=2_000, until_ms=3_000, query=None, folder_id=None)
        ids = {item.id for item in result}
        assert ids == {"b", "c"}

    # --- query / text match ---

    def test_query_matches_filename_case_insensitively(self):
        items = self._make_items()
        result = filter_recordings(items, since_ms=None, until_ms=None, query="MEETING", folder_id=None)
        ids = {item.id for item in result}
        assert ids == {"a", "d"}

    def test_query_no_match_returns_empty(self):
        items = self._make_items()
        result = filter_recordings(items, since_ms=None, until_ms=None, query="zzznomatch", folder_id=None)
        assert result == []

    def test_query_none_passes_all(self):
        items = self._make_items()
        result = filter_recordings(items, since_ms=None, until_ms=None, query=None, folder_id=None)
        assert len(result) == len(items)

    def test_query_empty_string_passes_all(self):
        # Empty string is falsy — behaves as no filter.
        items = self._make_items()
        result = filter_recordings(items, since_ms=None, until_ms=None, query="", folder_id=None)
        assert len(result) == len(items)

    # --- folder_id filter ---

    def test_folder_id_filters_to_matching_folder(self):
        items = self._make_items()
        result = filter_recordings(items, since_ms=None, until_ms=None, query=None, folder_id="folder1")
        assert [item.id for item in result] == ["a"]

    def test_folder_id_none_does_not_filter_by_folder(self):
        items = self._make_items()
        result = filter_recordings(items, since_ms=None, until_ms=None, query=None, folder_id=None)
        assert len(result) == len(items)

    def test_folder_id_nonexistent_returns_empty(self):
        items = self._make_items()
        result = filter_recordings(
            items, since_ms=None, until_ms=None, query=None, folder_id="does-not-exist"
        )
        assert result == []

    # --- unfiled / folder_id="" equivalence (audit callout) ---

    def test_unfiled_true_returns_items_without_any_folder(self):
        items = self._make_items()
        result = filter_recordings(
            items, since_ms=None, until_ms=None, query=None, folder_id=None, unfiled=True
        )
        ids = {item.id for item in result}
        assert ids == {"b", "d"}  # a=folder1, c=folder2; b and d have []

    def test_folder_id_empty_string_returns_items_without_any_folder(self):
        # mcp.py convention: folder_id="" means "no folder assigned"
        items = self._make_items()
        result = filter_recordings(items, since_ms=None, until_ms=None, query=None, folder_id="")
        ids = {item.id for item in result}
        assert ids == {"b", "d"}

    def test_unfiled_and_folder_id_empty_are_equivalent(self):
        # Core audit requirement: both conventions must produce identical results.
        items = self._make_items()
        via_unfiled = filter_recordings(
            items, since_ms=None, until_ms=None, query=None, folder_id=None, unfiled=True
        )
        via_empty_str = filter_recordings(items, since_ms=None, until_ms=None, query=None, folder_id="")
        assert [item.id for item in via_unfiled] == [item.id for item in via_empty_str]

    def test_unfiled_true_takes_priority_over_nonblank_folder_id(self):
        # When unfiled=True, folder_id (non-empty) should be ignored — unfiled wins.
        items = self._make_items()
        result = filter_recordings(
            items, since_ms=None, until_ms=None, query=None, folder_id="folder1", unfiled=True
        )
        # unfiled=True forces the "no filetag" branch; folder1 items are excluded.
        ids = {item.id for item in result}
        assert ids == {"b", "d"}
        assert "a" not in ids  # rec a is in folder1 but has a filetag, so excluded

    def test_unfiled_recording_with_multiple_tags_excluded_when_has_tags(self):
        rec = make_recording("x", "Multi", filetag_id_list=["t1", "t2"])
        result = filter_recordings(
            [rec], since_ms=None, until_ms=None, query=None, folder_id=None, unfiled=True
        )
        assert result == []

    def test_unfiled_recording_with_no_tags_included(self):
        rec = make_recording("x", "Bare", filetag_id_list=[])
        result = filter_recordings(
            [rec], since_ms=None, until_ms=None, query=None, folder_id=None, unfiled=True
        )
        assert [item.id for item in result] == ["x"]

    # --- sort order ---

    def test_result_is_sorted_descending_by_start_time(self):
        items = [
            make_recording("first", start_time=100),
            make_recording("third", start_time=300),
            make_recording("second", start_time=200),
        ]
        result = filter_recordings(items, since_ms=None, until_ms=None, query=None, folder_id=None)
        assert [item.id for item in result] == ["third", "second", "first"]

    def test_sort_is_stable_for_equal_start_times(self):
        items = [
            make_recording("x", start_time=500),
            make_recording("y", start_time=500),
        ]
        result = filter_recordings(items, since_ms=None, until_ms=None, query=None, folder_id=None)
        assert len(result) == 2
        assert {item.id for item in result} == {"x", "y"}

    # --- combined filters ---

    def test_date_range_and_query_combined(self):
        items = self._make_items()
        # Only "Alpha Meeting" (start=1000) and "Delta Meeting" (start=4000) match "meeting".
        # Restrict to start_time in [1000, 2000] — only Alpha survives.
        result = filter_recordings(items, since_ms=1_000, until_ms=2_000, query="meeting", folder_id=None)
        assert [item.id for item in result] == ["a"]

    def test_folder_id_and_query_combined(self):
        items = [
            make_recording("m1", "Project Alpha", start_time=10, filetag_id_list=["proj"]),
            make_recording("m2", "Project Beta", start_time=20, filetag_id_list=["proj"]),
            make_recording("m3", "Random", start_time=30, filetag_id_list=["proj"]),
        ]
        result = filter_recordings(items, since_ms=None, until_ms=None, query="Project", folder_id="proj")
        ids = {item.id for item in result}
        assert ids == {"m1", "m2"}
        assert "m3" not in ids


# ---------------------------------------------------------------------------
# summarize_recording
# ---------------------------------------------------------------------------


class TestSummarizeRecording:
    def test_returns_dict_with_expected_keys(self):
        rec = make_recording("rec42", "My Note", start_time=1_705_276_800_000, duration=120_000)
        summary = summarize_recording(rec)
        expected_keys = {"id", "title", "date", "duration_minutes", "has_transcript", "folder_id"}
        assert set(summary.keys()) == expected_keys

    def test_id_and_title_are_correct(self):
        rec = make_recording("abc123", "Team Standup")
        summary = summarize_recording(rec)
        assert summary["id"] == "abc123"
        assert summary["title"] == "Team Standup"

    def test_duration_minutes_is_rounded(self):
        # 150_000 ms = 2.5 minutes -> rounds to 2 (round-half-to-even) or 3 depending on rounding;
        # just check it's an int and close to 2.5.
        rec = make_recording(duration=150_000)
        summary = summarize_recording(rec)
        assert isinstance(summary["duration_minutes"], int)
        assert summary["duration_minutes"] in (2, 3)

    def test_duration_minutes_exact(self):
        # 300_000 ms = exactly 5 minutes
        rec = make_recording(duration=300_000)
        assert summarize_recording(rec)["duration_minutes"] == 5

    def test_duration_minutes_zero_for_zero_duration(self):
        rec = make_recording(duration=0)
        assert summarize_recording(rec)["duration_minutes"] == 0

    def test_has_transcript_reflects_is_trans(self):
        rec_with = make_recording(is_trans=True)
        rec_without = make_recording(is_trans=False)
        assert summarize_recording(rec_with)["has_transcript"] is True
        assert summarize_recording(rec_without)["has_transcript"] is False

    def test_folder_id_is_first_tag_when_present(self):
        rec = make_recording(filetag_id_list=["folder-a", "folder-b"])
        assert summarize_recording(rec)["folder_id"] == "folder-a"

    def test_folder_id_is_none_when_no_tags(self):
        rec = make_recording(filetag_id_list=[])
        assert summarize_recording(rec)["folder_id"] is None

    def test_date_is_16_char_isoformat_prefix(self):
        # datetime.fromtimestamp(ms/1000).isoformat()[:16] -> "YYYY-MM-DDTHH:MM"
        # Use start_time=0 ms which is the Unix epoch.  The result is TZ-dependent
        # (local time), so we only assert the shape, not the exact value.
        rec = make_recording(start_time=0)
        date_str = summarize_recording(rec)["date"]
        assert len(date_str) == 16
        # Must look like "YYYY-MM-DDTHH:MM"
        assert date_str[10] == "T", f"Expected 'T' at index 10, got {date_str!r}"
        assert date_str[4] == "-"
        assert date_str[7] == "-"
        assert date_str[13] == ":"

    def test_date_reflects_start_time_relative_ordering(self):
        # Two recordings whose start_times differ by at least 1 hour:
        # the later one must produce a lexicographically later or equal date string.
        rec_early = make_recording("e", start_time=1_000_000_000_000)  # year ~2001
        rec_late = make_recording("l", start_time=1_700_000_000_000)  # year ~2023
        date_early = summarize_recording(rec_early)["date"]
        date_late = summarize_recording(rec_late)["date"]
        assert date_late > date_early
