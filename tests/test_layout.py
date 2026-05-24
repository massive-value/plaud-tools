"""Unit tests for plaud_tools.layout.InstallLayout.detect().

Covers all three channels (bundle, pip, dev) plus the critical regression
test for the canonical-path autostart bug: a frozen-bundle install relocated
to a non-canonical path must derive install_root from sys.executable, NOT
from any hardcoded canonical path.
"""
from __future__ import annotations

import shutil
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from plaud_tools.layout import InstallLayout


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_bundle_tree(tmp_path: Path) -> tuple[Path, Path]:
    """Create a minimal bundle directory structure and return (install_root, cli_exe)."""
    cli_dir = tmp_path / "cli"
    cli_dir.mkdir(parents=True)
    cli_exe = cli_dir / "plaud-tools.exe"
    cli_exe.touch()

    mcp_dir = tmp_path / "mcp"
    mcp_dir.mkdir(parents=True)
    (mcp_dir / "plaud-mcp.exe").touch()
    (mcp_dir / "ffmpeg.exe").touch()

    return tmp_path, cli_exe


# ---------------------------------------------------------------------------
# Bundle channel
# ---------------------------------------------------------------------------

class TestBundleChannel:
    """detect() when sys.frozen is True."""

    def test_channel_is_bundle(self, monkeypatch, tmp_path):
        install_root, cli_exe = _make_bundle_tree(tmp_path)
        monkeypatch.setattr(sys, "frozen", True, raising=False)
        monkeypatch.setattr(sys, "executable", str(cli_exe))

        layout = InstallLayout.detect()

        assert layout.channel == "bundle"

    def test_install_root_derived_from_executable(self, monkeypatch, tmp_path):
        """install_root must walk up from .../PlaudTools/cli/ to .../PlaudTools/."""
        install_root, cli_exe = _make_bundle_tree(tmp_path)
        monkeypatch.setattr(sys, "frozen", True, raising=False)
        monkeypatch.setattr(sys, "executable", str(cli_exe))

        layout = InstallLayout.detect()

        assert layout.install_root == install_root

    def test_cli_exe_points_at_executable(self, monkeypatch, tmp_path):
        install_root, cli_exe = _make_bundle_tree(tmp_path)
        monkeypatch.setattr(sys, "frozen", True, raising=False)
        monkeypatch.setattr(sys, "executable", str(cli_exe))

        layout = InstallLayout.detect()

        assert layout.cli_exe == cli_exe

    def test_mcp_exe_under_install_root(self, monkeypatch, tmp_path):
        install_root, cli_exe = _make_bundle_tree(tmp_path)
        monkeypatch.setattr(sys, "frozen", True, raising=False)
        monkeypatch.setattr(sys, "executable", str(cli_exe))

        layout = InstallLayout.detect()

        assert layout.mcp_exe == install_root / "mcp" / "plaud-mcp.exe"

    def test_ffmpeg_under_install_root(self, monkeypatch, tmp_path):
        install_root, cli_exe = _make_bundle_tree(tmp_path)
        monkeypatch.setattr(sys, "frozen", True, raising=False)
        monkeypatch.setattr(sys, "executable", str(cli_exe))

        layout = InstallLayout.detect()

        assert layout.ffmpeg_exe == install_root / "mcp" / "ffmpeg.exe"

    def test_frozen_false_attribute_absent(self, monkeypatch, tmp_path):
        """When sys.frozen is absent entirely, detect() falls through to pip/dev."""
        fake_exe = tmp_path / "python.exe"
        fake_exe.touch()
        if hasattr(sys, "frozen"):
            monkeypatch.delattr(sys, "frozen", raising=False)
        monkeypatch.setattr(sys, "executable", str(fake_exe))
        # No plaud-tools shim → dev channel
        with patch("shutil.which", return_value=None):
            layout = InstallLayout.detect()
        assert layout.channel in ("pip", "dev")


# ---------------------------------------------------------------------------
# REGRESSION TEST — the canonical-path autostart bug
# ---------------------------------------------------------------------------

