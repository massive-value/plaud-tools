"""Unit tests for tray_app lifecycle helpers.

Covers setup/teardown helpers for PATH, PowerShell completions, session files,
and log files.  A stub ``winreg`` module is injected so the suite runs on both
Windows and Linux CI.

Tests use ``tmp_path`` exclusively and never touch the user's real PATH,
profile, or session.

Also covers snapshot tests for the rendered PS1 dispatcher output, asserting
that rendered scripts reference only the supplied install directory and contain
no stray install-dir references.
"""

from __future__ import annotations

import sys
import types
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Helper: patch Path.home reliably across Python versions
# ---------------------------------------------------------------------------


def _patch_home(monkeypatch, tray_helpers, home_path: Path) -> Path:
    """Redirect ``Path.home()``, simulate a frozen bundle, and return the
    expected completions directory so callers can write stale sourcing lines
    that ``_stale_sourcing_re()`` will match.

    After the layout.py migration, ``_stale_sourcing_re()`` derives its anchor
    from ``sys.executable`` (via ``InstallLayout.detect()``), not from a
    hardcoded canonical path.  This helper therefore:
    1. Creates a fake bundle layout rooted at ``home_path/Programs/PlaudTools/``.
    2. Sets ``sys.frozen = True`` and ``sys.executable`` to point at the CLI exe
       in that fake layout, so ``InstallLayout.detect()`` produces
       ``install_root = home_path/Programs/PlaudTools/``.
    3. Returns ``home_path/Programs/PlaudTools/completions`` — the completions
       dir the regex will be anchored to.

    Any stale sourcing line pointing at that completions directory will be
    matched; lines elsewhere will not.

    Uses a dotted import path for ``pathlib.Path.home`` so monkeypatch saves
    and restores the classmethod descriptor correctly across Python 3.11–3.13.
    """
    monkeypatch.setattr("pathlib.Path.home", staticmethod(lambda: home_path))
    monkeypatch.setenv("LOCALAPPDATA", str(home_path))

    # Create the fake bundle layout so InstallLayout.detect() resolves correctly.
    install_root = home_path / "Programs" / "PlaudTools"
    cli_dir = install_root / "cli"
    cli_dir.mkdir(parents=True, exist_ok=True)
    fake_exe = cli_dir / "plaud-tools.exe"
    if not fake_exe.exists():
        fake_exe.touch()

    # Simulate frozen bundle: sys.executable points at the CLI exe.
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(fake_exe))

    return install_root / "completions"


# ---------------------------------------------------------------------------
# Helpers: build a minimal winreg stub so tests run on non-Windows CI
# ---------------------------------------------------------------------------


class _FakeRegKey:
    """Minimal in-memory winreg key stub."""

    def __init__(self, data: dict | None = None) -> None:
        self._data: dict[str, tuple[str, int]] = {}
        if data:
            for k, v in data.items():
                # store as (value, REG_EXPAND_SZ)
                self._data[k] = (v, 2)

    # context manager
    def __enter__(self):
        return self

    def __exit__(self, *a):
        pass


def _make_winreg_stub(initial_path: str = "") -> types.ModuleType:
    """Return a fake ``winreg`` module backed by an in-memory registry."""
    mod = types.ModuleType("winreg")
    mod.HKEY_CURRENT_USER = 0x80000001
    mod.KEY_QUERY_VALUE = 0x0001
    mod.KEY_SET_VALUE = 0x0002
    mod.REG_EXPAND_SZ = 2
    mod.REG_SZ = 1

    key = _FakeRegKey()
    if initial_path:
        key._data["Path"] = (initial_path, mod.REG_EXPAND_SZ)

    def _open_key(hive, subkey, reserved, access):
        return key

    def _query_value_ex(k, name):
        if name not in k._data:
            raise FileNotFoundError(name)
        return k._data[name]

    def _set_value_ex(k, name, reserved, reg_type, value):
        k._data[name] = (value, reg_type)

    mod.OpenKey = _open_key
    mod.QueryValueEx = _query_value_ex
    mod.SetValueEx = _set_value_ex

    # expose the backing key so tests can inspect it
    mod._key = key
    return mod


