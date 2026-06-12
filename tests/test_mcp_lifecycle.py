"""Unit tests for mcp_lifecycle scoped shutdown helper.

Uses stub process enumerators so no real processes are created or killed.
"""
from __future__ import annotations

import sys
import types
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from plaud_tools.mcp_lifecycle import (
    ProcessInfo,
    _default_process_enumerator,
    enumerate_mcp_processes,
    mcp_shutdown_ps1_snippet,
    shutdown_mcp_children,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_enumerator(*entries: tuple[int, str]):
    """Return a zero-arg callable that yields ProcessInfo from (pid, exe_path) pairs."""
    infos = [ProcessInfo(pid=pid, exe_path=exe) for pid, exe in entries]

    def _enum():
        yield from infos

    return _enum


# ---------------------------------------------------------------------------
# enumerate_mcp_processes — scoping tests
# ---------------------------------------------------------------------------

def test_enumerate_finds_mcp_process_inside_install_dir(tmp_path: Path):
    mcp_exe = tmp_path / "mcp" / "plaud-mcp.exe"
    mcp_exe.parent.mkdir()
    mcp_exe.touch()

    enumerator = _make_enumerator(
        (1001, str(mcp_exe)),
    )
    results = enumerate_mcp_processes(tmp_path, enumerator=enumerator)
    assert len(results) == 1
    assert results[0].pid == 1001


def test_enumerate_ignores_mcp_process_outside_install_dir(tmp_path: Path):
    # Use a sibling directory so it is genuinely outside tmp_path
    other_dir = tmp_path.parent / (tmp_path.name + "_other")
    other_dir.mkdir(exist_ok=True)
    other_mcp = other_dir / "mcp" / "plaud-mcp.exe"
    other_mcp.parent.mkdir(exist_ok=True)
    other_mcp.touch()

    enumerator = _make_enumerator(
        (2001, str(other_mcp)),
    )
    results = enumerate_mcp_processes(tmp_path, enumerator=enumerator)
    assert results == []


def test_enumerate_ignores_unrelated_process_inside_install_dir(tmp_path: Path):
    cli_exe = tmp_path / "cli" / "plaud-tools.exe"
    cli_exe.parent.mkdir()
    cli_exe.touch()

    enumerator = _make_enumerator(
        (3001, str(cli_exe)),
    )
    results = enumerate_mcp_processes(tmp_path, enumerator=enumerator)
    assert results == []


def test_enumerate_returns_multiple_mcp_processes_under_install_dir(tmp_path: Path):
    mcp1 = tmp_path / "mcp" / "plaud-mcp.exe"
    mcp1.parent.mkdir()
    mcp1.touch()
    # A second nested copy (hypothetical; same name, deeper path)
    mcp2 = tmp_path / "extra" / "plaud-mcp.exe"
    mcp2.parent.mkdir()
    mcp2.touch()

    enumerator = _make_enumerator(
        (4001, str(mcp1)),
        (4002, str(mcp2)),
    )
    results = enumerate_mcp_processes(tmp_path, enumerator=enumerator)
    pids = {r.pid for r in results}
    assert pids == {4001, 4002}


def test_enumerate_ignores_process_with_empty_exe_path(tmp_path: Path):
    enumerator = _make_enumerator(
        (5001, ""),
    )
    results = enumerate_mcp_processes(tmp_path, enumerator=enumerator)
    assert results == []


def test_enumerate_mixes_inside_and_outside(tmp_path: Path):
    inside = tmp_path / "mcp" / "plaud-mcp.exe"
    inside.parent.mkdir()
    inside.touch()

    outside_dir = tmp_path.parent / "other"
    outside_dir.mkdir(exist_ok=True)
    outside = outside_dir / "plaud-mcp.exe"
    outside.touch()

    enumerator = _make_enumerator(
        (6001, str(inside)),
        (6002, str(outside)),
    )
    results = enumerate_mcp_processes(tmp_path, enumerator=enumerator)
    pids = {r.pid for r in results}
    assert pids == {6001}
    assert 6002 not in pids


def test_enumerate_case_insensitive_name(tmp_path: Path):
    # Windows paths may have mixed casing on the stem; we lower-case to compare
    mcp_exe = tmp_path / "mcp" / "Plaud-MCP.exe"
    mcp_exe.parent.mkdir()
    mcp_exe.touch()

    enumerator = _make_enumerator(
        (7001, str(mcp_exe)),
    )
    results = enumerate_mcp_processes(tmp_path, enumerator=enumerator)
    assert len(results) == 1
    assert results[0].pid == 7001


# ---------------------------------------------------------------------------
# shutdown_mcp_children — behavioural tests with mock kill/alive functions
# ---------------------------------------------------------------------------

def test_shutdown_returns_empty_when_no_processes(tmp_path: Path):
    enumerator = _make_enumerator()
    pids = shutdown_mcp_children(tmp_path, enumerator=enumerator)
    assert pids == []


def test_shutdown_returns_pid_list_for_found_processes(tmp_path: Path):
    mcp_exe = tmp_path / "mcp" / "plaud-mcp.exe"
    mcp_exe.parent.mkdir()
    mcp_exe.touch()

    enumerator = _make_enumerator((9001, str(mcp_exe)))
    # The process exits immediately after graceful signal (simulate by using a
    # very short grace period and relying on the real _process_alive() which
    # will return False for a non-existent PID on a sane system).
    pids = shutdown_mcp_children(
        tmp_path,
        grace_seconds=0.05,
        poll_interval=0.01,
        enumerator=enumerator,
    )
    assert pids == [9001]


def test_shutdown_scopes_only_to_install_dir(tmp_path: Path):
    inside = tmp_path / "mcp" / "plaud-mcp.exe"
    inside.parent.mkdir()
    inside.touch()

    outside_dir = tmp_path.parent / "other_install"
    outside_dir.mkdir(exist_ok=True)
    outside = outside_dir / "plaud-mcp.exe"
    outside.touch()

    enumerator = _make_enumerator(
        (8001, str(inside)),
        (8002, str(outside)),
    )
    pids = shutdown_mcp_children(
        tmp_path,
        grace_seconds=0.05,
        poll_interval=0.01,
        enumerator=enumerator,
    )
    # Only the process inside the install_dir should be in the returned list
    assert 8001 in pids
    assert 8002 not in pids


# ---------------------------------------------------------------------------
# mcp_shutdown_ps1_snippet — structural / content tests
# ---------------------------------------------------------------------------

def test_ps1_snippet_contains_install_dir():
    snippet = mcp_shutdown_ps1_snippet(r"C:\Programs\PlaudTools")
    assert r"C:\Programs\PlaudTools" in snippet


def test_ps1_snippet_does_not_contain_blanket_stop_process():
    snippet = mcp_shutdown_ps1_snippet(r"C:\Programs\PlaudTools")
    # The old pattern is "Stop-Process -Name plaud-mcp -Force" without a Where-Object filter
    assert "Stop-Process -Name plaud-mcp -Force -ErrorAction" not in snippet


def test_ps1_snippet_contains_where_object_path_filter():
    snippet = mcp_shutdown_ps1_snippet(r"C:\Programs\PlaudTools")
    assert "Where-Object" in snippet
    assert "Path" in snippet


def test_ps1_snippet_uses_poll_not_fixed_sleep():
    snippet = mcp_shutdown_ps1_snippet(r"C:\Programs\PlaudTools")
    assert "Start-Sleep -Seconds 2" not in snippet
    assert "HasExited" in snippet or "Milliseconds" in snippet


def test_ps1_snippet_includes_graceful_step():
    snippet = mcp_shutdown_ps1_snippet(r"C:\Programs\PlaudTools")
    # CloseMainWindow() is the graceful-first step
    assert "CloseMainWindow" in snippet


def test_ps1_snippet_strips_trailing_backslash():
    snippet_with = mcp_shutdown_ps1_snippet(r"C:\Programs\PlaudTools\\")
    snippet_without = mcp_shutdown_ps1_snippet(r"C:\Programs\PlaudTools")
    # Both should reference the same path without a trailing separator
    assert r"C:\Programs\PlaudTools\\" not in snippet_with
    assert r"C:\Programs\PlaudTools" in snippet_with
    assert r"C:\Programs\PlaudTools" in snippet_without


def test_ps1_snippet_respects_grace_seconds_parameter():
    snippet = mcp_shutdown_ps1_snippet(r"C:\Programs\PlaudTools", grace_seconds=10)
    assert "10" in snippet


def test_ps1_snippet_contains_scoped_stop_process():
    # After grace period, force-kill survivors — must be scoped (piped from $mcpProcs)
    snippet = mcp_shutdown_ps1_snippet(r"C:\Programs\PlaudTools")
    assert "Stop-Process" in snippet
    # Should be preceded by a pipe or applied to $mcpProcs, not a standalone Name filter
    assert "Stop-Process -Name plaud-mcp -Force\n" not in snippet


# ---------------------------------------------------------------------------
# _default_process_enumerator — PowerShell fallback parsing tests
# ---------------------------------------------------------------------------

# Fixture: canned ConvertTo-Csv output from PowerShell Get-Process
_PS_CSV_OUTPUT = '''"Id","Path"
"1234","C:\\Programs\\PlaudTools\\mcp\\plaud-mcp.exe"
"5678","C:\\Programs\\PlaudTools\\mcp\\plaud-mcp.exe"
"9999","C:\\Windows\\System32\\svchost.exe"
'''

# Fixture: empty output — no processes with a Path property
_PS_CSV_EMPTY = '"Id","Path"\n'


def _block_psutil(monkeypatch):
    """Force an ImportError when psutil is imported inside the enumerator."""
    # If psutil is already imported, temporarily remove it from sys.modules.
    monkeypatch.setitem(sys.modules, "psutil", None)  # None triggers ImportError on import


def test_powershell_fallback_parses_pid_and_exe_path(monkeypatch):
    """The PowerShell CSV fallback must parse pid + full exe path correctly.

    We force the psutil ImportError path by patching sys.modules, then
    stub subprocess.check_output so WMIC raises (simulating Win11 removal)
    and the PowerShell command returns our canned fixture output.
    """
    _block_psutil(monkeypatch)

    call_count = {"n": 0}

    def fake_check_output(cmd, **kwargs):
        call_count["n"] += 1
        if "wmic" in cmd[0].lower():
            # Simulate WMIC being absent on modern Win11
            raise FileNotFoundError("wmic not found")
        # PowerShell call
        return _PS_CSV_OUTPUT

    monkeypatch.setattr("subprocess.check_output", fake_check_output)
    monkeypatch.setattr("os.name", "nt")

    results = list(_default_process_enumerator())

    # Should have parsed all three rows
    assert len(results) == 3
    pids = {r.pid for r in results}
    assert pids == {1234, 5678, 9999}
    paths = {r.exe_path for r in results}
    assert r"C:\Programs\PlaudTools\mcp\plaud-mcp.exe" in paths
    assert r"C:\Windows\System32\svchost.exe" in paths


def test_powershell_fallback_filters_via_enumerate_mcp_processes(tmp_path, monkeypatch):
    """End-to-end: PowerShell fallback output flows through enumerate_mcp_processes scoping.

    enumerate_mcp_processes must find only the plaud-mcp process inside tmp_path.
    """
    _block_psutil(monkeypatch)

    # Create a plaud-mcp.exe inside tmp_path so path-resolve works
    mcp_exe = tmp_path / "mcp" / "plaud-mcp.exe"
    mcp_exe.parent.mkdir()
    mcp_exe.touch()

    # Build CSV fixture referencing the real tmp_path exe + an outside process
    outside_exe = r"C:\OtherInstall\mcp\plaud-mcp.exe"
    csv_output = (
        '"Id","Path"\n'
        f'"1001","{str(mcp_exe)}"\n'
        f'"2002","{outside_exe}"\n'
    )

    def fake_check_output(cmd, **kwargs):
        if "wmic" in cmd[0].lower():
            raise FileNotFoundError("wmic not found")
        return csv_output

    monkeypatch.setattr("subprocess.check_output", fake_check_output)
    monkeypatch.setattr("os.name", "nt")

    # enumerate_mcp_processes uses _default_process_enumerator when no stub given
    results = enumerate_mcp_processes(tmp_path)
    assert len(results) == 1
    assert results[0].pid == 1001


def test_warning_logged_when_zero_results_and_no_psutil(monkeypatch, caplog):
    """A WARNING must be emitted when psutil is absent and all fallbacks yield nothing.

    This ensures silent failures are observable in production logs (ADR 003 amendment).
    """
    import logging

    _block_psutil(monkeypatch)
    monkeypatch.setattr("os.name", "nt")

    def fake_check_output(cmd, **kwargs):
        if "wmic" in cmd[0].lower():
            raise FileNotFoundError("wmic not found")
        # PowerShell returns only the header — no rows
        return _PS_CSV_EMPTY

    monkeypatch.setattr("subprocess.check_output", fake_check_output)

    with caplog.at_level(logging.WARNING, logger="plaud_tools.mcp_lifecycle"):
        results = list(_default_process_enumerator())

    assert results == []
    warning_messages = [r.message for r in caplog.records if r.levelno >= logging.WARNING]
    assert any(
        "zero process entries" in msg or "disabled" in msg for msg in warning_messages
    ), f"Expected a WARNING about zero results, got: {warning_messages}"


def test_warning_logged_when_powershell_raises_and_no_psutil(monkeypatch, caplog):
    """A WARNING must be emitted when PowerShell itself fails and psutil is absent."""
    import logging

    _block_psutil(monkeypatch)
    monkeypatch.setattr("os.name", "nt")

    def fake_check_output(cmd, **kwargs):
        raise OSError("subprocess totally broken")

    monkeypatch.setattr("subprocess.check_output", fake_check_output)

    with caplog.at_level(logging.WARNING, logger="plaud_tools.mcp_lifecycle"):
        results = list(_default_process_enumerator())

    assert results == []
    warning_messages = [r.message for r in caplog.records if r.levelno >= logging.WARNING]
    assert any(
        "disabled" in msg or "fallback failed" in msg for msg in warning_messages
    ), f"Expected a WARNING about fallback failure, got: {warning_messages}"
