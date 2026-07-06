"""Tests for the tray environment verification and regex anchoring (issue #23)."""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# We import from plaud_tools.tray.setup directly; conftest.py already stubs
# pystray / PIL.
# ---------------------------------------------------------------------------
from plaud_tools.tray.setup import (
    APP_NAME,
    EnvStatus,
    _check_cli_path,
    _check_ps_completions,
    _install_completions_dir,
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
    """Verify the stale-sourcing regex is anchored to the running install directory.

    After the layout.py migration, _install_completions_dir() returns None in
    dev/test mode (no frozen bundle).  These tests simulate a frozen bundle by
    monkeypatching sys.frozen and sys.executable so the regex anchors to a real
    path derived from sys.executable (not a hardcoded canonical path).
    """

    def _make_line(self, path: str) -> str:
        return f'. "{path}"'

    def _setup_frozen_env(self, monkeypatch, tmp_path: Path) -> Path:
        """Simulate a frozen bundle and return the expected completions path."""
        install_root = tmp_path / "PlaudTestInstall"
        cli_dir = install_root / "cli"
        cli_dir.mkdir(parents=True)
        fake_exe = cli_dir / "plaud-tools.exe"
        fake_exe.touch()
        monkeypatch.setattr(sys, "frozen", True, raising=False)
        monkeypatch.setattr(sys, "executable", str(fake_exe))
        return install_root / "completions"

    def test_matches_current_install_path(self, monkeypatch, tmp_path):
        completions = self._setup_frozen_env(monkeypatch, tmp_path)
        line = self._make_line(str(completions / "plaud-tools.ps1"))
        result = _stale_sourcing_re()
        assert result is not None
        assert result.match(line.strip()) is not None

    def test_matches_old_name_in_install_dir(self, monkeypatch, tmp_path):
        """Stale lines using the old plaud.ps1 name should also be caught."""
        completions = self._setup_frozen_env(monkeypatch, tmp_path)
        line = self._make_line(str(completions / "plaud.ps1"))
        result = _stale_sourcing_re()
        assert result is not None
        assert result.match(line.strip()) is not None

    def test_does_not_match_different_install_dir(self, monkeypatch, tmp_path):
        """Lines pointing at a different completions folder must NOT be removed."""
        self._setup_frozen_env(monkeypatch, tmp_path)
        other_dir = "C:\\Users\\user\\Documents\\completions\\plaud-tools.ps1"
        line = self._make_line(other_dir)
        result = _stale_sourcing_re()
        assert result is not None
        assert result.match(line.strip()) is None

    def test_does_not_match_unrelated_script(self, monkeypatch, tmp_path):
        """A user's own completions script should never be matched."""
        self._setup_frozen_env(monkeypatch, tmp_path)
        line = self._make_line("C:\\Users\\user\\scripts\\my-completions\\plaud-helper.ps1")
        result = _stale_sourcing_re()
        assert result is not None
        assert result.match(line.strip()) is None

    def test_case_insensitive(self, monkeypatch, tmp_path):
        completions = self._setup_frozen_env(monkeypatch, tmp_path)
        line = self._make_line(str(completions / "plaud-tools.ps1").upper())
        result = _stale_sourcing_re()
        assert result is not None
        assert result.match(line.strip()) is not None

    def test_returns_none_in_dev_mode(self):
        """In dev/pip mode (not frozen), _stale_sourcing_re() returns None."""
        # In test mode sys.frozen is not set, so the function should return None.
        assert _stale_sourcing_re() is None

    def test_install_completions_dir_returns_none_in_dev_mode(self):
        """In dev/pip mode (not frozen), _install_completions_dir() returns None."""
        assert _install_completions_dir() is None


# ---------------------------------------------------------------------------
# _verify_env — non-frozen (dev) always reports all-ok
# ---------------------------------------------------------------------------


class TestVerifyEnvDevMode:
    """In dev mode (not frozen), _cli_dir and _completions_dir return None,
    so path_ok and completions_ok must be True (nothing to verify)."""

    def test_verify_env_dev_mode_all_ok(self):
        # sys.frozen is not set in test mode → _cli_dir() returns None
        with patch("plaud_tools.tray.setup._autostart_enabled", return_value=True):
            status = _verify_env()
        assert status.path_ok is True
        assert status.completions_ok is True

    def test_verify_env_dev_mode_autostart_missing(self):
        with patch("plaud_tools.tray.setup._autostart_enabled", return_value=False):
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
        with (
            patch("plaud_tools.tray.setup._autostart_enabled", return_value=False),
            patch("plaud_tools.tray.setup._autostart_opted_out", return_value=True),
        ):
            status = _verify_env()
        assert status.autostart_ok is True, (
            "Opt-out marker must be treated as a deliberate user choice, not "
            "a missing setup that the auto-heal pass should fight."
        )
        assert status.all_ok is True

    def test_verify_env_missing_when_neither_enabled_nor_opted_out(self):
        with (
            patch("plaud_tools.tray.setup._autostart_enabled", return_value=False),
            patch("plaud_tools.tray.setup._autostart_opted_out", return_value=False),
        ):
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
        # On non-Windows CI we simulate win32 by patching sys.platform and
        # injecting a fake winreg module so the platform guard in
        # _autostart_opt_out_marker_path / _set_autostart sees "win32".
        if sys.platform == "win32":
            monkeypatch.setattr(
                "winreg.OpenKey",
                MagicMock(return_value=MagicMock(__enter__=lambda s: s, __exit__=lambda *a: False)),
            )
            monkeypatch.setattr("winreg.SetValueEx", MagicMock())
            monkeypatch.setattr("winreg.DeleteValue", MagicMock())
        else:
            import types

            mock_winreg = types.ModuleType("winreg")
            mock_winreg.HKEY_CURRENT_USER = 0x80000001  # type: ignore[attr-defined]
            mock_winreg.KEY_SET_VALUE = 0x0002  # type: ignore[attr-defined]
            mock_winreg.REG_SZ = 1  # type: ignore[attr-defined]
            mock_winreg.OpenKey = MagicMock(  # type: ignore[attr-defined]
                return_value=MagicMock(__enter__=lambda s: s, __exit__=lambda *a: False)
            )
            mock_winreg.SetValueEx = MagicMock()  # type: ignore[attr-defined]
            mock_winreg.DeleteValue = MagicMock()  # type: ignore[attr-defined]
            monkeypatch.setitem(sys.modules, "winreg", mock_winreg)
            monkeypatch.setattr(setup_mod.sys, "platform", "win32", raising=False)

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

        with (
            patch("plaud_tools.tray.background._setup_cli_path") as cli,
            patch("plaud_tools.tray.background._setup_ps_completions") as compl,
            patch("plaud_tools.tray.background._set_autostart") as auto,
        ):
            app._auto_repair_env(status)

        cli.assert_not_called()
        compl.assert_not_called()
        auto.assert_not_called()

    def test_auto_repair_restores_all_three_when_frozen(self, monkeypatch):
        monkeypatch.setattr(sys, "frozen", True, raising=False)
        app = self._make_app()
        status = EnvStatus(path_ok=False, completions_ok=False, autostart_ok=False)

        with (
            patch("plaud_tools.tray.background._setup_cli_path") as cli,
            patch("plaud_tools.tray.background._setup_ps_completions") as compl,
            patch("plaud_tools.tray.background._set_autostart") as auto,
        ):
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

        with (
            patch("plaud_tools.tray.background._setup_cli_path") as cli,
            patch("plaud_tools.tray.background._setup_ps_completions") as compl,
            patch("plaud_tools.tray.background._set_autostart") as auto,
        ):
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

        with (
            patch("plaud_tools.tray.background._verify_env", side_effect=lambda: next(verify_results)),
            patch("plaud_tools.tray.background._setup_cli_path"),
            patch("plaud_tools.tray.background._set_autostart"),
        ):
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
        with patch("plaud_tools.tray.setup._cli_dir", return_value=cli):
            with patch("winreg.OpenKey") as mock_key:
                mock_key.return_value.__enter__ = lambda s: s
                mock_key.return_value.__exit__ = MagicMock(return_value=False)
                with patch("winreg.QueryValueEx", return_value=(str(cli), winreg.REG_EXPAND_SZ)):
                    assert _check_cli_path() is True

    @pytest.mark.skipif(sys.platform != "win32", reason="winreg only on Windows")
    def test_path_absent(self, tmp_path):
        import winreg

        cli = tmp_path / "cli"
        with patch("plaud_tools.tray.setup._cli_dir", return_value=cli):
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

        with patch("plaud_tools.tray.setup._completions_dir", return_value=ps1.parent):
            with patch("plaud_tools.tray.setup.Path") as mock_path_cls:  # noqa: F841
                # We only need to patch the home() call inside _check_ps_completions
                # to point at our tmp tree; easier to patch at a higher level.
                pass
        # Direct test: patch the profile list
        with patch("plaud_tools.tray.setup._completions_dir", return_value=ps1.parent):
            import plaud_tools.tray.setup as ta

            orig = ta._check_ps_completions  # noqa: F841  # captured for reference only
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
        with patch("plaud_tools.tray.setup._completions_dir", return_value=ps1.parent):
            import plaud_tools.tray.setup as ta

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
        assert "$existing -ne $exePathQuoted" in script_text

    def test_autostart_value_is_quoted(self, script_text: str):
        """The Run-key value must be double-quoted (#160): an unquoted spaced
        install path (a Windows username with a space under %LOCALAPPDATA%)
        is split into multiple arguments by the Run-key launcher, and an
        unquoted spaced path also opens a PATH-hijacking window.
        """
        assert "$exePathQuoted = '\"' + $exePath + '\"'" in script_text
        assert "-Value $exePathQuoted" in script_text

    def test_installed_newer_than_latest_branch_exists(self, script_text: str):
        """#159: an installed>latest branch must exist and must NOT fall
        through to the -Force wipe branch (which would silently downgrade a
        dev/pre-release build to the older published release).
        """
        assert "$installedVerNum -gt $latestVerNum -and -not $Force" in script_text

    def test_update_available_message_mentions_repair(self, script_text: str):
        """#159: stuck <=0.3.3 users (whose in-app updater predates the fix)
        need the -Repair escape hatch surfaced, not just "use the tray"."""
        assert "-Repair" in script_text
        # The mention must live near the "update available" guidance, not just
        # in the top-of-file usage comment.
        assert "re-run this installer with -Repair" in script_text


# ---------------------------------------------------------------------------
# install.ps1 — behavioural tests via pwsh (PATH array bug, #141)
# ---------------------------------------------------------------------------

_PATH_APPEND_PS1 = r"""
param([string]$CurrentPath, [string]$CliDir)
$ErrorActionPreference = 'Stop'
$parts = @(($CurrentPath -split ';') | Where-Object { $_ -ne '' } | ForEach-Object { $_.Trim() })
if ($parts -notcontains $CliDir) {
    $newPath = ($parts + $CliDir) -join ';'
} else {
    $newPath = $CurrentPath
}
Write-Host $newPath
"""


def _run_path_append(tmp_path: Path, current_path: str, cli_dir: str) -> str:
    harness = tmp_path / "path_append.ps1"
    harness.write_text(_PATH_APPEND_PS1, encoding="utf-8")
    result = subprocess.run(
        [
            "pwsh",
            "-NoProfile",
            "-NonInteractive",
            "-File",
            str(harness),
            "-CurrentPath",
            current_path,
            "-CliDir",
            cli_dir,
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, f"harness failed:\n{result.stdout}\n{result.stderr}"
    return result.stdout.strip()


@pytest.mark.skipif(shutil.which("pwsh") is None, reason="pwsh not available")
class TestInstallPs1PathArrayFix:
    """Reproduces #141: a single-entry PATH must not be corrupted.

    Before the ``@(...)`` fix, piping a single-item result through
    Where-Object/ForEach-Object collapsed it from an array to a bare string;
    ``$parts + $cliDir`` on two bare strings is then plain string
    concatenation (no separator), and the following ``-join`` operated on
    that single merged string -- silently destroying the sole existing PATH
    entry (observed against the real WindowsApps alias on a fresh profile).
    """

    def test_single_entry_path_appends_with_semicolon(self, tmp_path: Path):
        result = _run_path_append(
            tmp_path,
            current_path=r"C:\Users\test\AppData\Local\Microsoft\WindowsApps",
            cli_dir=r"C:\Users\test\AppData\Local\Programs\PlaudTools\cli",
        )
        assert result == (
            r"C:\Users\test\AppData\Local\Microsoft\WindowsApps;"
            r"C:\Users\test\AppData\Local\Programs\PlaudTools\cli"
        )
        # Regression witness: the corrupted output has no semicolon separator
        # and instead runs the two paths together.
        assert "WindowsAppsC:" not in result

    def test_empty_path_appends_without_leading_semicolon(self, tmp_path: Path):
        result = _run_path_append(tmp_path, current_path="", cli_dir=r"C:\Programs\PlaudTools\cli")
        assert result == r"C:\Programs\PlaudTools\cli"

    def test_multi_entry_path_still_appends_correctly(self, tmp_path: Path):
        """Multi-entry PATH was never broken (Where-Object preserves array-ness
        for >1 item) -- pinned here as a no-regression witness."""
        result = _run_path_append(
            tmp_path,
            current_path=r"C:\A;C:\B",
            cli_dir=r"C:\Programs\PlaudTools\cli",
        )
        assert result == r"C:\A;C:\B;C:\Programs\PlaudTools\cli"

    def test_already_present_is_not_duplicated(self, tmp_path: Path):
        result = _run_path_append(
            tmp_path,
            current_path=r"C:\Programs\PlaudTools\cli",
            cli_dir=r"C:\Programs\PlaudTools\cli",
        )
        assert result == r"C:\Programs\PlaudTools\cli"


# ---------------------------------------------------------------------------
# install.ps1 — behavioural test for the installed>latest branch (#159)
# ---------------------------------------------------------------------------

_VERSION_BRANCH_PS1 = r"""
param([string]$Installed, [string]$Latest, [switch]$Force)

function Get-NumericVersion {
    param([string]$v)
    $numeric = $v.TrimStart('v') -replace '-.*$', ''
    return [version]$numeric
}

$installedVerNum = Get-NumericVersion $Installed
$latestVerNum    = Get-NumericVersion $Latest

if ($installedVerNum -eq $latestVerNum -and -not $Force) {
    Write-Host "UP_TO_DATE"
} elseif ($installedVerNum -gt $latestVerNum -and -not $Force) {
    Write-Host "INSTALLED_NEWER_NOOP"
} elseif ($latestVerNum -gt $installedVerNum -and -not $Force) {
    Write-Host "UPDATE_AVAILABLE"
} else {
    Write-Host "WIPE_AND_REINSTALL"
}
"""


def _run_version_branch(tmp_path: Path, installed: str, latest: str, force: bool = False) -> str:
    harness = tmp_path / "version_branch.ps1"
    harness.write_text(_VERSION_BRANCH_PS1, encoding="utf-8")
    args = [
        "pwsh",
        "-NoProfile",
        "-NonInteractive",
        "-File",
        str(harness),
        "-Installed",
        installed,
        "-Latest",
        latest,
    ]
    if force:
        args.append("-Force")
    result = subprocess.run(args, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, f"harness failed:\n{result.stdout}\n{result.stderr}"
    return result.stdout.strip()


@pytest.mark.skipif(shutil.which("pwsh") is None, reason="pwsh not available")
class TestInstallPs1VersionBranch:
    """Reproduces #159: installed>latest must NOT fall into the -Force wipe
    branch (which would silently downgrade to the older published release).
    """

    def test_installed_newer_takes_noop_branch_not_wipe(self, tmp_path: Path):
        assert _run_version_branch(tmp_path, installed="0.7.0", latest="0.6.2") == "INSTALLED_NEWER_NOOP"

    def test_installed_newer_with_force_still_wipes(self, tmp_path: Path):
        """-Force must still win even when installed is ahead -- an explicit
        user request to reinstall is honored regardless of version."""
        assert (
            _run_version_branch(tmp_path, installed="0.7.0", latest="0.6.2", force=True)
            == "WIPE_AND_REINSTALL"
        )

    def test_equal_versions_still_up_to_date(self, tmp_path: Path):
        assert _run_version_branch(tmp_path, installed="0.6.2", latest="0.6.2") == "UP_TO_DATE"

    def test_latest_newer_still_update_available(self, tmp_path: Path):
        assert _run_version_branch(tmp_path, installed="0.6.0", latest="0.6.2") == "UPDATE_AVAILABLE"


# ---------------------------------------------------------------------------
# Autostart value quoting round-trip, Python side (#160)
# ---------------------------------------------------------------------------


class TestAutostartQuoting:
    """_set_autostart writes a quoted exe path; _autostart_enabled must strip
    the quotes back off before comparing, or every frozen install would
    permanently report autostart as "missing" and re-write the registry key
    on every launch.
    """

    def test_set_autostart_writes_quoted_value(self, tmp_path, monkeypatch):
        from plaud_tools.tray import setup as setup_mod

        fake_exe = tmp_path / "PlaudTools.exe"
        fake_exe.write_bytes(b"")
        monkeypatch.setattr(sys, "platform", "win32", raising=False)
        monkeypatch.setattr(sys, "executable", str(fake_exe))

        written: dict[str, object] = {}

        def fake_set_value_ex(key, name, reserved, reg_type, value):
            written["value"] = value

        import types

        mock_winreg = types.ModuleType("winreg")
        mock_winreg.HKEY_CURRENT_USER = 0x80000001  # type: ignore[attr-defined]
        mock_winreg.KEY_SET_VALUE = 0x0002  # type: ignore[attr-defined]
        mock_winreg.REG_SZ = 1  # type: ignore[attr-defined]
        mock_winreg.OpenKey = MagicMock(  # type: ignore[attr-defined]
            return_value=MagicMock(__enter__=lambda s: s, __exit__=lambda *a: False)
        )
        mock_winreg.SetValueEx = fake_set_value_ex  # type: ignore[attr-defined]
        monkeypatch.setitem(sys.modules, "winreg", mock_winreg)

        setup_mod._set_autostart(True)

        assert written["value"] == f'"{fake_exe}"'

    def test_autostart_enabled_strips_quotes_for_comparison(self, monkeypatch):
        from plaud_tools.tray import setup as setup_mod

        fake_exe = r"C:\Users\Jane Doe\AppData\Local\Programs\PlaudTools\PlaudTools.exe"
        monkeypatch.setattr(sys, "platform", "win32", raising=False)
        monkeypatch.setattr(sys, "executable", fake_exe)

        import types

        mock_winreg = types.ModuleType("winreg")
        mock_winreg.HKEY_CURRENT_USER = 0x80000001  # type: ignore[attr-defined]
        mock_winreg.OpenKey = MagicMock(  # type: ignore[attr-defined]
            return_value=MagicMock(__enter__=lambda s: s, __exit__=lambda *a: False)
        )
        mock_winreg.QueryValueEx = MagicMock(return_value=(f'"{fake_exe}"', 1))  # type: ignore[attr-defined]
        monkeypatch.setitem(sys.modules, "winreg", mock_winreg)

        assert setup_mod._autostart_enabled() is True


# ---------------------------------------------------------------------------
# Shared PowerShell-profile IO (#154)
# ---------------------------------------------------------------------------


class TestProfileIoHelpers:
    """_read_profile_text / _write_profile_text: encoding-tolerant read,
    BOM-preserving write. Every profile touch (setup, removal, the presence
    check) routes through these two so a fix lands once (#154).
    """

    def test_missing_file_reads_as_empty_no_bom(self, tmp_path):
        from plaud_tools.tray.setup import _read_profile_text

        content, had_bom = _read_profile_text(tmp_path / "does_not_exist.ps1")
        assert content == ""
        assert had_bom is False

    def test_bom_prefixed_file_is_detected_and_stripped(self, tmp_path):
        from plaud_tools.tray.setup import _read_profile_text

        profile = tmp_path / "profile.ps1"
        profile.write_bytes(b"\xef\xbb\xbf" + b"# hello\n")

        content, had_bom = _read_profile_text(profile)
        assert content == "# hello\n"
        assert had_bom is True

    def test_bom_less_file_reads_normally(self, tmp_path):
        from plaud_tools.tray.setup import _read_profile_text

        profile = tmp_path / "profile.ps1"
        # write_bytes avoids Path.write_text's platform newline translation
        # so the byte-for-byte content is exactly what _read_profile_text sees.
        profile.write_bytes(b"# hello\n")

        content, had_bom = _read_profile_text(profile)
        assert content == "# hello\n"
        assert had_bom is False

    def test_undecodable_file_returns_none_not_raise(self, tmp_path):
        """A legacy ANSI profile with non-UTF-8 bytes must not crash the
        caller: UnicodeDecodeError is a ValueError subclass, NOT an OSError,
        so it previously escaped every `except OSError:` guard (#154).
        """
        from plaud_tools.tray.setup import _read_profile_text

        profile = tmp_path / "profile.ps1"
        # 0xFF 0xFE is not valid UTF-8 (and not a UTF-8 BOM either).
        profile.write_bytes(b"\xff\xfe\x00\x01garbage")

        content, had_bom = _read_profile_text(profile)
        assert content is None
        assert had_bom is False

    def test_write_preserves_bom_when_original_had_one(self, tmp_path):
        from plaud_tools.tray.setup import _write_profile_text

        profile = tmp_path / "profile.ps1"
        _write_profile_text(profile, "# new content\n", had_bom=True)

        raw = profile.read_bytes()
        assert raw.startswith(b"\xef\xbb\xbf")

    def test_write_omits_bom_when_original_had_none(self, tmp_path):
        from plaud_tools.tray.setup import _write_profile_text

        profile = tmp_path / "profile.ps1"
        _write_profile_text(profile, "# new content\n", had_bom=False)

        raw = profile.read_bytes()
        assert not raw.startswith(b"\xef\xbb\xbf")

    def test_round_trip_preserves_bom_through_setup_ps_completions(self, tmp_path, monkeypatch):
        """End-to-end: a BOM'd profile keeps its BOM after _setup_ps_completions
        adds the sourcing line (previously silently stripped -- #154).
        """
        from plaud_tools.tray import setup as setup_mod

        completions_dir = tmp_path / "completions"
        completions_dir.mkdir()
        ps1 = completions_dir / "plaud-tools.ps1"
        ps1.write_text("# completions\n", encoding="utf-8")

        profile_dir = tmp_path / "home" / "Documents" / "PowerShell"
        profile_dir.mkdir(parents=True)
        profile = profile_dir / "Microsoft.PowerShell_profile.ps1"
        profile.write_bytes(b"\xef\xbb\xbf" + b"# my existing profile\n")

        monkeypatch.setattr(setup_mod, "_completions_dir", lambda: completions_dir)
        monkeypatch.setattr(setup_mod, "_stale_sourcing_re", lambda: None)
        monkeypatch.setattr(Path, "home", lambda: tmp_path / "home")

        setup_mod._setup_ps_completions()

        raw = profile.read_bytes()
        assert raw.startswith(b"\xef\xbb\xbf"), "BOM must survive the rewrite"
        assert str(ps1) in raw.decode("utf-8-sig")
