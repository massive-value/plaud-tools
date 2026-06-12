"""MCP child-process lifecycle helpers.

Provides a scoped, graceful shutdown helper for ``plaud-mcp`` processes
and the matching PowerShell snippet embedded in the update / uninstall
PS1 generators.

Design goals (ADR 003):
- Scope kills to processes whose executable path is *inside* a given
  install directory, so unrelated MCP processes from other users or
  installs on the same machine are never touched.
- Attempt graceful shutdown first (close stdin, then CTRL_BREAK signal);
  force-kill only after a configurable grace period.
- Poll until the process exits rather than sleeping a fixed duration.
"""
from __future__ import annotations

import ctypes
import logging
import os
import signal
import subprocess
import time
from pathlib import Path
from typing import Callable, Iterator, NamedTuple

__all__ = [
    "ProcessInfo",
    "enumerate_mcp_processes",
    "shutdown_mcp_children",
    "mcp_shutdown_ps1_snippet",
    "zip_layout_probe_ps1_snippet",
]

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Process enumeration
# ---------------------------------------------------------------------------

class ProcessInfo(NamedTuple):
    pid: int
    exe_path: str


def _default_process_enumerator() -> Iterator[ProcessInfo]:
    """Yield (pid, exe_path) for every running process.

    Fallback chain (Windows):
    1. ``psutil`` — preferred, cross-platform, full exe paths.
    2. WMIC — kept as a first Windows fallback for legacy compatibility, but
       WMIC was removed from Windows 11 22H2+ so this will silently fail on
       modern systems.
    3. PowerShell ``Get-Process`` — reliable on all modern Win11 installs.
       Yields full exe ``Path`` values, satisfying the ADR 003 path-scoping
       requirement (consumers filter by ``Path.resolve().relative_to(install_dir)``).

    On non-Windows systems, only psutil is attempted; when psutil is absent
    the enumerator yields nothing (non-Windows bundles are expected to include
    psutil — see task C4).

    When psutil is unavailable AND Windows enumeration yields zero results, a
    WARNING is emitted so silent failures are observable in logs.
    """
    try:
        import psutil  # type: ignore[import]
        for proc in psutil.process_iter(["pid", "exe"]):
            try:
                exe = proc.info.get("exe") or ""
                if exe:
                    yield ProcessInfo(pid=proc.info["pid"], exe_path=exe)
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
        return
    except ImportError:
        pass

    # psutil is absent — attempt Windows-only fallbacks.
    if os.name != "nt":
        # Non-Windows without psutil: nothing we can do; C4 should bundle psutil.
        logger.warning(
            "_default_process_enumerator: psutil not available on non-Windows platform; "
            "no processes can be enumerated. Install psutil to fix."
        )
        return

    _creationflags = subprocess.CREATE_NO_WINDOW if hasattr(subprocess, "CREATE_NO_WINDOW") else 0
    collected: list[ProcessInfo] = []

    # --- Fallback 1: WMIC (removed on Win11 22H2+, kept for legacy compat) ---
    try:
        out = subprocess.check_output(
            ["wmic", "process", "get", "ProcessId,ExecutablePath", "/FORMAT:CSV"],
            text=True,
            creationflags=_creationflags,
            stderr=subprocess.DEVNULL,
            timeout=10,
        )
        for line in out.splitlines():
            parts = [p.strip() for p in line.split(",")]
            if len(parts) >= 3:
                exe_path, pid_str = parts[1], parts[2]
                if not exe_path:
                    continue
                try:
                    collected.append(ProcessInfo(pid=int(pid_str), exe_path=exe_path))
                except ValueError:
                    continue
        if collected:
            logger.debug("_default_process_enumerator: WMIC yielded %d entries", len(collected))
            yield from collected
            return
        # WMIC ran but returned no entries — fall through to PowerShell.
        logger.debug("_default_process_enumerator: WMIC returned no entries, trying PowerShell")
    except Exception:
        logger.debug("_default_process_enumerator: WMIC fallback failed, trying PowerShell", exc_info=True)

    # --- Fallback 2: PowerShell Get-Process (available on all modern Win11) ---
    # We request Id and Path as CSV so we can parse full exe paths, satisfying
    # the ADR 003 path-scoping requirement.  Processes without a Path property
    # (system/kernel processes) are silently excluded — they are never plaud-mcp.
    #
    # Command emits CSV lines: "Id","Path"
    # e.g.: "1234","C:\Programs\PlaudTools\mcp\plaud-mcp.exe"
    _PS_CMD = (
        "Get-Process | Where-Object { $_.Path } | "
        "Select-Object Id,Path | "
        "ConvertTo-Csv -NoTypeInformation"
    )
    try:
        out = subprocess.check_output(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", _PS_CMD],
            text=True,
            creationflags=_creationflags,
            stderr=subprocess.DEVNULL,
            timeout=15,
        )
        # First line is the CSV header: "Id","Path" — skip it.
        lines = out.splitlines()
        for line in lines[1:]:
            line = line.strip()
            if not line:
                continue
            # Strip surrounding quotes and split on `","` to handle embedded commas in paths.
            # ConvertTo-Csv wraps every field in double-quotes.
            stripped = line.strip('"')
            # Split on `","` — the delimiter between quoted fields.
            parts = stripped.split('","', 1)
            if len(parts) != 2:
                continue
            pid_str, exe_path = parts
            if not exe_path:
                continue
            try:
                collected.append(ProcessInfo(pid=int(pid_str), exe_path=exe_path))
            except ValueError:
                continue

        if collected:
            logger.debug(
                "_default_process_enumerator: PowerShell Get-Process yielded %d entries",
                len(collected),
            )
        else:
            # Zero results with no psutil — likely a permissions or environment issue;
            # warn so operators can see the silent-failure in logs.
            logger.warning(
                "_default_process_enumerator: psutil unavailable and all Windows fallbacks "
                "(WMIC, PowerShell Get-Process) returned zero process entries. "
                "MCP child detection is disabled for this run."
            )

        yield from collected
    except Exception:
        logger.warning(
            "_default_process_enumerator: PowerShell Get-Process fallback failed; "
            "psutil is unavailable. MCP child detection is disabled for this run.",
            exc_info=True,
        )


