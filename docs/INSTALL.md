# Installing plaud-tools

plaud-tools is an unofficial third-party tool that bridges your existing Plaud account into AI assistants via a Python CLI and MCP server. You still need a real Plaud account; this package does not replace the Plaud mobile or web apps.

---

## Prerequisites

- **Python 3.11+**
- **ffmpeg** (optional) — required only when uploading audio files in formats other than MP3, OPUS, or OGG. Install via your package manager and ensure `ffmpeg` is on PATH, or set `FFMPEG_BIN` to the binary path.

---

## Install

### Tray bundle (Windows) — recommended

Open PowerShell and run:

```powershell
irm https://raw.githubusercontent.com/massive-value/plaud-tools/main/scripts/install.ps1 | iex
```

This downloads the latest release, extracts to `%LOCALAPPDATA%\Programs\PlaudTools\`, and launches the tray. No admin elevation required.

To uninstall: use the tray menu's "Uninstall…" item.

#### Manual zip install (advanced)

Download `PlaudTools.zip` from the latest GitHub release and unzip anywhere (e.g. `%LOCALAPPDATA%\Programs\PlaudTools\`). Run `PlaudTools.exe`.

On first launch the tray app:
- Adds `PlaudTools\cli\` to your user `PATH` via `HKCU\Environment`, so `plaud-tools` and `pt` work from any new shell without manual PATH editing. No admin elevation required.
- Sources `PlaudTools\completions\plaud-tools.ps1` from your PowerShell profile, enabling tab-completion for both `plaud-tools` and `pt`.

Open a **new** PowerShell or cmd window after the first launch — the PATH change takes effect in new shells only.

To remove the PATH entry manually: open **System Properties → Advanced → Environment Variables**, find the `Path` entry under "User variables", and remove the `PlaudTools\cli\` segment.

### From PyPI

```
pip install plaud-tools
```

### Editable install from source

```
git clone <repo-url> plaud-tools
cd plaud-tools
pip install -e .
```

Both pip install methods register two entry points:

| Command | Purpose |
|---|---|
| `plaud-tools` / `pt` | CLI |
| `plaud-mcp` | MCP server process (stdio) |

---

## Shell completions

### PowerShell (tray bundle)

The tray app sources `completions\plaud-tools.ps1` from your `$PROFILE` automatically on first run. No manual step needed.

### PowerShell (pip install)

Locate `plaud-tools.ps1` inside the package and add a sourcing line to your profile:

```powershell
$ps1 = Join-Path (python -c "import plaud_tools, pathlib; print(pathlib.Path(plaud_tools.__file__).parent / 'completions' / 'plaud-tools.ps1')") ""
Add-Content $PROFILE ". `"$ps1`""
```

Reload your profile or open a new shell: `plaud-tools <Tab>` will cycle through subcommands.

### bash

```bash
source "$(python -c "import plaud_tools, pathlib; print(pathlib.Path(plaud_tools.__file__).parent / 'completions' / 'plaud-tools.bash')")"
```

Add the line above to your `~/.bashrc` to make it permanent.

### zsh

Copy `_plaud_tools` from the package to a directory on your `$fpath`:

```zsh
cp "$(python -c "import plaud_tools, pathlib; print(pathlib.Path(plaud_tools.__file__).parent / 'completions' / '_plaud_tools')")" ~/.zsh/completions/
# ensure fpath includes ~/.zsh/completions before calling compinit
```

---

## Auth setup

Log in once with the CLI before using anything else:

```
plaud-tools login --email you@example.com --region us
```

Region choices: `us` or `eu`. If you pick the wrong one, the client auto-detects and switches on the first API call.

If you only ever signed up via Google OAuth and don't have a Plaud password, use "Forgot password" on [web.plaud.ai](https://web.plaud.ai) — Plaud will send a reset email even though you have never set a password before.

### Session storage

The session (JWT) is stored in the OS keyring when available, with a fallback to `~/.config/plaud-tools/session.json` (mode 600). You can also inject a token via environment variables without touching the stored session:

