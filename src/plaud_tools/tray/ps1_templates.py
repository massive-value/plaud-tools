"""Helpers for locating and rendering the bundled PS1 update/uninstall scripts.

The scripts live at ``src/plaud_tools/tray/scripts/{update,uninstall}.ps1`` in
the source tree, and are shipped into the bundle under a ``scripts/``
directory relative to ``sys._MEIPASS`` (PyInstaller onedir).

Public API
----------
``scripts_dir()``
    Return the directory that contains the bundled ``.ps1`` scripts.

``render_update_ps1(tray_pid, install_dir, zip_path, extract_dir)``
    Return a PowerShell dispatcher string that invokes ``update.ps1`` with the
    given arguments.

``render_uninstall_ps1(tray_pid, install_dir, log_dirs)``
    Return a PowerShell dispatcher string that invokes ``uninstall.ps1`` with
    the given arguments (log_dirs is a list of Path / str, may be empty).
"""

from __future__ import annotations

import sys
from pathlib import Path

__all__ = [
    "scripts_dir",
    "render_update_ps1",
    "render_uninstall_ps1",
]


def scripts_dir() -> Path:
    """Return the directory containing the bundled PS1 scripts.

    Search order (frozen):
    1. ``sys._MEIPASS / scripts``
    2. ``exe-parent / scripts``
    3. ``exe-parent / _internal / scripts``

    Falls back to the source-tree location in dev mode.
    """
    if getattr(sys, "frozen", False):
        meipass = getattr(sys, "_MEIPASS", None)
        candidates: list[Path] = []
        if meipass:
            candidates.append(Path(meipass) / "scripts")
        candidates.append(Path(sys.executable).parent / "scripts")
        candidates.append(Path(sys.executable).parent / "_internal" / "scripts")
        for c in candidates:
            if (c / "update.ps1").exists():
                return c
        return candidates[0]
    # Dev / editable install: scripts live in tray/scripts/, next to this file.
    return Path(__file__).parent / "scripts"


def _ps_escape(value: str) -> str:
    """Escape a string value for safe single-quote embedding in PowerShell.

    Single-quotes in PS1 strings are escaped by doubling them.
    """
    return value.replace("'", "''")


def render_update_ps1(
    tray_pid: int,
    install_dir: str,
    zip_path: str,
    extract_dir: str,
    dispatcher_path: str | None = None,
    new_version: str | None = None,
) -> str:
    """Return a PS1 dispatcher that calls the bundled update.ps1 with the given args.

    The dispatcher is a small self-contained script that:
    - Determines the scripts directory at runtime (handles _MEIPASS path).
    - Invokes ``update.ps1`` with the supplied parameters.
    - Exits immediately so the caller (the tray process) can quit.

    Parameters
    ----------
    tray_pid:
        PID of the calling tray process; update.ps1 waits for it to exit.
    install_dir:
        Absolute path to the PlaudTools install directory.
    zip_path:
        Absolute path to the downloaded update archive.
    extract_dir:
        Directory to extract the zip into (parent of install_dir).
    dispatcher_path:
        Absolute path to the dispatcher PS1 itself. Passed to update.ps1 as
        ``-DispatcherPath`` so update.ps1 can delete it after a successful
        run. Optional for backwards compatibility with older callers.
    new_version:
        The version being installed (e.g. ``"0.3.3"``). Passed to update.ps1
        as ``-NewVersion`` so it can (a) prune stale ``plaud_tools-*.dist-info``
        directories left behind by the overlay extraction — otherwise
        ``importlib.metadata.version`` resolves the OLD version and the tray
        keeps reporting the pre-update version — and (b) write the
        ``plaud_just_updated.txt`` success sentinel only AFTER a successful
        extraction. Optional for backwards compatibility with older callers.
    """
    scripts = scripts_dir()
    ps1 = scripts / "update.ps1"
    safe_ps1 = _ps_escape(str(ps1))
    safe_install = _ps_escape(install_dir)
    safe_zip = _ps_escape(zip_path)
    safe_extract = _ps_escape(extract_dir)
    line = (
        f"& '{safe_ps1}'"
        f" -TrayPid {tray_pid}"
        f" -InstallDir '{safe_install}'"
        f" -ZipPath '{safe_zip}'"
        f" -ExtractDir '{safe_extract}'"
    )
    if dispatcher_path:
        safe_dispatcher = _ps_escape(dispatcher_path)
        line += f" -DispatcherPath '{safe_dispatcher}'"
    if new_version:
        safe_version = _ps_escape(new_version)
        line += f" -NewVersion '{safe_version}'"
    return line + "\n"


def render_uninstall_ps1(
    tray_pid: int,
    install_dir: str,
    log_dirs: list[str] | None = None,
) -> str:
    """Return a PS1 dispatcher that calls the bundled uninstall.ps1 with the given args.

    Parameters
    ----------
    tray_pid:
        PID of the calling tray process; uninstall.ps1 waits for it to exit.
    install_dir:
        Absolute path to the PlaudTools install directory to delete.
    log_dirs:
        Optional list of log directory paths to delete.  Passed as a
        semicolon-joined ``-LogDirs`` argument.
    """
    scripts = scripts_dir()
    ps1 = scripts / "uninstall.ps1"
    safe_ps1 = _ps_escape(str(ps1))
    safe_install = _ps_escape(install_dir)
    log_dirs_str = ";".join(str(d) for d in (log_dirs or []))
    safe_log_dirs = _ps_escape(log_dirs_str)
    lines = [
        f"& '{safe_ps1}' -TrayPid {tray_pid} -InstallDir '{safe_install}'",
    ]
    if log_dirs_str:
        lines[0] += f" -LogDirs '{safe_log_dirs}'"
    lines[0] += "\n"
    return lines[0]
