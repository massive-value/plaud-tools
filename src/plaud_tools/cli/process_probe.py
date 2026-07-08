"""Process-enumerator introspection for `doctor` (formerly `mcp_lifecycle.py`).

Historically this module also contained a scoped-shutdown helper and two
PowerShell-snippet generators (ADR 003). An audit (2026-07-06) found those
~470 lines had zero production callers: ``install.ps1`` reimplements the
shutdown/zip-probe logic inline in PowerShell, and nothing else in ``src/``
invoked the Python helpers. They were deleted along with their dedicated
test file (``tests/test_mcp_lifecycle.py``). ``active_enumerator_name`` is
the one function with a real caller (``doctor.py``'s ``run_doctor``) and is
kept.
"""

from __future__ import annotations

import os

__all__ = ["active_enumerator_name"]


def active_enumerator_name() -> str:
    """Return a short label describing which process enumerator is active.

    - ``"psutil"``     — the ``psutil`` package is importable (preferred path).
    - ``"wmic"``       — psutil absent, Windows, and ``wmic.exe`` is available.
    - ``"powershell"`` — psutil absent, Windows, wmic not available (Win11 22H2+
                         or legacy missing), PowerShell is the final fallback.
    - ``"none"``       — psutil absent and not on Windows (POSIX without psutil).

    The result is purely probe-time: it answers "which enumerator *would* be
    used right now" without actually enumerating any processes.
    """
    try:
        import psutil  # type: ignore[import]  # noqa: F401

        return "psutil"
    except ImportError:
        pass

    if os.name != "nt":
        return "none"

    # On Windows, WMIC is tried first.  wmic.exe was removed from Win11 22H2+,
    # so probe via shutil.which rather than running it.
    import shutil

    if shutil.which("wmic") is not None:
        return "wmic"

    return "powershell"
