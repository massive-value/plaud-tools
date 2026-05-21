# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.1.13] - 2026-05-21

### Changed

- `scripts/install.ps1` extraction step now uses a .NET `ZipFile` loop
  with the same inline `[===---]` progress bar as the download step.
  `Expand-Archive` rendered an ugly Windows Terminal overlay; the new
  approach is visually consistent end-to-end.
- In-app update bat helper now passes `$ProgressPreference='SilentlyContinue'`
  to its `Expand-Archive` call so the same overlay is suppressed during
  the self-update extraction step.
- `WizardWindow` (Configure AI Agents) cleaned up: removed the session
  header ("Signed in as…"), **Test Connection** button, **Sign out**
  button, and version footer. All of these now live exclusively on
  `HomeWindow`. The dialog title is updated to "Configure AI Agents"
  and the window is sized to fit its reduced content.

## [0.1.12] - 2026-05-21

### Added

- Tray `HomeWindow` — left-clicking the tray icon now opens a dashboard
  window instead of doing nothing. Contains: session header ("Signed in as
  {email}. Token valid for {N} days."), **Configure AI Agents…** (opens the
  existing `WizardWindow`), **Test Connection**, **Check for Updates**
  (runs the update check inline; auto-opens `UpdateDialog` if a newer
  version is found; disabled when the background poller has already
  detected an update), **Sign out**, and **Uninstall…**. Version footer
  matches the `WizardWindow` style.

### Fixed

- Tray `HomeWindow` button order corrected: Configure AI Agents → Test
  Connection → Check for Updates.
- `HomeWindow` window height increased to 400×420 so all buttons are
  fully visible without scrolling.
- `HomeWindow` "Uninstall…" no longer destroys `HomeWindow` before the
  `UninstallDialog` is shown, preventing accidental uninstalls when the
  window was too small to read button labels.
- Disabled UPX compression in `pyinstaller/plaud-tray.spec`. The
  UPX-compressed bootloader was triggering Windows Defender's
  `Trojan:Win32/Bearfoos.A!ml` ML heuristic, causing Defender to
  quarantine and delete `PlaudTools.exe` silently during normal use.

## [0.1.11] - 2026-05-21

### Added

