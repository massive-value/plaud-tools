# 01 — Shell tab-completions for the CLI

Status: needs-triage
Labels: needs-triage

## Problem

The CLI has 24 subcommands (`list`, `search`, `detail`, `show`, `transcript`, `summary`, `rename`, `folders`, `move-to-folder`, `move`, `rename-speaker`, `transcribe`, `status`, `trash`, `restore`, `delete`, `trash-move`, `trash-restore`, `upload`, `merge`, `login`, `session`, `ping`, plus per-subcommand flags). Without completions, users have to remember the exact name and flags or repeatedly run `plaud --help` / `plaud <cmd> --help`.

## Goal

Tab-completion of subcommands and their flags in PowerShell (primary target), bash, and zsh.

## Implementation sketch

Two viable paths:

1. **Hand-write static completion scripts.** Smallest dep change. Reading `cli.py` once and emitting:
   - PowerShell: `Register-ArgumentCompleter -CommandName plaud,pld -ScriptBlock { … }` shipped as a `.ps1` in the bundle.
   - bash: `complete -F _plaud plaud pld` in a sourceable `.bash` file.
   - zsh: a `_plaud` function in `compdef` form.
   Cheap to write, but drifts whenever subcommands or flags change unless we add a CI check.

2. **Switch CLI to a library with built-in completion generation** — `click` (`click_completion`) or `argcomplete` for argparse. `argcomplete` is the minimal-change option since the CLI is already argparse-based: add `# PYTHON_ARGCOMPLETE_OK` and an `argcomplete.autocomplete(parser)` call, then users run `register-python-argcomplete plaud` to install the shell hook. Auto-stays-in-sync with the parser definition. Doesn't work for the frozen `plaud.exe` bundle without the `argcomplete` runtime, though — it expects a Python interpreter on the user's machine.

Option 1 is friendlier for the shipped frozen bundle (no Python runtime assumption). Option 2 is friendlier for pip-installed users. Could do both — they don't conflict.

## Open questions

- For the frozen bundle, where do the completion scripts live and how do they get sourced? On Windows, the natural spot is `%LOCALAPPDATA%\Programs\PlaudTools\completions\plaud.ps1` plus a one-time `Add-Content $PROFILE ". '<path>'"` at install time. That couples to [[cli-on-path]] — both want a first-run install step that touches user environment.
- Dynamic completions (e.g. `plaud show <TAB>` listing recent recording IDs from the cached session) — nice to have but a meaningful step up in complexity. Probably out of scope for v1; static subcommand/flag completion only.

## Acceptance criteria

- In PowerShell after install, `plaud <TAB>` cycles through subcommands.
- `plaud list --<TAB>` cycles through `list`'s flags.
- Completions stay in sync with the parser — either via auto-generation (option 2) or a CI test that diffs the static scripts against a snapshot of the parser tree (option 1).
- Documented in `docs/INSTALL.md`.

## Context

Filed alongside [[cli-on-path]] and [[cli-version-flag]] as part of the "CLI feels frictionless after a bundle install" cluster. Worth triaging these three together since they share the same "first-run install step touches user environment" plumbing.