# ---------------------------------------------------------------------------
# Fixture: import tray_app helpers without running the tray (no display needed)
# ---------------------------------------------------------------------------


@pytest.fixture()
def tray_helpers():
    """Return the tray_app module with heavy GUI deps stubbed out."""
    stubs = {
        "pystray": MagicMock(),
        "PIL": MagicMock(),
        "PIL.Image": MagicMock(),
        "PIL.ImageDraw": MagicMock(),
        "tkinter": MagicMock(),
        "tkinter.ttk": MagicMock(),
    }
    with patch.dict(sys.modules, stubs):
        import plaud_tools.tray_app as tray_app

        yield tray_app


# ---------------------------------------------------------------------------
# _cli_dir / _completions_dir  (dev-mode → None)
# ---------------------------------------------------------------------------


class TestCliDir:
    def test_returns_none_when_not_frozen(self, tray_helpers):
        # In dev mode sys.frozen is not set, so _cli_dir returns None
        with patch.object(sys, "frozen", False, create=True):
            result = tray_helpers._cli_dir()
        assert result is None

    def test_returns_cli_under_exe_when_frozen(self, tmp_path, tray_helpers, monkeypatch):
        fake_exe = tmp_path / "PlaudTools.exe"
        fake_exe.touch()
        monkeypatch.setattr(sys, "frozen", True, raising=False)
        monkeypatch.setattr(sys, "executable", str(fake_exe))
        monkeypatch.setattr(sys, "platform", "win32")
        result = tray_helpers._cli_dir()
        assert result == tmp_path / "cli"


# ---------------------------------------------------------------------------
# _setup_cli_path
# ---------------------------------------------------------------------------


class TestSetupCliPath:
    """Tests for _setup_cli_path — idempotent PATH injection via fake winreg."""

    def _run_setup(self, tray_helpers, tmp_path, initial_path, monkeypatch):
        """Helper: freeze the process, point cli to tmp_path/cli, run _setup_cli_path."""
        cli_dir = tmp_path / "cli"
        cli_dir.mkdir(exist_ok=True)

        monkeypatch.setattr(sys, "frozen", True, raising=False)
        monkeypatch.setattr(sys, "executable", str(tmp_path / "PlaudTools.exe"), raising=False)
        monkeypatch.setattr(sys, "platform", "win32")

        # Patch _cli_dir so it returns our fake cli dir
        monkeypatch.setattr(tray_helpers, "_cli_dir", lambda: cli_dir)

        winreg_stub = _make_winreg_stub(initial_path)

        fake_ctypes = MagicMock()
        fake_ctypes.windll = MagicMock()
        fake_ctypes.windll.user32 = MagicMock()
        fake_ctypes.windll.user32.SendMessageTimeoutW.return_value = 0

        with patch.dict(sys.modules, {"winreg": winreg_stub, "ctypes": fake_ctypes}):
            tray_helpers._setup_cli_path()

        stored_path, _ = winreg_stub._key._data.get("Path", ("", 2))
        return stored_path, str(cli_dir)

    def test_adds_cli_to_empty_path(self, tray_helpers, tmp_path, monkeypatch):
        stored, cli = self._run_setup(tray_helpers, tmp_path, "", monkeypatch)
        assert cli in stored

    def test_adds_cli_to_existing_path(self, tray_helpers, tmp_path, monkeypatch):
        stored, cli = self._run_setup(tray_helpers, tmp_path, r"C:\Windows\system32", monkeypatch)
        assert cli in stored
        assert r"C:\Windows\system32" in stored

    def test_idempotent_when_already_present(self, tray_helpers, tmp_path, monkeypatch):
        cli_dir = tmp_path / "cli"
        cli_dir.mkdir()
        # First call
        stored1, cli = self._run_setup(tray_helpers, tmp_path, "", monkeypatch)
        assert stored1.count(cli) == 1
        # Second call — should not double-add
        monkeypatch.setattr(tray_helpers, "_cli_dir", lambda: cli_dir)
        winreg_stub = _make_winreg_stub(stored1)
        fake_ctypes = MagicMock()
        fake_ctypes.windll = MagicMock()
        fake_ctypes.windll.user32 = MagicMock()
        # cli_dir already exists from the first call; ensure it still exists
        cli_dir.mkdir(exist_ok=True)
        with patch.dict(sys.modules, {"winreg": winreg_stub, "ctypes": fake_ctypes}):
            tray_helpers._setup_cli_path()
        stored2, _ = winreg_stub._key._data.get("Path", ("", 2))
        assert stored2.count(cli) == 1

    def test_skips_on_non_windows(self, tray_helpers, monkeypatch):
        monkeypatch.setattr(sys, "platform", "linux")
        # Should return immediately without touching winreg
        called = []
        with patch.dict(sys.modules, {"winreg": MagicMock(side_effect=lambda *a, **kw: called.append(1))}):
            tray_helpers._setup_cli_path()
        assert called == []

    def test_skips_when_cli_dir_is_none(self, tray_helpers, monkeypatch):
        monkeypatch.setattr(sys, "platform", "win32")
        monkeypatch.setattr(tray_helpers, "_cli_dir", lambda: None)
        winreg_stub = _make_winreg_stub()
        with patch.dict(sys.modules, {"winreg": winreg_stub}):
            tray_helpers._setup_cli_path()
        # Nothing should have been written
        assert "Path" not in winreg_stub._key._data

    def test_skips_when_cli_dir_does_not_exist(self, tray_helpers, tmp_path, monkeypatch):
        monkeypatch.setattr(sys, "platform", "win32")
        nonexistent = tmp_path / "no_such_cli"
        monkeypatch.setattr(tray_helpers, "_cli_dir", lambda: nonexistent)
        winreg_stub = _make_winreg_stub()
        fake_ctypes = MagicMock()
        with patch.dict(sys.modules, {"winreg": winreg_stub, "ctypes": fake_ctypes}):
            tray_helpers._setup_cli_path()
        assert "Path" not in winreg_stub._key._data


