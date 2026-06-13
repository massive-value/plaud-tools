"""Regression tests for the two-stage tray crash reported against an older build.

Observed traceback (paraphrased):

    File "plaud_tools/tray/windows/home.py", line 201, in _refresh_update_btn
    ...
    _tkinter.TclError: invalid command name ".!toplevel.!frame.!frame2.!button3"

    During handling of the above exception, another exception occurred:
    ...
    TypeError: _setup_logging.<locals>._tk_error() takes 3 positional
               arguments but 4 were given

Two independent bugs:

1. ``HomeWindow._refresh_update_btn`` is invoked asynchronously from the
   background update-poll thread (via ``root.after(0, ...)``) and could fire
   after the user closed the HomeWindow.  The ``self._update_btn is None`` guard
   does not catch a destroyed-but-non-None widget, so ``configure()`` raised
   ``TclError: invalid command name``.

2. ``_setup_logging`` assigns ``_tk_error`` to the *class* ``tk.Tk``, so Tk
   invokes it as a bound method (4 args incl. ``self``).  The original 3-arg
   signature raised ``TypeError`` *inside* the error reporter, escalating every
   recoverable callback error into a process-level crash.

All tests are display-free — widgets are MagicMocks.  ``conftest.py`` stubs
pystray / PIL so the tray modules import in CI.
"""

from __future__ import annotations

import inspect
from unittest.mock import MagicMock

from plaud_tools.tray_app import HomeWindow


def _make_home_window() -> HomeWindow:
    return HomeWindow(
        root=MagicMock(),
        on_test_connection=MagicMock(),
        on_check_for_update=MagicMock(),
        on_open_update=MagicMock(),
        on_open_wizard=MagicMock(),
        on_sign_out=MagicMock(),
        on_open_uninstall=MagicMock(),
        on_repair_setup=None,
        get_session_label=lambda: "Signed in as test@example.com.",
        get_update_info=lambda: None,
        get_env_status=lambda: None,
    )


# ---------------------------------------------------------------------------
# Bug 1 — _refresh_update_btn must not touch a destroyed widget
# ---------------------------------------------------------------------------


class TestRefreshUpdateBtnAfterClose:
    def test_skips_configure_when_widget_destroyed(self):
        """A closed window leaves _update_btn non-None but destroyed.

        winfo_exists() returns 0 (falsy); configure() must NOT be called.
        """
        hw = _make_home_window()
        hw._update_btn = MagicMock()
        hw._update_btn.winfo_exists.return_value = 0  # destroyed

        hw._refresh_update_btn()

        hw._update_btn.winfo_exists.assert_called_once()
        hw._update_btn.configure.assert_not_called()

    def test_configures_when_widget_alive(self):
        """A live widget is still configured normally (no regression)."""
        hw = _make_home_window()
        hw._update_btn = MagicMock()
        hw._update_btn.winfo_exists.return_value = 1  # alive

        hw._refresh_update_btn()

        hw._update_btn.configure.assert_called_once()
        _, kwargs = hw._update_btn.configure.call_args
        assert kwargs.get("text") == "Check for Updates"

    def test_noop_when_btn_is_none(self):
        """The original None guard still short-circuits before winfo_exists."""
        hw = _make_home_window()
        hw._update_btn = None
        hw._refresh_update_btn()  # must not raise


# ---------------------------------------------------------------------------
# Bug 2 — the Tk error reporter must accept the bound-method `self` arg
# ---------------------------------------------------------------------------


class TestTkErrorReporterSignature:
    def test_class_level_handler_accepts_self(self):
        """report_callback_exception is set on tk.Tk, so it's called bound.

        It must accept (self, exc, val, tb) — exactly the 4 args Tk passes.
        """
        import tkinter as tk

        from plaud_tools.tray.setup import _setup_logging

        _setup_logging()
        handler = tk.Tk.report_callback_exception
        params = list(inspect.signature(handler).parameters)
        assert len(params) == 4, f"expected (self, exc, val, tb), got {params}"

    def test_handler_invocable_with_four_args(self):
        """Simulate Tk's bound-method call: handler(self_obj, exc, val, tb)."""
        import tkinter as tk

        from plaud_tools.tray.setup import _setup_logging

        _setup_logging()
        handler = tk.Tk.report_callback_exception
        try:
            raise ValueError("boom")
        except ValueError:
            import sys

            exc, val, tb = sys.exc_info()
            # Must not raise TypeError about argument count.  (Pyright reads the
            # tkinter stub's 3-arg signature, not our reassigned 4-arg handler.)
            handler(MagicMock(), exc, val, tb)  # type: ignore[call-arg]
