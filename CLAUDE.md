# CLAUDE.md

This file provides working guidance for agents operating in this repository.

## Current State

The Python rewrite is complete. The active code lives under `src/plaud_tools/`:

- `client.py` — Plaud domain/client layer (auth, session, all API flows)
- `cli.py` — Python CLI (`plaud` / `pld` entry points)
- `mcp.py` — MCP handler functions (10 tools: browse, get, mutate, delete, rename_speaker, correct_transcript, upload, process, list_folders, merge_recordings)
- `server.py` — Python MCP server process (`plaud-mcp` entry point, stdio transport)

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
