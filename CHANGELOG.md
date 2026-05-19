# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Changed

- Migrated the issue tracker from local markdown files under `docs/issues/` to
  GitHub Issues at https://github.com/massive-value/plaud-tools/issues. Agent
  conventions in `CLAUDE.md` and `docs/agents/issue-tracker.md` updated to
  match.

## [0.1.8] - 2026-05-19

### Added

- `wait_for_summary()` on `PlaudClient`. `process_recording` and the
  non-detach upload path now wait for summary completion after transcription
  finishes.
- `--skip-summary` flag on the CLI `upload` command for transcript-only
  workflows.
- ISO 8601 string support for `--start-time` on the CLI and `start_time` on
  the MCP `upload_recording` tool.
- `plaud dump <id>` CLI command for raw `/file/detail` debug inspection.

### Fixed

- `body_decoded` bug in `_fetch_summary_from_data_link` — now correctly uses
  `response.text()`.
- `_extract_inline_summary` is more robust: handles `dict` `data_content`,
  tries multiple key names, and falls back to `data_type` matching when
  `data_id` does not find the item.
- CLI `summary` command now passes `include_summary=True` so the data-link
  fetch is actually attempted.

## [0.1.7] - 2026-05-19

### Added

- `--version` flag on the CLI.
- PATH setup helper and shell completions for bash and PowerShell, installed
  alongside the CLI.
- `pt` short alias as a second CLI entry point.

### Changed

- Renamed the primary CLI entry point from `plaud` to `plaud-tools`. The
  PyInstaller specs, tray app, and install docs have been updated to match.

### Fixed

- Release workflow updated for the `plaud-tools` rename and the new
  `pt.cmd` shim.

## [0.1.6] - 2026-05-19

### Fixed

- Frozen builds now bundle `dist-info` metadata so the tray version footer
  renders the real version instead of `v0.0.0+dev`. `copy_metadata('plaud-tools')`
  is now applied to all three PyInstaller specs.
- Frozen `plaud.exe` no longer crashes at startup with
  `attempted relative import with no known parent package`. A new
  `scripts/plaud_entry.py` wrapper mirrors the existing
  `plaud_mcp_entry.py` / `plaud_tray_entry.py` pattern.

[Unreleased]: https://github.com/massive-value/plaud-tools/compare/v0.1.8...HEAD
[0.1.8]: https://github.com/massive-value/plaud-tools/compare/v0.1.7...v0.1.8
[0.1.7]: https://github.com/massive-value/plaud-tools/compare/v0.1.6...v0.1.7
[0.1.6]: https://github.com/massive-value/plaud-tools/compare/v0.1.5...v0.1.6