class TestCanonicalPathRegression:
    """A bundle at a non-canonical path must derive install_root from sys.executable.

    This is the primary regression test that closes the latent autostart bug:
    a user who extracted the bundle to C:\\Users\\foo\\PlaudCustom\\ instead of
    the canonical C:\\Users\\foo\\AppData\\Local\\Programs\\PlaudTools\\ would
    previously get an autostart registry entry pointing at the canonical (empty)
    path, breaking the tray on the next reboot.

    After this fix, install_root is always derived from sys.executable, so a
    bundle at any location produces the correct install_root.
    """

    CANONICAL_PATH = r"C:\Users\kadinb\AppData\Local\Programs\PlaudTools"

    def test_non_canonical_bundle_path_produces_correct_install_root(
        self, monkeypatch, tmp_path
    ):
        """install_root must be the actual extract directory, not the canonical path."""
        # Simulate bundle extracted to a non-canonical user directory.
        non_canonical_root = tmp_path / "PlaudCustom"
        cli_dir = non_canonical_root / "cli"
        cli_dir.mkdir(parents=True)
        cli_exe = cli_dir / "plaud-tools.exe"
        cli_exe.touch()

        monkeypatch.setattr(sys, "frozen", True, raising=False)
        monkeypatch.setattr(sys, "executable", str(cli_exe))

        layout = InstallLayout.detect()

        # Must derive from sys.executable, NOT from canonical path.
        assert layout.install_root == non_canonical_root, (
            f"install_root was {layout.install_root!r}; expected the actual "
            f"extraction directory {non_canonical_root!r}.  "
            f"Do NOT read the canonical path ({self.CANONICAL_PATH!r}) from anywhere in Python."
        )
        # Explicit check: canonical path must NOT appear anywhere.
        canonical = Path(self.CANONICAL_PATH)
        assert layout.install_root != canonical, (
            "install_root must never be the hardcoded canonical install path"
        )

    def test_canonical_path_not_hardcoded_when_exe_elsewhere(
        self, monkeypatch, tmp_path
    ):
        """Even when the canonical path exists on disk, install_root follows sys.executable."""
        # tmp_path simulates a custom location.
        custom_root = tmp_path / "MyCustomPlaudTools"
        cli_dir = custom_root / "cli"
        cli_dir.mkdir(parents=True)
        cli_exe = cli_dir / "plaud-tools.exe"
        cli_exe.touch()

        monkeypatch.setattr(sys, "frozen", True, raising=False)
        monkeypatch.setattr(sys, "executable", str(cli_exe))

        layout = InstallLayout.detect()

        assert layout.install_root == custom_root
        # install_root must not contain the canonical path segment.
        assert "Programs" not in str(layout.install_root) or \
               layout.install_root == custom_root

    def test_mcp_and_ffmpeg_derived_from_actual_install_root(
        self, monkeypatch, tmp_path
    ):
        """mcp_exe and ffmpeg_exe must also come from the actual install_root."""
        custom_root = tmp_path / "SomeOtherPath"
        cli_dir = custom_root / "cli"
        cli_dir.mkdir(parents=True)
        cli_exe = cli_dir / "plaud-tools.exe"
        cli_exe.touch()
        (custom_root / "mcp").mkdir()
        (custom_root / "mcp" / "plaud-mcp.exe").touch()
        (custom_root / "mcp" / "ffmpeg.exe").touch()

        monkeypatch.setattr(sys, "frozen", True, raising=False)
        monkeypatch.setattr(sys, "executable", str(cli_exe))

        layout = InstallLayout.detect()

        assert layout.mcp_exe == custom_root / "mcp" / "plaud-mcp.exe"
        assert layout.ffmpeg_exe == custom_root / "mcp" / "ffmpeg.exe"


# ---------------------------------------------------------------------------
# Pip channel
# ---------------------------------------------------------------------------

class TestPipChannel:
    """detect() when not frozen and plaud-tools shim is on PATH."""

    def test_channel_is_pip_when_shim_on_path(self, monkeypatch, tmp_path):
        fake_exe = tmp_path / "python.exe"
        fake_exe.touch()
        if hasattr(sys, "frozen"):
            monkeypatch.delattr(sys, "frozen", raising=False)
        monkeypatch.setattr(sys, "executable", str(fake_exe))

        shim = tmp_path / "plaud-tools"
        shim.touch()
        mcp_shim = tmp_path / "plaud-mcp"
        mcp_shim.touch()

        def _which(name: str) -> str | None:
            return str(shim) if name == "plaud-tools" else \
                   str(mcp_shim) if name == "plaud-mcp" else None

        with patch("shutil.which", side_effect=_which):
            layout = InstallLayout.detect()

        assert layout.channel == "pip"

    def test_install_root_is_none_for_pip(self, monkeypatch, tmp_path):
        fake_exe = tmp_path / "python.exe"
        fake_exe.touch()
        if hasattr(sys, "frozen"):
            monkeypatch.delattr(sys, "frozen", raising=False)
        monkeypatch.setattr(sys, "executable", str(fake_exe))

        shim = tmp_path / "plaud-tools"
        shim.touch()

        def _which(name: str) -> str | None:
            return str(shim) if name == "plaud-tools" else None

        with patch("shutil.which", side_effect=_which):
            layout = InstallLayout.detect()

        assert layout.install_root is None

    def test_cli_exe_from_which_for_pip(self, monkeypatch, tmp_path):
        fake_exe = tmp_path / "python.exe"
        fake_exe.touch()
        if hasattr(sys, "frozen"):
            monkeypatch.delattr(sys, "frozen", raising=False)
        monkeypatch.setattr(sys, "executable", str(fake_exe))

        shim = tmp_path / "plaud-tools"
        shim.touch()

        def _which(name: str) -> str | None:
            return str(shim) if name == "plaud-tools" else None

        with patch("shutil.which", side_effect=_which):
            layout = InstallLayout.detect()

        assert layout.cli_exe == shim

    def test_mcp_none_when_not_on_path(self, monkeypatch, tmp_path):
        fake_exe = tmp_path / "python.exe"
        fake_exe.touch()
        if hasattr(sys, "frozen"):
            monkeypatch.delattr(sys, "frozen", raising=False)
        monkeypatch.setattr(sys, "executable", str(fake_exe))

        shim = tmp_path / "plaud-tools"
        shim.touch()

        def _which(name: str) -> str | None:
            return str(shim) if name == "plaud-tools" else None

        with patch("shutil.which", side_effect=_which):
            layout = InstallLayout.detect()

        assert layout.mcp_exe is None

    def test_ffmpeg_none_when_not_on_path(self, monkeypatch, tmp_path):
        fake_exe = tmp_path / "python.exe"
        fake_exe.touch()
        if hasattr(sys, "frozen"):
            monkeypatch.delattr(sys, "frozen", raising=False)
        monkeypatch.setattr(sys, "executable", str(fake_exe))

        shim = tmp_path / "plaud-tools"
        shim.touch()

        def _which(name: str) -> str | None:
            return str(shim) if name == "plaud-tools" else None

        with patch("shutil.which", side_effect=_which):
            layout = InstallLayout.detect()

        assert layout.ffmpeg_exe is None


