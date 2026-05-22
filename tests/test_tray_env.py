"""Tests for the tray environment verification and regex anchoring (issue #23)."""
from __future__ import annotations

import re
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# We import from tray_app directly; conftest.py already stubs pystray / PIL.
# ---------------------------------------------------------------------------
from plaud_tools.tray_app import (
    APP_NAME,
    EnvStatus,
    _check_cli_path,
    _check_ps_completions,
    _install_completions_dir,
    _install_dir,
    _stale_sourcing_re,
    _verify_env,
)


# ---------------------------------------------------------------------------
# EnvStatus helpers
# ---------------------------------------------------------------------------

class TestEnvStatus:
    def test_all_ok_when_all_true(self):
        s = EnvStatus(path_ok=True, completions_ok=True, autostart_ok=True)
        assert s.all_ok is True
        assert s.missing_labels() == []

    def test_all_ok_false_when_path_missing(self):
        s = EnvStatus(path_ok=False, completions_ok=True, autostart_ok=True)
        assert s.all_ok is False
        assert "PATH" in s.missing_labels()

    def test_all_ok_false_when_completions_missing(self):
        s = EnvStatus(path_ok=True, completions_ok=False, autostart_ok=True)
        assert s.all_ok is False
        assert "shell completions" in s.missing_labels()

    def test_all_ok_false_when_autostart_missing(self):
        s = EnvStatus(path_ok=True, completions_ok=True, autostart_ok=False)
        assert s.all_ok is False
        assert "autostart" in s.missing_labels()

    def test_missing_labels_all_three(self):
        s = EnvStatus(path_ok=False, completions_ok=False, autostart_ok=False)
        labels = s.missing_labels()
        assert labels == ["PATH", "shell completions", "autostart"]


# ---------------------------------------------------------------------------
# _stale_sourcing_re — must only match lines inside the install dir
# ---------------------------------------------------------------------------

class TestStaleSourceRe:
    """Verify the stale-sourcing regex is anchored to the install directory."""

    def _install_completions(self) -> str:
        return str(_install_completions_dir())

    def _make_line(self, path: str) -> str:
        return f'. "{path}"'

    def test_matches_current_install_path(self):
        completions = self._install_completions()
        line = self._make_line(str(Path(completions) / "plaud-tools.ps1"))
        assert _stale_sourcing_re().match(line.strip()) is not None

    def test_matches_old_name_in_install_dir(self):
        """Stale lines using the old plaud.ps1 name should also be caught."""
        completions = self._install_completions()
        line = self._make_line(str(Path(completions) / "plaud.ps1"))
        assert _stale_sourcing_re().match(line.strip()) is not None

    def test_does_not_match_different_install_dir(self):
        """Lines pointing at a different completions folder must NOT be removed."""
        other_dir = "C:\\Users\\user\\Documents\\completions\\plaud-tools.ps1"
        line = self._make_line(other_dir)
        assert _stale_sourcing_re().match(line.strip()) is None

    def test_does_not_match_unrelated_script(self):
        """A user's own completions script should never be matched."""
        line = self._make_line("C:\\Users\\user\\scripts\\my-completions\\plaud-helper.ps1")
        assert _stale_sourcing_re().match(line.strip()) is None

    def test_case_insensitive(self):
        completions = self._install_completions().upper()
        line = self._make_line(str(Path(completions) / "plaud-tools.ps1"))
        assert _stale_sourcing_re().match(line.strip()) is not None


# ---------------------------------------------------------------------------
# _verify_env — non-frozen (dev) always reports all-ok
# ---------------------------------------------------------------------------

class TestVerifyEnvDevMode:
    """In dev mode (not frozen), _cli_dir and _completions_dir return None,
    so path_ok and completions_ok must be True (nothing to verify)."""

    def test_verify_env_dev_mode_all_ok(self):
        # sys.frozen is not set in test mode → _cli_dir() returns None
        with patch("plaud_tools.tray_app._autostart_enabled", return_value=True):
            status = _verify_env()
        assert status.path_ok is True
        assert status.completions_ok is True

    def test_verify_env_dev_mode_autostart_missing(self):
        with patch("plaud_tools.tray_app._autostart_enabled", return_value=False):
            status = _verify_env()
        assert status.autostart_ok is False
        assert not status.all_ok


# ---------------------------------------------------------------------------
# Autostart opt-out — _set_autostart toggle ↔ _verify_env interaction
# ---------------------------------------------------------------------------

