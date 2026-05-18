# Domain Docs

This repo is configured as a single-context project.

## Read first

- `CONTEXT.md` at the repo root
- relevant ADRs under `docs/adr/` when they exist

If these files are missing, continue without surfacing that as a problem.

## Usage rules

- Prefer the domain language defined in `CONTEXT.md`
- Treat imported TypeScript code as reference behavior, not the target architecture
- Surface ADR conflicts explicitly instead of silently overriding them
