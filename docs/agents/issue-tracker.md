# Issue Tracker: GitHub Issues

Issues and PRDs for this repo live on GitHub Issues at https://github.com/massive-value/plaud-tools/issues.

## Conventions

- One issue per vertical slice of work
- Triage state is recorded via labels (see `triage-labels.md`)
- Related work is grouped using milestones
- "Blocked by" relationships are recorded in the issue body as `Blocked by #N`, not as a label

## When a skill says "publish to the issue tracker"

Create a new GitHub issue with `gh issue create -R massive-value/plaud-tools` (or the web UI), applying the appropriate triage label and milestone.

## When a skill says "fetch the relevant ticket"

Use `gh issue view <number> -R massive-value/plaud-tools` (or the web URL).
