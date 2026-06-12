"""Tests for the hardened single-instance mutex in tray/setup.py (E2).

All three outcomes are exercised via an injectable fake kernel32 so no real
Win32 handles are created.

Windows-only: ``plaud_tools.tray.setup`` does ``import tkinter`` at module top,
which is absent on the ``.[dev]``-only Linux/macOS CI runners.  The module-level
skipif mirrors the DPAPI win32 skip in test_auth.py, and each test imports
``_acquire_instance_lock`` inside its body so collection on non-Windows never
triggers the tkinter import.
"""

from __future__ import annotations

import sys
from unittest.mock import MagicMock

import pytest

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="single-instance mutex is Windows-only")

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_kernel32(handle: object, last_error: int) -> MagicMock:
    """Return a fake kernel32 whose CreateMutexW returns *handle*.

    ``ctypes.get_last_error()`` reads a thread-local value that is set by the
    ``use_last_error=True`` WinDLL.  Rather than patching ctypes internals we
    rely on the fact that _acquire_instance_lock calls ctypes.get_last_error()
    after CreateMutexW — so we simply pre-seed that thread-local value by
    calling the real ctypes function before the handle is created.

    Because we inject a fake kernel32, the WinDLL's internal write to the
    thread-local never happens.  We therefore monkeypatch ctypes.get_last_error
    inside each test instead of relying on a real WinDLL side-effect.
    """
    k32 = MagicMock()
    k32.CreateMutexW.return_value = handle
    k32.OpenEventW.return_value = 0  # default: event not found
    return k32


# ---------------------------------------------------------------------------
# Outcome 1 — acquired (handle non-NULL, last_error == 0)
# ---------------------------------------------------------------------------


def test_acquire_instance_lock_acquired(monkeypatch):
    """When CreateMutexW returns a valid handle and last_error is 0, return True."""
    monkeypatch.setattr(sys, "platform", "win32")

    import ctypes

    monkeypatch.setattr(ctypes, "get_last_error", lambda: 0)

    fake_handle = 0xDEADBEEF
    k32 = _make_kernel32(handle=fake_handle, last_error=0)

    from plaud_tools.tray.setup import _acquire_instance_lock

    result = _acquire_instance_lock(_kernel32=k32)

    assert result is True
    k32.CreateMutexW.assert_called_once()
    # No activation signalling expected.
    k32.OpenEventW.assert_not_called()


# ---------------------------------------------------------------------------
# Outcome 2 — already running (handle non-NULL, last_error == 183)
# ---------------------------------------------------------------------------


def test_acquire_instance_lock_already_running(monkeypatch):
    """When ERROR_ALREADY_EXISTS (183) is returned, return False and signal the running instance."""
    monkeypatch.setattr(sys, "platform", "win32")

    import ctypes

    monkeypatch.setattr(ctypes, "get_last_error", lambda: 183)

    fake_handle = 0xDEADBEEF
    k32 = _make_kernel32(handle=fake_handle, last_error=183)
    # Simulate OpenEventW finding the activation event.
    fake_event = 0xCAFEBABE
    k32.OpenEventW.return_value = fake_event

    from plaud_tools.tray.setup import _acquire_instance_lock

    result = _acquire_instance_lock(_kernel32=k32)

    assert result is False
    # Must attempt to signal the running instance.
    k32.OpenEventW.assert_called_once()
    k32.SetEvent.assert_called_once_with(fake_event)
    k32.CloseHandle.assert_called_once_with(fake_event)


def test_acquire_instance_lock_already_running_no_event(monkeypatch):
    """ERROR_ALREADY_EXISTS with OpenEventW returning 0 — still returns False, no SetEvent."""
    monkeypatch.setattr(sys, "platform", "win32")

    import ctypes

    monkeypatch.setattr(ctypes, "get_last_error", lambda: 183)

    fake_handle = 0xDEADBEEF
    k32 = _make_kernel32(handle=fake_handle, last_error=183)
    k32.OpenEventW.return_value = 0  # event not found

    from plaud_tools.tray.setup import _acquire_instance_lock

    result = _acquire_instance_lock(_kernel32=k32)

    assert result is False
    k32.OpenEventW.assert_called_once()
    k32.SetEvent.assert_not_called()
    k32.CloseHandle.assert_not_called()


# ---------------------------------------------------------------------------
# Outcome 3 — API failure (NULL handle) → fail-open
# ---------------------------------------------------------------------------


def test_acquire_instance_lock_null_handle_fail_open(monkeypatch, caplog):
    """When CreateMutexW returns NULL (0), fail-open: return True and log a warning."""
    import logging

    monkeypatch.setattr(sys, "platform", "win32")

    import ctypes

    monkeypatch.setattr(ctypes, "get_last_error", lambda: 5)  # ERROR_ACCESS_DENIED

    k32 = _make_kernel32(handle=0, last_error=5)

    from plaud_tools.tray.setup import _acquire_instance_lock

    with caplog.at_level(logging.WARNING):
        result = _acquire_instance_lock(_kernel32=k32)

    assert result is True, "NULL handle must fail-open (allow tray startup)"
    # No attempt to signal a running instance.
    k32.OpenEventW.assert_not_called()
    # A warning must be logged so the condition is diagnosable.
    assert any("NULL" in r.message or "CreateMutexW" in r.message for r in caplog.records), (
        "Expected a warning log about the NULL handle"
    )


# ---------------------------------------------------------------------------
# Non-Windows — always returns True without touching kernel32
# ---------------------------------------------------------------------------


def test_acquire_instance_lock_non_windows(monkeypatch):
    """On non-Windows platforms the function must return True without any Win32 calls."""
    monkeypatch.setattr(sys, "platform", "linux")

    k32 = MagicMock()

    from plaud_tools.tray.setup import _acquire_instance_lock

    result = _acquire_instance_lock(_kernel32=k32)

    assert result is True
    k32.CreateMutexW.assert_not_called()