# ---------------------------------------------------------------------------
# _remove_cli_path
# ---------------------------------------------------------------------------


class TestRemoveCliPath:
    """Tests for _remove_cli_path — removes cli entry from PATH via fake winreg."""

    def _run_remove(self, tray_helpers, cli_dir, initial_path, monkeypatch):
        monkeypatch.setattr(sys, "platform", "win32")
        monkeypatch.setattr(tray_helpers, "_cli_dir", lambda: cli_dir)

        winreg_stub = _make_winreg_stub(initial_path)
        fake_ctypes = MagicMock()
        fake_ctypes.windll = MagicMock()
        fake_ctypes.windll.user32 = MagicMock()

        with patch.dict(sys.modules, {"winreg": winreg_stub, "ctypes": fake_ctypes}):
            tray_helpers._remove_cli_path()

        stored_path, _ = winreg_stub._key._data.get("Path", ("", 2))
        return stored_path

    def test_removes_cli_from_path(self, tray_helpers, tmp_path, monkeypatch):
        cli_dir = tmp_path / "cli"
        cli_dir.mkdir()
        initial = f"C:\\Windows\\system32;{cli_dir};C:\\more"
        result = self._run_remove(tray_helpers, cli_dir, initial, monkeypatch)
        assert str(cli_dir) not in result
        assert "C:\\Windows\\system32" in result
        assert "C:\\more" in result

    def test_noop_when_not_in_path(self, tray_helpers, tmp_path, monkeypatch):
        cli_dir = tmp_path / "cli"
        cli_dir.mkdir()
        initial = "C:\\Windows\\system32;C:\\more"
        result = self._run_remove(tray_helpers, cli_dir, initial, monkeypatch)
        assert result == initial

    def test_noop_when_path_key_missing(self, tray_helpers, tmp_path, monkeypatch):
        cli_dir = tmp_path / "cli"
        cli_dir.mkdir()
        # Key has no Path value — should not raise
        result = self._run_remove(tray_helpers, cli_dir, "", monkeypatch)
        assert result == ""  # empty initial, nothing written back

    def test_skips_on_non_windows(self, tray_helpers, monkeypatch):
        monkeypatch.setattr(sys, "platform", "linux")
        called = []
        with patch.dict(sys.modules, {"winreg": MagicMock(side_effect=lambda *a, **kw: called.append(1))}):
            tray_helpers._remove_cli_path()
        assert called == []

    def test_skips_when_cli_dir_is_none(self, tray_helpers, monkeypatch):
        monkeypatch.setattr(sys, "platform", "win32")
        monkeypatch.setattr(tray_helpers, "_cli_dir", lambda: None)
        winreg_stub = _make_winreg_stub("C:\\Windows")
        with patch.dict(sys.modules, {"winreg": winreg_stub}):
            tray_helpers._remove_cli_path()
        # Original value must be untouched
        stored, _ = winreg_stub._key._data["Path"]
        assert stored == "C:\\Windows"


