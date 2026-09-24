# Lockfiles (Constraint Files) — Refresh Procedure

Wave 2 / C3 introduced per-platform constraint files under `constraints/`.
These are compiled with `uv pip compile` from `pyproject.toml` and pin every
transitive dependency to a specific version for reproducible builds.

## File inventory

| File | Platform | Extras | Used by |
|---|---|---|---|
| `constraints/windows.txt` | Windows x86_64, Python 3.12 | `[tray,dev]` | `release.yml` build job; CI `constraints-install` job |
| `constraints/macos.txt` | macOS (aarch64), Python 3.12 | `[dev]` | CI `constraints-install` job; future macOS bundle (D2) |
| `constraints/linux.txt` | Linux x86_64, Python 3.12 | `[dev]` | CI `constraints-install` job; future Linux bundle (D2) |

`tray` is Windows-only (`pystray`, `Pillow`, `sv-ttk`, `comtypes`).
macOS and Linux omit it until the bundle port lands (D2 roadmap).

## How to refresh (upgrade all pins)

`.github/workflows/constraints-refresh.yml` runs this automatically every
week and opens a PR if anything changed — Dependabot's `pip` ecosystem
doesn't understand these hand-compiled per-platform files, so this is how
they stay current without a human remembering to run the commands below.
Trigger it early with `gh workflow run constraints-refresh.yml`, or run the
commands yourself:

Requires: `uv` on PATH (`pip install uv`).  Run from repo root.  Network access required.

```sh
# Windows — includes [tray] extras
uv pip compile --upgrade \
    --python-platform windows --python-version 3.12 \
    --extra tray --extra dev \
    pyproject.toml -o constraints/windows.txt

# macOS
uv pip compile --upgrade \
    --python-platform macos --python-version 3.12 \
    --extra dev \
    pyproject.toml -o constraints/macos.txt

# Linux
uv pip compile --upgrade \
    --python-platform linux --python-version 3.12 \
    --extra dev \
    pyproject.toml -o constraints/linux.txt
```

Open one PR with all three updated files.  The `constraints-install` CI job will
verify each file is installable on its native runner before merge.

## When to refresh

- After any `pyproject.toml` dependency change (bounds or new deps) — the
  weekly workflow won't pick up a bound change on its own schedule fast
  enough to unblock you, so refresh by hand in the same PR.
- Otherwise the weekly scheduled workflow handles the regular cadence.
- Before a new release if the last refresh was more than 4 weeks ago and the
  scheduled workflow hasn't run (check for an open refresh PR first).

## How the constraints are used

**release.yml** (Windows build job):
```
pip install --force-reinstall -c constraints/windows.txt ".[tray,dev]"
```
`constraints/windows.txt` is also uploaded as a lightweight SBOM release asset
alongside `PlaudTools.zip` and `SHA256SUMS`.

**ci.yml**:
- `constraints-install` — each platform runner installs its matching
  constraint file to catch a broken resolution before it reaches the release
  job.
- `test-windows-constraints` — runs the full test suite against the pinned
  Windows set (not just an install check), so a version bump that resolves
  fine but breaks a test fails here instead of at release.

## Provenance

All three files were most recently refreshed by `uv pip compile --upgrade`
(uv 0.11.8) on 2026-09-24 from a Windows host.  Cross-platform resolution is
purely metadata-based (uv reads wheel tags and markers without downloading
binaries), so the macOS and Linux files are accurate even though they were
compiled on Windows.