def enumerate_mcp_processes(
    install_dir: Path,
    *,
    enumerator: Callable[[], Iterator[ProcessInfo]] | None = None,
) -> list[ProcessInfo]:
    """Return all running processes whose exe path is inside *install_dir*.

    Only processes named ``plaud-mcp`` (case-insensitive) are considered.
    The *enumerator* parameter lets callers inject a stub for unit testing.
    """
    _enum = enumerator or _default_process_enumerator
    install_dir_resolved = install_dir.resolve()
    matches: list[ProcessInfo] = []
    for proc in _enum():
        if not proc.exe_path:
            continue
        name = Path(proc.exe_path).stem.lower()
        if name != "plaud-mcp":
            continue
        try:
            exe_resolved = Path(proc.exe_path).resolve()
        except Exception:
            continue
        try:
            exe_resolved.relative_to(install_dir_resolved)
            matches.append(proc)
        except ValueError:
            # Not under install_dir — skip
            continue
    return matches


# ---------------------------------------------------------------------------
# Shutdown logic
# ---------------------------------------------------------------------------

def _terminate_gracefully(pid: int) -> None:
    """Send a graceful shutdown signal to *pid*.

    On Windows: GenerateConsoleCtrlEvent (CTRL_BREAK_EVENT) to the process
    group, then close stdin via NtClose handles is not easily accessible, so
    we rely on CTRL_BREAK followed by the grace-period poll.

    On POSIX: SIGTERM.
    """
    if os.name == "nt":
        try:
            # CTRL_BREAK_EVENT lets the child handle it via SetConsoleCtrlHandler
            ctypes.windll.kernel32.GenerateConsoleCtrlEvent(1, pid)  # 1 = CTRL_BREAK_EVENT
        except Exception:
            logger.debug("GenerateConsoleCtrlEvent failed for pid %d", pid, exc_info=True)
    else:
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass


