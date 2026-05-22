# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.2.0] - 2026-05-22

The v0.2.0 milestone consolidates the bundle/install/MCP-stability cycle. The
patch releases on the v0.1.x line (v0.1.20–v0.1.22) shipped the user-visible
work incrementally; v0.2.0 promotes the cumulative set and lands two final
internal-quality changes that close out the milestone:

### Changed

- Tray code reorganized: the 2,127-line `tray_app.py` is split into focused
  submodules under `src/plaud_tools/tray/` (`app`, `setup`, `updater`,
  `uninstaller`, `background`, `icons`, `toasts`, plus a `windows/` package
  for `LoginWindow`, `WizardWindow`, and `HomeWindow`). `tray_app.py` is now a
  ~250-line compatibility shim that re-exports every public symbol the rest
  of the codebase and tests rely on; external imports
  (`from plaud_tools.tray_app import main`) continue to work unchanged. (#38)

### Removed

- Dead code: `_fetch_transcript` private method on `PlaudClient`, the
  `build_read_handlers` alias in `mcp.py`, the unreachable `return ""` after
  `sys.exit(...)` in CLI `update`, and the redundant client rebuild in CLI
  `ping`. (#39)

### Internal

- Shared query helpers (`_parse_isoish`, `_filter_recordings`,
  `_summarize_recording`) deduplicated into a new `plaud_tools/query.py`
  module; previously the CLI and MCP layers each carried their own copies
  that had drifted in subtle ways (parameter naming, sort order, unfiled-
  filter semantics). Both call sites now import from `query.py` and the
  reconciled implementation supports both prior conventions. (#39)
- CLI `transcript` subcommand handler is now explicit; the previous accidental
  fall-through is replaced by an `if args.command == "transcript"` branch plus
  an `AssertionError` for any unrecognized command so future subcommands fail
  loudly when added without a handler. (#39)
- `transcribe_and_summarize` no longer computes `utcoffset()` twice. (#39)
- `_acquire_instance_lock` carries an inline comment explaining why the
  `Global\` named-event prefix is required (cross-session activation per the
  v0.1.18 single-instance fix). (#39)

### Milestone summary (work shipped during the v0.2.0 cycle)

For full detail see the v0.1.20–v0.1.22 sections below. Headline items:

- MCP: pagination (#30), structured error codes + `session_expired` tray
  toast (#33), split `delete_recording`/`rename_speaker` and `clear_folder`
  flag (#32), 7-tool description tighten (#30).
- Install: idempotent install-time tray setup (#23), `--repair`/`--force`
  switches with robust zip probe (#24), update/uninstall scripts shipped as
  bundle assets (#25), graceful scoped MCP shutdown (#22).
- Tray: first-run welcome toast and HomeWindow banner (#27), setup-failures
  banner (#46), log rotation + bounded `_test_connection` (#44), uninstall
  dialog polish + dangling AI-client config warning (#28).
- Client: `-302` region redirect now forwards request body on POST/PATCH/DELETE
  (#34), `PlaudApiError` carries structured fields (#42), `SessionManager`
  in-memory keyring cache (#43), bundled ffmpeg fallback for the CLI (#41).
- Diagnostics: `plaud-tools doctor` self-check command (#45).
- CI: bundle-smoke gate runs PyInstaller `--version` on every PR and before
  every release tag (#36).
- Tests: new lifecycle helper suite (#35), MCP tool-description golden
  snapshot (#37); coverage grew from ~270 to ~450 tests across the milestone.
- Build: UPX compression disabled across PyInstaller specs (#26).

## [0.1.22] - 2026-05-21

### Added

- Tray: `HomeWindow` now shows a yellow setup-failures banner when `_verify_env`
  reports any missing PATH, shell completions, or autostart entries; the banner
  transitions to green "Setup complete" and auto-dismisses on success, or shows
  the error and rebinds to open the log folder on failure. (#46)
- Install (`install.ps1`): `--repair` and `--force` switches plus a robust
  zip-layout probe that handles archives with or without a top-level directory.
  (#24)
- Install: setup helpers (PATH entry, PowerShell completions, autostart) are
  now idempotent and run at install time, not first launch. Stale `plaud.ps1`
  completion sourcing lines from older builds are stripped automatically. (#23)
- Install: `update.ps1` and `uninstall.ps1` are now shipped as bundle assets
  under `mcp/scripts/` instead of generated at runtime from string templates,
  which makes them auditable and code-signable. (#25)
- MCP: tool handlers now return structured `api_error` results with stable
  error codes (`session_expired`, `rate_limited`, `not_found`, `invalid_input`,
  `network`, `api_error`) instead of bare strings, and the tray shows a toast
  when a `session_expired` error is observed. (#33)
- CI: new `bundle-smoke` job on `windows-latest` that builds the PyInstaller
  bundle and runs `--version` on the frozen `plaud-tools.exe` and
  `plaud-mcp.exe`; the same smoke runs in `release.yml` before publishing. (#36)

### Changed

- Tray: `_test_connection` is bounded by a 15-second timeout via
  `_TEST_CONNECTION_TIMEOUT`; previously the call could hang the tray UI
  indefinitely on a slow Plaud API response. (#44)
- Tray uninstall dialog: uninstall buttons are now disabled while uninstall is
  in progress, and the dialog warns when AI-client configs still reference the
  installed paths after removal. (#28)

### Fixed

- Tray: `tray.log` now rotates via `RotatingFileHandler` (1 MB × 3 backups)
  instead of a single ever-growing `FileHandler`. (#44)
- MCP: `_call` no longer catches `RuntimeError` blindly; ffmpeg-not-found
  errors raised from `transcode_to_mp3` are now caught explicitly by
  `upload_recording` and surfaced as MCP error results. (#44)

### Performance

- Session: `SessionManager` now caches the loaded session in memory after the
  first keyring read, eliminating repeated keyring lookups on every API call.
  Cache is invalidated on `save()` and `clear()`. (#43)

### Tests

- New: `test_lifecycle_helpers.py` (433 tests) covering `_setup_ps_completions`,
  `_remove_ps_completions`, PATH setup/removal, and the bundled PS1 templates.
  (#35)
- New: `test_mcp_golden.py` golden-snapshot test pinning MCP tool descriptions
  to a JSON file under `tests/data/`. (#37)

## [0.1.21] - 2026-05-21

### Added

- `mcp_lifecycle.py` — scoped, graceful MCP child shutdown helper.
  `shutdown_mcp_children(install_dir)` kills only `plaud-mcp` processes
  whose executable path is inside the given install directory, attempts a
  graceful signal first (CTRL_BREAK on Windows, SIGTERM on POSIX), then
  polls until the process exits before force-killing after a configurable
  grace period (default 3 s). `mcp_shutdown_ps1_snippet(install_dir)` emits
  the equivalent PowerShell block for use in detached PS1 helpers. Both the
  update and uninstall PS1 generators now embed this snippet, replacing the
  previous blanket `Stop-Process -Name plaud-mcp -Force` and fixed
  `Start-Sleep -Seconds 2` race. `docs/adr/003-mcp-process-lifecycle.md`
  documents the tray↔MCP lifecycle contract. (#22)
- MCP `process_recording` accepts a `wait` mode: `none` returns immediately
  after the transcribe/summarize request is accepted, `transcript` waits only
  for transcript readiness, and `summary` preserves the previous blocking
  behavior. Thanks to first-time contributor @Baijack-star. (#31)
- First-run welcome: on first launch after `install.ps1`, a Windows toast
  notification appears explaining where the tray icon lives.  `HomeWindow`
  also shows a one-time blue banner directing the user to "Configure AI
  Agents…"; the banner is dismissed when that button is clicked.  The
  `plaud_just_installed.txt` sentinel is consumed immediately so neither
  surface repeats on subsequent launches.  Falls back gracefully when the
  toast API is unavailable. (#27)
- `plaud-tools doctor` — self-diagnosis CLI subcommand that prints a JSON
  document covering version, frozen/pip install mode, executable paths,
  session status (token masked), AI client MCP wiring, and the tray log
  path. (#45)
- MCP: `delete_recording` and `rename_speaker` are now top-level tools,
  separated from the generic `mutate_recording`. (#32)
- `PlaudApiError` now carries `http_status`, `plaud_code`, `plaud_msg`, and
  `raw_body` attributes when the API returns a structured error. Transport
  layer captures the error body before raising. (#42)

### Changed

- MCP `process_recording` now defaults to `wait="transcript"` so MCP clients
  do not block on long-running summary generation unless they explicitly
  request it. (#31)
- MCP `mutate_recording` enum is narrowed to `rename`, `trash`, `restore`,
  `move`; gains a `clear_folder: bool` flag that replaces the
  `folder_id="-"` sentinel. (#32)
- PyInstaller UPX compression disabled for `plaud-mcp.spec` and `plaud.spec`,
  matching the tray spec. (#26)

### Fixed

- `TrayApp._quit()` no longer calls `icon.stop()` synchronously on the
  tkinter main thread; the deadlock-prone path is replaced with a scheduled
  `root.destroy()` and a post-mainloop `icon.stop()`. (#22)

## [0.1.20] - 2026-05-21

### Changed

- `browse_recordings` MCP response is now `{"items": [...], "next_after": int|null}` — `next_after` is `null` when the page is short of `limit` and is the cursor to pass as `after` on the next call otherwise. (#30, #51)
- All 7 MCP tool descriptions tightened to one sentence each; total description token count reduced by ~37%. (#30, #51)
- README "Token & secrets" now correctly notes that plaud-tools surfaces a session-expired error when the Plaud token lapses and the user must re-run `plaud-tools login`; folder/file-tag semantics on MCP tools are clarified ("Folder ID (from `list_folders`)"). (#40, #50)

### Fixed

- `_request_json` now passes `body` through the `-302` region-redirect recursive retry, preventing POST/PATCH/DELETE requests from silently dropping their payload on a region mismatch. (#34, #48)
- Bundled CLI (`plaud-tools.exe`) can now transcode and upload `.wav`/`.m4a`
  files without ffmpeg on PATH. `_find_ffmpeg` falls back to the sibling
  `../mcp/ffmpeg.exe` when frozen and no ffmpeg is found beside the CLI exe. (#41, #49)

## [0.1.19] - 2026-05-21

### Fixed

- In-app update now kills `plaud-mcp.exe` before extracting the zip. The MCP
  process holds a file lock on `mcp/plaud-mcp.exe` while running; `Expand-Archive`
  threw a terminating error when it hit that locked file, leaving the zip
  in place, the PS1 script not self-deleted, and the installed binary unchanged.
  The update PS1 helper now mirrors the uninstall helper: it sends
  `Stop-Process -Name plaud-mcp -Force` after the tray exits and before
  extraction. The downloaded zip is also cleaned up after extraction.

## [0.1.18] - 2026-05-21

### Added

- Double-clicking `PlaudTools.exe` when the tray is already running now brings
  `HomeWindow` to the front. The second instance signals the running one via a
  named Windows event (`Global\PlaudToolsActivate`) and exits cleanly.
- `PlaudTools.exe` always opens `HomeWindow` on launch when signed in (or
  `LoginWindow` when not), so double-clicking the exe when it is not yet
  running also surfaces the UI immediately.
- Custom app icon applied to all tkinter title bars and the Windows taskbar
  (`SetCurrentProcessExplicitAppUserModelID`).
- Fresh install via `install.ps1` auto-opens `HomeWindow` after launch when
  credentials are already present.

### Fixed

- `UpdateDialog` no longer shows only a Close button when the background
  poller cached a `None` zip URL (race between poller startup and CI asset
  upload). The dialog now re-fetches and enables the install button once the
  asset is available.
- Uninstall helper no longer opens a blank cmd prompt window. Switched to a
  hidden PowerShell script, matching the in-app updater approach.
- `HomeWindow` "Check for Updates" button no longer stays grayed out after a
  manual check finds a new version.

## [0.1.17] - 2026-05-21

### Fixed

- Uninstall helper no longer opens a blank cmd prompt window. Switched from
  a `.bat` launched via `cmd /c start` to a hidden PowerShell script
  (`-WindowStyle Hidden`), matching the approach used by the in-app updater.

## [0.1.16] - 2026-05-21

### Fixed

- `HomeWindow` "Check for Updates" button no longer stays grayed out after a
  manual check finds a new version. The `_done` callback now calls
  `_refresh_update_btn()` on success, which re-enables the button as
  "Update available: vX.X.X — Install".
- Background update poller now refreshes the `HomeWindow` button in-place
  when it detects a new version while the window is already open, so the
  button updates without requiring the window to be closed and reopened.

## [0.1.15] - 2026-05-21

### Fixed

- In-app update now launches a hidden PowerShell script instead of a
  minimised cmd window, so no console window appears during extraction
  and relaunch.
- After a successful in-app update, the relaunched tray auto-opens
  HomeWindow with an "Updated to vX.X.X successfully." status message
  so users get clear confirmation the update completed.
- `HomeWindow` "Check for Updates" button now shows
  "Update available: vX.X.X — Install" and opens the UpdateDialog
  directly when the background poller has already detected a newer
  version, instead of being silently grayed out.

### Changed

- `pyproject.toml` is now the sole source of truth for the version.
  `plaud-tray.spec` reads it directly at build time and generates the
  PE VERSIONINFO resource itself; running `version_info.py` separately
  is no longer required. Build steps: `pip install -e . --no-deps` →
  `pyinstaller pyinstaller/plaud-tray.spec --noconfirm`.

## [0.1.14] - 2026-05-21

### Fixed

- In-app update bat helper now waits 2 seconds after the old tray
  process exits before launching the new exe. Without the delay the
  new process could start while the OS had not yet released the old
  process's single-instance mutex handle, causing the new exe to
  exit silently without ever appearing in the tray.

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

[Unreleased]: https://github.com/massive-value/plaud-tools/compare/v0.1.19...HEAD
[0.1.19]: https://github.com/massive-value/plaud-tools/compare/v0.1.18...v0.1.19
[0.1.18]: https://github.com/massive-value/plaud-tools/compare/v0.1.17...v0.1.18
[0.1.17]: https://github.com/massive-value/plaud-tools/compare/v0.1.16...v0.1.17
[0.1.16]: https://github.com/massive-value/plaud-tools/compare/v0.1.15...v0.1.16
[0.1.15]: https://github.com/massive-value/plaud-tools/compare/v0.1.14...v0.1.15
[0.1.14]: https://github.com/massive-value/plaud-tools/compare/v0.1.13...v0.1.14
[0.1.13]: https://github.com/massive-value/plaud-tools/compare/v0.1.12...v0.1.13
[0.1.12]: https://github.com/massive-value/plaud-tools/compare/v0.1.11...v0.1.12
[0.1.11]: https://github.com/massive-value/plaud-tools/compare/v0.1.10...v0.1.11
[0.1.10]: https://github.com/massive-value/plaud-tools/compare/v0.1.9...v0.1.10
[0.1.9]: https://github.com/massive-value/plaud-tools/compare/v0.1.8...v0.1.9
[0.1.8]: https://github.com/massive-value/plaud-tools/compare/v0.1.7...v0.1.8
[0.1.7]: https://github.com/massive-value/plaud-tools/compare/v0.1.6...v0.1.7
[0.1.6]: https://github.com/massive-value/plaud-tools/compare/v0.1.5...v0.1.6
