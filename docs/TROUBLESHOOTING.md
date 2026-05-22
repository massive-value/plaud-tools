# Troubleshooting

Two common issues are documented in the main [README](../README.md#troubleshooting). Everything else lives here.

---

## AI client doesn't see Plaud after wiring

MCP servers are loaded once at client startup. Closing the window isn't enough — the client must be fully quit.

- **Claude Desktop** — **File → Exit**, then reopen.
- **Claude Code** — `/exit` in the session, then run `claude` again in a new terminal.
- **Codex** — `Ctrl+C`, then run `codex` again in a new terminal.

If you've restarted and still don't see Plaud, run:

```
plaud-tools doctor
```

The output includes which clients are wired and where their config files live. Confirm:

1. The `plaud` entry is present in the config file.
2. The `command` field points at `plaud-mcp` (or an absolute path that exists).
3. `plaud-mcp --version` runs successfully from a fresh terminal.

---

## Broken or partial install

If the tray fails to start, files were quarantined by antivirus, or the install directory is inconsistent, re-run the installer with `-Repair`:

```powershell
& ([scriptblock]::Create((irm https://raw.githubusercontent.com/massive-value/plaud-tools/main/scripts/install.ps1))) -Repair
```

`-Repair` shuts down any running PlaudTools and `plaud-mcp` processes, wipes the existing install directory, and reinstalls from the latest release. Your saved sign-in is preserved.

`-Force` does the same thing but also bypasses the "already up to date" guard, useful when you want a clean reinstall of the current version:

```powershell
& ([scriptblock]::Create((irm https://raw.githubusercontent.com/massive-value/plaud-tools/main/scripts/install.ps1))) -Force
```

---

## Antivirus quarantine

PyInstaller-built executables occasionally trip antivirus heuristics. If `PlaudTools.exe`, `plaud-tools.exe`, or `plaud-mcp.exe` disappears from the install directory shortly after install:

1. Check the antivirus quarantine — look for `Trojan:Win32/Bearfoos.A!ml` or similar generic ML-based detections.
2. Restore the file and add an exclusion for `%LOCALAPPDATA%\Programs\PlaudTools\`.
3. Re-run `install.ps1 -Repair` to restore any missing files.

---

## Session expired

The Plaud access token lasts ~300 days. When it's within 30 days of expiry, `plaud-tools` raises a session-expired error and the tray menu shows **Session expires in N days — sign in again**.

```
plaud-tools login --email you@example.com --region us
```

The new token overwrites the stored one. Your AI client wiring continues to work — no need to re-wire.

If you waited until *after* expiry, the AI client will report it can't reach Plaud. Sign in again via the tray menu or the CLI command above, then restart the AI client (see "AI client doesn't see Plaud after wiring" above).

---

## ffmpeg not found

```
RuntimeError: Could not locate ffmpeg.
```

Required only for uploads of non-native formats (`.m4a`, `.mp4`, `.wav`, `.aac`, `.flac`, `.wma`, `.amr`). The Windows tray bundle ships ffmpeg internally; this error only happens with pip-installed `plaud-tools` on a machine without ffmpeg installed.

Install ffmpeg and confirm it's on `PATH`:

```
ffmpeg -version
```

Or point directly at the binary:

```
FFMPEG_BIN=/usr/local/bin/ffmpeg plaud-tools upload file.wav
```

---

## Wrong region

If your recordings don't appear, try the other region:

```
plaud-tools login --email you@example.com --region eu
```

The client also auto-detects region by following Plaud's `-302` redirect, so an initial mismatch corrects itself on the first API call. Manual region override is only needed if the auto-detection fails (rare).

---

## Google Sign-In users

The CLI login requires a Plaud password. Use "Forgot password" on [web.plaud.ai](https://web.plaud.ai) to set one — Plaud sends a reset email even if you've never set a password before. Then use that new password with `plaud-tools login`.

---

## PATH not picked up

The Windows tray bundle adds `PlaudTools\cli\` to your user PATH via `HKCU\Environment` on first launch. PATH changes only take effect in **new** shells — any PowerShell or cmd window opened before the install will not see `plaud-tools` on PATH.

Workaround: close and reopen your terminal. Or check PATH manually:

```powershell
[Environment]::GetEnvironmentVariable("Path", "User") -split ";" | Where-Object { $_ -like "*PlaudTools*" }
```

If the entry is missing, click **Repair setup** on the PlaudTools home window — it re-runs the first-launch environment setup.

---

## Session storage location

Sessions are stored in your OS keyring when available, with a file-store fallback.

- **Keyring** — managed by your OS (Windows Credential Manager, macOS Keychain, Linux Secret Service). Inspect via your OS's credential management tool.
- **File store** — `~/.config/plaud-tools/session.json` (mode `600`). Inspect with any text editor.

To see which source is active:

```
plaud-tools session show
```

The `source` field reports `env`, `keyring`, `file`, or `missing`.

---

## Multi-account

`plaud-tools` stores one session at a time. To switch accounts:

```
plaud-tools session clear
plaud-tools login --email other@example.com --region us
```

For concurrent multi-account use, inject the access token per-invocation via environment variables:

```
PLAUD_ACCESS_TOKEN=<token-a> plaud-tools list
PLAUD_ACCESS_TOKEN=<token-b> plaud-tools list
```

This bypasses the stored session entirely.

---

## Logs

- **Tray log:** `%LOCALAPPDATA%\PlaudTools\tray.log` (rotates at 1 MB × 3 backups)
- **MCP server log:** `%LOCALAPPDATA%\PlaudTools\mcp.log` (rotates at 1 MB × 3 backups)
- **Update transcripts:** `%TEMP%\plaud_update_<pid>.log` (one per in-tray update attempt)

Open the tray log folder directly from the tray menu: **Open log folder**.

Attach relevant log excerpts to bug reports along with `plaud-tools doctor` output.

---

## Still stuck

File an issue with `plaud-tools doctor` output and the relevant log excerpts: <https://github.com/massive-value/plaud-tools/issues>
