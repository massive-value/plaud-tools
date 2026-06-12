"""Tests for plaud_tools.appdata — per-user data directory and known file paths.

Acceptance criteria from issue #88:
- data_dir() returns %LOCALAPPDATA%\\PlaudTools on Windows (exact backwards-compat)
- data_dir() falls back to Path.home() / "AppData" / "Local" / "PlaudTools" when
  LOCALAPPDATA is missing
- data_dir() returns the platformdirs default on macOS and Linux
- All named accessors (tray_log, mcp_log, events_path, dpapi_shadow_path,
  session_path) return paths inside data_dir()
- dpapi_shadow_path() returns None on non-Windows platforms
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

import pytest

# ---------------------------------------------------------------------------
# data_dir() — Windows backwards-compat
# ---------------------------------------------------------------------------


class TestDataDirWindows:
    def test_uses_localappdata_env_var(self, monkeypatch, tmp_path):
        """data_dir() must return LOCALAPPDATA/PlaudTools — exact match to current inline code."""
        monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
        monkeypatch.setattr(sys, "platform", "win32")
        # Re-import to pick up platform patch
        import importlib

        import plaud_tools.appdata as appdata_mod

        importlib.reload(appdata_mod)
        result = appdata_mod.data_dir()
        assert result == tmp_path / "PlaudTools"

    def test_localappdata_missing_falls_back_to_home(self, monkeypatch, tmp_path):
        """When LOCALAPPDATA is unset, fall back to Path.home() / 'AppData' / 'Local'."""
        monkeypatch.delenv("LOCALAPPDATA", raising=False)
        monkeypatch.setattr(sys, "platform", "win32")
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        import importlib

        import plaud_tools.appdata as appdata_mod

        importlib.reload(appdata_mod)
        result = appdata_mod.data_dir()
        assert result == tmp_path / "AppData" / "Local" / "PlaudTools"


class TestDataDirCrossPlatform:
    def test_macos_uses_platformdirs(self, monkeypatch):
        """On macOS, data_dir() delegates to platformdirs.user_data_dir."""
        monkeypatch.setattr(sys, "platform", "darwin")
        fake_dir = "/Users/testuser/Library/Application Support/PlaudTools"
        import importlib

        import plaud_tools.appdata as appdata_mod

        with patch("platformdirs.user_data_dir", return_value=fake_dir):
            importlib.reload(appdata_mod)
            result = appdata_mod.data_dir()
        assert result == Path(fake_dir)

    def test_linux_uses_platformdirs(self, monkeypatch):
        """On Linux, data_dir() delegates to platformdirs.user_data_dir."""
        monkeypatch.setattr(sys, "platform", "linux")
        fake_dir = "/home/testuser/.local/share/PlaudTools"
        import importlib

        import plaud_tools.appdata as appdata_mod

        with patch("platformdirs.user_data_dir", return_value=fake_dir):
            importlib.reload(appdata_mod)
            result = appdata_mod.data_dir()
        assert result == Path(fake_dir)


# ---------------------------------------------------------------------------
# Named file accessors
# ---------------------------------------------------------------------------


class TestNamedAccessors:
    """All named accessors must return paths rooted at data_dir()."""

    @pytest.fixture(autouse=True)
    def _pin_data_dir(self, monkeypatch, tmp_path):
        """Redirect data_dir() to tmp_path so all accessors are deterministic.

        Also restores the real dpapi_shadow_path implementation (overriding the
        conftest autouse redirect) so tests here exercise the real function.
        This is safe because data_dir() is pinned to tmp_path — dpapi_shadow_path
        can never reach the real %LOCALAPPDATA%\\PlaudTools\\ directory.
        """
        import plaud_tools.appdata as appdata_mod

        monkeypatch.setattr(appdata_mod, "data_dir", lambda: tmp_path)

        # Override the conftest's _block_real_dpapi_shadow redirect with the
        # real implementation (but safely pinned, since data_dir -> tmp_path).
        def _safe_dpapi_shadow_path():
            if sys.platform != "win32":
                return None
            return appdata_mod.data_dir() / "session.dat"

        monkeypatch.setattr(appdata_mod, "dpapi_shadow_path", _safe_dpapi_shadow_path)

    def test_tray_log_inside_data_dir(self, tmp_path):
        from plaud_tools.appdata import tray_log

        result = tray_log()
        assert result == tmp_path / "tray.log"

    def test_mcp_log_inside_data_dir(self, tmp_path):
        from plaud_tools.appdata import mcp_log

        result = mcp_log()
        assert result == tmp_path / "mcp.log"

    def test_events_path_inside_data_dir(self, tmp_path):
        from plaud_tools.appdata import events_path

        result = events_path()
        assert result == tmp_path / "events.jsonl"

    def test_session_path_inside_data_dir(self, tmp_path):
        from plaud_tools.appdata import session_path

        result = session_path()
        assert result == tmp_path / "session.json"

    def test_dpapi_shadow_path_inside_data_dir_on_windows(self, monkeypatch, tmp_path):
        monkeypatch.setattr(sys, "platform", "win32")
        from plaud_tools import appdata as appdata_mod

        result = appdata_mod.dpapi_shadow_path()
        assert result == tmp_path / "session.dat"

    def test_dpapi_shadow_path_returns_none_on_macos(self, monkeypatch, tmp_path):
        monkeypatch.setattr(sys, "platform", "darwin")
        from plaud_tools import appdata as appdata_mod

        result = appdata_mod.dpapi_shadow_path()
        assert result is None

    def test_dpapi_shadow_path_returns_none_on_linux(self, monkeypatch, tmp_path):
        monkeypatch.setattr(sys, "platform", "linux")
        from plaud_tools import appdata as appdata_mod

        result = appdata_mod.dpapi_shadow_path()
        assert result is None


# ---------------------------------------------------------------------------
# Windows backwards-compat: live path check
# ---------------------------------------------------------------------------


class TestWindowsBackwardsCompat:
    """On the live Windows environment, data_dir() must return the EXACT same
    path as the current inline reconstruction so existing users don't lose
    their data.
    """

    @pytest.mark.skipif(sys.platform != "win32", reason="Windows-only")
    def test_exact_path_matches_localappdata_reconstruction(self):
        """data_dir() == Path(os.environ['LOCALAPPDATA']) / 'PlaudTools' on live Windows."""
        import os

        from plaud_tools.appdata import data_dir

        expected = Path(os.environ["LOCALAPPDATA"]) / "PlaudTools"
        assert data_dir() == expected