# ---------------------------------------------------------------------------
# _setup_ps_completions
# ---------------------------------------------------------------------------


class TestSetupPsCompletions:
    """Tests for _setup_ps_completions — injects source line into PS profiles."""

    def _make_completions_dir(self, tmp_path: Path) -> Path:
        completions = tmp_path / "completions"
        completions.mkdir()
        ps1 = completions / "plaud-tools.ps1"
        ps1.write_text("# completions", encoding="utf-8")
        return completions

    def test_adds_source_line_to_new_profile(self, tray_helpers, tmp_path, monkeypatch):
        completions = self._make_completions_dir(tmp_path)
        profile_dir = tmp_path / "Documents" / "PowerShell"

        monkeypatch.setattr(tray_helpers, "_completions_dir", lambda: completions)
        _patch_home(monkeypatch, tray_helpers, tmp_path)

        tray_helpers._setup_ps_completions()

        profile = profile_dir / "Microsoft.PowerShell_profile.ps1"
        assert profile.exists()
        content = profile.read_text(encoding="utf-8")
        assert f'. "{completions / "plaud-tools.ps1"}"' in content

    def test_idempotent_when_line_already_present(self, tray_helpers, tmp_path, monkeypatch):
        completions = self._make_completions_dir(tmp_path)
        source_line = f'. "{completions / "plaud-tools.ps1"}"'
        profile_dir = tmp_path / "Documents" / "PowerShell"
        profile_dir.mkdir(parents=True)
        profile = profile_dir / "Microsoft.PowerShell_profile.ps1"
        profile.write_text(source_line + "\n", encoding="utf-8")

        monkeypatch.setattr(tray_helpers, "_completions_dir", lambda: completions)
        _patch_home(monkeypatch, tray_helpers, tmp_path)

        tray_helpers._setup_ps_completions()

        content = profile.read_text(encoding="utf-8")
        assert content.count(source_line) == 1

    def test_removes_stale_plaud_ps1_lines(self, tray_helpers, tmp_path, monkeypatch):
        """Stale sourcing lines pointing at the canonical install dir are stripped.

        The production regex (``_stale_sourcing_re``) is anchored to the canonical
        install directory (``%LOCALAPPDATA%/Programs/PlaudTools/completions``) so
        the stale line must live there to be eligible for removal.  Unrelated
        scripts in other completions folders are intentionally never touched.
        """
        completions = self._make_completions_dir(tmp_path)
        profile_dir = tmp_path / "Documents" / "PowerShell"
        profile_dir.mkdir(parents=True)
        profile = profile_dir / "Microsoft.PowerShell_profile.ps1"

        canonical_completions = _patch_home(monkeypatch, tray_helpers, tmp_path)
        # Stale line points at the canonical install dir, so it should be stripped.
        stale_line = f'. "{canonical_completions / "plaud.ps1"}"'
        # Unrelated line points elsewhere and must be preserved.
        unrelated_line = '. "/opt/plaud/completions/plaud.ps1"'
        profile.write_text(stale_line + "\n" + unrelated_line + "\n", encoding="utf-8")

        monkeypatch.setattr(tray_helpers, "_completions_dir", lambda: completions)

        tray_helpers._setup_ps_completions()

        content = profile.read_text(encoding="utf-8")
        assert stale_line not in content
        # Lines outside the canonical install dir are deliberately preserved.
        assert unrelated_line in content

    def test_skips_when_completions_dir_is_none(self, tray_helpers, monkeypatch):
        monkeypatch.setattr(tray_helpers, "_completions_dir", lambda: None)
        # Should return without raising or touching any files
        tray_helpers._setup_ps_completions()

    def test_skips_when_ps1_file_missing(self, tray_helpers, tmp_path, monkeypatch):
        completions = tmp_path / "completions"
        completions.mkdir()
        # plaud-tools.ps1 does NOT exist
        monkeypatch.setattr(tray_helpers, "_completions_dir", lambda: completions)
        _patch_home(monkeypatch, tray_helpers, tmp_path)
        tray_helpers._setup_ps_completions()
        profile = tmp_path / "Documents" / "PowerShell" / "Microsoft.PowerShell_profile.ps1"
        assert not profile.exists()


