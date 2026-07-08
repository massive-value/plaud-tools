# CLAUDE.md

This file provides working guidance for agents operating in this repository.

## Current State

The Python rewrite is complete. The active code lives under `src/plaud_tools/`, grouped by which surface owns each concern:

- `core/` — shared Plaud domain/client layer (client, session, auth, transport, models, errors, appdata, layout, transcode, query, ai_clients) used by all three surfaces below
- `cli/` — Python CLI (`plaud-tools` / `pt` entry points) plus `doctor` diagnostics
- `mcp_pt/` — MCP handler functions (11 tools: browse_recordings, get_recording, mutate_recording, delete_recording, edit_transcript, upload_recording, process_recording, list_folders, merge_recordings, edit_summary, mutate_folder) and the MCP server process (`plaud-mcp` entry point, stdio transport). Named `mcp_pt` rather than `mcp` to avoid shadowing the `mcp` SDK package these modules import.
- `tray/` — Windows tray app, updater/uninstaller, and first-run setup (`plaud-tray` entry point)

The TypeScript prior art has been removed. The `har-captures/` directory contains live Plaud API traffic captures (gitignored — local reference only).

## Rewrite Principles

- Preserve fragile Plaud protocol behavior even when internal structure changes
- Prefer simpler architecture over mechanical parity with the old TypeScript layout
- Design MCP first, with the CLI as a second but still core surface
- Keep MCP and CLI façades intentionally different when that improves usability
- Build in vertical slices and validate each slice with tests plus sacrificial live Plaud data

## Agent Skills

### Issue tracker

Issues and PRDs for this repo live on GitHub Issues at https://github.com/massive-value/plaud-tools/issues. See `docs/agents/issue-tracker.md`.

### Triage labels

This repo uses the default triage vocabulary: `needs-triage`, `needs-info`, `ready-for-agent`, `ready-for-human`, and `wontfix`. See `docs/agents/triage-labels.md`.

### Domain docs

This is a single-context repo. Use the root `CONTEXT.md` and `docs/adr/` when present. See `docs/agents/domain.md`.
