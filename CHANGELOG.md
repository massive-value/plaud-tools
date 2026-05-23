# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.2.10] - 2026-05-23

Three bug fixes targeting uninstall reliability and tray UI.

1. **Uninstaller left files behind when plaud-mcp or ffmpeg were running.**
   Both `uninstall.ps1` and `update.ps1` killed processes by name (`plaud-mcp`)
   and missed child processes such as `ffmpeg` spawned during audio work.
   `Stop-Process` on a parent does not kill its children on Windows, so orphaned
   processes kept `_internal` DLLs locked and `Remove-Item` failed silently.
   Both scripts now enumerate processes by install-directory path so every
   executable under the install tree is caught regardless of name.

2. **Uninstaller silently left the directory intact on a timing race.**
   A single `Remove-Item -ErrorAction SilentlyContinue` with no retry meant
   that if any file handle was transiently held (even briefly after process
   exit), the whole directory was silently left behind.  `uninstall.ps1` now
   sleeps 2 seconds after the tray PID exits (lets Windows release PyInstaller
   DLL handles) and retries `Remove-Item` up to 5 times with 2-second gaps.

3. **Version label clipped off the bottom of the home window on first install.**
   The welcome banner shown on fresh install added ~60 px of content that the
   hardcoded `400×460` geometry could not accommodate, pushing `v0.2.x` below
   the window boundary.  The geometry is now computed dynamically via
   `update_idletasks()` + `winfo_reqheight()` so the window always fits its
   content.

   Also adds `Press Enter to close…` pauses to all early-exit paths in
   `install.ps1` (error, already-up-to-date, upgrade-via-tray) so messages are
   readable when the script runs in a new PowerShell window that auto-closes.

### Fixed

- **Uninstall / update now kill all install-dir processes by path**, not just
  `plaud-mcp` by name — catches ffmpeg children and any future executables.
  (`src/plaud_tools/scripts/uninstall.ps1`, `src/plaud_tools/scripts/update.ps1`)
- **Uninstall directory removal is now retried** with a 2 s post-exit sleep and
  up to 5 attempts (2 s apart) so transient file-lock races no longer silently
  leave the install directory intact.
  (`src/plaud_tools/scripts/uninstall.ps1`)
- **Home window auto-sizes its height** to content via `winfo_reqheight()`,
  fixing the version label being clipped when the welcome banner is shown.
  (`src/plaud_tools/tray/windows/home.py`)
- **Install script pauses on all early exits** so messages are readable in
  auto-closing PowerShell windows.  (`scripts/install.ps1`)

## [0.2.9] - 2026-05-23

Two fixes targeting post-install reliability of the frozen tray bundle.

1. **Silent PowerShell crash when launching the updater from a no-console app.**
   `subprocess.Popen` with `DETACHED_PROCESS` from a PyInstaller GUI (no-console)
   frozen app passes `NULL`/invalid stdio handles to the child process.
   PowerShell crashes immediately on startup before any script code runs —
   leaving no transcript, no sentinel, and the tray closed with no recovery path.
   Switched to `CREATE_NO_WINDOW` with explicit `DEVNULL` handles so PowerShell
   gets valid (NUL) stdio regardless of the parent console state.  Also adds
   `-ExecutionPolicy Bypass` (enterprise policy cannot silently block the
   dispatcher), `-NonInteractive` (suppresses prompts), and a 0.5 s post-launch
   poll that writes a failure sentinel and logs the exit code if PowerShell exits
   immediately.  `update.ps1` gains a heartbeat marker written before
   `Start-Transcript` so future failures can distinguish "PowerShell never ran
   the script" from "script ran but failed mid-way".

