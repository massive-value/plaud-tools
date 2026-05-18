# Plaud Toolkit

Internal toolkit that bridges [Plaud](https://www.plaud.ai/) recordings into AI assistants.

## Rewrite Status

This repository is being repurposed into a Python rewrite.
The imported TypeScript monorepo is still present, but it is reference behavior rather than the target architecture.

The active phase-1 target is:

- a shared Python Plaud domain/client layer
- a Python MCP server with a smaller workflow-oriented tool surface
- a Python CLI for terminal workflows

Current progress is tracked in [docs/python-rewrite-status.md](docs/python-rewrite-status.md).

The Windows tray app and the inherited TypeScript packages are still useful for comparison and reverse-engineered protocol details, but they are no longer the intended long-term structure of this repo.

## Legacy Contents

The rest of this README still documents the imported TypeScript/tray implementation.
Treat it as legacy reference material until the Python rewrite becomes the default user-facing path.

## What's in the box

- **`@plaud/tray`** — Windows system-tray app. One-click sign-in, manages the MCP wiring for Claude Desktop / Claude Code / Codex CLI, auto-heals stale paths after updates, polls GitHub Releases every 24h for updates.
- **`@plaud/core`** — Shared library: auth, API client, secrets storage in the Windows Credential Manager.
- **`@plaud/cli`** — Command-line tool: list / download / transcript / sync recordings.
- **`@plaud/mcp`** — Standalone MCP server you can wire into any client manually if you don't want the tray app.

The Plaud API is reverse-engineered from the web app. Unofficial — not affiliated with or endorsed by Plaud.

---

## Install (end user)

1. Download `PlaudToolkit-vX.Y.Z-win-x64.zip` from the [latest release](https://github.com/massive-value/plaud-toolkit/releases/latest).
2. **Right-click the zip → Properties → check "Unblock" → OK** before extracting. Squire's policy disables SmartScreen "Run anyway", so this step is mandatory.
3. Extract to `C:\Users\<your-username>\AppData\Local\Programs\` (use the **literal** path — Windows Explorer's Extract dialog doesn't expand `%LOCALAPPDATA%`). Double-click `PlaudToolkit.exe`.
4. Right-click the tray icon → **Sign in**, then **Manage AI clients** to connect Claude Desktop / Claude Code / Codex CLI. Restart the AI client afterward.

Full step-by-step, screenshots-eligible details, and troubleshooting (Mark-of-the-Web recovery, MS Store Claude Desktop quirks, Google Sign-In setup, etc.) are in **[docs/INSTALL.md](docs/INSTALL.md)**.

## Update

The tray polls GitHub Releases every 24 hours. When a new release is published, the tray icon shows an update dot and the right-click menu surfaces **Open release page**. Click through to download the new zip and replace the install (manual update for v1; auto-install lands later).

After replacing the install, the tray's auto-heal pass on launch silently rewrites any AI-client config that was pointing at the old `plaud-mcp.exe` path. No manual re-connect needed.

## Uninstall

```powershell
# Quit the running tray
Get-Process PlaudToolkit -ErrorAction SilentlyContinue | Stop-Process -Force
Get-Process plaud-mcp -ErrorAction SilentlyContinue | Stop-Process -Force

# Run the bundled uninstaller — clears keychain, autostart Run-key, AND reverts
# the `plaud` MCP entry from each AI-client config (sibling entries preserved)
& "$env:LOCALAPPDATA\Programs\PlaudToolkit\PlaudToolkit.exe" --uninstall

# Remove the install folder
Remove-Item -Recurse -Force "$env:LOCALAPPDATA\Programs\PlaudToolkit"
```

---

## Develop

```bash
git clone https://github.com/massive-value/plaud-toolkit.git
cd plaud-toolkit
npm install                 # installs all workspaces (no per-package install)
```

There is no build step for `core` / `cli` / `mcp` — everything runs through `tsx`. The tray app uses `electron-vite` for dev mode and `electron-builder` for the packaged zip.

```bash
npm test                    # full vitest suite (200 unit + 3 live-API integration on a credentialled machine)
npx tsc --noEmit            # type-check across the monorepo

npm run dev:tray            # tray app, hot-reload
npm run build:tray          # packaged zip → packages/tray/release/PlaudToolkit-v0.2.1-win-x64.zip
                            #   (requires `bun` on PATH for the embedded MCP exe step)
```

See `packages/tray/BUILD.md` for the build pipeline detail (staging script, electron-builder config, rcedit/icon constraints, troubleshooting).

### CLI

```bash
# Auth + sync
npx tsx packages/cli/bin/plaud.ts login
npx tsx packages/cli/bin/plaud.ts list
npx tsx packages/cli/bin/plaud.ts transcript <recording-id>
npx tsx packages/cli/bin/plaud.ts download <recording-id> ./audio/
npx tsx packages/cli/bin/plaud.ts sync ./plaud-notes/
npx tsx packages/cli/bin/plaud.ts devices

# Rename + folders
npx tsx packages/cli/bin/plaud.ts rename <recording-id> <new-name>
npx tsx packages/cli/bin/plaud.ts folders
npx tsx packages/cli/bin/plaud.ts move-to-folder <recording-id> <folder-id|->   # '-' clears

# Trash lifecycle
npx tsx packages/cli/bin/plaud.ts trash                           # list
npx tsx packages/cli/bin/plaud.ts trash-move <recording-id>...
npx tsx packages/cli/bin/plaud.ts trash-restore <recording-id>...
npx tsx packages/cli/bin/plaud.ts delete <recording-id>... --yes  # IRREVERSIBLE

# Templates + transcribe
npx tsx packages/cli/bin/plaud.ts templates [category-id]
npx tsx packages/cli/bin/plaud.ts transcribe <recording-id> [--template TYPE]
npx tsx packages/cli/bin/plaud.ts status [recording-id]

# Merge
npx tsx packages/cli/bin/plaud.ts merge <id1> <id2> [...] <new-filename>

# Upload an audio file, preserving the original recording date. mp3/opus/ogg
# upload directly; m4a/mp4/wav/aac/flac/wma/amr are transcoded to mp3 first
# via a bundled ffmpeg-static binary. Files > 5 MiB use S3 multipart automatically.
npx tsx packages/cli/bin/plaud.ts upload ./old-meeting.mp3                           # date = file mtime
npx tsx packages/cli/bin/plaud.ts upload ./old-meeting.m4a --date 2024-08-15T10:00Z  # transcode + override
npx tsx packages/cli/bin/plaud.ts upload ./big-meeting.wav --folder <folder-id> --transcribe
```

### Standalone MCP server (without the tray app)

```json
{
  "mcpServers": {
    "plaud": {
      "command": "npx",
      "args": ["tsx", "/absolute/path/to/plaud-toolkit/packages/mcp/src/index.ts"]
    }
  }
}
```

Tools exposed (18 total):

- **Read** — `plaud_list_recordings`, `plaud_get_transcript`, `plaud_get_recording_detail`, `plaud_user_info`, `plaud_get_mp3_url`, `plaud_list_devices`
- **Rename** — `plaud_rename_recording`
- **Folders** — `plaud_list_folders`, `plaud_set_folder`
- **Trash** — `plaud_list_trash`, `plaud_move_to_trash`, `plaud_restore_from_trash`, `plaud_permanently_delete`
- **Templates** — `plaud_list_template_categories`, `plaud_list_summary_templates`
- **Transcribe** — `plaud_transcribe_and_summarize`, `plaud_get_task_status`
- **Merge** — `plaud_merge_recordings`

## Token & secrets

Plaud JWTs last ~300 days. The library auto-refreshes silently when the token is within 30 days of expiry — no manual intervention after the first sign-in.

The tray app stores secrets in the Windows Credential Manager via `@napi-rs/keyring` (with a 0600-mode `~/.plaud/secrets.json` fallback if the keychain is unavailable). The CLI / standalone MCP server use a 0600-mode `~/.plaud/config.json` for backwards compatibility.

## License

MIT — see [LICENSE](LICENSE).
