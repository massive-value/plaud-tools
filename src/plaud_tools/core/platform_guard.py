"""Keep the stdlib ``platform`` module away from WMI on Windows.

On Windows, ``platform.system()`` / ``uname()`` / ``machine()`` ask WMI first
through CPython's ``_wmi`` module. ``_wmi.exec_query`` gives up after 100 ms
when WMI is slow to connect (common when an AI client starts several servers
at once), but its worker thread keeps running. On Python 3.12 that thread then
calls ``CloseHandle`` on a value read from the caller's dead stack frame
(fixed upstream in 3.13 by GH-130727, never backported). The stray close can
land on the ntdll thread pool's worker-factory handle, after which two pool
threads spin in ``NtWaitForWorkViaWorkerFactory`` at ~1 core each for the rest
of the process's life.

``keyring`` triggers this: importing it runs ``platform.system()`` (via
``jaraco.context``), and the MCP server imports it on the first tool call.
"""

from __future__ import annotations

import platform
import sys
from typing import NoReturn


def _wmi_unavailable(*_keys: str) -> NoReturn:
    raise OSError("WMI queries disabled by plaud-tools (see platform_guard)")


def disable_wmi_queries() -> None:
    """Make ``platform`` use its non-WMI fallbacks. Call once at process start.

    ``platform`` already catches ``OSError`` from its WMI helper and falls back
    to ``sys.getwindowsversion()`` and ``PROCESSOR_ARCHITECTURE``, the same path
    it takes today whenever the WMI query times out. No-op off Windows.
    """
    if sys.platform == "win32":
        platform._wmi_query = _wmi_unavailable  # type: ignore[attr-defined]