def _process_alive(pid: int) -> bool:
    """Return True if a process with *pid* is still running."""
    if os.name == "nt":
        SYNCHRONIZE = 0x100000
        handle = ctypes.windll.kernel32.OpenProcess(SYNCHRONIZE, False, pid)
        if not handle:
            return False
        result = ctypes.windll.kernel32.WaitForSingleObject(handle, 0)
        ctypes.windll.kernel32.CloseHandle(handle)
        # 0x102 = WAIT_TIMEOUT (still running), 0 = WAIT_OBJECT_0 (exited)
        return result == 0x102
    else:
        try:
            os.kill(pid, 0)
            return True
        except ProcessLookupError:
            return False
        except PermissionError:
            return True  # process exists but we can't signal it


def _force_kill(pid: int) -> None:
    """Forcibly terminate *pid*."""
    if os.name == "nt":
        PROCESS_TERMINATE = 0x0001
        handle = ctypes.windll.kernel32.OpenProcess(PROCESS_TERMINATE, False, pid)
        if handle:
            ctypes.windll.kernel32.TerminateProcess(handle, 1)
            ctypes.windll.kernel32.CloseHandle(handle)
    else:
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


def shutdown_mcp_children(
    install_dir: Path,
    *,
    grace_seconds: float = 3.0,
    poll_interval: float = 0.1,
    enumerator: Callable[[], Iterator[ProcessInfo]] | None = None,
) -> list[int]:
    """Gracefully shut down all ``plaud-mcp`` processes inside *install_dir*.

    Steps:
    1. Enumerate running ``plaud-mcp`` processes whose exe path is under
       *install_dir*.
    2. Send a graceful signal (CTRL_BREAK on Windows, SIGTERM on POSIX) to
       each.
    3. Poll until each process exits, waiting up to *grace_seconds*.
    4. Force-kill any that are still alive after the grace period.

    Returns the list of PIDs that were acted on.
    """
    procs = enumerate_mcp_processes(install_dir, enumerator=enumerator)
    if not procs:
        logger.debug("shutdown_mcp_children: no plaud-mcp processes found under %s", install_dir)
        return []

    pids = [p.pid for p in procs]
    logger.info("shutdown_mcp_children: signalling %d process(es): %s", len(pids), pids)

    for pid in pids:
        _terminate_gracefully(pid)

    deadline = time.monotonic() + grace_seconds
    still_alive = list(pids)
    while still_alive and time.monotonic() < deadline:
        time.sleep(poll_interval)
        still_alive = [pid for pid in still_alive if _process_alive(pid)]

    for pid in still_alive:
        logger.warning("shutdown_mcp_children: grace period expired, force-killing pid %d", pid)
        _force_kill(pid)

    # Poll briefly to confirm force-killed processes are gone
    confirm_deadline = time.monotonic() + 2.0
    remaining = list(still_alive)
    while remaining and time.monotonic() < confirm_deadline:
        time.sleep(poll_interval)
        remaining = [pid for pid in remaining if _process_alive(pid)]
    if remaining:
        logger.error("shutdown_mcp_children: could not kill pids %s", remaining)

    return pids


# ---------------------------------------------------------------------------
# PowerShell snippet for PS1 generators
# ---------------------------------------------------------------------------

