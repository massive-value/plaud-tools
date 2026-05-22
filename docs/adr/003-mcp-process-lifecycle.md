# ADR 003 — Tray↔MCP Process Lifecycle Contract

**Status:** accepted (2026-05-21)

## Problem

The bundle ships three long-running processes: `PlaudTools.exe` (tray app),
`plaud-mcp.exe` (MCP server), and optionally `plaud-cli.exe`. The tray app
acts as the lifecycle manager for the bundle.

Two operations — in-app update and uninstall — need to terminate
`plaud-mcp.exe` before they can replace or delete files in the install
directory (Windows file-lock semantics: a running `.exe` holds a lock on its
own image file).

The original implementation used a blanket
`Stop-Process -Name plaud-mcp -Force` in the detached PowerShell helpers.
This had three problems:

1. **Over-broad scope** — it would kill any process named `plaud-mcp` on the
   machine, including processes belonging to other users, other installs (e.g.
   a dev checkout alongside the production bundle), or future multi-instance
   scenarios.

2. **Race condition** — a fixed `Start-Sleep -Seconds 2` after the kill was
   used to wait for the process to fully exit before `Expand-Archive`. If an
   AI client respawned `plaud-mcp` in that 2 s window, the extraction would
   still fail.

3. **Tray deadlock** — `_quit()` called `icon.stop()` synchronously on the
   tkinter main thread. pystray's backend thread can be waiting to post a
   callback back to the tk thread; calling `icon.stop()` from *within* a
   pystray callback (i.e. "Quit" menu item) could deadlock.

## Decisions

### Who spawns `plaud-mcp`

`plaud-mcp.exe` is spawned by AI clients (Claude Desktop, Claude Code, Codex)
via their MCP server configuration. The tray app does **not** spawn it. The
tray sets up the AI client configuration (the `mcp_servers` entry) but
delegates process management to the AI client.

### Who is allowed to kill `plaud-mcp`

The tray app is the only component in the bundle that kills `plaud-mcp`.
It does so only during:

- **In-app update** — to release the file lock on `mcp/plaud-mcp.exe` before
  `Expand-Archive` overwrites it.
- **Uninstall** — to release file locks before `Remove-Item` deletes the
  install directory.

No other component issues `Stop-Process` or equivalent.

### Scoped shutdown helper

`src/plaud_tools/mcp_lifecycle.py` provides two public surfaces:

**`shutdown_mcp_children(install_dir, *, grace_seconds=3.0)`** (Python)
Enumerates running `plaud-mcp` processes whose executable path is inside
`install_dir`. Sends a graceful signal (CTRL_BREAK on Windows, SIGTERM on
POSIX), polls until exit, then force-kills survivors after `grace_seconds`.
Returns the list of PIDs acted on.

**`mcp_shutdown_ps1_snippet(install_dir, grace_seconds=3)`** (PowerShell)
Returns a PowerShell code block for embedding in the detached PS1 helpers.
Uses `Get-Process -Name plaud-mcp | Where-Object { $_.Path.StartsWith(...) }`
to scope the kill, `CloseMainWindow()` for graceful shutdown, and polls with
`Start-Sleep -Milliseconds 100` until `HasExited` before force-killing.
Replaces the old `Stop-Process -Name plaud-mcp -Force` one-liner.

Both helpers use poll-until-exit rather than a fixed sleep, eliminating the
2 s race window.

### `_quit()` deadlock fix

`TrayApp._quit()` no longer calls `icon.stop()` directly. It schedules
`root.destroy()` via `_tk()` (the tkinter-safe scheduling wrapper). When
`root.mainloop()` returns, `_run()` calls `icon.stop()` from the main thread —
which is always safe. This removes the deadlock path where `icon.stop()` was
called from within a pystray menu callback (pystray thread), potentially
blocking on a callback that needed the tk thread to service it.

## Alternatives rejected

- **Kill by PID rather than name+path**: The tray does not spawn `plaud-mcp`
  directly, so it has no PID to track. Path-scoped enumeration is the
  correct primitive.
- **Use psutil as a required dependency**: `psutil` is optional.
  `mcp_lifecycle.py` falls back to WMIC on Windows when `psutil` is absent.
  The PowerShell snippet used in production bundles does not need Python at all.
- **Fixed sleep as a fallback**: Rejected in favour of polling. A fixed sleep
  always waits the full duration even when the process exits immediately; a
  poll exits as soon as the process is gone, or escalates exactly on deadline.
- **Call `icon.stop()` from a background thread in `_quit()`**: This avoids
  the deadlock but introduces a new race where the thread might outlive the
  process. Letting `_run()` own the `icon.stop()` call after mainloop exits
  is simpler and correct.
