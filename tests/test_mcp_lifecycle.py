"""Unit tests for mcp_lifecycle scoped shutdown helper.

Uses stub process enumerators so no real processes are created or killed.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from plaud_tools.mcp_lifecycle import (
    ProcessInfo,
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
# Windows fallback parsing tests
#
# These exercise the WMIC / PowerShell CSV PARSING and the subprocess fallback
# chain WITHOUT pretending the host is Windows. The production gate
# (`os.name == "nt"`) lives only in `_default_process_enumerator`; the parsing
# and subprocess plumbing live in `_windows_fallback_enumerator` and the two
# pure `_parse_*_csv` helpers, which are platform-agnostic.
#
# We deliberately DO NOT mutate `os.name`. Mutating the global `os.name` to
# "nt" on a Linux runner makes `pathlib.Path(...)` resolve to `WindowsPath`,
# which later crashes pytest's end-of-session cache write with
# "cannot instantiate 'WindowsPath' on your system" — the PR #96 CI failure.
# ---------------------------------------------------------------------------

from plaud_tools.mcp_lifecycle import (  # noqa: E402  (grouped with fallback tests)
    _parse_powershell_csv,
    _parse_wmic_csv,
    _windows_fallback_enumerator,
)

# Fixture: canned ConvertTo-Csv output from PowerShell Get-Process
_PS_CSV_OUTPUT = '''"Id","Path"
"1234","C:\\Programs\\PlaudTools\\mcp\\plaud-mcp.exe"
"5678","C:\\Programs\\PlaudTools\\mcp\\plaud-mcp.exe"
"9999","C:\\Windows\\System32\\svchost.exe"
'''

# Fixture: empty output — no processes with a Path property
_PS_CSV_EMPTY = '"Id","Path"\n'


@pytest.fixture(autouse=True)
def _assert_os_name_not_leaked():
    """Trip-wire: fail loudly if any test leaks a mutated global ``os.name``.

    Guards against re-introducing the global ``os.name = "nt"`` pattern that
    poisons ``pathlib`` flavour on non-Windows CI (PR #96).
    """
    import os as _os

    original = _os.name
    yield
    assert _os.name == original, (
        f"global os.name leaked: was {original!r}, now {_os.name!r}; "
        "do not mutate os.name — test the platform-agnostic fallback helpers instead"
    )


def _wmic_unavailable(out_for_powershell):
    """Return a fake ``subprocess.check_output`` where WMIC raises (Win11 22H2+).

    PowerShell calls return *out_for_powershell* (a str or a callable raising).
    """
    def fake_check_output(cmd, **kwargs):
        if "wmic" in cmd[0].lower():
            raise FileNotFoundError("wmic not found")
        if callable(out_for_powershell):
            return out_for_powershell()
        return out_for_powershell

    return fake_check_output


# --- Pure parser tests (no subprocess, no os.name) -------------------------

def test_parse_powershell_csv_extracts_pid_and_path():
    results = _parse_powershell_csv(_PS_CSV_OUTPUT)
    assert len(results) == 3
    assert {r.pid for r in results} == {1234, 5678, 9999}
    paths = {r.exe_path for r in results}
    assert r"C:\Programs\PlaudTools\mcp\plaud-mcp.exe" in paths
    assert r"C:\Windows\System32\svchost.exe" in paths


def test_parse_powershell_csv_header_only_yields_nothing():
    assert _parse_powershell_csv(_PS_CSV_EMPTY) == []


def test_parse_powershell_csv_skips_non_integer_pid():
    bad = '"Id","Path"\n"notanint","C:\\x\\plaud-mcp.exe"\n"42","C:\\y\\plaud-mcp.exe"\n'
    results = _parse_powershell_csv(bad)
    assert [r.pid for r in results] == [42]


def test_parse_wmic_csv_extracts_pid_and_path():
    # WMIC CSV: Node,ExecutablePath,ProcessId
    out = "Node,ExecutablePath,ProcessId\nHOST,C:\\a\\plaud-mcp.exe,1234\nHOST,,5678\n"
    results = _parse_wmic_csv(out)
    # Row with empty ExecutablePath is skipped.
    assert len(results) == 1
    assert results[0].pid == 1234
    assert results[0].exe_path == r"C:\a\plaud-mcp.exe"


# --- Subprocess fallback-chain tests (stub check_output, no os.name) -------

def test_windows_fallback_parses_pid_and_exe_path(monkeypatch):
    """WMIC absent → PowerShell CSV parsed into ProcessInfo with full paths."""
    monkeypatch.setattr("subprocess.check_output", _wmic_unavailable(_PS_CSV_OUTPUT))

    results = list(_windows_fallback_enumerator())

    assert len(results) == 3
    assert {r.pid for r in results} == {1234, 5678, 9999}
    paths = {r.exe_path for r in results}
    assert r"C:\Programs\PlaudTools\mcp\plaud-mcp.exe" in paths
    assert r"C:\Windows\System32\svchost.exe" in paths


def test_windows_fallback_prefers_wmic_when_it_returns_entries(monkeypatch):
    """When WMIC yields entries, PowerShell is not consulted."""
    wmic_out = "Node,ExecutablePath,ProcessId\nHOST,C:\\a\\plaud-mcp.exe,7000\n"
    ps_called = {"hit": False}

    def fake_check_output(cmd, **kwargs):
        if "wmic" in cmd[0].lower():
            return wmic_out
        ps_called["hit"] = True
        return _PS_CSV_OUTPUT

    monkeypatch.setattr("subprocess.check_output", fake_check_output)

    results = list(_windows_fallback_enumerator())
    assert [r.pid for r in results] == [7000]
    assert ps_called["hit"] is False, "PowerShell should not run when WMIC succeeds"


def test_windows_fallback_filters_via_enumerate_mcp_processes(tmp_path, monkeypatch):
    """End-to-end: fallback output flows through enumerate_mcp_processes scoping.

    We inject ``_windows_fallback_enumerator`` directly as the ``enumerator``
    stub — this drives the real fallback + parsing code WITHOUT touching the
    ``os.name`` gate (the gate lives only in ``_default_process_enumerator``).
    """
    # Create a plaud-mcp.exe inside tmp_path so path-resolve works on this host.
    mcp_exe = tmp_path / "mcp" / "plaud-mcp.exe"
    mcp_exe.parent.mkdir()
    mcp_exe.touch()

    # Reference the real tmp_path exe (native flavour) + an unrelated outside path.
    outside_exe = str(tmp_path.parent / "other" / "plaud-mcp.exe")
    csv_output = (
        '"Id","Path"\n'
        f'"1001","{str(mcp_exe)}"\n'
        f'"2002","{outside_exe}"\n'
    )
    monkeypatch.setattr("subprocess.check_output", _wmic_unavailable(csv_output))

    results = enumerate_mcp_processes(tmp_path, enumerator=_windows_fallback_enumerator)
    assert len(results) == 1
    assert results[0].pid == 1001


def test_warning_logged_when_zero_results(monkeypatch, caplog):
    """WARNING fires when WMIC is absent and PowerShell yields zero rows.

    Mirrors the production silent-failure path (psutil absent + WMIC removed +
    PowerShell empty); we test the fallback helper directly so no os.name churn.
    """
    import logging

    monkeypatch.setattr("subprocess.check_output", _wmic_unavailable(_PS_CSV_EMPTY))

    with caplog.at_level(logging.WARNING, logger="plaud_tools.mcp_lifecycle"):
        results = list(_windows_fallback_enumerator())

    assert results == []
    warning_messages = [r.message for r in caplog.records if r.levelno >= logging.WARNING]
    assert any(
        "zero process entries" in msg or "disabled" in msg for msg in warning_messages
    ), f"Expected a WARNING about zero results, got: {warning_messages}"


def test_warning_logged_when_powershell_raises(monkeypatch, caplog):
    """WARNING fires when WMIC is absent and PowerShell itself raises."""
    import logging

    def _raise():
        raise OSError("subprocess totally broken")

    monkeypatch.setattr("subprocess.check_output", _wmic_unavailable(_raise))

    with caplog.at_level(logging.WARNING, logger="plaud_tools.mcp_lifecycle"):
        results = list(_windows_fallback_enumerator())

    assert results == []
    warning_messages = [r.message for r in caplog.records if r.levelno >= logging.WARNING]
    assert any(
        "disabled" in msg or "fallback failed" in msg for msg in warning_messages
    ), f"Expected a WARNING about fallback failure, got: {warning_messages}"