def mcp_shutdown_ps1_snippet(install_dir: str, grace_seconds: int = 3) -> str:
    """Return a PowerShell code block that shuts down scoped plaud-mcp processes.

    The snippet:
    - Finds all ``plaud-mcp`` processes whose ``Path`` property starts with
      *install_dir* (case-insensitive).
    - Sends a graceful CTRL_BREAK via ``[Console]::TreatControlCAsInput`` /
      ``GenerateConsoleCtrlEvent`` is not easily available in PS without P/Invoke,
      so the graceful step uses ``CloseMainWindow()``.
    - Polls until exit, then force-kills after *grace_seconds* seconds.
    - Replaces the old ``Stop-Process -Name plaud-mcp -Force`` one-liner.

    The returned string is ready to be embedded verbatim in a PS1 file.
    """
    # Normalise: ensure no trailing backslash so StartsWith works cleanly
    safe_dir = install_dir.rstrip("\\").rstrip("/")
    return (
        f"# Shut down plaud-mcp processes scoped to the install directory.\n"
        f"$installDir = '{safe_dir}'\n"
        f"$mcpProcs = Get-Process -Name 'plaud-mcp' -ErrorAction SilentlyContinue | Where-Object {{\n"
        f"    $_.Path -and $_.Path.ToLower().StartsWith($installDir.ToLower())\n"
        f"}}\n"
        f"if ($mcpProcs) {{\n"
        f"    foreach ($p in $mcpProcs) {{ $p.CloseMainWindow() | Out-Null }}\n"
        f"    $deadline = (Get-Date).AddSeconds({grace_seconds})\n"
        f"    while ($mcpProcs | Where-Object {{ !$_.HasExited }}) {{\n"
        f"        if ((Get-Date) -gt $deadline) {{ break }}\n"
        f"        Start-Sleep -Milliseconds 100\n"
        f"    }}\n"
        f"    $mcpProcs | Where-Object {{ !$_.HasExited }} | Stop-Process -Force -ErrorAction SilentlyContinue\n"
        f"    # Poll until fully exited\n"
        f"    $exitDeadline = (Get-Date).AddSeconds(2)\n"
        f"    while (($mcpProcs | Where-Object {{ !$_.HasExited }}) -and (Get-Date) -lt $exitDeadline) {{\n"
        f"        Start-Sleep -Milliseconds 100\n"
        f"    }}\n"
        f"}}\n"
    )


# ---------------------------------------------------------------------------
# PowerShell snippet: zip layout probe for PS1 generators
# ---------------------------------------------------------------------------

def zip_layout_probe_ps1_snippet(zip_var: str, install_dir_var: str, dest_var: str) -> str:
    """Return a PowerShell code block that probes a zip's layout and sets *dest_var*.

    Known shapes:
      A) Single top-level directory (e.g. ``PlaudTools\\...``): sets *dest_var*
         to the **parent** of the install dir so files land at
         ``Programs\\PlaudTools\\`` not ``Programs\\PlaudTools\\PlaudTools\\``.
      B) Files at root of zip (flat layout): sets *dest_var* to the install dir
         itself.

    Parameters
    ----------
    zip_var:
        Name of the PS variable (without ``$``) holding the zip file path.
    install_dir_var:
        Name of the PS variable (without ``$``) holding the install directory.
    dest_var:
        Name of the PS variable (without ``$``) to assign the destination to.

    The returned string is ready to be embedded verbatim in a PS1 file.
    """
    return (
        f"# Probe zip layout: shape A (top-level folder) → extract to parent;\n"
        f"# shape B (flat/multi-root) → extract directly to install dir.\n"
        f"Add-Type -AssemblyName System.IO.Compression.FileSystem\n"
        f"$_zip = [System.IO.Compression.ZipFile]::OpenRead(${zip_var})\n"
        f"try {{\n"
        f"    $_topLevel = @{{}}\n"
        f"    foreach ($_e in $_zip.Entries) {{\n"
        f"        $_name = $_e.FullName.TrimStart('/', '\\')\n"
        f"        if (-not $_name) {{ continue }}\n"
        f"        $_seg = ($_name -split '[/\\\\]')[0]\n"
        f"        if ($_seg) {{ $_topLevel[$_seg] = 1 }}\n"
        f"    }}\n"
        f"    $_roots = @($_topLevel.Keys)\n"
        f"    if ($_roots.Count -eq 1) {{\n"
        f"        $_prefix = $_roots[0] + '/'\n"
        f"        $_hasChildren = $_zip.Entries | Where-Object {{\n"
        f"            $_.FullName -ne $_prefix -and $_.FullName.StartsWith($_prefix)\n"
        f"        }}\n"
        f"        if ($_hasChildren) {{\n"
        f"            ${dest_var} = Split-Path ${install_dir_var} -Parent\n"
        f"        }} else {{\n"
        f"            ${dest_var} = ${install_dir_var}\n"
        f"        }}\n"
        f"    }} else {{\n"
        f"        ${dest_var} = ${install_dir_var}\n"
        f"    }}\n"
        f"}} finally {{\n"
        f"    $_zip.Dispose()\n"
        f"}}\n"
    )
