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

## Rewrite priorities

- reduce MCP tool count while improving reliability and token efficiency
- keep curated MCP responses small by default
- preserve live-working Plaud behavior at the protocol boundary
- validate incrementally with automated tests and sacrificial live Plaud data