# ---------------------------------------------------------------------------
# _remove_ps_completions
# ---------------------------------------------------------------------------


class TestRemovePsCompletions:
    """Tests for _remove_ps_completions — removes all plaud sourcing lines."""

    def test_removes_source_line(self, tray_helpers, tmp_path, monkeypatch):
        """The plaud sourcing line is stripped by _remove_ps_completions.

        The production regex only strips lines pointing at the canonical
        PlaudTools install directory, so the test writes its stale line there.
        """
        profile_dir = tmp_path / "Documents" / "PowerShell"
        profile_dir.mkdir(parents=True)
        profile = profile_dir / "Microsoft.PowerShell_profile.ps1"

        canonical_completions = _patch_home(monkeypatch, tray_helpers, tmp_path)
        plaud_line = f'. "{canonical_completions / "plaud-tools.ps1"}"'
        other_line = "# unrelated line\n"
        profile.write_text(plaud_line + "\n" + other_line, encoding="utf-8")

        tray_helpers._remove_ps_completions()

        content = profile.read_text(encoding="utf-8")
        assert plaud_line not in content
        assert other_line.strip() in content

    def test_noop_when_profile_missing(self, tray_helpers, tmp_path, monkeypatch):
        _patch_home(monkeypatch, tray_helpers, tmp_path)
        # No profiles exist — should not raise
        tray_helpers._remove_ps_completions()

    def test_removes_all_plaud_variants(self, tray_helpers, tmp_path, monkeypatch):
        """Both old plaud.ps1 and new plaud-tools.ps1 sourcing lines are removed.

        Both variants must live under the canonical install dir for the
        regex to strip them.  Other content is preserved.
        """
        profile_dir = tmp_path / "Documents" / "PowerShell"
        profile_dir.mkdir(parents=True)
        profile = profile_dir / "Microsoft.PowerShell_profile.ps1"

        canonical_completions = _patch_home(monkeypatch, tray_helpers, tmp_path)
        old_line = f'. "{canonical_completions / "plaud.ps1"}"'
        new_line = f'. "{canonical_completions / "plaud-tools.ps1"}"'
        lines = [
            old_line + "\n",
            new_line + "\n",
            "# keep this\n",
        ]
        profile.write_text("".join(lines), encoding="utf-8")

        tray_helpers._remove_ps_completions()

        content = profile.read_text(encoding="utf-8")
        assert old_line not in content
        assert new_line not in content
        assert "# keep this" in content

    def test_handles_both_profile_locations(self, tray_helpers, tmp_path, monkeypatch):
        """Sourcing lines are removed from both PowerShell and WindowsPowerShell profiles."""
        canonical_completions = _patch_home(monkeypatch, tray_helpers, tmp_path)
        plaud_line = f'. "{canonical_completions / "plaud-tools.ps1"}"'
        for subfolder in ("PowerShell", "WindowsPowerShell"):
            profile_dir = tmp_path / "Documents" / subfolder
            profile_dir.mkdir(parents=True)
            profile = profile_dir / "Microsoft.PowerShell_profile.ps1"
            profile.write_text(plaud_line + "\n", encoding="utf-8")

        tray_helpers._remove_ps_completions()

        for subfolder in ("PowerShell", "WindowsPowerShell"):
            profile = tmp_path / "Documents" / subfolder / "Microsoft.PowerShell_profile.ps1"
            content = profile.read_text(encoding="utf-8")
            assert plaud_line not in content


