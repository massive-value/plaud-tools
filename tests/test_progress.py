"""MCP progress notifications (#221): sent when the client gave a progress token, silent otherwise."""

from __future__ import annotations

import asyncio

from mcp.client import Client

from plaud_tools.mcp_pt import server as server_mod
from plaud_tools.mcp_pt.mcp import _json_result
from plaud_tools.mcp_pt.progress import current_progress, shifted


def _server_whose_list_folders_reports_progress(monkeypatch):
    """A real server whose list_folders handler reports two progress steps from its worker thread."""

    def list_folders():
        report = current_progress()
        assert report is not None
        report(1, 2, "half way")
        report(2, 2, "done")
        return _json_result({"folders": []})

    monkeypatch.setattr(server_mod, "build_handlers", lambda get_client: {"list_folders": list_folders})
    return server_mod._make_server()


def _call_list_folders(server, **kwargs):
    async def run():
        async with Client(server) as client:
            return await client.call_tool("list_folders", {}, **kwargs)

    return asyncio.run(run())


def test_progress_sent_when_the_client_gives_a_progress_token(monkeypatch):
    events: list[tuple] = []

    async def on_progress(progress, total, message):
        events.append((progress, total, message))

    server = _server_whose_list_folders_reports_progress(monkeypatch)
    _call_list_folders(server, progress_callback=on_progress)
    assert events == [(1, 2, "half way"), (2, 2, "done")]


def test_nothing_sent_without_a_progress_token(monkeypatch):
    server = _server_whose_list_folders_reports_progress(monkeypatch)
    result = _call_list_folders(server)  # no progress_callback, so no token on the request
    assert result.is_error is not True


def test_handlers_called_outside_call_tool_have_no_progress_callback():
    assert current_progress() is None


def test_shifted_makes_staged_waits_report_one_rising_count():
    seen: list[tuple] = []
    shifted(lambda *args: seen.append(args), 40.0)(5.0, 50.0, "summary")
    assert seen == [(45.0, 90.0, "summary")]
    assert shifted(None, 40.0) is None
