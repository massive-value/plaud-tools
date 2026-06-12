"""Tests for incremental filtered browse (D1).

Covers:
- collect_filtered_paged: multi-page matches, exact-boundary has_more, empty results,
  page-fetch count bounded when limit is small
- browse_recordings MCP handler: response shape, pagination cursor (next_after),
  and page-fetch count with multi-page upstream fixtures
- CLI list/search incremental paging: filtered path calls upstream in pages
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import MagicMock

from plaud_tools.models import Recording
from plaud_tools.query import BROWSE_PAGE_SIZE, collect_filtered_paged

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def make_rec(
    id: str,
    filename: str = "Meeting",
    start_time: int = 1_000,
    filetag_id_list: list[str] | None = None,
) -> Recording:
    return Recording(
        id=id,
        filename=filename,
        start_time=start_time,
        filetag_id_list=filetag_id_list if filetag_id_list is not None else [],
    )


def page_fetcher_from_pages(pages: list[list[Recording]]) -> tuple[Any, list[int]]:
    """Return (fetch_page callable, call_log) where call_log records each skip seen."""
    calls: list[int] = []
    page_size_seen = BROWSE_PAGE_SIZE

    def fetch_page(skip: int, page_size: int) -> list[Recording]:
        calls.append(skip)
        # Derive which page index to return from skip.
        idx = skip // page_size_seen
        if idx < len(pages):
            return pages[idx]
        return []

    return fetch_page, calls


# ---------------------------------------------------------------------------
# collect_filtered_paged — core unit tests
# ---------------------------------------------------------------------------


class TestCollectFilteredPaged:
    def test_empty_upstream_returns_empty_page_no_more(self):
        fetch, calls = page_fetcher_from_pages([[]])
        page, has_more = collect_filtered_paged(
            fetch,
            BROWSE_PAGE_SIZE,
            since_ms=None,
            until_ms=None,
            query=None,
            folder_id=None,
            after=0,
            limit=10,
        )
        assert page == []
        assert has_more is False
        assert len(calls) == 1

    def test_single_page_all_match(self):
        recs = [make_rec(f"r{i}", start_time=i * 1000) for i in range(5)]
        fetch, calls = page_fetcher_from_pages([recs])
        page, has_more = collect_filtered_paged(
            fetch,
            BROWSE_PAGE_SIZE,
            since_ms=None,
            until_ms=None,
            query=None,
            folder_id=None,
            after=0,
            limit=10,
        )
        assert len(page) == 5
        assert has_more is False
        assert len(calls) == 1

    def test_matches_span_two_upstream_pages(self):
        # Build two full pages; every record matches the query "meeting".
        page1 = [
            make_rec(f"p1r{i}", filename="Meeting A", start_time=i * 1000)
            for i in range(BROWSE_PAGE_SIZE)
        ]
        page2 = [
            make_rec(f"p2r{i}", filename="Meeting B", start_time=(BROWSE_PAGE_SIZE + i) * 1000)
            for i in range(10)
        ]
        fetch, calls = page_fetcher_from_pages([page1, page2])
        page, has_more = collect_filtered_paged(
            fetch,
            BROWSE_PAGE_SIZE,
            since_ms=None,
            until_ms=None,
            query="meeting",
            folder_id=None,
            after=0,
            limit=5,
        )
        # limit=5 with after=0 means we need 6 matches to confirm has_more.
        # Page 1 gives 200 matches — all from one page fetch.
        assert len(page) == 5
        assert has_more is True
        # Should stop after the first upstream page (200 matches >> 6 needed).
        assert len(calls) == 1

    def test_sparse_matches_force_multiple_upstream_pages(self):
        # Only every BROWSE_PAGE_SIZE-th record matches "special".
        # Page 1: 200 records, only index 0 matches.
        # Page 2: 200 records, only index 0 matches.
        # Page 3: 200 records, only index 0 matches (last page = full).
        # Page 4: 3 records, only index 0 matches.
        def make_page(offset: int, count: int) -> list[Recording]:
            recs = []
            for i in range(count):
                name = "special" if i == 0 else "other"
                recs.append(make_rec(f"r{offset + i}", filename=name, start_time=(offset + i) * 1000))
            return recs

        pages = [
            make_page(0, BROWSE_PAGE_SIZE),
            make_page(BROWSE_PAGE_SIZE, BROWSE_PAGE_SIZE),
            make_page(BROWSE_PAGE_SIZE * 2, BROWSE_PAGE_SIZE),
            make_page(BROWSE_PAGE_SIZE * 3, 3),
        ]
        fetch, calls = page_fetcher_from_pages(pages)
        page, has_more = collect_filtered_paged(
            fetch,
            BROWSE_PAGE_SIZE,
            since_ms=None,
            until_ms=None,
            query="special",
            folder_id=None,
            after=0,
            limit=3,
        )
        # need = 0 + 3 + 1 = 4 matches; 4 pages each contribute 1 match.
        assert len(page) == 3
        assert has_more is True
        # Must have fetched all 4 pages to accumulate 4 matches.
        assert len(calls) == 4

    def test_exact_boundary_has_more_false_when_matches_equal_limit(self):
        # Exactly `limit` matches total — has_more must be False.
        limit = 5
        recs = [make_rec(f"r{i}", filename="keep", start_time=i * 1000) for i in range(limit)]
        fetch, calls = page_fetcher_from_pages([recs])
        page, has_more = collect_filtered_paged(
            fetch,
            BROWSE_PAGE_SIZE,
            since_ms=None,
            until_ms=None,
            query="keep",
            folder_id=None,
            after=0,
            limit=limit,
        )
        assert len(page) == limit
        assert has_more is False

    def test_exact_boundary_has_more_true_when_matches_equal_limit_plus_one(self):
        # Exactly `limit + 1` matches total — has_more must be True.
        limit = 5
        recs = [make_rec(f"r{i}", filename="keep", start_time=i * 1000) for i in range(limit + 1)]
        fetch, calls = page_fetcher_from_pages([recs])
        page, has_more = collect_filtered_paged(
            fetch,
            BROWSE_PAGE_SIZE,
            since_ms=None,
            until_ms=None,
            query="keep",
            folder_id=None,
            after=0,
            limit=limit,
        )
        assert len(page) == limit
        assert has_more is True

    def test_after_cursor_skips_already_seen_matches(self):
        # 10 matching records; after=5, limit=3 → page is items[5:8], has_more because item 8..9 exist.
        recs = [make_rec(f"r{i}", filename="match", start_time=i * 1000) for i in range(10)]
        fetch, calls = page_fetcher_from_pages([recs])
        page, has_more = collect_filtered_paged(
            fetch,
            BROWSE_PAGE_SIZE,
            since_ms=None,
            until_ms=None,
            query="match",
            folder_id=None,
            after=5,
            limit=3,
        )
        assert len(page) == 3
        # has_more: need = 5+3+1=9 items; we have 10 total, so True.
        assert has_more is True

    def test_after_cursor_at_last_page(self):
        # 7 matching records; after=5, limit=3 → only 2 remain → page=[2 items], has_more=False.
        recs = [make_rec(f"r{i}", filename="match", start_time=i * 1000) for i in range(7)]
        fetch, calls = page_fetcher_from_pages([recs])
        page, has_more = collect_filtered_paged(
            fetch,
            BROWSE_PAGE_SIZE,
            since_ms=None,
            until_ms=None,
            query="match",
            folder_id=None,
            after=5,
            limit=3,
        )
        assert len(page) == 2
        assert has_more is False

    def test_fetch_count_bounded_by_need_not_total_pages(self):
        # 5 full pages of 200 matching records each; limit=2, after=0.
        # need = 3 → after first page (200 matches) we already have enough → 1 fetch.
        pages = [
            [make_rec(f"p{p}r{i}", filename="yes", start_time=i * 1000) for i in range(BROWSE_PAGE_SIZE)]
            for p in range(5)
        ]
        fetch, calls = page_fetcher_from_pages(pages)
        page, has_more = collect_filtered_paged(
            fetch,
            BROWSE_PAGE_SIZE,
            since_ms=None,
            until_ms=None,
            query="yes",
            folder_id=None,
            after=0,
            limit=2,
        )
        assert len(page) == 2
        assert has_more is True
        # Must NOT fetch all 5 pages.
        assert len(calls) == 1

    def test_upstream_exhaustion_stops_loop(self):
        # Upstream returns a short final page — loop must stop even if we don't have `need` matches.
        recs = [make_rec(f"r{i}", filename="match", start_time=i * 1000) for i in range(3)]
        fetch, calls = page_fetcher_from_pages([recs])
        page, has_more = collect_filtered_paged(
            fetch,
            BROWSE_PAGE_SIZE,
            since_ms=None,
            until_ms=None,
            query="match",
            folder_id=None,
            after=0,
            limit=10,
        )
        # Only 3 matches available despite limit=10.
        assert len(page) == 3
        assert has_more is False
        assert len(calls) == 1

    def test_no_matches_in_any_page(self):
        # Each page has records but none match the query.
        pages = [
            [make_rec(f"r{i}", filename="other", start_time=i * 1000) for i in range(BROWSE_PAGE_SIZE)],
            [make_rec(f"s{i}", filename="other", start_time=i * 1000) for i in range(5)],
        ]
        fetch, calls = page_fetcher_from_pages(pages)
        page, has_more = collect_filtered_paged(
            fetch,
            BROWSE_PAGE_SIZE,
            since_ms=None,
            until_ms=None,
            query="nomatch",
            folder_id=None,
            after=0,
            limit=10,
        )
        assert page == []
        assert has_more is False
        # Must have fetched both pages (first was full, second was short → stopped).
        assert len(calls) == 2


# ---------------------------------------------------------------------------
# browse_recordings MCP handler — integration with collect_filtered_paged
# ---------------------------------------------------------------------------


class TestBrowseRecordingsMcpHandler:
    """Verify the browse_recordings handler shape and paging contract."""

    def _make_client(self, pages: list[list[Recording]]) -> tuple[Any, list[int]]:
        """Return (client mock, call_log) recording list_recordings call skips."""
        calls: list[int] = []

        def list_recordings_stub(query=None):  # type: ignore[override]
            if query is None:
                # Unfiltered path — not under test here.
                return []
            skip = query.skip or 0
            calls.append(skip)
            idx = skip // BROWSE_PAGE_SIZE
            if idx < len(pages):
                return pages[idx]
            return []

        mock_client = MagicMock()
        mock_client.list_recordings.side_effect = list_recordings_stub
        return mock_client, calls

    def _build_handlers(self, mock_client: Any) -> Any:
        from plaud_tools.mcp import build_handlers

        return build_handlers(lambda: mock_client)

    def test_response_shape_has_items_and_next_after(self):
        recs = [make_rec(f"r{i}", filename="Meeting", start_time=i * 1000) for i in range(5)]
        client, _ = self._make_client([recs])
        handlers = self._build_handlers(client)
        result = handlers["browse_recordings"](limit=3, query="meeting")
        payload = json.loads(result["content"][0]["text"])
        assert "items" in payload
        assert "next_after" in payload

    def test_has_more_false_when_fewer_than_limit_matches(self):
        recs = [make_rec(f"r{i}", filename="Meeting", start_time=i * 1000) for i in range(2)]
        client, _ = self._make_client([recs])
        handlers = self._build_handlers(client)
        result = handlers["browse_recordings"](limit=5, query="meeting")
        payload = json.loads(result["content"][0]["text"])
        assert payload["next_after"] is None
        assert len(payload["items"]) == 2

    def test_has_more_true_resolves_next_after_cursor(self):
        # 6 matches, limit=5 → next_after = 5.
        recs = [make_rec(f"r{i}", filename="Meeting", start_time=i * 1000) for i in range(6)]
        client, _ = self._make_client([recs])
        handlers = self._build_handlers(client)
        result = handlers["browse_recordings"](limit=5, query="meeting")
        payload = json.loads(result["content"][0]["text"])
        assert payload["next_after"] == 5
        assert len(payload["items"]) == 5

    def test_second_page_via_after_returns_remainder(self):
        # 6 matches; first call gets [0:5], second call with after=5 gets [5:6].
        recs = [make_rec(f"r{i}", filename="Meeting", start_time=i * 1000) for i in range(6)]
        client, _ = self._make_client([recs])
        handlers = self._build_handlers(client)
        result = handlers["browse_recordings"](limit=5, after=5, query="meeting")
        payload = json.loads(result["content"][0]["text"])
        assert len(payload["items"]) == 1
        assert payload["next_after"] is None

    def test_multi_upstream_pages_fetched_for_sparse_matches(self):
        # 3 full upstream pages, 1 match per page.
        def make_page(offset: int) -> list[Recording]:
            recs = []
            for i in range(BROWSE_PAGE_SIZE):
                name = "match" if i == 0 else "skip"
                recs.append(make_rec(f"r{offset + i}", filename=name, start_time=(offset + i) * 1000))
            return recs

        pages = [make_page(0), make_page(BROWSE_PAGE_SIZE), make_page(BROWSE_PAGE_SIZE * 2)]
        client, call_log = self._make_client(pages)
        handlers = self._build_handlers(client)
        result = handlers["browse_recordings"](limit=2, query="match")
        payload = json.loads(result["content"][0]["text"])
        # need = 0+2+1 = 3; 3 pages give 3 matches → all 3 pages fetched.
        assert len(payload["items"]) == 2
        assert payload["next_after"] == 2
        assert len(call_log) == 3

    def test_fetch_count_bounded_when_first_page_saturates_need(self):
        # 200 matching records in page 1; limit=2 → need=3, satisfied after 1 fetch.
        page1 = [
            make_rec(f"r{i}", filename="meeting", start_time=i * 1000)
            for i in range(BROWSE_PAGE_SIZE)
        ]
        page2 = [
            make_rec(f"s{i}", filename="meeting", start_time=(BROWSE_PAGE_SIZE + i) * 1000)
            for i in range(5)
        ]
        client, call_log = self._make_client([page1, page2])
        handlers = self._build_handlers(client)
        result = handlers["browse_recordings"](limit=2, query="meeting")
        payload = json.loads(result["content"][0]["text"])
        assert len(payload["items"]) == 2
        assert payload["next_after"] == 2
        # Must NOT fetch the second page.
        assert len(call_log) == 1

    def test_empty_upstream_returns_empty_items_no_next_after(self):
        client, _ = self._make_client([[]])
        handlers = self._build_handlers(client)
        result = handlers["browse_recordings"](limit=10, query="anything")
        payload = json.loads(result["content"][0]["text"])
        assert payload["items"] == []
        assert payload["next_after"] is None

    def test_no_filters_uses_direct_upstream_query(self):
        # Without filters the handler should pass skip/limit directly to list_recordings,
        # not go through collect_filtered_paged.
        direct_recs = [make_rec(f"r{i}", start_time=i * 1000) for i in range(3)]
        mock_client = MagicMock()
        mock_client.list_recordings.return_value = direct_recs
        handlers = self._build_handlers(mock_client)
        result = handlers["browse_recordings"](limit=3)
        payload = json.loads(result["content"][0]["text"])
        assert len(payload["items"]) == 3
        # Exactly one call with a PlaudRecordingQuery.
        assert mock_client.list_recordings.call_count == 1
        call_query = mock_client.list_recordings.call_args[0][0]
        assert call_query.limit == 3
        assert call_query.is_trash == 0


# ---------------------------------------------------------------------------
# CLI list/search — filtered path uses incremental paging
# ---------------------------------------------------------------------------


class TestCliIncrementalBrowse:
    """Verify that the CLI list/search filtered path pages upstream incrementally."""

    def _make_client(self, pages: list[list[Recording]]) -> tuple[Any, list[int]]:
        calls: list[int] = []

        def list_recordings_stub(query=None):  # type: ignore[override]
            if query is None:
                return []
            skip = query.skip or 0
            calls.append(skip)
            idx = skip // BROWSE_PAGE_SIZE
            if idx < len(pages):
                return pages[idx]
            return []

        mock_client = MagicMock()
        mock_client.list_recordings.side_effect = list_recordings_stub
        return mock_client, calls

    def test_list_with_query_filter_uses_paging(self):
        from plaud_tools.cli import run_cli

        recs = [make_rec(f"r{i}", filename="Meeting Note", start_time=i * 1000) for i in range(5)]
        client, call_log = self._make_client([recs])
        output = run_cli(["list", "--query", "meeting", "--limit", "3"], client=client)
        items = json.loads(output)
        assert len(items) == 3
        # Upstream was called at least once with a skip parameter.
        assert len(call_log) >= 1

    def test_search_uses_paging(self):
        from plaud_tools.cli import run_cli

        recs = [make_rec(f"r{i}", filename="Project X", start_time=i * 1000) for i in range(4)]
        client, call_log = self._make_client([recs])
        output = run_cli(["search", "project", "--limit", "2"], client=client)
        items = json.loads(output)
        assert len(items) == 2
        assert len(call_log) >= 1

    def test_list_without_filters_does_not_use_paging_path(self):
        # Without filters the CLI calls list_recordings with a limit query directly.
        from plaud_tools.cli import run_cli

        direct_recs = [make_rec(f"r{i}", start_time=i * 1000) for i in range(3)]
        mock_client = MagicMock()
        mock_client.list_recordings.return_value = direct_recs
        output = run_cli(["list", "--limit", "3"], client=mock_client)
        items = json.loads(output)
        assert len(items) == 3
        # Exactly one call and it must carry limit=3 in the query object.
        assert mock_client.list_recordings.call_count == 1
        query_arg = mock_client.list_recordings.call_args[0][0]
        assert query_arg.limit == 3

    def test_list_sparse_matches_fetches_multiple_pages(self):
        from plaud_tools.cli import run_cli

        # Page 1: 200 records, 1 matches query; Page 2: 3 records, all match.
        def make_page1() -> list[Recording]:
            recs = []
            for i in range(BROWSE_PAGE_SIZE):
                name = "target" if i == 0 else "other"
                recs.append(make_rec(f"p1r{i}", filename=name, start_time=i * 1000))
            return recs

        page2 = [
            make_rec(f"p2r{i}", filename="target", start_time=(BROWSE_PAGE_SIZE + i) * 1000)
            for i in range(3)
        ]
        client, call_log = self._make_client([make_page1(), page2])
        output = run_cli(["list", "--query", "target", "--limit", "3"], client=client)
        items = json.loads(output)
        assert len(items) == 3
        # Needed 4 matches (limit+1 = 4); page1 gives 1, page2 gives 3 → 2 fetches.
        assert len(call_log) == 2
