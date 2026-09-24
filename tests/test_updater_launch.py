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
from unittest.mock import MagicMock

import pytest

from plaud_tools.tray import updater
from plaud_tools.tray.updater import _CREATE_BREAKAWAY_FROM_JOB, UpdateDialog, _launch_updater

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


# ---------------------------------------------------------------------------
# #157 — _install_worker's _on_error must not touch a destroyed install_btn
#
# _on_error is delivered via root.after() from the download worker thread,
# so the UpdateDialog window (and its install_btn) may have been closed in
# the meantime. Before the fix, `install_btn.config(state="normal")` had no
# widget-alive guard -- the same v0.3.3 crash class as HomeWindow's
# _refresh_update_btn, just in a different file.
# ---------------------------------------------------------------------------


def _make_dialog() -> UpdateDialog:
    root = MagicMock()
    root.after.side_effect = lambda _delay, fn: fn()
    app = MagicMock()
    app._update_info = ("9.9.9", "https://example.com/release", "https://evil.example/x.zip", None)
    return UpdateDialog(root, app)


class TestInstallWorkerOnErrorWidgetGuard:
    def test_skips_configure_when_install_btn_destroyed(self):
        """An untrusted zip host makes _check_download_host raise immediately
        (no network I/O needed to reach _on_error) -- fast path to the guard.
        """
        dialog = _make_dialog()
        status_var = MagicMock()
        install_btn = MagicMock()
        install_btn.winfo_exists.return_value = 0  # destroyed

        dialog._install_worker("https://evil.example/x.zip", None, status_var, install_btn)

        status_var.set.assert_called_once()
        assert "Download failed" in status_var.set.call_args[0][0]
        install_btn.configure.assert_not_called()

    def test_configures_when_install_btn_alive(self):
        """No regression: a live button is still re-enabled on failure."""
        dialog = _make_dialog()
        status_var = MagicMock()
        install_btn = MagicMock()
        install_btn.winfo_exists.return_value = 1  # alive

        dialog._install_worker("https://evil.example/x.zip", None, status_var, install_btn)

        install_btn.configure.assert_called_once_with(state="normal")


# ---------------------------------------------------------------------------
# Install worker: cleanup, cancel, wedged-updater kill, single-install guard
# ---------------------------------------------------------------------------

ZIP_URL = "https://github.com/massive-value/plaud-tools/releases/download/v9.9.9/PlaudTools.zip"


def _fake_download(monkeypatch, chunks):
    """Serve *chunks* (bytes, or an exception to raise) from the zip download."""
    resp = MagicMock()
    resp.__enter__.return_value = resp
    resp.headers.get.return_value = None
    resp.read.side_effect = list(chunks)
    monkeypatch.setattr(updater, "_open_update_url", lambda url, timeout: resp)


@pytest.fixture()
def worker_env(tmp_path, monkeypatch):
    """Point every %TEMP% write at tmp_path (sentinels must never hit the real TEMP)."""
    monkeypatch.setattr("tempfile.tempdir", str(tmp_path))
    return tmp_path


def _started_dialog() -> UpdateDialog:
    dialog = _make_dialog()
    assert dialog._try_begin_install()
    return dialog


def test_download_error_deletes_partial_zip(worker_env, monkeypatch):
    _fake_download(monkeypatch, [b"partial", OSError("connection reset")])
    dialog = _started_dialog()

    dialog._install_worker(ZIP_URL, None, MagicMock(), MagicMock())

    assert list(worker_env.glob("plaud_update_*.zip")) == []
    assert not dialog._in_progress.is_set()


def test_cancel_stops_download_and_never_launches_updater(worker_env, monkeypatch):
    _fake_download(monkeypatch, [b"chunk", b"chunk", b""])
    launch = MagicMock()
    monkeypatch.setattr(updater, "_launch_updater", launch)
    dialog = _started_dialog()
    dialog._cancel.set()  # user clicked Cancel

    dialog._install_worker(ZIP_URL, None, MagicMock(), MagicMock())

    launch.assert_not_called()
    assert list(worker_env.glob("plaud_update_*.zip")) == []
    assert not dialog._in_progress.is_set()


def test_heartbeat_timeout_kills_the_updater(worker_env, monkeypatch):
    """A wedged PowerShell must be killed, or it can wake up and install
    after the tray already reported failure (two updaters racing)."""
    _fake_download(monkeypatch, [b"zipbytes", b""])
    monkeypatch.setattr(updater, "verify_zip_checksum", lambda *a: None)
    monkeypatch.setattr(updater, "render_update_ps1", lambda **kw: "# dispatcher\n")
    monkeypatch.setattr(updater, "_UPDATER_HEARTBEAT_TIMEOUT_S", 0.3)
    layout = MagicMock(install_root=worker_env / "PlaudTools")
    monkeypatch.setattr(updater.InstallLayout, "detect", classmethod(lambda cls: layout))
    proc = MagicMock(pid=999)
    proc.poll.return_value = None  # alive, but never writes the heartbeat
    monkeypatch.setattr(updater, "_launch_updater", lambda ps_path: proc)
    dialog = _started_dialog()

    dialog._install_worker(ZIP_URL, "unused", MagicMock(), MagicMock())

    proc.kill.assert_called_once()
    dialog._app._quit.assert_not_called()
    assert (worker_env / "plaud_update_failed.txt").exists()
    assert list(worker_env.glob("plaud_update_*.zip")) == []
    assert not dialog._in_progress.is_set()


def test_only_one_install_at_a_time():
    dialog = _make_dialog()
    assert dialog._try_begin_install() is True
    assert dialog._try_begin_install() is False
