"""Helpers for locating and rendering the bundled PS1 update/uninstall scripts.

The scripts live at ``src/plaud_tools/tray/scripts/{update,uninstall}.ps1`` in
the source tree, and are shipped into the bundle under a ``scripts/``
directory relative to ``sys._MEIPASS`` (PyInstaller onedir).

Public API
----------
``scripts_dir()``
    Return the directory that contains the bundled ``.ps1`` scripts.

``render_update_ps1(tray_pid, install_dir, zip_path, extract_dir, ...)``
    Return a PowerShell dispatcher string that invokes ``update.ps1`` with the
    given arguments: the new release's copy when one is supplied, with the
    bundled copy as the fallback.

``render_uninstall_ps1(tray_pid, install_dir, log_dir, dispatcher_path)``
    Return a PowerShell dispatcher string that invokes ``uninstall.ps1`` with
    the given arguments.
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
    next_script: str | None = None,
) -> str:
    """Return a PS1 dispatcher that runs update.ps1 with the given args.

    Without ``next_script`` the dispatcher is one line that calls the bundled
    update.ps1 (the copy shipped with the running, older version).

    With ``next_script`` (the new release's update.ps1, copied out of the
    verified zip) the dispatcher runs that copy first, so updater fixes take
    effect on the same update they ship in. If the new copy never starts (it
    fails to parse, or rejects the arguments this tray passes), it writes no
    heartbeat file, and the dispatcher falls back to the bundled copy. The
    reason is handed to the bundled copy in ``$env:PLAUD_UPDATE_FALLBACK`` so
    it lands in the update log. Once the new copy has written its heartbeat,
    it owns the update and the fallback never runs, so two updaters can never
    act on the same install.

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
        as ``-NewVersion`` so it writes the ``plaud_just_updated.txt`` success
        sentinel only AFTER a successful swap. Optional for backwards
        compatibility with older callers.
    next_script:
        Absolute path to the new release's update.ps1, already extracted from
        the checksum-verified zip. The dispatcher deletes it when done.
    """
    args = (
        f" -TrayPid {tray_pid}"
        f" -InstallDir '{_ps_escape(install_dir)}'"
        f" -ZipPath '{_ps_escape(zip_path)}'"
        f" -ExtractDir '{_ps_escape(extract_dir)}'"
    )
    if dispatcher_path:
        args += f" -DispatcherPath '{_ps_escape(dispatcher_path)}'"
    if new_version:
        args += f" -NewVersion '{_ps_escape(new_version)}'"
    bundled = f"& '{_ps_escape(str(scripts_dir() / 'update.ps1'))}'{args}"
    if not next_script:
        return bundled + "\n"

    # update.ps1 writes this heartbeat as its very first line (same path it
    # builds from $env:TEMP and -TrayPid). Its absence after the call means
    # the new copy never ran a line.
    safe_next = _ps_escape(next_script)
    return "\n".join(
        [
            "$ErrorActionPreference = 'Continue'",
            f"$alive = Join-Path $env:TEMP 'plaud_update_{tray_pid}.alive.txt'",
            "$reason = 'it did not start'",
            "try {",
            f"    & '{safe_next}'{args}",
            "} catch {",
            "    $reason = $_.Exception.Message",
            "}",
            "if (-not (Test-Path -LiteralPath $alive)) {",
            '    $env:PLAUD_UPDATE_FALLBACK = "New updater could not run ($reason); used the installed one."',
            f"    {bundled}",
            "}",
            f"Remove-Item -LiteralPath '{safe_next}' -ErrorAction SilentlyContinue",
            "",
        ]
    )


def render_uninstall_ps1(
    tray_pid: int,
    install_dir: str,
    log_dir: str | None = None,
    dispatcher_path: str | None = None,
) -> str:
    """Return a PS1 dispatcher that calls the bundled uninstall.ps1 with the given args.

    Parameters
    ----------
    tray_pid:
        PID of the calling tray process; uninstall.ps1 waits for it to exit.
    install_dir:
        Absolute path to the PlaudTools install directory to delete.
    log_dir:
        Optional data directory whose ``tray.log*`` / ``mcp.log*`` files
        uninstall.ps1 deletes after the tray exits (``-LogDir``).  Only those
        log files are removed; the directory and credentials stay.
    dispatcher_path:
        Absolute path to this dispatcher in %TEMP%, passed as
        ``-DispatcherPath`` so uninstall.ps1 can delete it when done.
    """
    scripts = scripts_dir()
    ps1 = scripts / "uninstall.ps1"
    line = f"& '{_ps_escape(str(ps1))}' -TrayPid {tray_pid} -InstallDir '{_ps_escape(install_dir)}'"
    if log_dir:
        line += f" -LogDir '{_ps_escape(log_dir)}'"
    if dispatcher_path:
        line += f" -DispatcherPath '{_ps_escape(dispatcher_path)}'"
    return line + "\n"