class TestAutostartOptOut:
    """Pins the auto-heal contract: a user who deliberately disabled autostart
    via the "Start with Windows" menu must not have it silently re-enabled on
    the next tray launch.  The marker file written by ``_set_autostart(False)``
    is what makes the opt-out persistent across launches.
    """

    def test_verify_env_treats_opt_out_marker_as_autostart_ok(self):
        with patch("plaud_tools.tray_app._autostart_enabled", return_value=False), \
             patch("plaud_tools.tray_app._autostart_opted_out", return_value=True):
            status = _verify_env()
        assert status.autostart_ok is True, (
            "Opt-out marker must be treated as a deliberate user choice, not "
            "a missing setup that the auto-heal pass should fight."
        )
        assert status.all_ok is True

    def test_verify_env_missing_when_neither_enabled_nor_opted_out(self):
        with patch("plaud_tools.tray_app._autostart_enabled", return_value=False), \
             patch("plaud_tools.tray_app._autostart_opted_out", return_value=False):
            status = _verify_env()
        assert status.autostart_ok is False
        assert "autostart" in status.missing_labels()

    def test_opt_out_marker_path_returns_none_in_dev_mode(self):
        """Dev/test runs are never frozen, so the marker has no canonical home
        and ``_autostart_opted_out`` always returns False — auto-heal cannot
        act on this anyway because it gates on ``sys.frozen``.
        """
        from plaud_tools.tray.setup import _autostart_opt_out_marker_path, _autostart_opted_out
        assert _autostart_opt_out_marker_path() is None
        assert _autostart_opted_out() is False

    def test_opt_out_marker_round_trip_when_frozen(self, tmp_path, monkeypatch):
        """In a frozen bundle, _set_autostart(False) drops the marker file and
        _set_autostart(True) clears it.  Mocks sys.frozen + sys.executable to
        simulate the bundle layout, and stubs out the winreg side-effects so
        the test runs cross-platform.
        """
        from plaud_tools.tray import setup as setup_mod

        fake_exe = tmp_path / "PlaudTools.exe"
        fake_exe.write_bytes(b"")
        monkeypatch.setattr(sys, "frozen", True, raising=False)
        monkeypatch.setattr(sys, "executable", str(fake_exe))
        # The marker side of the toggle is what we care about — winreg side
        # only matters on Windows and is exercised in other Windows-only tests.
        if sys.platform == "win32":
            monkeypatch.setattr("winreg.OpenKey", MagicMock(
                return_value=MagicMock(__enter__=lambda s: s, __exit__=lambda *a: False)))
            monkeypatch.setattr("winreg.SetValueEx", MagicMock())
            monkeypatch.setattr("winreg.DeleteValue", MagicMock())
        else:
            monkeypatch.setattr(setup_mod.sys, "platform", "linux", raising=False)

        marker = setup_mod._autostart_opt_out_marker_path()
        assert marker is not None
        assert not marker.exists()

        setup_mod._set_autostart(False)
        assert marker.exists(), "Disabling autostart must drop the opt-out marker"

        setup_mod._set_autostart(True)
        assert not marker.exists(), "Re-enabling autostart must clear the opt-out marker"


# ---------------------------------------------------------------------------
# Tray auto-heal — _run_verify_env + _auto_repair_env
# ---------------------------------------------------------------------------

