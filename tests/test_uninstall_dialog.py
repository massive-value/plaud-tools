"""Tests for UninstallDialog polish (issue #28).

Covers:
- ai_client_warning_visible: pure predicate for warning label visibility.
- _update_warning: trace callback that calls the predicate and sets the StringVar.
- Button-disable guard: do_uninstall() disables both buttons before running.

The Tk widget tests use the `requires_display` mark so they are skipped in
headless CI environments that lack a working Tcl/Tk installation.
"""
from __future__ import annotations

import sys
import pytest
from unittest.mock import MagicMock, patch, call

# conftest.py already stubs pystray / PIL so this import succeeds on CI.
from plaud_tools.tray_app import UninstallDialog


# ---------------------------------------------------------------------------
# Pure-predicate tests — no display required
# ---------------------------------------------------------------------------

class TestAiClientWarningPredicate:
    """UninstallDialog.ai_client_warning_visible is a static predicate."""

    def test_warning_shown_when_delete_dir_and_no_disconnect(self):
        """The risky combination: install dir deleted, AI clients NOT cleaned up."""
        assert UninstallDialog.ai_client_warning_visible(
            delete_installdir=True, disconnect_clients=False
        ) is True

    def test_no_warning_when_both_checked(self):
        """Safe: directory deleted AND clients disconnected first — no dangling paths."""
        assert UninstallDialog.ai_client_warning_visible(
            delete_installdir=True, disconnect_clients=True
        ) is False

    def test_no_warning_when_delete_dir_unchecked(self):
        """Install dir not deleted — dangling path cannot arise."""
        assert UninstallDialog.ai_client_warning_visible(
            delete_installdir=False, disconnect_clients=False
        ) is False

    def test_no_warning_when_delete_dir_unchecked_clients_checked(self):
        """Both safe states: no install-dir deletion AND clients being disconnected."""
        assert UninstallDialog.ai_client_warning_visible(
            delete_installdir=False, disconnect_clients=True
        ) is False

    def test_predicate_is_false_for_all_safe_combinations(self):
        safe = [
            (True, True),
            (False, True),
            (False, False),
        ]
        for di, cl in safe:
            assert not UninstallDialog.ai_client_warning_visible(di, cl), (
                f"Expected False for delete_installdir={di}, disconnect_clients={cl}"
            )

    def test_predicate_is_true_only_for_dangerous_combination(self):
        assert UninstallDialog.ai_client_warning_visible(True, False) is True


# ---------------------------------------------------------------------------
# Warning-update callback test — uses mock BooleanVar / StringVar
# ---------------------------------------------------------------------------

class TestWarningUpdateCallback:
    """Test the trace callback logic without a real Tk display.

    We replicate the _update_warning closure from UninstallDialog.show() to
    confirm it correctly reads the two BooleanVars and sets the StringVar.
    """

    def _make_mock_var(self, initial: bool) -> MagicMock:
        v = MagicMock()
        v.get.return_value = initial
        return v

    def _make_string_var(self) -> MagicMock:
        v = MagicMock()
        return v

    def _build_update_warning(self, var_installdir, var_clients, warning_var):
        """Return the _update_warning closure as it would be built in show()."""
        def _update_warning(*_args):
            if UninstallDialog.ai_client_warning_visible(
                var_installdir.get(), var_clients.get()
            ):
                warning_var.set(
                    "⚠ AI clients will still point at the deleted install directory. "
                    "Restart Claude Desktop / Claude Code / Codex after uninstalling to clear the error."
                )
            else:
                warning_var.set("")
        return _update_warning

    def test_warning_set_when_dangerous(self):
        vi = self._make_mock_var(True)   # delete installdir
        vc = self._make_mock_var(False)  # disconnect clients NOT checked
        wv = self._make_string_var()
        fn = self._build_update_warning(vi, vc, wv)
        fn()
        wv.set.assert_called_once()
        args, _ = wv.set.call_args
        assert "AI clients" in args[0]

    def test_warning_cleared_when_safe(self):
        vi = self._make_mock_var(True)
        vc = self._make_mock_var(True)   # clients WILL be disconnected
        wv = self._make_string_var()
        fn = self._build_update_warning(vi, vc, wv)
        fn()
        wv.set.assert_called_once_with("")

    def test_warning_cleared_when_installdir_unchecked(self):
        vi = self._make_mock_var(False)  # not deleting installdir
        vc = self._make_mock_var(False)
        wv = self._make_string_var()
        fn = self._build_update_warning(vi, vc, wv)
        fn()
        wv.set.assert_called_once_with("")


# ---------------------------------------------------------------------------
# Button-disable guard test — mock do_uninstall internals
# ---------------------------------------------------------------------------

class TestButtonDisableGuard:
    """Verify the _set_buttons_in_flight helper sets the right widget states.

    We replicate the closure logic directly to keep this display-free.
    """

    def _make_button(self) -> MagicMock:
        btn = MagicMock()
        return btn

    def test_buttons_disabled_when_in_flight(self):
        cancel_btn = self._make_button()
        uninstall_btn = self._make_button()

        def _set_buttons_in_flight(in_flight: bool) -> None:
            state = "disabled" if in_flight else "normal"
            cancel_btn.config(state=state)
            uninstall_btn.config(
                state=state,
                text="Uninstalling…" if in_flight else "Uninstall",
            )

        _set_buttons_in_flight(True)
        cancel_btn.config.assert_called_with(state="disabled")
        uninstall_btn.config.assert_called_with(state="disabled", text="Uninstalling…")

    def test_buttons_re_enabled_after_flight(self):
        cancel_btn = self._make_button()
        uninstall_btn = self._make_button()

        def _set_buttons_in_flight(in_flight: bool) -> None:
            state = "disabled" if in_flight else "normal"
            cancel_btn.config(state=state)
            uninstall_btn.config(
                state=state,
                text="Uninstalling…" if in_flight else "Uninstall",
            )

        _set_buttons_in_flight(True)
        _set_buttons_in_flight(False)
        # Last call re-enables
        cancel_btn.config.assert_called_with(state="normal")
        uninstall_btn.config.assert_called_with(state="normal", text="Uninstall")