```
PLAUD_ACCESS_TOKEN=<token> PLAUD_REGION=us plaud-tools list
```

### Verify the session

```
plaud-tools session show
```

This prints the stored email, region, and token expiry status. A `"status": "valid"` response means you're ready.

---

## CLI quick-start

```
# List recent recordings (default: 20)
plaud-tools list

# List with filters
plaud-tools list --since 2025-01-01 --limit 10
plaud-tools list --query "meeting"

# Show a recording (title, date, duration, speakers, headline)
plaud-tools show <recording-id>

# Fetch the full transcript
plaud-tools transcript <recording-id>

# Fetch the AI summary
plaud-tools summary <recording-id>

# Rename a recording
plaud-tools rename <recording-id> "New title"

# List folders
plaud-tools folders

# Move a recording to a folder (use '-' to clear)
plaud-tools move <recording-id> <folder-id>

# Trash / restore
plaud-tools trash <recording-id>
plaud-tools restore <recording-id>

# Upload a local audio file (transcodes non-native formats via ffmpeg)
plaud-tools upload /path/to/file.m4a --title "Interview"

# Trigger transcription on an existing recording
plaud-tools transcribe <recording-id>

# Check processing status
plaud-tools status <recording-id>
```

Native audio formats (no transcode needed): `.mp3`, `.opus`, `.ogg`, `.oga`

Formats transcoded to MP3 via ffmpeg: `.m4a`, `.mp4`, `.wav`, `.aac`, `.flac`, `.wma`, `.amr`

---

## MCP wiring

The MCP server is non-interactive. Log in via the CLI **before** starting your AI client; the server reads the saved session automatically.

### Claude Desktop

Add a `plaud` entry to `claude_desktop_config.json`:

- **Windows:** `%APPDATA%\Claude\claude_desktop_config.json`
- **macOS:** `~/Library/Application Support/Claude/claude_desktop_config.json`

```json
{
  "mcpServers": {
    "plaud": {
      "command": "plaud-mcp"
    }
  }
}
```

Fully quit and relaunch Claude Desktop after saving the file — MCP servers are loaded once at startup.

### Claude Code

Add a `plaud` entry to `~/.claude.json` (user-level) or `.claude.json` in your project root:

```json
{
  "mcpServers": {
    "plaud": {
      "command": "plaud-mcp"
    }
  }
}
```

### Available MCP tools

| Tool | What it does |
|---|---|
| `browse_recordings` | List and filter recordings by date, title, folder |
| `get_recording` | Full detail for one recording; opt in to transcript / speakers / summary |
| `mutate_recording` | Rename, trash, restore, delete, move to folder, rename speaker |
| `upload_recording` | Upload a local audio file (transcodes via ffmpeg if needed) |
| `process_recording` | Trigger transcription + summarization, block until complete |
| `list_folders` | List Plaud folders (id, name, color, icon) |
| `merge_recordings` | Merge two or more recordings into a single new recording |

---

## Troubleshooting

### Session expired

```
plaud-tools login --email you@example.com --region us
```

Re-run the login command. The new token overwrites the stored one.

### ffmpeg not found

```
RuntimeError: Could not locate ffmpeg.
```

Install ffmpeg and confirm it is on PATH:

```
ffmpeg -version
```

Or point directly at the binary:

```
FFMPEG_BIN=/usr/local/bin/ffmpeg plaud-tools upload file.wav
```

### Wrong region

If your recordings are not appearing, try the other region:

```
plaud-tools login --email you@example.com --region eu
```

The client also auto-detects region by following Plaud's `-302` redirect, so an initial mismatch corrects itself on the first API call.

### AI client doesn't see Plaud after wiring

MCP servers are loaded once at startup. Fully quit (not minimize) and relaunch your AI client after editing the config file.

### Google Sign-In users

The CLI login requires a Plaud password. Use "Forgot password" on [web.plaud.ai](https://web.plaud.ai) to set one; Plaud sends a reset email even if you have never set a password before.
