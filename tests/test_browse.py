"""Tests for the shared recording list query (#222).

Covers:
- query_recordings: one list request per call, after/limit slicing, has_more,
  limit=None, trash listing, folder filters refused on the trash
- browse_recordings MCP handler: response shape, next_after cursor, the
  folder="" unfiled sentinel, argument validation
- CLI list and trash --list: same query, trash filters
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from plaud_tools.core.models import Recording
from plaud_tools.core.query import query_recordings


def make_rec(
    id: str,
    filename: str = "Meeting",
    start_time: int = 1_000,
    folder_id: str | None = None,
) -> Recording:
    return Recording(id=id, filename=filename, start_time=start_time, folder_id=folder_id)


class LibraryClient:
    """Serves a fixed library and trash; counts list requests."""

    def __init__(self, library: list[Recording], trash: list[Recording] | None = None):
        self.library = library
        self.trash = trash or []
        self.requests: list[str] = []

    def list_recordings(self, query: Any = None) -> list[Recording]:
        assert query is None, "the shared query always fetches the whole list"
        self.requests.append("recordings")
        return list(self.library)

    def list_trash(self) -> list[Recording]:
        self.requests.append("trash")
        return list(self.trash)


def numbered(count: int, filename: str = "Meeting") -> list[Recording]:
    # Newest first from mid-October 2026: r0 has the latest start_time.
    return [
        make_rec(f"r{i}", filename=filename, start_time=1_792_000_000_000 - i * 60_000) for i in range(count)
    ]


# ---------------------------------------------------------------------------
# query_recordings
# ---------------------------------------------------------------------------


class TestQueryRecordings:
    def test_filtered_browse_is_one_list_request(self):
        client = LibraryClient(numbered(900))
        page, has_more = query_recordings(client, query="meeting", since_ms=0, after=500, limit=5)
        assert [r.id for r in page] == ["r500", "r501", "r502", "r503", "r504"]
        assert has_more is True
        assert client.requests == ["recordings"]

    def test_has_more_false_on_the_exact_last_page(self):
        client = LibraryClient(numbered(6))
        page, has_more = query_recordings(client, after=3, limit=3)
        assert [r.id for r in page] == ["r3", "r4", "r5"]
        assert has_more is False

    def test_limit_none_returns_every_match(self):
        library = [make_rec(f"r{i}", folder_id="tag1" if i % 3 == 0 else None) for i in range(650)]
        page, has_more = query_recordings(LibraryClient(library), folder_id="tag1")
        assert len(page) == 217
        assert has_more is False

    def test_trash_lists_the_trash_with_filters(self):
        trash = [make_rec("old", filename="Old call", start_time=1), make_rec("new", start_time=5)]
        client = LibraryClient(numbered(3), trash=trash)
        page, _ = query_recordings(client, trash=True, since_ms=2)
        assert [r.id for r in page] == ["new"]
        assert client.requests == ["trash"]

    @pytest.mark.parametrize("folder", [{"folder_id": "tag1"}, {"unfiled": True}])
    def test_trash_refuses_folder_filters(self, folder):
        client = LibraryClient([])
        with pytest.raises(ValueError, match="no folder"):
            query_recordings(client, trash=True, **folder)
        assert client.requests == []


# ---------------------------------------------------------------------------
# browse_recordings MCP handler
# ---------------------------------------------------------------------------


def browse(client: Any, **kwargs: Any) -> tuple[dict[str, Any], dict[str, Any]]:
    from plaud_tools.mcp_pt.mcp import build_handlers

    result = build_handlers(lambda: client)["browse_recordings"](**kwargs)
    return result, json.loads(result["content"][0]["text"])


class TestBrowseRecordingsMcpHandler:
    def test_pages_with_next_after_until_exhausted(self):
        client = LibraryClient(numbered(5))
        _, page1 = browse(client, limit=2)
        _, page3 = browse(client, limit=2, after=4)
        assert [item["id"] for item in page1["items"]] == ["r0", "r1"]
        assert page1["next_after"] == 2
        assert [item["id"] for item in page3["items"]] == ["r4"]
        assert page3["next_after"] is None

    def test_folder_empty_string_returns_only_unfiled_recordings(self):
        client = LibraryClient([make_rec("filed", folder_id="f1"), make_rec("unfiled")])
        _, payload = browse(client, limit=10, folder="")
        assert [item["id"] for item in payload["items"]] == ["unfiled"]

    def test_trash_with_folder_is_a_validation_error(self):
        result, payload = browse(LibraryClient([]), trash=True, folder="f1")
        assert result["isError"] is True
        assert payload["error_code"] == "validation"

    @pytest.mark.parametrize("kwargs", [{"limit": 0}, {"limit": -5}, {"limit": 10, "after": -1}])
    def test_bad_paging_arguments_never_reach_plaud(self, kwargs):
        client = LibraryClient([])
        result, payload = browse(client, **kwargs)
        assert result["isError"] is True
        assert payload["error_code"] == "validation"
        assert client.requests == []


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


class TestCliListing:
    def test_list_and_search_share_the_query(self):
        from plaud_tools.cli.cli import run_cli

        library = [make_rec("a", "Project X", start_time=2), make_rec("b", "Lunch", start_time=1)]
        client = LibraryClient(library)
        assert [item["id"] for item in json.loads(run_cli(["search", "project"], client))] == ["a"]
        assert [item["id"] for item in json.loads(run_cli(["list", "--limit", "1"], client))] == ["a"]
        assert client.requests == ["recordings", "recordings"]

    def test_trash_list_takes_since_and_limit(self):
        from plaud_tools.cli.cli import run_cli

        trash = [*numbered(10), make_rec("august", start_time=1_786_000_000_000)]
        output = run_cli(
            ["trash", "--list", "--since", "2026-09-01", "--limit", "5"], LibraryClient([], trash)
        )
        assert [item["id"] for item in json.loads(output)] == ["r0", "r1", "r2", "r3", "r4"]
        output = run_cli(["trash", "--list", "--all", "--since", "2026-09-01"], LibraryClient([], trash))
        assert len(json.loads(output)) == 10
