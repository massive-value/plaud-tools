# plaud-tools

Bridge your [Plaud](https://www.plaud.ai/) recordings into AI assistants via a Python CLI and an MCP server.

[![CI](https://github.com/massive-value/plaud-tools/actions/workflows/ci.yml/badge.svg)](https://github.com/massive-value/plaud-tools/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/plaud-tools)](https://pypi.org/project/plaud-tools/)
![status: alpha](https://img.shields.io/badge/status-alpha-orange)
![Python](https://img.shields.io/badge/python-3.11%2B-blue)
[![License: LGPL-3.0-or-later](https://img.shields.io/badge/license-LGPL--3.0--or--later-blue)](LICENSE)

> **Alpha software.** This project is pre-1.0. APIs, tool names, and CLI flags
> may change between minor versions. Pin a known-good version in production
> wiring and review the [CHANGELOG](CHANGELOG.md) before upgrading.

> **Unofficial — not affiliated with Plaud.** The Plaud API is reverse-engineered
> from the Plaud web app. "Plaud" is a trademark of Plaud Inc.; this project is
> not affiliated with, endorsed by, or sponsored by Plaud. Use at your own risk:
> Plaud's Terms of Service may restrict reverse-engineering and automated
> access, and your account could be rate-limited or suspended. You still need
> a real Plaud account; this package does not replace the Plaud mobile or web
> apps.

## Install

From PyPI:

```
pip install plaud-tools
```

Windows users can grab a bundled tray app + frozen CLI + MCP server (no Python
install required). Recommended one-liner:

```powershell
irm https://raw.githubusercontent.com/massive-value/plaud-tools/main/scripts/install.ps1 | iex
```

Or download `PlaudTools.zip` from the
[latest release](https://github.com/massive-value/plaud-tools/releases/latest)
and unzip manually. See [docs/INSTALL.md](docs/INSTALL.md) for the full
walkthrough.

## Quickstart

```
# Sign in once. Region is `us` or `eu`; auto-detected on first API call.
plaud-tools login --email you@example.com --region us

# Recent recordings
plaud-tools list

# Full detail for one recording (title, date, duration, headline)
plaud-tools show <recording-id>
```

The CLI ships as both `plaud-tools` and the shorter alias `pt`. Run
`plaud-tools --help` for the full subcommand list.

### Wire the MCP server into your AI client

Log in via the CLI **before** starting the AI client; the MCP server reads the
saved session automatically.

**Claude Desktop** — add to `claude_desktop_config.json` (Windows:
`%APPDATA%\Claude\claude_desktop_config.json`; macOS:
`~/Library/Application Support/Claude/claude_desktop_config.json`):

```json
{
  "mcpServers": {
    "plaud": {
      "command": "plaud-mcp"
    }
  }
}
```

**Claude Code** — same JSON block, in `~/.claude.json` (user-level) or
`.claude.json` at the project root.

**Codex CLI** — in `~/.codex/config.toml`:

```toml
[mcp_servers.plaud]
command = "plaud-mcp"
```

Fully quit and relaunch the AI client after editing the config — MCP servers
are loaded once at startup. Full wiring details, troubleshooting, and tray
bundle install steps live in [docs/INSTALL.md](docs/INSTALL.md).

## What's in the box

| Entry point | What it is |
|---|---|
| `plaud-tools` / `pt` | Python CLI — list, search, transcribe, upload, rename, trash, merge recordings |
| `plaud-mcp` | MCP server (stdio transport) — exposes the tool surface below to AI clients |
| `plaud-tray` (optional, `pip install plaud-tools[tray]`) | Windows system-tray app for one-click sign-in and MCP wiring |

## MCP tool surface

The MCP server exposes 7 workflow-oriented tools (canonical list in
[`src/plaud_tools/server.py`](src/plaud_tools/server.py)):

| Tool | What it does |
|---|---|
| `browse_recordings` | List and filter recordings by date, title, folder |
| `get_recording` | Full detail for one recording; opt in to transcript / speakers / summary |
| `mutate_recording` | Rename, trash, restore, delete, move to folder, rename speaker |
| `upload_recording` | Upload a local audio file (transcodes via ffmpeg if needed) |
| `process_recording` | Trigger transcription + summarization; block until both complete |
| `list_folders` | List Plaud folders (id, name, color, icon) |
| `merge_recordings` | Merge two or more recordings into a single new recording |

## Develop

```
git clone https://github.com/massive-value/plaud-tools.git
cd plaud-tools
pip install -e ".[dev]"
pytest -q
```

See [CONTRIBUTING.md](CONTRIBUTING.md) for the full contributor workflow,
including the `PLAUD_LIVE_READS=1` live-test gate.

## Token & secrets

Plaud JWTs last ~300 days. When the stored token is within 30 days of expiry,
`plaud-tools` will raise a session-expired error and prompt you to sign in
again — run `plaud-tools login` to refresh your credentials. Sessions are
stored in the OS keyring when available, with a mode-`600` fallback at
`~/.config/plaud-tools/session.json`.

## Docs

- [docs/INSTALL.md](docs/INSTALL.md) — install + AI-client wiring walkthrough
- [CONTRIBUTING.md](CONTRIBUTING.md) — dev setup, testing, PR workflow
- [SECURITY.md](SECURITY.md) — security policy and vulnerability disclosure
- [CHANGELOG.md](CHANGELOG.md) — release notes
- [LICENSE](LICENSE) — LGPL-3.0-or-later