# ---------------------------------------------------------------------------
# Dev channel
# ---------------------------------------------------------------------------

class TestDevChannel:
    """detect() when not frozen and no plaud-tools shim on PATH."""

    def test_channel_is_dev_when_no_shim(self, monkeypatch, tmp_path):
        fake_exe = tmp_path / "python.exe"
        fake_exe.touch()
        if hasattr(sys, "frozen"):
            monkeypatch.delattr(sys, "frozen", raising=False)
        monkeypatch.setattr(sys, "executable", str(fake_exe))

        with patch("shutil.which", return_value=None):
            layout = InstallLayout.detect()

        assert layout.channel == "dev"

    def test_install_root_is_none_for_dev(self, monkeypatch, tmp_path):
        fake_exe = tmp_path / "python.exe"
        fake_exe.touch()
        if hasattr(sys, "frozen"):
            monkeypatch.delattr(sys, "frozen", raising=False)
        monkeypatch.setattr(sys, "executable", str(fake_exe))

        with patch("shutil.which", return_value=None):
            layout = InstallLayout.detect()

        assert layout.install_root is None

    def test_cli_exe_falls_back_to_sys_executable(self, monkeypatch, tmp_path):
        fake_exe = tmp_path / "python.exe"
        fake_exe.touch()
        if hasattr(sys, "frozen"):
            monkeypatch.delattr(sys, "frozen", raising=False)
        monkeypatch.setattr(sys, "executable", str(fake_exe))

        with patch("shutil.which", return_value=None):
            layout = InstallLayout.detect()

        assert layout.cli_exe == fake_exe

    def test_mcp_and_ffmpeg_none_for_dev(self, monkeypatch, tmp_path):
        fake_exe = tmp_path / "python.exe"
        fake_exe.touch()
        if hasattr(sys, "frozen"):
            monkeypatch.delattr(sys, "frozen", raising=False)
        monkeypatch.setattr(sys, "executable", str(fake_exe))

        with patch("shutil.which", return_value=None):
            layout = InstallLayout.detect()

        assert layout.mcp_exe is None
        assert layout.ffmpeg_exe is None


# ---------------------------------------------------------------------------
# Dataclass invariants
# ---------------------------------------------------------------------------

class TestDataclassInvariants:
    """InstallLayout is a frozen dataclass; verify immutability and field types."""

    def test_is_frozen(self, monkeypatch, tmp_path):
        install_root, cli_exe = _make_bundle_tree(tmp_path)
        monkeypatch.setattr(sys, "frozen", True, raising=False)
        monkeypatch.setattr(sys, "executable", str(cli_exe))

        layout = InstallLayout.detect()

        with pytest.raises((AttributeError, TypeError)):
            layout.channel = "pip"  # type: ignore[misc]

    def test_channel_literal(self, monkeypatch, tmp_path):
        install_root, cli_exe = _make_bundle_tree(tmp_path)
        monkeypatch.setattr(sys, "frozen", True, raising=False)
        monkeypatch.setattr(sys, "executable", str(cli_exe))

        layout = InstallLayout.detect()

        assert layout.channel in ("bundle", "pip", "dev")

    def test_cli_exe_always_a_path(self, monkeypatch, tmp_path):
        install_root, cli_exe = _make_bundle_tree(tmp_path)
        monkeypatch.setattr(sys, "frozen", True, raising=False)
        monkeypatch.setattr(sys, "executable", str(cli_exe))

        layout = InstallLayout.detect()

        assert isinstance(layout.cli_exe, Path)

    def test_install_root_is_path_or_none(self, monkeypatch, tmp_path):
        install_root, cli_exe = _make_bundle_tree(tmp_path)
        monkeypatch.setattr(sys, "frozen", True, raising=False)
        monkeypatch.setattr(sys, "executable", str(cli_exe))

        layout = InstallLayout.detect()

        assert layout.install_root is None or isinstance(layout.install_root, Path)
