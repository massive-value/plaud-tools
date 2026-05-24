"""Install layout detection for plaud-tools.

``InstallLayout`` represents the on-disk arrangement of binaries for the
*running* install, derived entirely from ``sys.executable``.

Key design points (ADR 004):
- Channel detection branches on ``sys.platform × getattr(sys, "frozen", False)``.
- ``install_root`` is derived from ``sys.executable``; the canonical install path
  ``%LOCALAPPDATA%\\Programs\\PlaudTools\\`` is NOT used here — it lives only in
  ``scripts/install.ps1``.  This closes the latent autostart bug where a
  manually-relocated bundle's autostart registry entry pointed at the empty
  canonical location instead of the actual install.
- Independent of ``appdata.py``; do not import between them.
"""
from __future__ import annotations

import shutil
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

__all__ = ["InstallLayout"]

# Expected stem of the CLI executable inside a frozen bundle.
_BUNDLE_CLI_STEM = "plaud-tools"
# Expected stem of the tray executable inside a frozen bundle.
_BUNDLE_TRAY_STEM = "PlaudTools"


@dataclass(frozen=True)
class InstallLayout:
    """On-disk layout of the *running* plaud-tools install.

    Fields
    ------
    channel:
        ``"bundle"`` — frozen Windows bundle (PyInstaller).
        ``"pip"``    — installed via ``pip install plaud-tools``.
        ``"dev"``    — running from a development venv (no shim).
    install_root:
        Root directory of the bundle install (e.g.
        ``C:\\Users\\foo\\PlaudCustom\\``), derived from ``sys.executable``.
        ``None`` for pip / dev channels.
    cli_exe:
        Absolute path to the plaud-tools CLI executable.  Always set;
        falls back to ``shutil.which`` then ``sys.executable``.
    mcp_exe:
        Absolute path to the plaud-mcp executable, or ``None`` when not
        found (pip/dev channels with no ``plaud-mcp`` on PATH).
    ffmpeg_exe:
        Absolute path to ffmpeg, or ``None`` when not found (pip/dev
        channels with no ``ffmpeg`` on PATH).
    """

    channel: Literal["bundle", "pip", "dev"]
    install_root: Path | None
    cli_exe: Path
    mcp_exe: Path | None
    ffmpeg_exe: Path | None

    # ------------------------------------------------------------------
    # Factory
    # ------------------------------------------------------------------

    @classmethod
    def detect(cls) -> "InstallLayout":
        """Detect the layout of the running install from ``sys.executable``.

        Channel resolution rules (per ADR 004):
        - ``bundle``: ``getattr(sys, "frozen", False)`` is true AND the
          executable basename (stem) matches either the CLI or tray entry
          point.  ``install_root`` walks up from the exe directory to its
          parent (e.g. ``.../PlaudTools/cli/plaud-tools.exe`` →
          ``.../PlaudTools/``).
        - ``pip``: ``sys.executable`` resolves under a directory that looks
          like a Scripts/bin venv directory (a sibling of ``site-packages``).
          ``install_root`` is ``None``.
        - ``dev``: fallback — ``sys.executable`` is the venv interpreter
          itself (no plaud-tools shim).  ``install_root`` is ``None``.
        """
        frozen: bool = getattr(sys, "frozen", False)
        exe = Path(sys.executable)

        if frozen:
            return cls._detect_bundle(exe)

        return cls._detect_pip_or_dev(exe)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @classmethod
    def _detect_bundle(cls, exe: Path) -> "InstallLayout":
        """Build a bundle layout from the frozen executable path.

        The bundle ships TWO frozen entry points (per ``scripts/install.ps1``
        and ``pyinstaller/plaud-tray.spec``):

        - **CLI** at ``.../PlaudTools/cli/plaud-tools.exe`` (in a ``cli/``
          subdirectory).
        - **Tray** at ``.../PlaudTools/PlaudTools.exe`` (directly at the
          install root, no subdirectory).

        ``install_root`` must resolve to ``.../PlaudTools/`` in both cases —
        walking up one level for the CLI exe, but staying put for the tray exe.
        """
        exe_dir = exe.parent
        exe_stem = exe.stem.lower()

        if exe_stem == _BUNDLE_CLI_STEM.lower():
            # .../PlaudTools/cli/plaud-tools.exe → install_root is cli/'s parent
            install_root = exe_dir.parent
            cli_exe: Path = exe
        elif exe_stem == _BUNDLE_TRAY_STEM.lower():
            # .../PlaudTools/PlaudTools.exe → install_root IS the exe directory
            install_root = exe_dir
            cli_candidate = install_root / "cli" / f"{_BUNDLE_CLI_STEM}.exe"
            cli_exe = cli_candidate if cli_candidate.exists() else exe
        else:
            # Unknown frozen entry point: assume it sits at the install root.
            # Defensive fallback so future bundle entry points don't crash here.
            install_root = exe_dir
            cli_candidate = install_root / "cli" / f"{_BUNDLE_CLI_STEM}.exe"
            cli_exe = cli_candidate if cli_candidate.exists() else exe

        # MCP exe lives at install_root/mcp/plaud-mcp.exe
        mcp_candidate = install_root / "mcp" / "plaud-mcp.exe"
        mcp_exe: Path | None = mcp_candidate

        # ffmpeg lives at install_root/mcp/ffmpeg.exe
        ffmpeg_candidate = install_root / "mcp" / "ffmpeg.exe"
        ffmpeg_exe: Path | None = ffmpeg_candidate

        return cls(
            channel="bundle",
            install_root=install_root,
            cli_exe=cli_exe,
            mcp_exe=mcp_exe,
            ffmpeg_exe=ffmpeg_exe,
        )

    @classmethod
    def _detect_pip_or_dev(cls, exe: Path) -> "InstallLayout":
        """Build a pip or dev layout from the (non-frozen) interpreter path."""
        # pip install: sys.executable is a venv interpreter whose sibling
        # Scripts/ (Windows) or bin/ (POSIX) directory contains the
        # plaud-tools shim AND a site-packages directory sits one level up.
        channel: Literal["pip", "dev"] = _infer_channel(exe)

        cli_which = shutil.which("plaud-tools")
        cli_exe: Path = Path(cli_which) if cli_which else exe

        mcp_which = shutil.which("plaud-mcp")
        mcp_exe: Path | None = Path(mcp_which) if mcp_which else None

        ffmpeg_which = shutil.which("ffmpeg")
        ffmpeg_exe: Path | None = Path(ffmpeg_which) if ffmpeg_which else None

        return cls(
            channel=channel,
            install_root=None,
            cli_exe=cli_exe,
            mcp_exe=mcp_exe,
            ffmpeg_exe=ffmpeg_exe,
        )


def _infer_channel(exe: Path) -> Literal["pip", "dev"]:
    """Return ``"pip"`` if *exe* looks like a pip/venv installation, else ``"dev"``."""
    # A pip-installed plaud-tools exposes a console script shim in the
    # Scripts/ (Windows) or bin/ (POSIX) directory *next to* site-packages.
    # The interpreter exe lives one level up from Scripts/bin.
    # Check if a plaud-tools script shim is on PATH — if so it's pip.
    if shutil.which("plaud-tools") is not None:
        return "pip"

    # Alternatively inspect the venv structure: if the interpreter directory
    # has a sibling site-packages it's a venv (dev or pip).
    # The distinction at this point is that pip-installs have a plaud-tools
    # shim; dev venvs do not.  Already checked above, so fall through to dev.
    return "dev"
