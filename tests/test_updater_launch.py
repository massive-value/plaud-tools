"""Tests for the updater's detached-launch behaviour (job breakaway + fallback).

The in-app updater MUST outlive the tray process. The tray runs inside a
Windows Job Object; without breaking away, a kill-on-close job kills the child
PowerShell the instant the tray exits — before update.ps1 runs a line. These
tests pin the launch flags and the graceful fallback when the job forbids
breakaway (CreateProcess raises OSError / ERROR_ACCESS_DENIED).
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from plaud_tools.tray import updater
from plaud_tools.tray.updater import _CREATE_BREAKAWAY_FROM_JOB, _launch_updater

# _launch_updater uses Windows-only creation flags (CREATE_NO_WINDOW etc.); the
# in-app updater only ever runs on the frozen Windows bundle. Skip elsewhere.
pytestmark = pytest.mark.skipif(
    sys.platform != "win32", reason="updater launch is Windows-only (CREATE_NO_WINDOW)"
)


class _FakeProc:
    pid = 4321


def test_launch_updater_requests_breakaway(monkeypatch):
    """When the job permits breakaway, the child is launched WITH the
    CREATE_BREAKAWAY_FROM_JOB flag so it escapes the tray's job object.
    """
    seen: dict[str, int] = {}

    def fake_popen(args, creationflags=0, **kwargs):
        seen["flags"] = creationflags
        return _FakeProc()

    monkeypatch.setattr(updater.subprocess, "Popen", fake_popen)

    proc = _launch_updater(Path("C:/Temp/plaud_update_1.ps1"))

    assert proc.pid == 4321
    assert seen["flags"] & _CREATE_BREAKAWAY_FROM_JOB, "breakaway flag must be set"
    assert seen["flags"] & subprocess.CREATE_NO_WINDOW
    assert seen["flags"] & subprocess.CREATE_NEW_PROCESS_GROUP


def test_launch_updater_falls_back_without_breakaway(monkeypatch):
    """When the job forbids breakaway (CreateProcess raises OSError), the
    updater retries WITHOUT the flag rather than failing the update outright.
    """
    calls: list[int] = []

    def fake_popen(args, creationflags=0, **kwargs):
        calls.append(creationflags)
        if len(calls) == 1:
            # First attempt (with breakaway) is denied by the job.
            raise OSError(5, "Access is denied")
        return _FakeProc()

    monkeypatch.setattr(updater.subprocess, "Popen", fake_popen)

    proc = _launch_updater(Path("C:/Temp/plaud_update_1.ps1"))

    assert proc.pid == 4321
    assert len(calls) == 2, "must retry exactly once on breakaway denial"
    assert calls[0] & _CREATE_BREAKAWAY_FROM_JOB, "first attempt requests breakaway"
    assert not (calls[1] & _CREATE_BREAKAWAY_FROM_JOB), "fallback drops breakaway"
    assert calls[1] & subprocess.CREATE_NO_WINDOW
    assert calls[1] & subprocess.CREATE_NEW_PROCESS_GROUP


def test_launch_updater_propagates_non_breakaway_errors(monkeypatch):
    """A failure on the fallback launch (no breakaway) is a genuine error and
    must propagate — we do not silently swallow it.
    """

    def fake_popen(args, creationflags=0, **kwargs):
        raise OSError(2, "The system cannot find the file specified")

    monkeypatch.setattr(updater.subprocess, "Popen", fake_popen)

    with pytest.raises(OSError):
        _launch_updater(Path("C:/Temp/plaud_update_1.ps1"))