# ---------------------------------------------------------------------------
# _delete_session_files
# ---------------------------------------------------------------------------


class TestDeleteSessionFiles:
    """Tests for _delete_session_files — deletes stored credentials."""

    def test_deletes_fallback_session_file(self, tray_helpers, tmp_path, monkeypatch):
        config_dir = tmp_path / ".config" / "plaud-tools"
        config_dir.mkdir(parents=True)
        session_file = config_dir / "session.json"
        session_file.write_text('{"access_token":"tok"}', encoding="utf-8")

        _patch_home(monkeypatch, tray_helpers, tmp_path)

        # Stub out SessionStore so we only test file deletion
        mock_store = MagicMock()
        with patch("plaud_tools.tray_app.SessionStore", return_value=mock_store):
            tray_helpers._delete_session_files()

        assert not session_file.exists()
        mock_store.clear.assert_called_once()

    def test_noop_when_no_session_file(self, tray_helpers, tmp_path, monkeypatch):
        _patch_home(monkeypatch, tray_helpers, tmp_path)
        mock_store = MagicMock()
        with patch("plaud_tools.tray_app.SessionStore", return_value=mock_store):
            # Should not raise
            tray_helpers._delete_session_files()
        mock_store.clear.assert_called_once()

    def test_calls_session_store_clear(self, tray_helpers, tmp_path, monkeypatch):
        _patch_home(monkeypatch, tray_helpers, tmp_path)
        mock_store = MagicMock()
        with patch("plaud_tools.tray_app.SessionStore", return_value=mock_store):
            tray_helpers._delete_session_files()
        mock_store.clear.assert_called_once()


# ---------------------------------------------------------------------------
# _delete_log_files
# ---------------------------------------------------------------------------


class TestDeleteLogFiles:
    """Tests for _delete_log_files — removes tray.log* from both log dirs."""

    def test_deletes_log_files_from_plaud_dir(self, tray_helpers, tmp_path, monkeypatch):
        log_dir = tmp_path / "Plaud"
        log_dir.mkdir()
        log1 = log_dir / "tray.log"
        log1.write_text("log content", encoding="utf-8")
        log2 = log_dir / "tray.log.1"
        log2.write_text("rotated", encoding="utf-8")

        # appdata.data_dir() branches on sys.platform; pin to win32 so the
        # LOCALAPPDATA env-var override is honoured on all platforms (Linux and
        # macOS otherwise use platformdirs which ignores LOCALAPPDATA).
        monkeypatch.setattr(sys, "platform", "win32")
        monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
        tray_helpers._delete_log_files()

        assert not log1.exists()
        assert not log2.exists()

    def test_deletes_log_files_from_plaud_tools_dir(self, tray_helpers, tmp_path, monkeypatch):
        log_dir = tmp_path / "PlaudTools"
        log_dir.mkdir()
        log_file = log_dir / "tray.log"
        log_file.write_text("log", encoding="utf-8")

        # Pin sys.platform to win32 so data_dir() uses LOCALAPPDATA on all
        # platforms; without this macOS would resolve to ~/Library/... and the
        # log file under tmp_path would never be found and deleted.
        monkeypatch.setattr(sys, "platform", "win32")
        monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
        tray_helpers._delete_log_files()

        assert not log_file.exists()

    def test_deletes_from_both_dirs(self, tray_helpers, tmp_path, monkeypatch):
        for name in ("Plaud", "PlaudTools"):
            d = tmp_path / name
            d.mkdir()
            (d / "tray.log").write_text("x", encoding="utf-8")

        # Pin sys.platform to win32 so data_dir() uses LOCALAPPDATA on all
        # platforms; without this macOS would resolve to ~/Library/... and the
        # log files under tmp_path would never be found and deleted.
        monkeypatch.setattr(sys, "platform", "win32")
        monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
        tray_helpers._delete_log_files()

        for name in ("Plaud", "PlaudTools"):
            assert not (tmp_path / name / "tray.log").exists()

    def test_preserves_non_log_files(self, tray_helpers, tmp_path, monkeypatch):
        log_dir = tmp_path / "Plaud"
        log_dir.mkdir()
        keeper = log_dir / "config.json"
        keeper.write_text("{}", encoding="utf-8")
        (log_dir / "tray.log").write_text("x", encoding="utf-8")

        # Pin sys.platform to win32 so data_dir() uses LOCALAPPDATA on all
        # platforms (macOS would otherwise use platformdirs and skip tmp_path).
        monkeypatch.setattr(sys, "platform", "win32")
        monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
        tray_helpers._delete_log_files()

        assert keeper.exists()

    def test_noop_when_log_dirs_absent(self, tray_helpers, tmp_path, monkeypatch):
        # Pin sys.platform to win32 so data_dir() uses LOCALAPPDATA on all
        # platforms; the test verifies no exception is raised regardless of
        # whether the directories exist.
        monkeypatch.setattr(sys, "platform", "win32")
        monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
        # Neither Plaud nor PlaudTools directories exist — should not raise
        tray_helpers._delete_log_files()


