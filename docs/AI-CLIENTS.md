# Wiring plaud-tools into AI clients

The Windows tray bundle ships a **Configure AI Agents…** wizard that auto-detects installed clients and writes their config files for you. Most users don't need this document — use the wizard.

This page exists for:

- **macOS and Linux users**, who don't have the tray wizard.
- **Users on a new or unrecognized AI client** that the wizard doesn't know about.
- **Users who prefer to edit config files manually** for auditability or IT-policy reasons.
- **Anyone debugging a connection** — the JSON/TOML here is the source of truth for what the wizard writes.

> **Prerequisite:** the `plaud-mcp` executable must be on `PATH`, or you must point the client config at an absolute path. The Windows tray bundle puts `plaud-mcp.exe` on `PATH` automatically; pip installs do so via the entry point. If `plaud-mcp --version` doesn't run from a fresh terminal, fix the install first.

---

## Claude Desktop

### Config file locations

| OS | Path |
|---|---|
| Windows (standard install) | `%APPDATA%\Claude\claude_desktop_config.json` |
| Windows (Microsoft Store install) | `%LOCALAPPDATA%\Packages\Claude_*\LocalCache\Roaming\Claude\claude_desktop_config.json` |
| macOS | `~/Library/Application Support/Claude/claude_desktop_config.json` |
| Linux (unofficial builds) | Check the build's docs; usually `~/.config/Claude/claude_desktop_config.json` |

### Config snippet

Open the file (create it if it doesn't exist) and add a `plaud` entry under `mcpServers`:

```json
{
  "mcpServers": {
    "plaud": {
      "command": "plaud-mcp"
    }
  }
}
```

If `plaud-mcp` is not on `PATH`, use the absolute path:

```json
{
  "mcpServers": {
    "plaud": {
      "command": "C:\\Users\\you\\AppData\\Local\\Programs\\PlaudTools\\mcp\\plaud-mcp.exe"
    }
  }
}
```

### Restart

Closing the window keeps Claude Desktop running in the tray. Use **File → Exit** to fully quit, then reopen from the Start menu (Windows) or Applications folder (macOS).

### Verifying

In Claude, paste:

```text
List the MCP tools you have available for Plaud.
```

You should see eleven tools: `browse_recordings`, `get_recording`, `mutate_recording`, `delete_recording`, `edit_transcript`, `upload_recording`, `process_recording`, `list_folders`, `merge_recordings`, `edit_summary`, and `mutate_folder`.

---

## Claude Code

### Config file location

Same on every OS: `~/.claude.json` (user-level) or `.claude.json` in your project root (project-level).

### Config snippet

```json
{
  "mcpServers": {
    "plaud": {
      "command": "plaud-mcp"
    }
  }
}
```

### Restart

Claude Code holds the MCP connection open for the lifetime of the session. In your existing session, type `/exit`, then run `claude` again in a new terminal.

### Verifying

Same as Claude Desktop — ask Claude to list its Plaud tools.

---

## Codex

### Config file location

Same on every OS: `~/.codex/config.toml`.

### Config snippet

```toml
[mcp_servers.plaud]
command = "plaud-mcp"
```

If `plaud-mcp` is not on `PATH`, use the absolute path:

```toml
[mcp_servers.plaud]
command = "C:\\Users\\you\\AppData\\Local\\Programs\\PlaudTools\\mcp\\plaud-mcp.exe"
```

### Restart

Press `Ctrl+C` to end your Codex session, then run `codex` again in a new terminal.

### Verifying

In Codex, ask it to list its available MCP servers; you should see `plaud` listed.

---

## Other MCP clients

Any MCP-aware client can talk to `plaud-mcp` over stdio. The minimum it needs to know:

- **Command:** `plaud-mcp` (or absolute path to `plaud-mcp.exe` on Windows)
- **Transport:** stdio
- **No arguments, no environment variables required for the common case.**

For tokens injected via environment (CI, scripted agents, etc.):

```
PLAUD_ACCESS_TOKEN=<token> PLAUD_REGION=us plaud-mcp
```

This bypasses the keyring and file-store session lookup entirely.

---

## Disconnecting

### Via the tray wizard (Windows bundle)

Open the tray menu → **Manage AI clients…** → click **Disconnect** next to the client.

### Manually

Open the same config file you edited above and remove the `plaud` entry from `mcpServers` (JSON) or the `[mcp_servers.plaud]` block (TOML). Save, then restart the client.

---

## Available MCP tools

| Tool | What it does |
|---|---|
| `browse_recordings` | List and filter recordings by date, title, folder, or trash status |
| `get_recording` | Full detail for one recording; opt in to transcript (with offset/length slicing) / speakers / summary |
| `mutate_recording` | Rename, trash, restore, or move one recording or a batch (`recording_ids`) |
| `delete_recording` | Permanently delete a recording (requires explicit confirmation) |
| `edit_transcript` | Rename a speaker label (`action="rename_speaker"`, matches the displayed name or the original `Speaker N`) or literal find-and-replace on transcript text (`action="correct"`, supports `dry_run`) |
| `upload_recording` | Upload a local audio file (transcodes via ffmpeg if needed) |
| `process_recording` | Trigger transcription + summarization; `wait` controls how long to block, returning `still_processing` if a soft deadline is hit |
| `list_folders` | List Plaud folders (id, name, color, icon) |
| `merge_recordings` | Merge two or more recordings into a single new recording |
| `edit_summary` | Literal find-and-replace (`action="correct"`, supports `dry_run`) or full overwrite (`action="replace"`) of a recording's AI summary |
| `mutate_folder` | Create, edit, or delete a folder |
