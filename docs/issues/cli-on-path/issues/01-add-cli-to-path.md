# 01 — Add bundled CLI directory to PATH at install time

Status: needs-triage
Labels: needs-triage

## Problem

After installing the PlaudTools bundle to `%LOCALAPPDATA%\Programs\PlaudTools\`, the shipped `plaud.exe` lives at `…\PlaudTools\cli\plaud.exe` and is not on PATH. Users who want to use the CLI have to invoke it by full path or add the directory to PATH themselves.

The `plaud-mcp.exe` server is wired into AI clients via absolute path by the tray's "Manage AI clients" flow, so it has no discoverability problem. The CLI does — it's intended for humans to type.

Today the only `plaud` / `pld` on PATH is the pip-installed wrapper from `pip install plaud-tools`, which is a dev-time install path, not the shipped bundle. A user who installed only the tray bundle has no `plaud` command available.

## Goal

After installing the tray bundle, `plaud` and `pld` work from any shell without manual PATH editing.

## Options to evaluate

1. **Append `…\PlaudTools\cli\` to the user PATH at install time.** Cleanest UX. Requires an installer step — currently the "install" is "unzip and run", so there's no install-time hook. Would need either a real installer (MSI, Inno Setup, Squirrel) or a first-run step in the tray that edits the user-env PATH via `HKCU\Environment` and broadcasts `WM_SETTINGCHANGE`.
2. **Drop a shim `plaud.cmd` / `pld.cmd` into a directory already on user PATH** (e.g. `%LOCALAPPDATA%\Microsoft\WindowsApps\`). Simpler than option 1 — just two small text files — but writing into Microsoft's reserved WindowsApps dir is fragile and not guaranteed across Windows versions. Better target: create our own `%LOCALAPPDATA%\Programs\PlaudTools\bin\` and add only that one dir to PATH (option 1 still needed for the PATH bit).
3. **Move to a real installer** (Inno Setup is the lowest-friction Windows option). Handles PATH, Start menu, uninstall, and per-user vs all-users in one shot. Bigger lift but solves several adjacent frictionless-install asks at once.

Option 1 done from the tray at first run is the smallest viable step; option 3 is the right destination if this becomes a pattern.

## Acceptance criteria

- After a clean install of the tray bundle, opening a new PowerShell/cmd window and typing `plaud --help` shows the CLI usage.
- The PATH change is reversible — uninstalling the bundle (or the user toggling it off) removes the entry.
- No admin elevation required (per-user only).
- Documented in `docs/INSTALL.md` so users know `plaud` is available globally after install.

## Out of scope

- A top-level `--version` flag on the CLI (`plaud --version`). Worth filing separately if anyone wants it.
- Shell completions (PowerShell `Register-ArgumentCompleter`, bash, zsh).
- macOS / Linux equivalents — the bundle is Windows-only today.

## Context

Came out of testing the v0.1.5 build locally: the frozen `cli\plaud.exe` works (after fixing the relative-import bug in commit TBD), but the user had to invoke it by full path because nothing about the bundle puts it on PATH. The tray's UX is otherwise frictionless, so the CLI should match.
