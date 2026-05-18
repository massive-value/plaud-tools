# Python Rewrite Status

Last updated: 2026-05-18

## Current state

The Python rewrite is now the active direction for phase 1.
The imported TypeScript monorepo remains in the repository as behavioral prior art, not as the target architecture.

Implemented so far:

- Python package scaffolding under `src/plaud_tools/`
- Shared read-only Plaud client with:
  - JWT session reuse through a centralized session manager
  - file-backed local session store at `~/.config/plaud-tools/session.json`
  - region failover on Plaud `status: -302` responses
  - browser-like request headers on Plaud API calls
  - recording list and recording detail reads
  - transcript fetch from the `transaction` content link
  - inline summary extraction from `pre_download_content_list`
  - first safe write flows:
    - recording rename
    - folder listing
    - folder assignment and clearing
    - trash listing
    - trash move and restore
    - transcript read-modify-write through speaker rename
    - transcribe and summarize job submission
    - processing task status inspection
- Thin Python façades for:
  - CLI browse/detail/transcript flows
  - CLI session bootstrap via `session set` and `session show`
  - CLI login via Plaud email/password, storing the resulting session
  - CLI rename, folders, and move-to-folder workflows
  - CLI trash, trash-move, and trash-restore workflows
  - CLI rename-speaker workflow
  - CLI transcribe and status workflows
- MCP handlers with simplified 3-tool surface:
  - `browse_recordings` (filter by folder, date range, query, with offset pagination)
  - `get_recording` (unified detail + transcript/summary/speakers via `include` list)
  - `mutate_recording` (rename, trash, restore, delete, move, rename_speaker)
- Session persistence with:
  - OS keyring preference through Python `keyring`
  - locked-down file fallback at `~/.config/plaud-tools/session.json`
- Automated coverage for:
  - protocol normalization and error handling
  - region switching and session expiry
  - login request behavior
  - keyring preference and file fallback
  - CLI shaping
  - MCP curated responses
  - opt-in live read smoke test gated behind `PLAUD_LIVE_READS=1`

## What is not done yet

All planned phases are complete.

## Current slice boundary

Phase 1 issues 01–05 are complete. All workflows below are implemented and tested:

- auth/session/credential management
- browse/search/filter recordings (CLI + MCP `browse_recordings`)
- inspect one recording with transcript, summary, speakers (CLI + MCP `get_recording`)
- recording mutations: rename, trash, restore, permanent delete, folder move, speaker rename (CLI + MCP `mutate_recording`)
- processing jobs: transcribe/summarize submission and task status inspection
- upload and merge audio (CLI `upload`/`merge`, MCP `upload_recording`/`process_recording`)

Phase 2 — MCP server process — is complete as of 2026-05-18:

- `src/plaud_tools/server.py` — low-level MCP server over stdio transport
- 5 tools with full JSON Schema: `browse_recordings`, `get_recording`, `mutate_recording`, `upload_recording`, `process_recording`
- `plaud-mcp` CLI entry point (in `pyproject.toml`)
- `mcp>=1.0,<2.0` added as a package dependency

## Verification

- `pytest -q`
  - current result: `130 passed, 2 skipped`
- live smoke path:
  - set `PLAUD_LIVE_READS=1`
  - optionally set `PLAUD_SESSION_PATH` to a sacrificial session file
  - run `pytest -q tests/test_live_integration.py`
- direct live validation completed on 2026-05-17 against sacrificial Plaud artifacts:
  - login/session reuse
  - recording detail reads
  - recording rename and restore
  - folder assignment and clearing
  - trash move and restore
  - transcribe and summarize submission
  - task status inspection
  - speaker rename and restore on a completed transcript
  - sacrificial recordings restored to baseline after each test

## Wiring `plaud-mcp` to Claude Desktop / Claude Code

After `pip install -e .` (or a real install), add this to `claude_desktop_config.json` or `.claude.json`:

```json
{
  "mcpServers": {
    "plaud": {
      "command": "plaud-mcp"
    }
  }
}
```

Log in once with the CLI before starting Claude:

```
plaud login --email you@example.com --region us
```

The MCP server reads the saved session automatically.

## Next recommended slice

- Replace `docs/INSTALL.md` with accurate Python install/setup instructions
