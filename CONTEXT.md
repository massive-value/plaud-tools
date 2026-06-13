# Project Context

## Purpose

This project exists to bridge Plaud recordings into AI-friendly workflows.

The target phase-1 product is a Python implementation with:

- a reusable Plaud domain/client layer
- a Python MCP server optimized for agent use
- a Python CLI optimized for common human terminal workflows

## Current reality

The repository contents were imported from another TypeScript-based project.
That code is useful as a behavioral reference, but it is not the desired final architecture for this repo.

## Important domain facts

- Plaud's public API behavior used here is reverse-engineered from the web app
- session and region behavior are fragile and must be preserved carefully
- some write flows only work when requests mimic browser headers closely
- transcript and summary data are not always returned inline and often require fetching linked content
- uploads require special handling for accepted file types, transcoding, and multipart transfer

## Authentication model

Plaud's web client uses a three-token JWT system, verified by decoding the
responses in `har-captures/plaud-login-capture.har`:

- **access_token** (`typ=UT`) — issued by `POST /auth/access-token`,
  **300-day** lifetime.  This is the long-lived user token.
- **workspace_token** (`typ=WT`) — issued by
  `POST /user-app/auth/workspace/token/{workspace_id}` using the UT,
  **1-day** lifetime.  Scoped to a workspace; carries `wid`, `role`, etc.
- **refresh_token** (`typ=WRT`) — issued alongside the WT by the same
  workspace endpoint, **30-day** lifetime.  Renews the WT without
  going through full re-authentication.

`plaud-tools` takes a deliberate shortcut: we only store the 300-day UT
and use it directly as `Authorization: Bearer ...` on every API call.
The endpoints we hit (browse, get, mutate, upload, process, list_folders,
merge) all accept the UT, so we never need the WT+WRT exchange.  Trade-off:
users must re-authenticate once a year, but we avoid the complexity of
a refresh loop.  `SessionManager.require()` defends against this by
raising `PlaudSessionExpiredError` when the UT is within 30 days of
expiry, prompting re-login while the user still has a working token.

If a future Plaud API endpoint requires a workspace-scoped token, this
model needs to grow: store all three tokens, exchange via the workspace
endpoint, refresh on demand.  Until then, simplicity wins.

## Distribution channels

**Bundle** (also "tray bundle") — the frozen Windows distribution shipped as `PlaudTools.zip` on every GitHub release. Contains `PlaudTools.exe` (tray app), a frozen CLI, a frozen MCP server, and ffmpeg. No Python required. Bundle users install by running `scripts/install.ps1` (standard path) or by manually extracting the zip.

**pip install** — installation via `pip install plaud-tools` from PyPI. Requires Python 3.11+. Users in this channel manage upgrades with `pip install --upgrade plaud-tools` or the `plaud-tools update` subcommand.

**Bundle users** and **pip users** have different update and uninstall paths; features in this area must treat them separately.

## Install layout and app data

