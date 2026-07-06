"""Tests for HomeWindow's session-label warn colouring (§6.1).

The session label text (produced by ``TrayApp._session_label``, see
test_tray_expiry_ux.py) now says "expires" instead of "valid" once a token
is within the tray warning window or the require() refuse buffer.
``HomeWindow._refresh_session`` colours the label to match instead of always
rendering it in the same neutral/bold style regardless of urgency.

Display-free -- widgets are MagicMocks, following the precedent in
test_homewindow_setup_failures.py.
"""

from __future__ import annotations

from unittest.mock import MagicMock

from plaud_tools.tray_app import HomeWindow


def _make_home_window(session_label: str) -> HomeWindow:
    hw = HomeWindow(
        root=MagicMock(),
        on_test_connection=MagicMock(),
        on_check_for_update=MagicMock(),
        on_open_update=MagicMock(),
        on_open_wizard=MagicMock(),
        on_sign_out=MagicMock(),
        on_open_uninstall=MagicMock(),
        on_repair_setup=None,
        get_session_label=lambda: session_label,
        get_update_info=lambda: None,
        get_env_status=lambda: None,
    )
    hw._session_var = MagicMock()
    hw._session_label_widget = MagicMock()
    hw._session_label_widget.winfo_exists.return_value = True
    hw._session_label_default_fg = "SystemButtonText"
    return hw


class TestSessionLabelColour:
    def test_expiring_label_is_coloured_red(self):
        hw = _make_home_window("Signed in as test@example.com. Token expires in 5 days — sign in again soon.")

        hw._refresh_session()

        hw._session_label_widget.configure.assert_called_once_with(foreground="#c0392b")

    def test_healthy_label_uses_default_colour(self):
        hw = _make_home_window("Signed in as test@example.com. Token valid for 20 days.")

        hw._refresh_session()

        hw._session_label_widget.configure.assert_called_once_with(foreground="SystemButtonText")

    def test_not_signed_in_uses_default_colour(self):
        hw = _make_home_window("Not signed in.")

        hw._refresh_session()

        hw._session_label_widget.configure.assert_called_once_with(foreground="SystemButtonText")

    def test_sets_session_var_text(self):
        hw = _make_home_window("Signed in as test@example.com. Token valid for 20 days.")

        hw._refresh_session()

        hw._session_var.set.assert_called_once_with("Signed in as test@example.com. Token valid for 20 days.")

    def test_skips_configure_when_widget_destroyed(self):
        """Async-callback safety (#157 class): a closed window must not raise."""
        hw = _make_home_window("Signed in as test@example.com. Token expires in 1 days — sign in again soon.")
        hw._session_label_widget.winfo_exists.return_value = 0

        hw._refresh_session()  # must not raise

        hw._session_label_widget.configure.assert_not_called()

    def test_noop_when_session_var_not_built(self):
        hw = _make_home_window("Signed in as test@example.com. Token valid for 20 days.")
        hw._session_var = None

        hw._refresh_session()  # must not raise
