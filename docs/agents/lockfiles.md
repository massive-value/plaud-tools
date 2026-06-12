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

- After any `pyproject.toml` dependency change (bounds or new deps).
- On a regular cadence (e.g. monthly) to pick up security patches.
- Before a new release if the last refresh was more than 4 weeks ago.

## How the constraints are used

**release.yml** (Windows build job):
```
pip install --force-reinstall -c constraints/windows.txt ".[tray,dev]"
```
`constraints/windows.txt` is also uploaded as a lightweight SBOM release asset
alongside `PlaudTools.zip` and `SHA256SUMS`.

**ci.yml** (`constraints-install` job):
Each platform runner installs its matching constraint file to catch breakage
before it reaches the release job.

## Provenance

All three files were compiled by `uv pip compile` (uv 0.11.21) on 2026-06-12
from a Windows host.  Cross-platform resolution is purely metadata-based (uv
reads wheel tags and markers without downloading binaries), so the macOS and
Linux files are accurate even though they were compiled on Windows.
