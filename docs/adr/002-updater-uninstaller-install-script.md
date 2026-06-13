# ADR 002 — Integrated Updater, Uninstaller, and Install Script

**Status:** accepted (2026-05-21)

## Problem

plaud-tools is now public and ships to two populations with different update and uninstall needs. Bundle users (no Python, no pip) have no mechanism to update or uninstall without manual file-system work in a hidden AppData folder. pip users have a natural upgrade path but no first-class CLI command for it.

## Decisions

### Install script

A PowerShell script at `scripts/install.ps1` in the repo serves as the standard install path for bundle users. Users run:

```powershell
irm https://raw.githubusercontent.com/massive-value/plaud-tools/main/scripts/install.ps1 | iex
```

The script downloads the latest `PlaudTools.zip` from GitHub releases, extracts to `%LOCALAPPDATA%\Programs\PlaudTools\`, sets up PATH, autostart, and PS completions. If an existing install is detected, the script errors and directs the user to the tray updater instead — the script is for onboarding only, not upgrades. The manual zip extraction path is retained for power users and air-gapped machines; the README leads with the install script as the flagship path, and `docs/INSTALL-METHODS.md` documents the manual zip plus pip install as alternatives.

The script lives at a stable raw GitHub URL on `main` rather than as a per-release asset, so no CI changes are needed and the URL never rotates.

### Tray updater

The tray checks for updates at startup and again at a random interval between 20 and 28 hours (per-process jitter, seeded at startup) to avoid thundering-herd spikes against the GitHub releases API.

When an update is available, the tray shows an "Update available: vX.X.X" menu item (existing behavior) plus an update dialog. The dialog has an "Install update and restart" button. Clicking it:

1. Disables the button and updates its label to "Downloading… (X MB / Y MB)" then "Installing…".
2. Downloads `PlaudTools.zip` from the GitHub release to a temp dir.
3. Writes a helper `.bat` script that waits for the tray process to exit, extracts the zip over `%LOCALAPPDATA%\Programs\PlaudTools\`, relaunches `PlaudTools.exe`, and deletes itself.
4. Launches the helper script and exits the tray.

The `.bat` helper is necessary because Windows cannot replace a running `.exe` in place. On download failure the button re-enables with an error label so the user can retry; the error is not silently swallowed.

### Uninstaller

A "Uninstall…" tray menu item opens a checklist dialog. Default state:

| Item | Default |
|---|---|
| Remove from user PATH | checked |
| Remove autostart registry key | checked |
| Remove PowerShell profile sourcing lines | checked |
| Delete install directory | checked |
| Delete session / credentials | unchecked |
| Delete log files | unchecked |

Session and log items are unchecked by default so a reinstall works immediately without re-logging in and so logs survive for diagnostics.

Deleting the install directory uses the same `.bat` helper pattern as the updater — the tray cannot delete itself while running.

### pip update command

`plaud-tools update` invokes `sys.executable -m pip install --upgrade plaud-tools`, streams pip output live to the terminal, and appends a note that pipx, uv, and conda users should use their own upgrade command instead. No PyPI pre-check; pip's own "Already satisfied" output is sufficient.

### Log directory

The tray log lives at `%LOCALAPPDATA%\PlaudTools\tray.log` (not `%LOCALAPPDATA%\Plaud\`, which belongs to the official Plaud desktop app).

## Alternatives rejected

- **In-app progress bar**: A separate progress window for the download was rejected in favour of status text on the existing button. The download is a one-time opt-in action; a full progress window is disproportionate.
- **Install script doubles as updater**: Rejected. Re-invoking an external script from inside a running process adds complexity around script location and process lifetime. The tray's self-contained Python + `.bat` approach is simpler.
- **Separate `Uninstall.exe`**: Rejected for alpha software. The tray menu item covers the common case; a second frozen binary adds build complexity for minimal gain.
- **Silent auto-update**: Rejected. Alpha software with a reverse-engineered API; a bad release should not silently overwrite a working install.
- **`plaud-tools update` checks PyPI first**: Rejected. pip already prints "Requirement already satisfied"; a redundant pre-check adds code with no user-visible benefit.

## Amendment — 2026-06-12 (Wave 0 / A3 + Wave 2 / C1: supply-chain integrity)

### SHA256SUMS verification (Wave 0 / A3 + Wave 2 / C1)

Every GitHub release now publishes a `SHA256SUMS` asset alongside `PlaudTools.zip` in the standard `sha256sum` two-space format (`<hex>  PlaudTools.zip`).

**Installer (`scripts/install.ps1`)** — after downloading `PlaudTools.zip`, the script fetches `SHA256SUMS` and compares `Get-FileHash -Algorithm SHA256` against the listed hash. On mismatch the install is aborted with an error. When `SHA256SUMS` is absent (older releases that predate wave 0) the script warns and proceeds (soft-fail for backward compatibility with the rollout window).

**In-app updater (`tray/updater.py`)** — the same verify-then-extract contract is applied: `verify_zip_checksum(zip_path, sums_url)` is called after the download completes. `ChecksumMismatch` or any network error fetching `SHA256SUMS` causes the update to be refused; the tray re-enables the Install button with an error label so the user can retry.

Both sides are fail-closed when the asset is present and soft-fail when absent. The soft-fail branch is tracked for removal once SHA256SUMS is universal across all supported release branches.

**ffmpeg pin (Wave 0 / A3)** — the release pipeline downloads ffmpeg from a pinned versioned URL and verifies it against a hardcoded SHA-256 before bundling. GitHub Actions steps in both `ci.yml` and `release.yml` are pinned to full commit SHAs.

### Update host allowlist (Wave 3 / D6)

Update downloads are restricted to an explicit allowlist: `github.com` and `objects.githubusercontent.com` (exact `hostname` match, not substring). `_check_download_host(url)` is called before any download begins in the updater worker; a host not in the allowlist raises `ValueError` and the download is refused. Redirects to off-allowlist hosts are therefore also refused at the point the redirect URL is resolved.

**Rationale:** The GitHub releases API can return any asset URL it chooses. Without an allowlist, a compromised or misbehaving API response could redirect downloads to an attacker-controlled server. The allowlist ensures the binary always originates from GitHub infrastructure regardless of what the API returns.

### Absolute PowerShell path (Wave 3 / D6)

The tray and lifecycle helpers now invoke PowerShell via its absolute path:
`%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe`. Previously the `powershell` name was resolved via `PATH`, which an attacker with write access to the user's `PATH` environment could shadow. The absolute path is immune to PATH manipulation.
