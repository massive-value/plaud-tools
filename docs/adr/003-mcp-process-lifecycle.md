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
- **Use psutil as a required dependency**: ~~`psutil` is optional.~~
  As of 2026-06-12 (Wave 2 / C4), `psutil>=5.9` is a **required** dependency
  of the `[tray]` optional-dependency extra in `pyproject.toml` and is listed
  in the tray bundle's `hiddenimports` in `pyinstaller/plaud-tray.spec`.  In
  the frozen bundle psutil is always available and is always the active
  enumerator; the PowerShell fallback in `_windows_fallback_enumerator` is
  retained as defense-in-depth but is not exercised during normal operation.
  See the amendment below for details.
- **Fixed sleep as a fallback**: Rejected in favour of polling. A fixed sleep
  always waits the full duration even when the process exits immediately; a
  poll exits as soon as the process is gone, or escalates exactly on deadline.
- **Call `icon.stop()` from a background thread in `_quit()`**: This avoids
  the deadlock but introduces a new race where the thread might outlive the
  process. Letting `_run()` own the `icon.stop()` call after mainloop exits
  is simpler and correct.

## Amendment — 2026-06-12 (Wave 0 / A5: enumeration honesty)

The original docstring for `_default_process_enumerator` claimed it fell back
"finally to parsing the output of `tasklist /FO CSV /V`", but this path was
never implemented. The actual fallback was WMIC only — which itself was removed
from Windows 11 22H2+. This left the enumerator silently yielding nothing when
`psutil` was absent on modern Windows, violating the observability goal and
breaking MCP child detection during update/uninstall on affected machines.

**Change:** The fallback chain in `_default_process_enumerator` is now:

1. `psutil` (preferred, cross-platform)
2. WMIC via `subprocess` (legacy fallback, Windows only; silently skipped on
   Win11 22H2+ where WMIC is absent)
3. PowerShell `Get-Process | Where-Object { $_.Path } | ConvertTo-Csv` via
   `subprocess` (modern Windows fallback; yields full `Path` property values,
   satisfying the path-scoping requirement from this ADR)

When `psutil` is absent **and** all Windows fallbacks yield zero entries, a
`WARNING` is logged rather than silently returning, making the failure
observable in production logs. The alternative decision reference above has
been updated accordingly.

Deciding whether to bundle `psutil` into the frozen distribution is a separate
concern tracked as task C4.

## Amendment — 2026-06-12 (Wave 2 / C4: psutil bundled as tray dependency)

`psutil>=5.9` is now a declared dependency of the `[tray]` optional-dependency
extra (`pyproject.toml`) and is collected into the frozen tray bundle via
explicit `hiddenimports` in `pyinstaller/plaud-tray.spec`:

```python
'psutil',
'psutil._psutil_windows',
'psutil._psutil_common',
'psutil._pswindows',
```

psutil ships C extensions that PyInstaller's static analysis cannot discover
automatically.  The explicit `hiddenimports` entries ensure the extension
modules are present in the frozen archive.

**Fallback retention:** The PowerShell fallback chain in
`_windows_fallback_enumerator` (WMIC → `Get-Process | ConvertTo-Csv`) is
**retained** as defense-in-depth.  In normal operation the frozen bundle uses
psutil; the fallbacks are only reachable if the bundle is run in a degraded
environment where psutil fails to import despite being present (e.g. an
unexpected DLL load failure).

**CI frozen-import proof:** The `bundle-smoke` job in `.github/workflows/ci.yml`
now builds the tray spec and runs
`PlaudTools.exe --diagnose-enum`.  This flag is implemented in
`scripts/plaud_tray_entry.py`; it imports `plaud_tools.mcp_lifecycle`, reports
`enumerator=psutil` when psutil is importable in the frozen context, and exits 0
without starting the tkinter/pystray GUI (critical: CI runners have no display).
The CI step asserts the output contains `enumerator=psutil`; any regression
(e.g. a missing C-extension hiddenimport) causes a hard CI failure before merge.
