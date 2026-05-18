# ADR 001 — Python Rewrite and MCP Simplification

**Status:** complete (2026-05-18)

## Problem

The repo was imported from a TypeScript monorepo that demonstrated working Plaud behaviors but also brought package structure, documentation, and product assumptions that no longer matched the repo's direction. The inherited MCP surface was broader and more verb-shaped than desired for agent use, and the docs overstated the role of a Windows tray app that was out of scope.

## Decision

Rewrite the shared backend surfaces (domain/client layer, MCP server, CLI) in Python. Treat the TypeScript code as behavioral reference only. Design the MCP surface first around a small set of workflow-oriented tools; keep the CLI as a second but still core surface with terminal-friendly ergonomics that do not need to mirror MCP exactly.

## Implementation decisions

- Build a single Python domain layer (`src/plaud_tools/client.py`) as the deep module for all Plaud behavior: auth/session, region resolution, request shaping, payload normalization, upload/transcode, transcript mutation.
- MCP façade is explicitly workflow-oriented: 5 tools (`browse_recordings`, `get_recording`, `mutate_recording`, `upload_recording`, `process_recording`) rather than one tool per verb or one giant action router.
- CLI façade is optimized for human terminal patterns and intentionally diverges from MCP names and argument shapes where that improves usability.
- MCP is non-interactive for auth. Login happens through the CLI; the MCP server reads a saved session and fails with a clear error if none exists.
- Session storage uses a centralized abstraction with OS keyring preference and a locked-down file fallback (`~/.config/plaud-tools/session.json`). No other module touches secrets directly.
- Keep the implementation synchronous unless async is clearly required. The MCP server entry point (`server.py`) uses `asyncio` only because the MCP SDK requires it; the domain layer is fully synchronous.
- Preserve five critical behavior groups exactly as reverse-engineered from the TypeScript source and HAR captures: auth/session including region failover, browser-like HTTP fingerprinting, upload/transcode/multipart flows, transcript read-modify-write, and curated MCP response shaping.
- Single-context repo: shared vocabulary and architectural guidance live at the root (`CONTEXT.md`, `docs/adr/`) rather than split across product-specific subdirectories.

## Testing decisions

- Three testing layers: unit tests for shaping/parsing/filtering/CLI/MCP output; fixture-based protocol tests for Plaud response normalization and request assembly; opt-in live integration tests gated behind `PLAUD_LIVE_READS=1`.
- The domain layer receives the strongest automated coverage because it carries the most protocol knowledge and the highest regression risk.
- Live tests use sacrificial recordings and folders reserved for test execution so that real account content is never mutated accidentally.
- Test externally observable behavior and protocol contracts, not internal helper structure.

## Out of scope

- Windows tray app
- Preserving the TypeScript package layout or public surface shapes
- Standalone packaged executables
- Backward compatibility for the old MCP tool names and schemas