- `plaud-tools update` CLI subcommand for pip users. Wraps
  `sys.executable -m pip install --upgrade plaud-tools` with inherited
  stdio for live pip output and propagates pip's exit code. Prints a
  trailing reminder that pipx, uv, and conda users should use their own
  upgrade command instead. (#10)
- `scripts/install.ps1` one-liner installer for bundle users. Resolves
  the latest GitHub release, downloads `PlaudTools.zip` to temp,
  extracts to `%LOCALAPPDATA%\Programs\PlaudTools\`, and launches
  `PlaudTools.exe`. Refuses to overwrite an existing install (points the
  user to the tray updater). No admin elevation required. `docs/INSTALL.md`
  now leads with the one-liner; manual zip extraction is retained as the
  advanced path. (#13)
- Tray in-app updater. When an update is available the tray now shows an
  `UpdateDialog` with current/available versions and an "Install update
  and restart" button. The button downloads `PlaudTools.zip` with live
  byte-count progress, writes a `.bat` helper that waits for the tray
  PID to exit, expands the zip over the install directory, relaunches
  `PlaudTools.exe`, and self-deletes. Download failures re-enable the
  button with an error label. In dev mode the install action is
  unavailable; the existing browser-fallback menu item is preserved.
  `_check_for_update` now also returns the zip asset URL. (#14)
- Tray uninstaller. New "Uninstall…" tray menu item opens a checklist
  dialog with six items: remove from user PATH, remove autostart
  registry key, remove PowerShell profile sourcing lines, delete install
  directory (default checked); delete session/credentials, delete log
  files (default unchecked). Install directory deletion uses a `.bat`
  helper that waits on the tray PID, removes the directory, and
  self-deletes — Windows cannot remove a running `.exe` in place. In dev
  mode the install-dir step is skipped with a log warning. (#12)

### Changed

- Tray update check is now wake-aware with jittered cadence. The
  fire-and-forget `_poll_update` call is replaced by `_update_poll_loop`
  which runs the first check immediately, then sleeps in 5-minute
  chunks comparing wall-clock elapsed time against a random
  `[20h, 28h]` interval (re-rolled per check). Wall-clock comparison
  catches checks missed during laptop sleep within 5 minutes of waking;
  the jitter spreads GitHub API hits across the user fleet. (#11)
- Tray log directory moved from `%LOCALAPPDATA%\Plaud\` to
  `%LOCALAPPDATA%\PlaudTools\` so we no longer share a directory with
  the official Plaud desktop app. `_open_log_folder` updated to match.
  (#9)

## [0.1.10] - 2026-05-19

### Fixed

- Python 3.11 / 3.10 syntax error in `client.py`: a nested f-string reusing
  the same quote type is only valid in Python 3.12+. Extracted the inner
  expression to a local variable so all supported Python versions parse
  correctly.
- `release.yml` publish job now explicitly declares `contents: read` alongside
  `id-token: write`. When a job overrides `permissions`, all unlisted
  permissions default to `none`; the omission caused `actions/checkout` to
  fail with "repository not found" on the first tag push.

## [0.1.9] - 2026-05-19

### Added

- `CONTRIBUTING.md` covering dev setup, the `PLAUD_LIVE_READS=1` live-test
  gate, branching, and the GitHub Issues tracker.
- `.github/` issue and pull request templates: `bug_report.md`,
  `feature_request.md`, `config.yml` (blank issues disabled, security
  contact link), and `PULL_REQUEST_TEMPLATE.md`.
- `.github/workflows/ci.yml` running `pytest -q` on every PR and push to
  `master` across a 3×2 matrix (Python 3.11/3.12/3.13 × windows-latest /
  ubuntu-latest, `fail-fast: false`).
- `publish` job in `.github/workflows/release.yml` that builds the sdist and
  wheel and uploads them to PyPI via Trusted Publishing (OIDC) on every `v*`
  tag push. Depends on the existing `build` job and uses the `pypi`
  environment.
- CI status badge in `README.md`.

### Changed

- Migrated the issue tracker from local markdown files under `docs/issues/` to
  GitHub Issues at https://github.com/massive-value/plaud-tools/issues. Agent
  conventions in `CLAUDE.md` and `docs/agents/issue-tracker.md` updated to
  match.
- Rewrote `README.md` to describe the current Python package — alpha and
  trademark disclaimers, `pip install plaud-tools` quickstart, the 7-tool MCP
  surface, and `plaud-tools` / `pt` / `plaud-mcp` / `plaud-tray` entry
  points. Dropped the stale TypeScript / `npm` / `tsx` / 18-tool sections.
- `docs/INSTALL.md`: removed an internal employer reference from the
  intro and expanded the MCP tools table to all 7 tools.
- `CLAUDE.md`: corrected the stale "5 tools" claim for `mcp.py` to reflect
  the actual 7-tool surface (`browse`, `get`, `mutate`, `upload`, `process`,
  `list_folders`, `merge_recordings`).
- `.gitignore`: narrowed the blanket `.claude/` ignore to
  `.claude/settings.local.json` and `.claude/worktrees/` so shareable
  project settings can be tracked.

### Removed

- `docs/python-rewrite-status.md` — superseded by the new README and
  CONTRIBUTING.md.

### Fixed

- `tests/test_ai_clients.py` no longer hard-codes a personal Windows
  username in the sample MCP exe path.

## [0.1.8] - 2026-05-19

### Added

- `wait_for_summary()` on `PlaudClient`. `process_recording` and the
  non-detach upload path now wait for summary completion after transcription
  finishes.
- `--skip-summary` flag on the CLI `upload` command for transcript-only
  workflows.
- ISO 8601 string support for `--start-time` on the CLI and `start_time` on
  the MCP `upload_recording` tool.
- `plaud dump <id>` CLI command for raw `/file/detail` debug inspection.

### Fixed

- `body_decoded` bug in `_fetch_summary_from_data_link` — now correctly uses
  `response.text()`.
- `_extract_inline_summary` is more robust: handles `dict` `data_content`,
  tries multiple key names, and falls back to `data_type` matching when
  `data_id` does not find the item.
- CLI `summary` command now passes `include_summary=True` so the data-link
  fetch is actually attempted.

## [0.1.7] - 2026-05-19

### Added

- `--version` flag on the CLI.
- PATH setup helper and shell completions for bash and PowerShell, installed
  alongside the CLI.
- `pt` short alias as a second CLI entry point.

### Changed

- Renamed the primary CLI entry point from `plaud` to `plaud-tools`. The
  PyInstaller specs, tray app, and install docs have been updated to match.

### Fixed

- Release workflow updated for the `plaud-tools` rename and the new
  `pt.cmd` shim.

## [0.1.6] - 2026-05-19

### Fixed

- Frozen builds now bundle `dist-info` metadata so the tray version footer
  renders the real version instead of `v0.0.0+dev`. `copy_metadata('plaud-tools')`
  is now applied to all three PyInstaller specs.
- Frozen `plaud.exe` no longer crashes at startup with
  `attempted relative import with no known parent package`. A new
  `scripts/plaud_entry.py` wrapper mirrors the existing
  `plaud_mcp_entry.py` / `plaud_tray_entry.py` pattern.

[Unreleased]: https://github.com/massive-value/plaud-tools/compare/v0.1.13...HEAD
[0.1.13]: https://github.com/massive-value/plaud-tools/compare/v0.1.12...v0.1.13
[0.1.12]: https://github.com/massive-value/plaud-tools/compare/v0.1.11...v0.1.12
[0.1.11]: https://github.com/massive-value/plaud-tools/compare/v0.1.10...v0.1.11
[0.1.10]: https://github.com/massive-value/plaud-tools/compare/v0.1.9...v0.1.10
[0.1.9]: https://github.com/massive-value/plaud-tools/compare/v0.1.8...v0.1.9
[0.1.8]: https://github.com/massive-value/plaud-tools/compare/v0.1.7...v0.1.8
[0.1.7]: https://github.com/massive-value/plaud-tools/compare/v0.1.6...v0.1.7
[0.1.6]: https://github.com/massive-value/plaud-tools/compare/v0.1.5...v0.1.6
