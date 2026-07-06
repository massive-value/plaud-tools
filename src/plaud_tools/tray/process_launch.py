"""Shared helper for launching detached PowerShell child processes from the tray.

The frozen tray binary is a no-console (windowed) PyInstaller build. Two
independent, previously-shipped bugs bite this exact launch pattern:

1. ``DETACHED_PROCESS`` gives the child NULL stdio handles when the parent
   itself has none (true for a windowed/no-console exe) -- PowerShell crashes
   instantly, before running a single line of the script. ``CREATE_NO_WINDOW``
   (which allocates a *hidden* console rather than none at all) plus explicit
   ``subprocess.DEVNULL`` handles avoids this (the class of bug behind the
   d33c401 fix, reintroduced independently in the uninstaller and toast paths
   -- issue #142).
2. The tray runs inside a Windows Job Object. A helper that must outlive the
   tray process (the in-app updater, the uninstall helper -- both launched
   moments before the tray quits) needs ``CREATE_BREAKAWAY_FROM_JOB`` so a
   kill-on-close job doesn't tear the child down before it runs. If the job
   forbids breakaway, ``CreateProcess`` raises ``OSError`` (``ERROR_ACCESS_
   DENIED``); the caller should retry without the flag rather than fail the
   whole operation.

Every PowerShell-launch site in the tray MUST go through
:func:`launch_hidden_powershell` so a fix to either bug class lands once,
not once per call site.
"""

from __future__ import annotations

import logging
import os
import subprocess

# Absolute path to PowerShell to prevent PATH-hijacking attacks. %SystemRoot%
# is typically C:\Windows; fall back to the hard-coded canonical path if the
# env var is absent (should never happen on a standard Windows install, but
# defensive is better).
POWERSHELL_EXE: str = os.path.join(
    os.environ.get("SystemRoot", r"C:\Windows"),
    r"System32\WindowsPowerShell\v1.0\powershell.exe",
)

# subprocess.CREATE_BREAKAWAY_FROM_JOB detaches the child from the tray's Job
# Object so it survives the tray exiting. Referenced defensively in case a
# future Python drops the attribute.
_CREATE_BREAKAWAY_FROM_JOB: int = getattr(subprocess, "CREATE_BREAKAWAY_FROM_JOB", 0x01000000)


def launch_hidden_powershell(
    args: list[str],
    *,
    cwd: str | None = None,
    breakaway: bool = False,
) -> subprocess.Popen[bytes]:
    """Launch *args* (a ``powershell.exe`` command line) hidden, with safe stdio.

    ``CREATE_NO_WINDOW`` (never ``DETACHED_PROCESS``) plus explicit
    ``DEVNULL`` handles on all three streams keeps the child from crashing the
    instant it starts when the parent is a no-console frozen app (#142).

    When ``breakaway=True`` the child MUST outlive this process (the updater
    and uninstall-helper launches, since the tray quits moments after calling
    this): ``CREATE_BREAKAWAY_FROM_JOB`` is requested, with a same-args retry
    (flag dropped) if the enclosing Job Object forbids it -- so the operation
    is never worse off than launching without the flag at all.
    """
    base_flags = subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP
    kwargs: dict[str, object] = {
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
    }
    if cwd is not None:
        kwargs["cwd"] = cwd

    if not breakaway:
        return subprocess.Popen(args, creationflags=base_flags, **kwargs)  # type: ignore[call-overload]

    try:
        return subprocess.Popen(
            args,
            creationflags=base_flags | _CREATE_BREAKAWAY_FROM_JOB,
            **kwargs,  # type: ignore[call-overload]
        )
    except OSError:
        logging.warning(
            "launch_hidden_powershell: CREATE_BREAKAWAY_FROM_JOB denied; launching in-job",
            exc_info=True,
        )
        return subprocess.Popen(args, creationflags=base_flags, **kwargs)  # type: ignore[call-overload]


__all__ = ["POWERSHELL_EXE", "launch_hidden_powershell"]
