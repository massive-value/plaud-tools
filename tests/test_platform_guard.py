"""The MCP server keeps stdlib ``platform`` off WMI (Python 3.12 ``_wmi`` stray CloseHandle)."""

from __future__ import annotations

import platform
import sys

import pytest

import plaud_tools.mcp_pt.server as server_mod
from plaud_tools.core.platform_guard import disable_wmi_queries


@pytest.mark.skipif(sys.platform != "win32", reason="_wmi exists only on Windows")
def test_platform_answers_without_wmi(monkeypatch):
    import _wmi

    queries = []
    monkeypatch.setattr(_wmi, "exec_query", lambda query: queries.append(query) or "")
    monkeypatch.setattr(platform, "_wmi_query", platform._wmi_query)  # restored after the test
    monkeypatch.setattr(platform, "_uname_cache", None)

    disable_wmi_queries()

    assert platform.system() == "Windows"
    assert platform.machine()
    assert queries == []


def test_main_disables_wmi_before_serving(monkeypatch):
    calls = []
    monkeypatch.setattr(server_mod, "disable_wmi_queries", lambda: calls.append("guard"))
    monkeypatch.setattr(server_mod, "_setup_mcp_logging", lambda: calls.append("logging"))
    monkeypatch.setattr(server_mod.asyncio, "run", lambda coro: (coro.close(), calls.append("run")))
    monkeypatch.setattr(sys, "argv", ["plaud-mcp"])

    server_mod.main()

    assert calls == ["guard", "logging", "run"]
