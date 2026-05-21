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

## Distribution channels

**Bundle** (also "tray bundle") — the frozen Windows distribution shipped as `PlaudTools.zip` on every GitHub release. Contains `PlaudTools.exe` (tray app), a frozen CLI, a frozen MCP server, and ffmpeg. No Python required. Bundle users install by running `scripts/install.ps1` (standard path) or by manually extracting the zip.

**pip install** — installation via `pip install plaud-tools` from PyPI. Requires Python 3.11+. Users in this channel manage upgrades with `pip install --upgrade plaud-tools` or the `plaud-tools update` subcommand.

**Bundle users** and **pip users** have different update and uninstall paths; features in this area must treat them separately.

## Rewrite priorities

- reduce MCP tool count while improving reliability and token efficiency
- keep curated MCP responses small by default
- preserve live-working Plaud behavior at the protocol boundary
- validate incrementally with automated tests and sacrificial live Plaud data