2. **Stale dist-info in the bundle causes the tray to report the wrong version
   and loop forever on update checks.**  `copy_metadata('plaud-tools')` in
   `plaud-tray.spec` collects whatever `dist-info` `importlib.metadata` resolves
   in the build environment.  Without `--force-reinstall`, `pip install` leaves
   old dist-info on disk alongside the new one; PyInstaller bundles both, and at
   runtime `importlib.metadata` picks the lower version.  The CI release step now
   passes `--force-reinstall` to guarantee a single canonical dist-info in the
   bundle.

### Fixed

- **Updater no longer silently crashes on enterprise or no-console machines.**
  `CREATE_NO_WINDOW` + explicit `DEVNULL` handles replace the old
  `DETACHED_PROCESS` launch; `-ExecutionPolicy Bypass -NonInteractive` flags
  added; 0.5 s launch-health poll writes a sentinel on immediate exit.
  `update.ps1` heartbeat marker added before `Start-Transcript`.
  (`src/plaud_tools/tray/updater.py`, `src/plaud_tools/scripts/update.ps1`)
- **Release CI bundles the correct dist-info version.**
  `pip install --force-reinstall` in the release workflow removes stale
  dist-info before building, preventing the tray from reporting the previous
  version at runtime.  (`.github/workflows/release.yml`)

## [0.2.8] - 2026-05-22

Follow-up to v0.2.7's DPAPI shadow fix.  Two narrow but biting issues:

1. **The very first MCP call right after a v0.2.6 → v0.2.7 upgrade could
   still hit `session_expired`.**  v0.2.7 self-heals the shadow file inside
   the tray's `_load_session()`, but in the frozen bundle that runs after
   ~3-5 s of `pystray` / `PIL` imports — long enough for an AI client to
   notice its MCP child died during the bundle swap and respawn it before
   the shadow exists on disk.  v0.2.8 prises that work out of the import
   path so it runs at process start.
2. **`pytest` locally would overwrite the user's real session.dat.**  One
   v0.2.6-era test in `tests/test_client.py` constructed `SessionStore`
   with no `dpapi_path=`, which post-v0.2.7 silently DPAPI-encrypted
   synthetic test data straight into the production
   `%LOCALAPPDATA%\PlaudTools\session.dat`.  The next MCP call read the
   3-byte test token, failed JWT decode, and the tray prompted for sign-in.
   Pure-developer regression (CI was unaffected — `LOCALAPPDATA` is unset
   on Linux), but it cost real sign-ins on Kadin's dev machine while
   working v0.2.8.  Fixed plus belt-and-braces.

### Added

- **Eager DPAPI shadow self-heal in the tray entry script.**
  `SessionStore.prime_dpapi_shadow()` — a single, non-retrying keyring read
  that writes the shadow if and only if the read succeeded and the shadow
  is missing.  Called from `scripts/plaud_tray_entry.py` *before*
  `from plaud_tools.tray_app import main` so it runs ahead of the pystray /
  PIL import chain, tightening the window between tray-launch and
  shadow-existence to a fraction of the previous ~3-5 s.  Signed-out users
  pay no retry budget here (single read, no backoff loop); the existing
  `load_with_source()` self-heal remains as the slower-path safety net.
  Skipped on the `--com-activate` toast-handler path so short-lived helper
  processes don't pay the keyring cost.  Five new tests in
  `tests/test_auth.py` covering the healthy / already-primed / signed-out /
  keyring-raises / dpapi-disabled cases.
- **Conftest trip-wire against real-path DPAPI writes.**
  `tests/conftest.py` now monkeypatches
  `plaud_tools.session._default_dpapi_path` to `None` for every test
  (autouse), so any future regression that forgets `dpapi_path=` writes
  *nothing* instead of corrupting the user's session.  A second autouse
  fixture snapshots the real shadow's mtime around each test and raises
  loudly if it changed.  The path snapshot is captured at module import
  time so the redirect fixture cannot shadow the trip-wire.

### Fixed

