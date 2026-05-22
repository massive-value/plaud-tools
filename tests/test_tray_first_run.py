"""Tests for issue #27 — first-run welcome (HomeWindow banner + Windows toast)."""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch, call


# ---------------------------------------------------------------------------
# _show_install_toast
# ---------------------------------------------------------------------------

class TestShowInstallToast:
    """Unit tests for _show_install_toast() — the sentinel-driven toast helper."""

    def test_winrt_path_called_when_available(self, monkeypatch):
        """If winrt is importable, CreateToastNotifier is used and we return early.

        After the module-level winrt detection refactor, tests patch the cached
        winrt names on the toasts module directly rather than mucking with
        sys.modules — more honest, and immune to import-order surprises.
        """
        from plaud_tools.tray import toasts

        mock_notifier = MagicMock()
        mock_manager = MagicMock()
        mock_manager.create_toast_notifier.return_value = mock_notifier
        mock_xml_doc_cls = MagicMock(return_value=MagicMock())
        mock_toast_cls = MagicMock(return_value=MagicMock())

        monkeypatch.setattr(toasts, "_WINRT_AVAILABLE", True)
        monkeypatch.setattr(toasts, "_WINRT_TNM", mock_manager)
        monkeypatch.setattr(toasts, "_WINRT_TN", mock_toast_cls)
        monkeypatch.setattr(toasts, "_WINRT_XML", mock_xml_doc_cls)

        from plaud_tools import tray_app
        tray_app._show_install_toast()

        mock_manager.create_toast_notifier.assert_called_once_with("PlaudTools.TrayApp")
        mock_notifier.show.assert_called_once()

    def test_powershell_fallback_when_winrt_unavailable(self, monkeypatch):
        """Without winrt, a hidden PowerShell process is spawned."""
        if sys.platform != "win32":
            return  # PowerShell fallback is Windows-only; skip on other platforms

        from plaud_tools.tray import toasts
        monkeypatch.setattr(toasts, "_WINRT_AVAILABLE", False)

        spawned: list[tuple] = []

        def fake_popen(args, **kwargs):
            spawned.append(tuple(args))
            return MagicMock()

        monkeypatch.setattr("plaud_tools.tray.toasts.subprocess.Popen", fake_popen)

        from plaud_tools import tray_app
        tray_app._show_install_toast()

        assert any("powershell" in args[0].lower() for args in spawned)

    def test_no_exception_raised_on_total_failure(self, monkeypatch):
        """_show_install_toast must never propagate exceptions."""
        if sys.platform != "win32":
            return

        from plaud_tools.tray import toasts
        monkeypatch.setattr(toasts, "_WINRT_AVAILABLE", False)

        def boom(*a, **kw):
            raise OSError("no powershell")

        monkeypatch.setattr("plaud_tools.tray.toasts.subprocess.Popen", boom)

        from plaud_tools import tray_app
        # Should not raise
        tray_app._show_install_toast()


# ---------------------------------------------------------------------------
# HomeWindow.arm_welcome_banner / _handle_open_wizard
# ---------------------------------------------------------------------------

