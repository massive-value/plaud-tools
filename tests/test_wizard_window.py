"""Tests for WizardWindow._do failure/success readability (§6.2).

Before this fix, a connect/disconnect failure crammed the exception straight
into the row's 12-char-wide button (``btn.configure(text=f"Failed: {exc}")``)
-- unreadable, and the button was left permanently stuck in that state
instead of recovering to a usable Connect/Disconnect label. Failures now
render in the shared help area below the client rows (in red), and the
button is restored via ``_render()``.

All tests are display-free (no real Tk window) -- ``WizardWindow`` is built
via ``__new__`` with MagicMock stand-ins for every tkinter object, following
the precedent in test_login_window.py / test_homewindow_setup_failures.py.
"""

from __future__ import annotations

from unittest.mock import MagicMock

from plaud_tools.tray.windows import wizard as wizard_mod
from plaud_tools.tray.windows.wizard import WizardWindow


def _make_wizard(cid: str = "claude-desktop") -> WizardWindow:
    wiz = WizardWindow.__new__(WizardWindow)
    wiz._root = MagicMock()
    wiz._on_done = MagicMock()
    wiz._win = MagicMock()
    wiz._help_var = MagicMock()
    wiz._help_label = MagicMock()
    wiz._row_widgets = {
        cid: {
            "badge": MagicMock(),
            "badge_var": MagicMock(),
            "btn": MagicMock(),
        }
    }
    return wiz


class TestFailureIsReadable:
    def test_failure_does_not_cram_exception_into_button_text(self, monkeypatch):
        wiz = _make_wizard()
        monkeypatch.setattr(wizard_mod, "_mcp_exe", lambda: "mcp.exe")
        monkeypatch.setattr(wizard_mod, "status_all", lambda mcp: {"claude-desktop": "not-connected"})
        monkeypatch.setattr(
            wizard_mod,
            "connect",
            MagicMock(side_effect=RuntimeError("config file is locked by another process")),
        )

        wiz._do("claude-desktop", "connect", "mcp.exe")

        btn = wiz._row_widgets["claude-desktop"]["btn"]
        for call in btn.configure.call_args_list:
            text = call.kwargs.get("text", "")
            assert "Failed:" not in text
            assert "config file is locked" not in text

    def test_failure_message_appears_in_help_area(self, monkeypatch):
        wiz = _make_wizard()
        monkeypatch.setattr(wizard_mod, "_mcp_exe", lambda: "mcp.exe")
        monkeypatch.setattr(wizard_mod, "status_all", lambda mcp: {"claude-desktop": "not-connected"})
        monkeypatch.setattr(wizard_mod, "connect", MagicMock(side_effect=RuntimeError("permission denied")))

        wiz._do("claude-desktop", "connect", "mcp.exe")

        wiz._help_var.set.assert_called_once()
        (message,), _ = wiz._help_var.set.call_args
        assert "permission denied" in message
        assert "Claude Desktop" in message

    def test_failure_colours_help_label_red(self, monkeypatch):
        wiz = _make_wizard()
        monkeypatch.setattr(wizard_mod, "_mcp_exe", lambda: "mcp.exe")
        monkeypatch.setattr(wizard_mod, "status_all", lambda mcp: {"claude-desktop": "not-connected"})
        monkeypatch.setattr(wizard_mod, "connect", MagicMock(side_effect=RuntimeError("boom")))

        wiz._do("claude-desktop", "connect", "mcp.exe")

        wiz._help_label.configure.assert_called_with(foreground="#c0392b")

    def test_failure_restores_button_via_render(self, monkeypatch):
        """The button must not stay stuck on 'Connecting…' after a failure --
        _render() re-applies the real (unchanged) status."""
        wiz = _make_wizard()
        monkeypatch.setattr(wizard_mod, "_mcp_exe", lambda: "mcp.exe")
        monkeypatch.setattr(wizard_mod, "status_all", lambda mcp: {"claude-desktop": "not-connected"})
        monkeypatch.setattr(wizard_mod, "connect", MagicMock(side_effect=RuntimeError("boom")))

        wiz._do("claude-desktop", "connect", "mcp.exe")

        btn = wiz._row_widgets["claude-desktop"]["btn"]
        last_call = btn.configure.call_args_list[-1]
        assert last_call.kwargs.get("text") == "Connect"
        assert last_call.kwargs.get("state") == "normal"

    def test_on_done_not_called_on_failure(self, monkeypatch):
        wiz = _make_wizard()
        monkeypatch.setattr(wizard_mod, "_mcp_exe", lambda: "mcp.exe")
        monkeypatch.setattr(wizard_mod, "status_all", lambda mcp: {"claude-desktop": "not-connected"})
        monkeypatch.setattr(wizard_mod, "connect", MagicMock(side_effect=RuntimeError("boom")))

        wiz._do("claude-desktop", "connect", "mcp.exe")

        wiz._on_done.assert_not_called()


class TestSuccessStillWorks:
    def test_connect_success_shows_green_confirmation(self, monkeypatch):
        wiz = _make_wizard()
        monkeypatch.setattr(wizard_mod, "_mcp_exe", lambda: "mcp.exe")
        monkeypatch.setattr(wizard_mod, "status_all", lambda mcp: {"claude-desktop": "connected"})
        monkeypatch.setattr(wizard_mod, "connect", MagicMock())

        wiz._do("claude-desktop", "connect", "mcp.exe")

        wiz._help_label.configure.assert_called_with(foreground="#15803d")
        (message,), _ = wiz._help_var.set.call_args
        assert "Connected Claude Desktop" in message
        wiz._on_done.assert_called_once()

    def test_disconnect_success_clears_help_text(self, monkeypatch):
        wiz = _make_wizard()
        monkeypatch.setattr(wizard_mod, "_mcp_exe", lambda: "mcp.exe")
        monkeypatch.setattr(wizard_mod, "status_all", lambda mcp: {"claude-desktop": "not-connected"})
        monkeypatch.setattr(wizard_mod, "disconnect", MagicMock())

        wiz._do("claude-desktop", "disconnect", "mcp.exe")

        wiz._help_var.set.assert_called_once_with("")
        wiz._on_done.assert_called_once()