- **`test_session_store_prefers_keyring_when_available` no longer writes
  to the user's real `%LOCALAPPDATA%\PlaudTools\session.dat`.**  The test
  now pins `dpapi_path` under `tmp_path` like every other DPAPI-aware
  test in the suite.  Comment in-place documents the regression so it
  doesn't get reintroduced.

## [0.2.7] - 2026-05-22

The headline change is a **DPAPI shadow-file fallback** that finally closes
out the recurring `session_expired` events seen by MCP clients on cold-start.
Two earlier patches (v0.2.3 single-retry, v0.2.6 progressive 3.6 s budget)
narrowed the window but never eliminated it: the root cause is a
Windows-side cold-start race inside `vaultcli.dll` / `win32ctypes` where
`keyring.get_password` returns `None` for hundreds of milliseconds despite
the credential being present (confirmed in production: the *same* MCP
process firing `session_expired` reports `days_until_expiry=291`,
`store_source='keyring'` from a diagnostic call milliseconds later).  Rather
than continue tuning retries against an undocumented internal state machine,
v0.2.7 sidesteps the Credential Manager service entirely on the fallback
path by reading a user-DPAPI-encrypted shadow file written alongside every
keyring save.

This release also folds in the unreleased post-v0.2.6 work that was sitting
on `main`: tray Help affordances, login form hardening, one-shot
plaud-toolkit credential migration, the progressive-backoff retry shape
itself, and a tray auto-heal pass at startup.

### Added

- **DPAPI shadow-file fallback for session storage (Windows).**
  `SessionStore` now writes a `%LOCALAPPDATA%\PlaudTools\session.dat` file
  alongside every `set_password` call.  The file is encrypted via
  `Crypt32.CryptProtectData` in user scope — same primitive Credential
  Manager uses internally, but invoked directly so spawned MCP processes
  have a path to the session that does not depend on the credential
  service's cold-start settling window.  On load, the retry budget against
  the keyring is exhausted first (preserving today's healthy-keyring fast
  path); only if every keyring read still returns `None` does
  `_load_from_dpapi` decrypt the shadow file.  A telemetry-grade warning
  fires when the fallback path succeeds so we can see in `mcp.log` /
  `tray.log` how often the underlying Windows bug is biting users.  The
  feature is gated to `service_name == "plaud-tools"` and to
  `sys.platform == "win32"`; tests opt in explicitly via the new
  `dpapi_path=` constructor parameter or opt out with `dpapi_path=None`.
  Eight new tests in `tests/test_auth.py`, including a Windows-only live
  `_dpapi_protect`/`_dpapi_unprotect` roundtrip so the ctypes signature
  is exercised on `windows-latest` CI instead of only at user runtime.
- **Tray Help / Visit website button.**  A new menu item (just above
  Uninstall/Quit) and matching `HomeWindow` button (below "View Logs")
  both open the GitHub repo via a single `REPO_URL` constant in
  `tray/app.py`.  `HomeWindow` geometry bumped 420→460 to fit the extra
  button.
- **One-shot migration from the predecessor `plaud-toolkit` credential
  scheme.**  `SessionStore._load_from_legacy_keyring()` +
  `_migrate_legacy_session()` read the two Windows Credential Manager
  entries that `plaud-toolkit` (the TypeScript predecessor) wrote
  (`jwt.plaud-toolkit`/`jwt` and `profile.plaud-toolkit`/`profile`),
  rewrite them under the canonical `plaud-tools`/`session` JSON shape,
  and delete the legacy entries.  Returns `source="legacy_keyring"` from
  `load_with_source()` so MCP diagnostics surface the migration.  Gated
  to the canonical service name so test fixtures with synthetic service
  names never touch the user's real vault.  Four regression tests in
  `tests/test_auth.py`.
