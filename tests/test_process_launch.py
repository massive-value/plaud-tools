"""Tests for the shared PowerShell-launch helper (#142).

Before this helper existed, the uninstall-helper launch (uninstaller.py) and
the toast-notification launch (toasts.py) used ``DETACHED_PROCESS`` with no
explicit stdio handles. From a no-console frozen tray, that gives the child
NULL stdio and crashes PowerShell before it runs a single line -- the
uninstaller silently no-ops and no toast ever appears. Only the in-app
updater (updater.py) had already been fixed, with ``CREATE_NO_WINDOW`` +
explicit ``DEVNULL`` handles + a job-breakaway retry. These tests pin
``launch_hidden_powershell`` (the extraction of that fix) so every call site
routed through it inherits the same safe behaviour.
"""

from __future__ import annotations

import subprocess
import sys

import pytest

from plaud_tools.tray import process_launch
from plaud_tools.tray.process_launch import (
    _CREATE_BREAKAWAY_FROM_JOB,
    launch_hidden_powershell,
)

pytestmark = pytest.mark.skipif(
    sys.platform != "win32", reason="launch flags are Windows-only (CREATE_NO_WINDOW etc.)"
)


class _FakeProc:
    pid = 4242


def test_never_uses_detached_process(monkeypatch):
    """DETACHED_PROCESS gives a no-console frozen app's child NULL stdio and
    crashes it instantly (#142) -- the flag must never be requested.
    """
    seen: dict[str, int] = {}

    def fake_popen(args, creationflags=0, **kwargs):
        seen["flags"] = creationflags
        return _FakeProc()

    monkeypatch.setattr(process_launch.subprocess, "Popen", fake_popen)

    launch_hidden_powershell(["powershell.exe", "-Command", "Write-Host hi"])

    assert not (seen["flags"] & subprocess.DETACHED_PROCESS)
    assert seen["flags"] & subprocess.CREATE_NO_WINDOW
    assert seen["flags"] & subprocess.CREATE_NEW_PROCESS_GROUP


def test_stdio_is_explicit_devnull_not_inherited(monkeypatch):
    """Explicit DEVNULL handles (not None/inherited) are required alongside
    CREATE_NO_WINDOW to avoid the NULL-handle crash.
    """
    seen: dict[str, object] = {}

    def fake_popen(args, **kwargs):
        seen.update(kwargs)
        return _FakeProc()

    monkeypatch.setattr(process_launch.subprocess, "Popen", fake_popen)

    launch_hidden_powershell(["powershell.exe"])

    assert seen["stdin"] is subprocess.DEVNULL
    assert seen["stdout"] is subprocess.DEVNULL
    assert seen["stderr"] is subprocess.DEVNULL


def test_breakaway_false_does_not_request_job_breakaway(monkeypatch):
    """Fire-and-forget launches (toasts) don't need to outlive this process."""
    seen: dict[str, int] = {}

    def fake_popen(args, creationflags=0, **kwargs):
        seen["flags"] = creationflags
        return _FakeProc()

    monkeypatch.setattr(process_launch.subprocess, "Popen", fake_popen)

    launch_hidden_powershell(["powershell.exe"], breakaway=False)

    assert not (seen["flags"] & _CREATE_BREAKAWAY_FROM_JOB)


def test_breakaway_true_requests_job_breakaway(monkeypatch):
    """Helpers that must outlive this process (updater, uninstaller) request
    CREATE_BREAKAWAY_FROM_JOB so a kill-on-close tray Job Object doesn't tear
    them down before they run.
    """
    seen: dict[str, int] = {}

    def fake_popen(args, creationflags=0, **kwargs):
        seen["flags"] = creationflags
        return _FakeProc()

    monkeypatch.setattr(process_launch.subprocess, "Popen", fake_popen)

    launch_hidden_powershell(["powershell.exe"], breakaway=True)

    assert seen["flags"] & _CREATE_BREAKAWAY_FROM_JOB


def test_breakaway_falls_back_when_job_forbids_it(monkeypatch):
    """When the enclosing Job Object forbids breakaway, CreateProcess raises
    OSError; the launch must retry once without the flag rather than fail.
    """
    calls: list[int] = []

    def fake_popen(args, creationflags=0, **kwargs):
        calls.append(creationflags)
        if len(calls) == 1:
            raise OSError(5, "Access is denied")
        return _FakeProc()

    monkeypatch.setattr(process_launch.subprocess, "Popen", fake_popen)

    proc = launch_hidden_powershell(["powershell.exe"], breakaway=True)

    assert proc.pid == 4242
    assert len(calls) == 2
    assert calls[0] & _CREATE_BREAKAWAY_FROM_JOB
    assert not (calls[1] & _CREATE_BREAKAWAY_FROM_JOB)


def test_breakaway_false_propagates_errors_without_retry(monkeypatch):
    """A launch failure with breakaway=False (toasts) is not retried -- the
    caller (toasts.py) is responsible for its own best-effort try/except.
    """
    calls: list[int] = []

    def fake_popen(args, creationflags=0, **kwargs):
        calls.append(creationflags)
        raise OSError(2, "The system cannot find the file specified")

    monkeypatch.setattr(process_launch.subprocess, "Popen", fake_popen)

    with pytest.raises(OSError):
        launch_hidden_powershell(["powershell.exe"], breakaway=False)

    assert len(calls) == 1


def test_execution_policy_flags_injected_when_missing(monkeypatch):
    """A stock machine's default ``Restricted`` execution policy refuses to run
    any script; every call site must get ``-NonInteractive -ExecutionPolicy
    Bypass`` even if it forgot to pass them itself (#142).
    """
    seen: dict[str, list[str]] = {}

    def fake_popen(args, **kwargs):
        seen["args"] = args
        return _FakeProc()

    monkeypatch.setattr(process_launch.subprocess, "Popen", fake_popen)

    launch_hidden_powershell(["powershell.exe", "-WindowStyle", "Hidden", "-Command", "Write-Host hi"])

    assert seen["args"] == [
        "powershell.exe",
        "-NonInteractive",
        "-ExecutionPolicy",
        "Bypass",
        "-WindowStyle",
        "Hidden",
        "-Command",
        "Write-Host hi",
    ]


def test_execution_policy_flags_not_duplicated(monkeypatch):
    """A caller that already passes the flags (the updater, historically)
    must not get them injected a second time.
    """
    seen: dict[str, list[str]] = {}

    def fake_popen(args, **kwargs):
        seen["args"] = args
        return _FakeProc()

    monkeypatch.setattr(process_launch.subprocess, "Popen", fake_popen)

    original = ["powershell.exe", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", "x.ps1"]
    launch_hidden_powershell(original)

    assert seen["args"] == original
    assert seen["args"].count("-NonInteractive") == 1


def test_cwd_is_forwarded(monkeypatch):
    seen: dict[str, object] = {}

    def fake_popen(args, **kwargs):
        seen.update(kwargs)
        return _FakeProc()

    monkeypatch.setattr(process_launch.subprocess, "Popen", fake_popen)

    launch_hidden_powershell(["powershell.exe"], cwd="C:/Temp")

    assert seen["cwd"] == "C:/Temp"