class TestAutoHealEnv:
    """The setup-failure banner should be a last resort, not a daily greeting.
    Auto-heal restores PATH/completions/autostart silently in the frozen bundle
    context so the banner only surfaces when something genuinely cannot be
    fixed without user input.
    """

    def _make_app(self):
        """Minimal TrayApp stand-in with the attributes _run_verify_env touches."""
        from plaud_tools.tray.background import _BackgroundMixin

        class _StubApp(_BackgroundMixin):
            def __init__(self) -> None:
                self._root = None
                self._home_win = None
                self._env_status = None

        return _StubApp()

    def test_auto_repair_no_op_in_dev_mode(self, monkeypatch):
        """Dev builds never write to the user's PATH / Run key."""
        monkeypatch.setattr(sys, "frozen", False, raising=False)
        app = self._make_app()
        status = EnvStatus(path_ok=False, completions_ok=False, autostart_ok=False)

        with patch("plaud_tools.tray.background._setup_cli_path") as cli, \
             patch("plaud_tools.tray.background._setup_ps_completions") as compl, \
             patch("plaud_tools.tray.background._set_autostart") as auto:
            app._auto_repair_env(status)

        cli.assert_not_called()
        compl.assert_not_called()
        auto.assert_not_called()

    def test_auto_repair_restores_all_three_when_frozen(self, monkeypatch):
        monkeypatch.setattr(sys, "frozen", True, raising=False)
        app = self._make_app()
        status = EnvStatus(path_ok=False, completions_ok=False, autostart_ok=False)

        with patch("plaud_tools.tray.background._setup_cli_path") as cli, \
             patch("plaud_tools.tray.background._setup_ps_completions") as compl, \
             patch("plaud_tools.tray.background._set_autostart") as auto:
            app._auto_repair_env(status)

        cli.assert_called_once()
        compl.assert_called_once()
        auto.assert_called_once_with(True)

    def test_auto_repair_skips_what_is_already_ok(self, monkeypatch):
        """Only the missing slots trigger their respective helper.  In
        particular, when ``status.autostart_ok`` is True because of an opt-out
        marker, the auto-heal must not re-register autostart.
        """
        monkeypatch.setattr(sys, "frozen", True, raising=False)
        app = self._make_app()
        status = EnvStatus(path_ok=False, completions_ok=True, autostart_ok=True)

        with patch("plaud_tools.tray.background._setup_cli_path") as cli, \
             patch("plaud_tools.tray.background._setup_ps_completions") as compl, \
             patch("plaud_tools.tray.background._set_autostart") as auto:
            app._auto_repair_env(status)

        cli.assert_called_once()
        compl.assert_not_called()
        auto.assert_not_called()

    def test_run_verify_env_clears_banner_when_auto_repair_succeeds(self, monkeypatch):
        """End-to-end: status starts non-ok, auto-repair runs, second verify
        returns all-ok, and the banner never gets shown.
        """
        monkeypatch.setattr(sys, "frozen", True, raising=False)
        app = self._make_app()

        missing = EnvStatus(path_ok=False, completions_ok=True, autostart_ok=False)
        healthy = EnvStatus(path_ok=True, completions_ok=True, autostart_ok=True)
        verify_results = iter([missing, healthy])

        with patch("plaud_tools.tray.background._verify_env",
                   side_effect=lambda: next(verify_results)), \
             patch("plaud_tools.tray.background._setup_cli_path"), \
             patch("plaud_tools.tray.background._set_autostart"):
            app._run_verify_env()

        assert app._env_status is healthy
        assert app._env_status.all_ok is True


# ---------------------------------------------------------------------------
# _check_cli_path — registry-level PATH check (Windows mock)
# ---------------------------------------------------------------------------

class TestCheckCliPath:
    def test_returns_true_on_non_windows(self):
        with patch.object(sys, "platform", "linux"):
            assert _check_cli_path() is True

    @pytest.mark.skipif(sys.platform != "win32", reason="winreg only on Windows")
    def test_path_present(self, tmp_path):
        import winreg
        cli = tmp_path / "cli"
        # Fake frozen mode so _cli_dir returns a path
        with patch("plaud_tools.tray_app._cli_dir", return_value=cli):
            with patch("winreg.OpenKey") as mock_key:
                mock_key.return_value.__enter__ = lambda s: s
                mock_key.return_value.__exit__ = MagicMock(return_value=False)
                with patch("winreg.QueryValueEx", return_value=(str(cli), winreg.REG_EXPAND_SZ)):
                    assert _check_cli_path() is True

    @pytest.mark.skipif(sys.platform != "win32", reason="winreg only on Windows")
    def test_path_absent(self, tmp_path):
        import winreg
        cli = tmp_path / "cli"
        with patch("plaud_tools.tray_app._cli_dir", return_value=cli):
            with patch("winreg.OpenKey") as mock_key:
                mock_key.return_value.__enter__ = lambda s: s
                mock_key.return_value.__exit__ = MagicMock(return_value=False)
                with patch("winreg.QueryValueEx", return_value=("C:\\Other\\Stuff", winreg.REG_EXPAND_SZ)):
                    assert _check_cli_path() is False


# ---------------------------------------------------------------------------
# _check_ps_completions — profile-level check
# ---------------------------------------------------------------------------