- **Tray auto-heal at startup.**  `_BackgroundMixin._run_verify_env` now
  silently calls a new `_auto_repair_env` pass when `_verify_env` reports
  any missing slot (PATH, shell completions, autostart), then re-verifies
  before deciding whether to surface the yellow setup-failures banner.
  Restores PATH / completions / autostart in the frozen-bundle context so
  users no longer have to click "Repair setup" after a state-degrading
  scenario (in-app upgrades, system cleanup tools, weird Windows resets).
  Gated on `sys.frozen`.  Respects an explicit user opt-out via a marker
  file `<install_dir>\.autostart_disabled` written by `_set_autostart
  (False)` and cleared by `_set_autostart(True)`; the marker lives in the
  install dir so it survives `Expand-Archive -Force` upgrades but is wiped
  by uninstall.  `_verify_env` treats `autostart_ok = _autostart_enabled()
  or _autostart_opted_out()`, so the banner stays hidden when a user has
  deliberately turned off "Start with Windows".  Eight new tests in
  `tests/test_tray_env.py` covering opt-out semantics, dev-mode no-op,
  frozen-mode restore-all, and the banner-stays-hidden invariant.

### Changed

- **Keyring retry — progressive backoff.**  The fixed-count
  `_KEYRING_RETRY_ATTEMPTS` retry has been replaced with
  `_KEYRING_RETRY_DELAYS_S = (0.1, 0.1, 0.2, 0.4, 0.8, 1.0, 1.0)`
  (8 attempts, ~3.6 s worst case).  Fixed 100 ms × N hammered the
  credential service every 100 ms while it was still warming up; the
  bumped budget wasn't enough on the second observed cold-start
  (`session_expired` fired at T+500 ms while a diagnose ~50 ms later
  found `days_until_expiry=291`).  Exception and `None` paths are
  unified.  `_KEYRING_RETRY_DELAY_S` is retained as the base unit so
  tests can monkeypatch it to 0 for instant runs — an autouse fixture in
  `tests/conftest.py` does this for the whole suite (~10 s saved across
  the few tests that hit the real keyring with synthetic service names).
  Note: this is no longer the *primary* defense against the cold-start
  race — DPAPI fallback is — but it still smooths the healthy-keyring
  path through transient blips.
- **LoginWindow hardening.**  Explicit empty-email/password validation
  with an inline message; broad `except Exception` around `auth.login()`
  so transient/unexpected errors log a full traceback to `tray.log` but
  show a short friendly message inline.  Closes the "full stack trace
  shown in the UI on empty-field submit" report.
- **Startup session-load retry path.**  An earlier attempt added an outer
  poll loop in `tray/app.py`; that was folded back into the inner
  `_get_password_with_retry` progressive backoff so there is exactly one
  retry layer to reason about.  `_load_session()` also single-passes the
  store now, so the legitimate signed-out case pays one retry budget
  instead of two.
- README rewritten to lead with the Windows tray bundle install +
  lifecycle (install → sign in → wire AI clients → restart → updates →
  uninstall) for non-technical users. Quickstart is GUI-only after the
  install one-liner; the only CLI command shown in the README is the
  PowerShell `irm | iex` installer. PyPI and manual-zip install paths
  moved out of the README into `docs/INSTALL-METHODS.md`.
- `docs/INSTALL.md` renamed to `docs/INSTALL-METHODS.md` and split into
  four focused documents:
  - `docs/INSTALL-METHODS.md` — pip install, manual zip extraction,
    install from source, shell completions.
  - `docs/AI-CLIENTS.md` — manual JSON/TOML wiring for Claude Desktop,
    Claude Code, and Codex across Windows, macOS, and Linux. Source of
    truth for what the tray wizard writes.
  - `docs/CLI.md` — curated `plaud-tools` CLI reference grouped by
    workflow (sign-in, browse, edit, audio, diagnostics).
  - `docs/TROUBLESHOOTING.md` — ffmpeg setup, region mismatches,
    antivirus quarantine, PATH issues, session storage location,
    multi-account.
- `docs/adr/002-updater-uninstaller-install-script.md` updated to
  reference the renamed install-methods doc.

### Security