# ---------------------------------------------------------------------------
# Snapshot / isolation tests for rendered PS1 dispatchers
# ---------------------------------------------------------------------------


class TestRenderedPs1NoStrayPaths:
    """Assert rendered dispatcher strings reference only the supplied install dir."""

    INSTALL_DIR = r"C:\Programs\PlaudTools"
    OTHER_DIR = r"C:\Programs\OtherTool"

    def test_render_update_ps1_no_stray_install_dir(self):
        from plaud_tools.ps1_templates import render_update_ps1

        result = render_update_ps1(
            tray_pid=1,
            install_dir=self.INSTALL_DIR,
            zip_path=r"C:\Temp\update.zip",
            extract_dir=r"C:\Programs",
        )
        assert self.OTHER_DIR not in result

    def test_render_uninstall_ps1_no_stray_install_dir(self):
        from plaud_tools.ps1_templates import render_uninstall_ps1

        result = render_uninstall_ps1(
            tray_pid=1,
            install_dir=self.INSTALL_DIR,
        )
        assert self.OTHER_DIR not in result

    def test_render_update_ps1_contains_only_supplied_dir(self):
        from plaud_tools.ps1_templates import render_update_ps1

        result = render_update_ps1(
            tray_pid=42,
            install_dir=self.INSTALL_DIR,
            zip_path=r"C:\Temp\plaud_update_42.zip",
            extract_dir=r"C:\Programs",
        )
        # The supplied install dir must appear
        assert self.INSTALL_DIR in result
        # The supplied zip path must appear
        assert r"C:\Temp\plaud_update_42.zip" in result

    def test_render_uninstall_ps1_contains_only_supplied_dir(self):
        from plaud_tools.ps1_templates import render_uninstall_ps1

        result = render_uninstall_ps1(
            tray_pid=99,
            install_dir=self.INSTALL_DIR,
            log_dirs=[r"C:\Users\foo\AppData\Local\PlaudTools"],
        )
        assert self.INSTALL_DIR in result
        # Arbitrary other install dir must not appear
        assert r"C:\Programs\AnotherTool" not in result

    def test_update_ps1_script_uses_install_dir_parameter_not_literals(self):
        """The raw update.ps1 code sections use $InstallDir, not hard-coded paths.

        The docstring may show examples like C:\\Programs\\PlaudTools, but the
        functional code must not reference any machine-specific install path.
        """
        from plaud_tools.ps1_templates import scripts_dir

        content = (scripts_dir() / "update.ps1").read_text(encoding="utf-8")
        # The functional code must reference $InstallDir
        assert "$InstallDir" in content
        # Must not reference any arbitrary other install path (machine-specific)
        assert r"C:\Programs\OtherTool" not in content
        assert r"C:\Users\SomeUser\PlaudTools" not in content

    def test_uninstall_ps1_script_uses_install_dir_parameter_not_literals(self):
        """The raw uninstall.ps1 code sections use $InstallDir, not hard-coded paths."""
        from plaud_tools.ps1_templates import scripts_dir

        content = (scripts_dir() / "uninstall.ps1").read_text(encoding="utf-8")
        assert "$InstallDir" in content
        assert r"C:\Programs\OtherTool" not in content
        assert r"C:\Users\SomeUser\PlaudTools" not in content