class TestCheckPsCompletions:
    def test_returns_true_when_not_frozen(self):
        # _completions_dir() → None in dev mode
        assert _check_ps_completions() is True

    def test_found_in_one_profile(self, tmp_path):
        ps1 = tmp_path / "completions" / "plaud-tools.ps1"
        ps1.parent.mkdir()
        ps1.write_text("# completions\n")
        source_line = f'. "{ps1}"'
        profile = tmp_path / "profile.ps1"
        profile.write_text(source_line + "\n", encoding="utf-8")

        with patch("plaud_tools.tray_app._completions_dir", return_value=ps1.parent):
            with patch("plaud_tools.tray_app.Path") as mock_path_cls:
                # We only need to patch the home() call inside _check_ps_completions
                # to point at our tmp tree; easier to patch at a higher level.
                pass
        # Direct test: patch the profile list
        with patch("plaud_tools.tray_app._completions_dir", return_value=ps1.parent):
            import plaud_tools.tray_app as ta
            orig = ta._check_ps_completions
            # Monkey-patch profiles list via Path.home()
            with patch.object(Path, "home", return_value=tmp_path / "home"):
                docs = tmp_path / "home" / "Documents"
                docs.mkdir(parents=True, exist_ok=True)
                prof_dir = docs / "PowerShell"
                prof_dir.mkdir(parents=True, exist_ok=True)
                actual_profile = prof_dir / "Microsoft.PowerShell_profile.ps1"
                actual_profile.write_text(source_line + "\n", encoding="utf-8")
                result = ta._check_ps_completions()
            assert result is True

    def test_not_found_in_profiles(self, tmp_path):
        ps1 = tmp_path / "completions" / "plaud-tools.ps1"
        ps1.parent.mkdir()
        ps1.write_text("# completions\n")
        with patch("plaud_tools.tray_app._completions_dir", return_value=ps1.parent):
            import plaud_tools.tray_app as ta
            with patch.object(Path, "home", return_value=tmp_path / "home"):
                docs = tmp_path / "home" / "Documents"
                docs.mkdir(parents=True, exist_ok=True)
                (docs / "PowerShell").mkdir(parents=True, exist_ok=True)
                (docs / "PowerShell" / "Microsoft.PowerShell_profile.ps1").write_text(
                    "# no sourcing here\n", encoding="utf-8"
                )
                (docs / "WindowsPowerShell").mkdir(parents=True, exist_ok=True)
                (docs / "WindowsPowerShell" / "Microsoft.PowerShell_profile.ps1").write_text(
                    "# no sourcing here\n", encoding="utf-8"
                )
                result = ta._check_ps_completions()
            assert result is False


# ---------------------------------------------------------------------------
# install.ps1 content assertions (snapshot-style)
# ---------------------------------------------------------------------------

class TestInstallPs1:
    """Verify install.ps1 contains the expected idempotency patterns."""

    @pytest.fixture()
    def script_text(self) -> str:
        ps1 = Path(__file__).resolve().parents[1] / "scripts" / "install.ps1"
        return ps1.read_text(encoding="utf-8")

    def test_sets_up_path(self, script_text: str):
        assert "HKCU:\\Environment" in script_text
        assert "$cliDir" in script_text

    def test_sets_up_completions(self, script_text: str):
        assert "plaud-tools.ps1" in script_text
        assert "$sourceLine" in script_text

    def test_sets_up_autostart(self, script_text: str):
        assert "CurrentVersion\\Run" in script_text or "CurrentVersion/Run" in script_text
        assert "PlaudTools" in script_text

    def test_autostart_name_matches_python_app_name(self, script_text: str):
        """install.ps1's HKCU Run value name MUST match _AUTOSTART_NAME (= APP_NAME).

        Regression for the install.ps1 / Python mismatch where the script wrote
        'PlaudTools' (no space) but the tray read 'Plaud Tools' (with space),
        making _autostart_enabled() always report missing autostart after a
        fresh install.  We pin the script to use APP_NAME verbatim in a quoted
        form so a future rename in Python is caught by CI.
        """
        # The script should reference the exact APP_NAME literal in a quoted
        # form (single- or double-quoted) on the Set-ItemProperty / -Name path.
        single_quoted = f"'{APP_NAME}'"
        double_quoted = f'"{APP_NAME}"'
        assert single_quoted in script_text or double_quoted in script_text, (
            f"install.ps1 must reference the Python APP_NAME ({APP_NAME!r}) as a "
            f"quoted literal for the HKCU\\...\\Run value name.  Found neither "
            f"{single_quoted!r} nor {double_quoted!r} in the script."
        )

    def test_autostart_cleans_up_legacy_name(self, script_text: str):
        """install.ps1 must remove the stale 'PlaudTools' (no-space) Run value.

        Older revisions of the script wrote the wrong name; users who upgraded
        through the buggy version have two Run entries.  The script must strip
        the legacy one on every run so they get auto-cleaned the next time
        they reinstall.
        """
        assert "Remove-ItemProperty" in script_text
        # And the target of that Remove-ItemProperty must be the legacy name.
        assert "'PlaudTools'" in script_text

    def test_regex_anchored_to_install_dir(self, script_text: str):
        """The stale-sourcing pattern in install.ps1 must reference $completionsDir / $escapedDir."""
        assert "$escapedDir" in script_text or "$stalePattern" in script_text

    def test_five_steps(self, script_text: str):
        """install.ps1 now has 5 steps, not 4."""
        assert "[5/5]" in script_text
        assert "[4/5]" in script_text
        assert "[1/5]" in script_text

    def test_idempotent_path(self, script_text: str):
        """PATH update guard: only add if not already present."""
        assert "$parts -notcontains $cliDir" in script_text or "notcontains" in script_text

    def test_idempotent_autostart(self, script_text: str):
        """Autostart should only be set if value differs."""
        assert "$existing -ne $exePath" in script_text