- When DPAPI fallback handles a save (because the keyring backend
  raised), the plaintext JSON `~/.config/plaud-tools/session.json`
  fallback no longer fires.  Previously a keyring-write failure on
  Windows would silently drop the token to plaintext on disk; now the
  DPAPI-encrypted shadow absorbs the save and the plaintext file stays
  empty.  Pinned by `test_save_succeeds_when_keyring_fails_but_dpapi_works`.

## [0.2.6] - 2026-05-22

Completes the toast-click activation story started in v0.2.4/v0.2.5, and
hardens the keyring read path against a second class of transient Windows
Credential Manager failure.

### Added

- Clicking a PlaudTools toast notification now opens the tray's home or login
  window directly.  A new ``INotificationActivationCallback`` COM server
  (``comtypes``) is registered at tray startup under
  ``HKCU\Software\Classes\CLSID\{DC6F6422-E7ED-4F4E-BBDE-8332A399DBD5}\LocalServer32``
  pointing at ``PlaudTools.exe --com-activate``.  When the user clicks a
  toast, Windows launches that process, ``Activate()`` signals the named
  Win32 event ``Global\PlaudToolsActivate``, and the running tray's
  ``_watch_activate_event`` thread opens the home or login window as
  appropriate.  Closes #83.
- ``HKCU\Software\Classes\AppUserModelId\PlaudTools.TrayApp`` now includes a
  ``CustomActivator`` value wiring every PlaudTools toast to the COM activator
  above.  The CLSID keys are removed on uninstall.
- ``comtypes>=1.4`` added to the ``tray`` optional-dependency group and to the
  PyInstaller hidden-imports list.

### Changed

- Update-available notification switched from ``pystray.icon.notify()``
  (``Shell_NotifyIcon`` balloon) back to the WinRT/PowerShell
  ``CreateToastNotifier`` path so that ``CustomActivator`` is honoured on
  click.
- Toast messages updated: "click here to sign in again" / "click here to
  install" instead of directing users to the tray menu.

### Fixed

- ``SessionStore._get_password_with_retry`` now retries once on a transient
  ``None`` result in addition to exceptions.  Observed in production
  (post-v0.2.5): ``keyring.get_password`` returned ``None`` while the entry
  existed; a diagnostic call 50 ms later returned the same session with 299
  days remaining.  Two consecutive ``None`` results are still treated as a
  genuine absent entry; the retry adds at most 100 ms to the signed-out path.

## [0.2.5] - 2026-05-22

Fixes the update notification introduced in v0.2.4, which was silently dropped
by Windows because the ``PlaudTools.TrayApp`` AUMID was not registered as a
COM-activatable server.

### Fixed

- Update notifications now actually appear.  Switched from the
  winrt/PowerShell ``CreateToastNotifier`` path (which Windows 11 silently
  drops for unpackaged apps without full COM registration) to pystray's
  ``icon.notify()`` (``Shell_NotifyIcon``), which uses the already-running
  tray icon and requires no AUMID registration.  The PowerShell toast path is
  retained as a fallback if pystray notify raises.

### Added