# ---------------------------------------------------------------------------
# Snapshot: rendered update dispatcher matches expected structure
# ---------------------------------------------------------------------------


class TestRenderUpdatePs1Snapshot:
    """Structural snapshot tests for the rendered update dispatcher."""

    def _render(self, **kwargs):
        from plaud_tools.ps1_templates import render_update_ps1

        defaults = dict(
            tray_pid=1234,
            install_dir=r"C:\Programs\PlaudTools",
            zip_path=r"C:\Temp\update.zip",
            extract_dir=r"C:\Programs",
        )
        defaults.update(kwargs)
        return render_update_ps1(**defaults)

    def test_rendered_is_single_line_invocation(self):
        result = self._render()
        # Dispatcher is a single & 'script' -Param value ... invocation
        stripped_lines = [line for line in result.splitlines() if line.strip()]
        assert len(stripped_lines) == 1

    def test_rendered_starts_with_call_operator(self):
        assert self._render().lstrip().startswith("&")

    def test_rendered_passes_tray_pid(self):
        result = self._render(tray_pid=5678)
        assert "5678" in result
        assert "-TrayPid" in result

    def test_rendered_passes_install_dir_param(self):
        result = self._render(install_dir=r"C:\Programs\PlaudTools")
        assert "-InstallDir" in result
        assert r"C:\Programs\PlaudTools" in result

    def test_rendered_passes_zip_path_param(self):
        result = self._render(zip_path=r"C:\Temp\plaud_1234.zip")
        assert "-ZipPath" in result
        assert r"C:\Temp\plaud_1234.zip" in result

    def test_rendered_passes_extract_dir_param(self):
        result = self._render(extract_dir=r"C:\Programs")
        assert "-ExtractDir" in result

    def test_rendered_ends_with_newline(self):
        assert self._render().endswith("\n")


class TestRenderUninstallPs1Snapshot:
    """Structural snapshot tests for the rendered uninstall dispatcher."""

    def _render(self, **kwargs):
        from plaud_tools.ps1_templates import render_uninstall_ps1

        defaults = dict(
            tray_pid=999,
            install_dir=r"C:\Programs\PlaudTools",
        )
        defaults.update(kwargs)
        return render_uninstall_ps1(**defaults)

    def test_rendered_starts_with_call_operator(self):
        assert self._render().lstrip().startswith("&")

    def test_rendered_passes_tray_pid(self):
        result = self._render(tray_pid=777)
        assert "777" in result
        assert "-TrayPid" in result

    def test_rendered_passes_install_dir(self):
        result = self._render(install_dir=r"C:\Programs\PlaudTools")
        assert "-InstallDir" in result
        assert r"C:\Programs\PlaudTools" in result

    def test_rendered_ends_with_newline(self):
        assert self._render().endswith("\n")

    def test_rendered_log_dirs_semicolon_separated(self):
        result = self._render(
            log_dirs=[
                r"C:\Users\foo\AppData\Local\PlaudTools",
                r"C:\Users\foo\AppData\Local\Plaud",
            ]
        )
        # Semicolon joins them
        assert "PlaudTools;" in result or ";C:" in result
        assert "-LogDirs" in result