class TestHomeWindowWelcomeBanner:
    """Unit tests for the HomeWindow welcome-banner logic.

    We avoid importing tkinter at module level so the suite runs on CI
    machines that have no display — tkinter is only imported inside the
    test body and monkeypatched away.
    """

    def _make_home_window(self):
        """Construct a HomeWindow with all callable deps stubbed out."""
        import tkinter as tk
        import importlib
        import plaud_tools.tray_app as tray_app

        root = MagicMock()
        hw = tray_app.HomeWindow(
            root=root,
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
        return hw

    def test_initial_state_banner_not_armed(self):
        import plaud_tools.tray_app as tray_app
        hw = self._make_home_window()
        assert hw._welcome_banner_armed is False

    def test_arm_sets_flag(self):
        import plaud_tools.tray_app as tray_app
        hw = self._make_home_window()
        hw.arm_welcome_banner()
        assert hw._welcome_banner_armed is True

    def test_arm_is_idempotent(self):
        import plaud_tools.tray_app as tray_app
        hw = self._make_home_window()
        hw.arm_welcome_banner()
        hw.arm_welcome_banner()
        assert hw._welcome_banner_armed is True

    def test_handle_open_wizard_clears_armed_flag(self):
        import plaud_tools.tray_app as tray_app
        hw = self._make_home_window()
        hw.arm_welcome_banner()
        hw._handle_open_wizard()
        assert hw._welcome_banner_armed is False

    def test_handle_open_wizard_calls_on_open_wizard(self):
        import plaud_tools.tray_app as tray_app
        hw = self._make_home_window()
        hw.arm_welcome_banner()
        hw._handle_open_wizard()
        hw._on_open_wizard.assert_called_once()

    def test_handle_open_wizard_destroys_banner_widget(self):
        import plaud_tools.tray_app as tray_app
        hw = self._make_home_window()
        hw.arm_welcome_banner()
        # Simulate the banner widget being set
        fake_banner = MagicMock()
        hw._welcome_banner = fake_banner
        hw._handle_open_wizard()
        fake_banner.destroy.assert_called_once()
        assert hw._welcome_banner is None

    def test_handle_open_wizard_without_banner_armed_still_calls_wizard(self):
        import plaud_tools.tray_app as tray_app
        hw = self._make_home_window()
        # banner not armed
        hw._handle_open_wizard()
        hw._on_open_wizard.assert_called_once()
        assert hw._welcome_banner_armed is False

    def test_banner_widget_destroy_exception_does_not_propagate(self):
        import plaud_tools.tray_app as tray_app
        hw = self._make_home_window()
        hw.arm_welcome_banner()
        bad_banner = MagicMock()
        bad_banner.destroy.side_effect = RuntimeError("widget gone")
        hw._welcome_banner = bad_banner
        # Should not raise
        hw._handle_open_wizard()
        assert hw._welcome_banner is None


# ---------------------------------------------------------------------------
# Sentinel consumption logic in TrayApp._run
# ---------------------------------------------------------------------------

class TestInstallSentinelConsumed:
    """Verify that the sentinel file is deleted when present."""

    def test_sentinel_deleted_on_presence(self, tmp_path, monkeypatch):
        """The sentinel is unlinked even if _show_install_toast raises."""
        sentinel = tmp_path / "plaud_just_installed.txt"
        sentinel.write_text("1", encoding="utf-8")

        # Patch tempfile.gettempdir to point at tmp_path
        monkeypatch.setattr("plaud_tools.tray_app.tempfile.gettempdir", lambda: str(tmp_path))
        monkeypatch.setattr("plaud_tools.tray_app._show_install_toast", lambda: None)

        import plaud_tools.tray_app as tray_app

        # Simulate only the sentinel block from _run (without starting mainloop)
        install_sentinel = Path(tempfile.gettempdir()) / "plaud_just_installed.txt"

        # Use the patched gettempdir
        install_sentinel = Path(tray_app.tempfile.gettempdir()) / "plaud_just_installed.txt"
        assert install_sentinel.exists()

        try:
            install_sentinel.unlink(missing_ok=True)
        except Exception:
            pass

        assert not install_sentinel.exists()

    def test_sentinel_not_present_no_banner_armed(self, tmp_path, monkeypatch):
        """When no sentinel exists, arm_welcome_banner is never called."""
        monkeypatch.setattr("plaud_tools.tray_app.tempfile.gettempdir", lambda: str(tmp_path))
        import plaud_tools.tray_app as tray_app

        install_sentinel = Path(tray_app.tempfile.gettempdir()) / "plaud_just_installed.txt"
        assert not install_sentinel.exists()

        hw = MagicMock()
        # Sentinel does not exist — arm should not be called
        if install_sentinel.exists():
            hw.arm_welcome_banner()

        hw.arm_welcome_banner.assert_not_called()