**Install layout** — the on-disk arrangement of binaries for the *running*
plaud-tools install, derived from `sys.executable`.  Distribution-channel
aware (bundle / pip / dev).  Represented by `InstallLayout` in `layout.py`.
Tray uninstall, tray update, MCP-child process scoping, and AI-client wiring
all act on the *running* install, never a hardcoded canonical path.  The
canonical install path (`%LOCALAPPDATA%\Programs\PlaudTools\`) is the
responsibility of `scripts/install.ps1` and does not survive into the Python
code.

**App data** — the per-user data directory and the known files inside it
(session storage, tray log, MCP log, events).  Channel-agnostic,
platform-aware via `platformdirs`.  On Windows: `%LOCALAPPDATA%\PlaudTools\`.
On macOS / Linux: per `platformdirs.user_data_dir` conventions.  Lives in
`appdata.py`.  All log and event files share the data directory; logs do
not get a separate `user_log_dir` subtree (deliberate — preserves existing
Windows file locations as a no-op and keeps Mac/Linux conventions simple).

## Destructive-operation handling — CLI vs MCP (Decision D4)

The CLI and MCP surfaces apply different but complementary mechanisms to guard
destructive operations:

**CLI (`cli.py`)** — interactive sessions use a `--yes` / `-y` flag.  Without
the flag, a destructive subcommand (e.g. `plaud delete`) prints a confirmation
prompt and exits; with `--yes` it proceeds immediately.  This is appropriate
for terminal users who can read and respond to stdout.

**MCP (`server.py` + `mcp.py`)** — the MCP server runs over stdio and cannot
display interactive prompts.  Safety is layered:

1. `ToolAnnotations` on every `types.Tool` entry in `_TOOLS` declare
   machine-readable capability hints (`readOnlyHint`, `destructiveHint`,
   `idempotentHint`, `openWorldHint`).  Well-behaved MCP clients (e.g. Claude
   Desktop) surface these to the user or gate execution automatically.
2. `delete_recording` additionally requires a `confirm: boolean` parameter.
   When `confirm` is absent or `false`, the handler returns a structured
   validation error (`error_code: "validation"`) instructing the caller to
   re-invoke with `confirm=true` only after obtaining explicit human approval.
   This means even clients that ignore `ToolAnnotations` cannot silently
   hard-delete a recording.

**Rationale:** servers declare capability hints; clients enforce policy.  No
server-side interactive prompt is possible over stdio.  The `confirm` gate is
the MCP-native equivalent of `--yes`, moved into the tool schema so the LLM
must carry explicit evidence of consent through the call.

## Transport resilience

**Request timeouts.** `UrllibTransport` applies a 30 s default timeout to every call; S3 chunk PUTs get a 120 s ceiling. A hung `urlopen` now surfaces as `PlaudApiError` instead of wedging the process indefinitely.

**Region-redirect bound.** Plaud's `-302` region-redirect is followed at most once per request. A second `-302` raises `PlaudApiError("region redirect loop")`.

**Retry / backoff (Wave 2 / C5).** HTTP 429 and 5xx responses are retried up to twice with exponential backoff + ±25 % jitter (base ≈ 1 s → 3 s). When the server supplies a `Retry-After` header the client sleeps the larger of the header value and the computed delay. The transcription, summary, and merge poll loops treat a transient error as a skipped poll and continue until their deadline instead of aborting early.

## Browse and upload behavior

**Incremental filtered browse.** `browse_recordings` / CLI `list`/`search` page the upstream API with `skip`/`limit`, filter each page, and stop once enough matches are collected to answer `has_more` honestly. The entire library is never pulled into memory.

**Streaming disk-chunked uploads.** `upload_recording` reads 5 MiB chunks from disk per multipart part; transcoding writes the MP3 directly to a temp file (`transcode_to_mp3_path`). Large recordings do not scale memory with file size. The presign → multipart → complete protocol is unchanged.

## Supply-chain integrity

**ffmpeg pin.** The release pipeline downloads ffmpeg from a pinned versioned URL and verifies it against a hardcoded SHA-256.

**SHA256SUMS asset.** Every release publishes a `SHA256SUMS` file alongside `PlaudTools.zip`. Both the PowerShell installer (`scripts/install.ps1`) and the in-app updater verify the zip before extracting, and verification is unconditionally **fail-closed** (#113): a hash mismatch aborts, and so does an absent `SHA256SUMS` asset. (The original rollout warned-and-proceeded when the asset was absent, to cover pre-wave-0 releases still in the upgrade path; that soft-fail branch was removed in v0.3.2 once the asset had shipped in ≥2 tagged releases. Both install and update fetch `releases/latest`, which always carries the asset, so an absent asset now signals a malformed release or tampered asset list.)

**GitHub Actions SHA pins.** All GitHub Actions steps are pinned to full commit SHAs in both `ci.yml` and `release.yml`.

**Tri-platform constraint lockfiles.** `constraints/{windows,macos,linux}.txt` pin the full dependency closure per platform (compiled with `uv pip compile`). The Windows lockfile ships as a lightweight SBOM release asset; a CI job verifies all three install on their native OS.

## Session cache freshness

`SessionManager` caches the loaded session in memory. The cache is invalidated by detecting an out-of-band session update: a cheap mtime probe on the backing file when available, or a 5-minute TTL otherwise. The within-window hot path skips keyring reads entirely.

## Diagnostics — `doctor` output

`plaud-tools doctor` returns a JSON document. The `mcp_lifecycle` section includes an `enumerator` field reporting which process enumerator is active: `psutil` (normal bundle), `wmic` (legacy Windows), `powershell` (modern Windows without psutil), or `none` (no enumerator available). The dev-fallback MCP path in the same section is platform-aware (no `.exe` suffix on POSIX).

## Tray updater security

**Host allowlist.** Update downloads are restricted to `github.com` and `objects.githubusercontent.com` (exact hostname match). Any redirect to a different host is refused before the download starts.

**Absolute PowerShell path.** The tray and lifecycle helpers invoke PowerShell via its absolute path (`%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe`) to prevent PATH-hijack attacks.

## Scripting and CI authentication

`login --password` leaks the password via process listings and shell history. For scripting and CI use the `PLAUD_ACCESS_TOKEN` environment variable or `plaud-tools session set --token <token>` instead. The `--password` flag remains available for interactive terminal use.

## Rewrite priorities

- reduce MCP tool count while improving reliability and token efficiency
- keep curated MCP responses small by default
- preserve live-working Plaud behavior at the protocol boundary
- validate incrementally with automated tests and sacrificial live Plaud data
