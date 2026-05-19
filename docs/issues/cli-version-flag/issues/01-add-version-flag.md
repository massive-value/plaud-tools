# 01 — Add top-level `--version` flag to the CLI

Status: needs-triage
Labels: needs-triage

## Problem

`plaud --version` (and `pld --version`) exit with argparse error code 2 because there's no top-level `--version` action — the CLI requires a subcommand. Users can't ask the binary which version they have without invoking a real subcommand or reading the bundle's filename.

`plaud-mcp.exe --version` already works (see `src/plaud_tools/server.py` for the pattern). The CLI should match.

## Goal

`plaud --version` prints `plaud <version>` and exits 0, where `<version>` is `plaud_tools.__version__`.

## Implementation sketch

In `src/plaud_tools/cli.py`, on the top-level `ArgumentParser`:

```python
from . import __version__
parser.add_argument("--version", action="version", version=f"plaud {__version__}")
```

argparse's `action="version"` handles the rest (prints and exits 0). No subcommand dispatch changes needed.

## Acceptance criteria

- `plaud --version` → prints `plaud 0.1.5` (or whatever the current version is) and exits 0.
- `pld --version` → same.
- Frozen `cli\plaud.exe --version` → same (verifies the importlib.metadata fix from commit 6dc15e0 is still working in bundled form).
- No regression to existing subcommands.

## Context

Came out of testing the v0.1.5 build — the tray version footer and `plaud-mcp.exe --version` both report the right version now, but `plaud.exe --version` errors out, which is the obvious thing a user types to confirm what they have installed.