- Tray now registers ``HKCU\Software\Classes\AppUserModelId\PlaudTools.TrayApp``
  at startup (``DisplayName`` + ``IconUri``).  This is the prerequisite for the
  COM click-to-install follow-on (#83) and makes the existing session-expired
  toast path viable on Windows 11.

## [0.2.4] - 2026-05-22

### Added

- Tray now shows a Windows toast notification when an update is detected, and
  again on each subsequent startup while the update remains uninstalled.
  Previously the tray menu and home window updated silently, so users only saw
  the new version if they happened to open the tray menu.  The toast fires via
  the existing winrt/PowerShell fallback path used by session-expired
  notifications.  Within a single session the same version is only toasted
  once to avoid repetition during the 20–28 h re-check interval; a fresh
  startup always toasts if an update is waiting.  Clicking the toast is not
  yet actionable — see #83 for the COM activation follow-on.

## [0.2.3] - 2026-05-22

A two-bug patch release shipped within an hour of v0.2.2, driven by the very
first diagnostic ``mcp.log`` / enriched ``session_expired`` payload that
v0.2.2 captured in real-world use.

### Fixed

- ``SessionStore._load_from_keyring`` swallowed transient
  ``keyring.get_password`` exceptions and returned ``None``.  The MCP server
  then treated that ``None`` as ``no_session`` and surfaced a sign-in prompt
  to the user — even though the very next keyring read returned the same
  session cleanly (caught in the v0.2.2 diagnostic: ``store_source='keyring'
  days_until_expiry=299 token_typ='UT'`` immediately after a ``no_session``
  fire).  Windows Credential Manager has occasional transient failures
  under load.  ``_get_password_with_retry`` now retries once on exception
  with a 100 ms backoff before falling back to the file store.  Clean
  ``None`` payloads (legitimate "no entry exists") still propagate
  immediately so signed-out states aren't slowed.  Regression coverage in
  ``tests/test_auth.py``. (#81, partial fix for #80)
- ``_setup_mcp_logging`` used ``logging.basicConfig`` which is documented
  as a no-op when the root logger already has handlers.  The frozen
  plaud-mcp wrote its mcp.log banner fine; the pip-installed plaud-mcp
  v0.2.2 did not, because something in the pip-launch import chain
  configured a root handler first.  Attach the rotating file handler
  directly via ``logging.getLogger().addHandler(handler)``; pre-existing
  handlers are preserved, not replaced.  Regression coverage in
  ``tests/test_server.py``. (#81, partial fix for #80)

## [0.2.2] - 2026-05-22

A bundle-correctness and observability release shaking out three issues that
surfaced during the v0.2.1 in-app upgrade flow.

### Fixed

- In-app updater would silently abort and leave the user stranded with no tray
  running when `Expand-Archive` hit a locked DLL (Claude Desktop respawning
  `plaud-mcp.exe` mid-extraction is the common trigger).  `update.ps1` now
  retries the scoped MCP kill with respawn detection (`MaxAttempts=8`,
  `StableMs=500`), writes a structured failure sentinel to `%TEMP%` on any
  abort, restarts the tray from a `finally` block so the user is never left
  without a tray, and surfaces the failure as a tkinter `messagebox.showerror`
  on next launch — pointing at the transcript log that the rewritten script
  now keeps in `%TEMP%\plaud_update_<TrayPid>.log`.  The dispatcher path is
  cleaned up via an explicit `-DispatcherPath` parameter rather than the
  self-deleting `Remove-Item $MyInvocation.MyCommand.Path` line that was
  itself a hazard.  Regression coverage in `tests/test_ps1_templates.py`. (#76, closes #75)
- `tray/toasts.py` was attempting to import `winrt` on every toast call and
  catching the `ModuleNotFoundError` at DEBUG level, which produced a
  multi-line traceback in `tray.log` for every notification on the shipped
  bundle (which doesn't ship `winrt`).  Detection now happens once at module
  load via `_WINRT_AVAILABLE`; the per-call helpers `_show_winrt_toast` and
  `_show_powershell_toast` are silent on the expected "not installed" path
  and only log when an installed `winrt` blows up at runtime.  Regression
  coverage in `tests/test_tray_first_run.py` and `tests/test_mcp_error_codes.py`. (#77)

### Added

- MCP server now writes a rotating log to `%LOCALAPPDATA%\PlaudTools\mcp.log`
  (mirrors the tray log path).  Without this, every `logging.*` call inside
  the MCP code path went nowhere unless the parent client captured stderr —
  Claude Desktop did, Codex did not, and neither persisted across sessions,
  which made the recurring "session expired" toast in #78 impossible to
  diagnose.  `_setup_mcp_logging` is wired into `server.main()` after argparse
  so `--version` stays side-effect-free. (#79, partial fix for #78)
- `session_expired` events in `events.jsonl` and the corresponding MCP/tray
  log lines now carry safe diagnostic metadata: `store_source` (env / keyring
  / file / missing), `env_token_present`, `mcp_pid`, `mcp_version`, plus
  `region`, `token_typ`, and `days_until_expiry` when a session is loadable.
  Token bytes never appear.  The tray log line changes from "session_expired
  event received from MCP" to one that includes all keys, sorted for stable
  diff-able output via the extracted `_format_session_expired_diag` helper.
  Regression coverage in `tests/test_mcp_error_codes.py` and
  `tests/test_tray_correctness.py`. (#79, partial fix for #78)

## [0.2.1] - 2026-05-22

A small follow-up release covering two install/session correctness fixes
that landed after v0.2.0.

### Fixed

- `install.ps1` was writing the HKCU autostart registry value under the
  name `PlaudTools` (no space), while the tray reads/writes under
  `_AUTOSTART_NAME = APP_NAME = "Plaud Tools"` (with a space).  Every
  fresh install left the tray reporting "missing autostart" in
  HomeWindow's setup-failures banner, and clicking Repair wrote a
  *second* Run entry under the correct name — so users ended up with
  both keys firing on login.  install.ps1 now writes the correct name
  and also strips the legacy `PlaudTools` value on every run so users
  who upgraded through the buggy script get auto-cleaned the next time
  they reinstall.  Regression coverage in
  `tests/test_tray_env.py` pins both behaviors against the Python
  `APP_NAME` constant. (#73)
- MCP server constructed a fresh `SessionManager(store)` on every tool
  call from inside `get_client`, defeating the in-memory keyring cache
  added in v0.1.22 (#43) and forcing the 30-day buffer check to
  re-validate from cold state every call.  `_make_server` now builds
  one `SessionManager` per process and reuses it across all
  `get_client` invocations.  Regression coverage in
  `tests/test_server.py` pins the singleton. (#74)

### Added

- `SessionStore._save_to_keyring`, `_load_from_keyring`, and
  `_load_keyring_module` now log a `WARNING` with `exc_info` on every
  silent-fallback path.  The prior bare `except Exception: return False`
  made "saved keyring OK but session is gone next launch" symptoms
  impossible to diagnose from `tray.log`.  We still fall back to the
  file store on failure — just no longer in silence.  Regression
  coverage in `tests/test_auth.py`. (#74)

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

[Unreleased]: https://github.com/massive-value/plaud-tools/compare/v0.2.8...HEAD
[0.2.8]: https://github.com/massive-value/plaud-tools/compare/v0.2.7...v0.2.8
[0.2.7]: https://github.com/massive-value/plaud-tools/compare/v0.2.6...v0.2.7
[0.2.6]: https://github.com/massive-value/plaud-tools/compare/v0.2.5...v0.2.6
[0.2.5]: https://github.com/massive-value/plaud-tools/compare/v0.2.4...v0.2.5
[0.2.4]: https://github.com/massive-value/plaud-tools/compare/v0.2.3...v0.2.4
[0.2.3]: https://github.com/massive-value/plaud-tools/compare/v0.2.2...v0.2.3
[0.2.2]: https://github.com/massive-value/plaud-tools/compare/v0.2.1...v0.2.2
[0.2.1]: https://github.com/massive-value/plaud-tools/compare/v0.2.0...v0.2.1
[0.2.0]: https://github.com/massive-value/plaud-tools/compare/v0.1.22...v0.2.0
[0.1.22]: https://github.com/massive-value/plaud-tools/compare/v0.1.21...v0.1.22
[0.1.21]: https://github.com/massive-value/plaud-tools/compare/v0.1.20...v0.1.21
[0.1.20]: https://github.com/massive-value/plaud-tools/compare/v0.1.19...v0.1.20
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
