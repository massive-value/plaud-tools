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

The `dev` extra installs `pytest`, `pyinstaller`, and `build`. The `tray`
extra installs the optional Windows tray dependencies (`pystray`, `Pillow`,
`sv-ttk`). On non-Windows hosts you can drop the `tray` extra.

## Running tests

```
pytest -q
```

The default suite is offline and runs in a few seconds.

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
