# Contributing to plaud-tools

Thanks for your interest in improving `plaud-tools`. This project is alpha,
maintained best-effort, and welcomes patches that improve correctness,
coverage, or docs.

## Dev setup

```
git clone https://github.com/massive-value/plaud-tools.git
cd plaud-tools
pip install -e ".[dev,tray]"
```

The `dev` extra installs `pytest`, `pyinstaller`, `build`, `mypy`, and
`mcp[cli]`. The `tray` extra installs the optional Windows tray dependencies
(`pystray`, `Pillow`, `sv-ttk`). On non-Windows hosts you can drop the `tray`
extra.

Equivalent with [uv](https://docs.astral.sh/uv/) (faster, and what CI's
`constraints/*.txt` files are compiled with):

```
uv venv
uv pip install -e ".[dev,tray]"
```

`uv run <cmd>` also works without activating the venv, but note it silently
creates a `uv.lock` project lockfile the first time you use it — this repo
pins dependencies via `constraints/*.txt` instead (see
`docs/agents/lockfiles.md`), so `uv.lock` is gitignored; delete it or ignore it.

### Testing MCP tools interactively (Inspector)

The MCP server (`plaud-mcp`) uses the low-level `mcp.server.lowlevel.Server`
class, not `FastMCP`, so the `mcp dev` CLI shortcut doesn't work — it only
supports `FastMCP` instances. Use the standalone Inspector instead, which
works with any stdio MCP server (requires Node.js/`npx`):

```
npx @modelcontextprotocol/inspector uv run plaud-mcp
```

This opens a browser UI pre-filled with `Command: uv`, `Arguments: run
plaud-mcp`. Click **Connect**, then **Tools → List Tools** to see all 11
tools, select one, fill in its arguments, and **Run Tool**. It talks to your
real Plaud session (keyring-backed), so reads are safe but destructive tools
(`delete_recording`, `mutate_recording` with `action=trash`, etc.) act on
your actual account — point `PLAUD_SESSION_PATH` at a sacrificial session
first if you want to exercise those.

## Running tests

```
pytest -q
```

The default suite is offline and runs in a few seconds.

### Regenerating the MCP golden fixture

`tests/data/tool_descriptions.golden.json` is a snapshot of every MCP tool's
name, description, and `inputSchema`.  If you intentionally change any tool
definition in `mcp_pt/server.py`, regenerate it and commit the updated fixture
alongside your code change:

```
PLAUD_GOLDEN_REGEN=1 pytest tests/test_mcp_golden.py
```

Live tests that hit the real Plaud API are gated behind the
`PLAUD_LIVE_READS=1` environment variable and must only be run against a
**sacrificial Plaud account** — never your real one. The live suite renames,
trashes, and restores recordings; it always tries to restore baseline, but
treat any account it touches as expendable:

```
PLAUD_LIVE_READS=1 pytest -q tests/test_live_integration.py
```

You can also point `PLAUD_SESSION_PATH` at an isolated session file so the
live tests don't disturb your normal login.

## Branching and PR workflow

- Branch off `master` with a descriptive name (e.g. `fix-folder-clear`,
  `issue-12-merge-progress`).
- Keep PRs focused — one logical change per PR. Smaller diffs review faster.
- Run `pytest -q` locally before pushing. CI will run it again on the PR.
- Reference the GitHub issue in the PR description if one exists. The
  CHANGELOG entry is part of the PR — add a bullet under `## [Unreleased]`.
- Squash-merge is the default; the merge commit subject becomes the
  changelog-grade summary.

## Issue tracker

Issues and PRDs live on GitHub Issues at
<https://github.com/massive-value/plaud-tools/issues>. The previous
local-markdown convention under `docs/issues/` has been retired — please file
new bugs and feature requests on GitHub.

For security issues, follow the private disclosure process in
[SECURITY.md](SECURITY.md) instead of filing a public issue.

## AI agent collaborators

If you're an AI agent contributing to this repo, read [CLAUDE.md](CLAUDE.md)
(and [AGENTS.md](AGENTS.md), which delegates to it) first. They document the
working conventions, the canonical tool surface, and the rewrite principles
that constrain what changes are in or out of scope.
