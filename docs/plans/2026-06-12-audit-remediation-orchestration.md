# Audit Remediation — Orchestration Plan

**Audience:** an Opus orchestrator session driving subagents in this repo.
**Source:** principal-level audit of plaud-tools @ v0.2.11 (2026-06-12), plus owner decisions recorded below.
**Goal:** land every audit finding — Critical through Low — as small, independently verifiable PRs.

---

## 1. Mission & ground rules for the orchestrator

1. **One task card = one branch = one PR.** Never batch unrelated task cards into a single PR. Use worktree isolation for any two agents running concurrently that touch overlapping files (see conflict matrix, §6).
2. **Subagents implement; you verify.** After each subagent reports done, run the gates in §7 yourself (or via a verifier subagent) before merging. Do not trust "tests pass" claims without output.
3. **Respect the fragile protocol boundary.** `client.py` request shapes, headers, body fields, and polling flows mimic the Plaud web client and are reverse-engineered (see `CONTEXT.md`). Tasks marked **[LIVE-GATE]** change protocol-adjacent behavior and must stop at "PR ready + tested against stubs"; the human validates against sacrificial live Plaud data before merge.
4. **Bundle-affecting changes require local PyInstaller validation** before they ship (owner's standing rule). Tasks marked **[BUNDLE-GATE]** end with a note to the human to run the local build/swap workflow.
5. **Match existing style.** This codebase has high comment discipline (comments explain *why*, often citing issues/ADRs). Subagents must read the target file fully before editing and preserve that idiom.
6. **CHANGELOG.md** gets one entry per merged PR, under an Unreleased heading.
7. Issues/PRDs live on GitHub Issues (`massive-value/plaud-tools`). Optionally file one issue per wave with the `ready-for-agent` label for traceability; not required for execution.

## 2. Read-first list (orchestrator context priming)

Every implementation subagent prompt should name the specific files it owns. The orchestrator itself should read once at session start:

- `CONTEXT.md` — token model, distribution channels, install layout vs app data
- `CLAUDE.md` — rewrite principles
- `docs/adr/003-mcp-process-lifecycle.md`, `docs/adr/004-install-layout-and-app-data.md`
- `tests/conftest.py` — the autouse real-state guards every new test must coexist with

## 3. Owner decisions (already made — do not re-litigate)

| # | Decision |
|---|----------|
| D1 | GitHub org 2FA is enforced. No further release-account hardening tasks needed beyond what's in this plan (SHA-pinned actions, hash publication/verification). |
| D2 | **Design lockfiles and CI for all three platforms now** (Windows, macOS, Linux), ahead of the planned Mac/Linux bundles. |
| D3 | Encode **429/backoff handling** observed-API-politeness into the client polling/retry paths. |
| D4 | MCP destructive-op handling: **ToolAnnotations on all tools + required `confirm` param on `delete_recording`**; document the CLI (`--yes`) vs MCP asymmetry decision in `CONTEXT.md`. Servers declare, clients enforce; no server-side interactive prompts. |
| D5 | Explicit non-fixes (do NOT implement): WT/WRT token refresh loop; tkinter UI automation tests; in-memory password zeroing; replacing urllib with httpx; full A/B update rollback (M10) — revisit only if failure telemetry shows need. |

## 4. Wave plan

Waves are ordered by dependency. **Within a wave, tasks run in parallel** unless the conflict matrix (§6) says otherwise. Effort: S/M/L. Risk: Low/Med.

---

### Wave 0 — Quick wins (all S, all Low risk, fully parallel)

**A1. Transport timeout** — `src/plaud_tools/transport.py`
Add `timeout` support to `UrllibTransport.request` (default 30s, constructor-configurable). Thread a longer timeout (120s) through `PlaudClient._s3_put` chunk uploads (5 MiB on slow links). Add a stub-transport test asserting the timeout parameter reaches `urlopen`, and that `TimeoutError`/`URLError(socket.timeout)` surfaces as `PlaudApiError`.
*Acceptance:* no `urlopen(` call in `src/` without an explicit timeout; new tests pass.

**A2. Bound region-redirect recursion** — `src/plaud_tools/client.py:442-446`
`_request_json` retries a `-302` region redirect at most once (e.g. `_redirected: bool = False` keyword). On a second `-302`, raise `PlaudApiError("region redirect loop")`. Preserve the existing `update_region` persistence on the first redirect — that behavior is load-bearing.
*Acceptance:* test with a stub transport returning `-302` forever → exactly 2 requests then error; existing redirect test still passes.

**A3. Release pipeline integrity (publish side)** — `.github/workflows/release.yml` only
Three changes in one PR (same file, one agent):
(a) Pin ffmpeg: replace the floating `ffmpeg-release-essentials.zip` URL with a versioned URL + `Get-FileHash`-verified SHA-256 constant; fail the build on mismatch.
(b) Emit a `SHA256SUMS` file (hash of `PlaudTools.zip`) and upload it as a release asset alongside the zip.
(c) Pin all actions (`actions/checkout`, `actions/setup-python`, `softprops/action-gh-release`, `pypa/gh-action-pypi-publish`) to full commit SHAs with a trailing `# vX` comment. Also pin in `ci.yml`.
*Acceptance:* workflow YAML lints (`actionlint` if available); hash constant documented with the ffmpeg version it pins.
*Note:* C1 (verify side) depends on this shipping in a release **before** enforcement — see §7 release sequencing.

**A4. events.jsonl rotation** — `src/plaud_tools/mcp.py:20-29`
In `_write_event`, if the file exceeds ~1 MB before append, rotate (rename to `events.jsonl.1`, replacing any existing one) then append fresh. Must remain never-raising. Check `tray/background.py`'s poll loop tolerates rotation (it re-reads by offset or whole-file — verify and adapt if it tracks file position).
*Acceptance:* unit test: write past cap → rotation occurred, new event present, no exception when directory is read-only.

**A5. mcp_lifecycle enumeration honesty** — `src/plaud_tools/mcp_lifecycle.py:46-85`
Implement the docstring-promised `tasklist /FO CSV` fallback after WMIC fails (WMIC is removed on current Win11). Note: `tasklist` CSV does not include full exe paths — use `tasklist /FI "IMAGENAME eq plaud-mcp.exe" /FO CSV` and resolve paths via `wmic`-free means, or fall back to PowerShell `Get-Process plaud-mcp | Select Path,Id` one-liner via subprocess (preferred: matches the path-scoping requirement). Log at WARNING when enumeration yields zero processes *and* psutil was unavailable, so silent-failure is observable. Update the docstring to match reality. Decision on bundling psutil itself is C4, not this task.
*Acceptance:* unit tests with injected enumerator unchanged; new test covers fallback parsing with fixture output; ADR 003 amended with a short note.

**A6. Structured TypeError guard in call_tool** — `src/plaud_tools/server.py:292-299`
Wrap `handler(**arguments)` so unexpected/missing argument names return the standard `{"error", "error_code": "validation", "retryable": false}` payload instead of leaking a raw TypeError to the MCP framework.
*Acceptance:* test in `tests/test_server.py` calling a handler with a bogus kwarg → isError result with `error_code: validation`.

---

### Wave 1 — Quality baseline (run AFTER Wave 0 merges; B1 → B2 sequential, rest parallel)

**B1. Ruff (lint + format)** — `pyproject.toml`, `.github/workflows/ci.yml`, repo-wide mechanical fixes
Add `[tool.ruff]` config (target py311, line length matching current code ~100-110, sensible rule set: E/F/W/I/UP/B). Add a CI job step `ruff check` + `ruff format --check`. Apply autofixes in the same PR. **This task touches everything — nothing else may run concurrently.**
*Acceptance:* CI green; diff is mechanical only (verifier subagent confirms no semantic changes).

**B2. Mypy baseline** — `pyproject.toml`, CI (after B1)
`[tool.mypy]` with lenient global settings (`ignore_missing_imports = true`), stricter per-module opt-ins for `session.py`, `client.py`, `layout.py`, `appdata.py`. Fix cheap errors; `# type: ignore[code]` with comment for the rest. Add `py.typed` marker. CI step.
*Acceptance:* `mypy src/plaud_tools` exits 0 in CI on all matrix platforms.

**B3. query.py unit tests** — new `tests/test_query.py`
Direct coverage for `parse_isoish` (date vs datetime, Z suffix, end_of_day, invalid), `filter_recordings` (each filter, unfiled vs folder_id="" equivalence, sort order), `summarize_recording`.
*Acceptance:* meaningful assertions, not snapshot-only; runs cross-platform (no tz assumptions — use wide tolerances or fixed-offset datetimes).

**B4. Real-ffmpeg transcode smoke** — `.github/workflows/ci.yml` (bundle-smoke job), small test or script
In the existing Windows bundle-smoke job, generate a tiny wav (e.g. via Python `wave` module), run `transcode_to_mp3` against a real ffmpeg (download step can reuse A3's pinned URL+hash), assert non-empty MP3 magic bytes.
*Acceptance:* CI job exercises the real subprocess path.

**B5. Tri-platform test matrix (D2)** — `.github/workflows/ci.yml`
Add `macos-latest` to the test matrix (windows/ubuntu/macos × 3.11-3.13). Fix any test that assumed two platforms. Keep bundle-smoke Windows-only for now but structure the job so adding mac/linux bundle jobs later is additive.
*Acceptance:* full matrix green.

---

### Wave 2 — Critical (parallel except noted)

**C1. Hash verification (consume side)** — `scripts/install.ps1`, `src/plaud_tools/tray/updater.py`, `src/plaud_tools/scripts/update.ps1` **[BUNDLE-GATE]**
Installer and in-app updater download `SHA256SUMS` next to the zip, verify before extract/launch. **Rollout constraint:** older releases have no SHA256SUMS asset. Behavior: if the sums asset exists, verification is mandatory (fail closed with a clear message); if absent, log/warn and proceed (this branch can be removed two releases later). In `updater.py`, verify after download, before writing the dispatcher/sentinel.
*Acceptance:* pester-style or Python tests for the hash-check logic (tamper a byte → refusal); existing `tests/test_install_ps1.py` patterns extended. Depends on A3 having shipped in at least one tagged release for end-to-end proof; code can merge before that.

**C2. Non-blocking MCP server** — `src/plaud_tools/server.py` **[BUNDLE-GATE]**
Wrap handler invocation in `await asyncio.to_thread(...)`. Review the two shared mutable points for thread tolerance: `SessionManager._cached_session` (idempotent assignment — fine; add a comment) and `_write_event` appends (open-append-close per call — fine). Add a responsiveness test: start a handler that sleeps (stub client), assert a concurrent `list_tools` answers within 1s.
*Acceptance:* new async test passes; all existing server/interface tests pass.

**C3. Tri-platform lockfiles (D2)** — new `constraints/` dir, `.github/workflows/release.yml`, docs
Generate per-platform constraint files (`constraints/windows.txt`, `macos.txt`, `linux.txt`) via `uv pip compile` (or pip-tools) from `pyproject.toml` extras `[tray,dev]` (Windows) and `[dev]` (mac/linux until tray ships there). Release workflow installs with `-c constraints/windows.txt`. Add a `docs/agents/` note or README section on the refresh procedure (`uv pip compile --upgrade`, one PR). Lightweight SBOM: attach the constraints file used to the GitHub release.
*Acceptance:* release workflow uses the constraint file; CI job verifies constraints are installable on each OS (can fold into B5 matrix).
*Sequencing:* same file as A3 (`release.yml`) — run after A3 merges.

**C4. psutil decision + frozen enumeration test** — `pyproject.toml`, `pyinstaller/*.spec`, `tests/` **[BUNDLE-GATE]**
Decide: add `psutil` to the `[tray]` extra and tray-spec hiddenimports (preferred — small, removes reliance on shelling out), keeping A5's fallback as defense. Add a bundle-smoke CI assertion that the frozen tray can import psutil (e.g. a `--diagnose-enum` hidden flag or a build-time import check).
*Acceptance:* `pip install ".[tray]"` pulls psutil; bundle smoke proves the frozen import; ADR 003 amended (it currently documents psutil-as-optional).

**C5. 429 backoff & polite polling (D3)** — `src/plaud_tools/client.py`, `src/plaud_tools/errors.py` **[LIVE-GATE]**
(a) In `_request_json`, on `PlaudApiError` with `http_status == 429` or `>=500`, retry up to 2 times with exponential backoff + jitter (1s, 3s base), honoring `Retry-After` header when present (transport must expose response headers on error — extend `PlaudApiError.from_http_error` to capture `Retry-After`).
(b) In `wait_for_transcription` / `wait_for_summary` / `merge_recordings` polling loops, treat a transient error as a skipped poll (continue until deadline) instead of aborting.
*Acceptance:* stub-transport tests for retry counts, Retry-After honoring, and poll-loop survival of one transient failure. Human validates politeness against live data.

**C6. MCP destructive-op annotations (D4)** — `src/plaud_tools/server.py`, `src/plaud_tools/mcp.py`, `CONTEXT.md`, `tests/data/tool_descriptions.golden.json`
(a) Add `ToolAnnotations` to all 9 tools: `browse_recordings`/`get_recording`/`list_folders` → `readOnlyHint: true`; `mutate_recording`/`rename_speaker`/`upload_recording`/`process_recording`/`merge_recordings` → `destructiveHint: false` (reversible or additive), idempotent where true; `delete_recording` → `destructiveHint: true, idempotentHint: false`.
(b) Add required `confirm: boolean` param to `delete_recording` schema; handler returns a `validation` error telling the agent to pass `confirm: true` after user confirmation when absent/false.
(c) Document in `CONTEXT.md`: CLI uses `--yes`, MCP uses annotations + confirm param; rationale (servers declare, clients enforce).
(d) Update the golden tool-descriptions fixture.
*Acceptance:* golden test updated deliberately (not blindly regenerated); new handler test for confirm-gate; MCP SDK version supports annotations (mcp>=1.0 does — verify at implementation time).

---

### Wave 3 — High-leverage

**D1. Incremental filtered browse** — `src/plaud_tools/mcp.py:148-189`, `src/plaud_tools/cli.py` list/search **[LIVE-GATE]**
Replace fetch-everything-then-filter: page upstream with `skip`/`limit` (page size ~200), filter each page, stop once `after + limit + 1` matches found (the +1 resolves `has_more` honestly). Keep `query.filter_recordings` as the per-page filter. Preserve exact response shape (`items`, `next_after`).
*Acceptance:* stub tests with multi-page fixtures covering: matches spanning pages, exact-boundary has_more, empty results. Human live-validates ordering assumptions (server-side `sort_by=start_time` + `is_desc` interaction with `is_trash`).

**D2. Streaming uploads** — `src/plaud_tools/client.py:86-177`, `src/plaud_tools/transcode.py`, `src/plaud_tools/mcp.py`, `src/plaud_tools/cli.py` **[LIVE-GATE]**
`upload_recording` accepts a `Path` (keep a bytes overload for compat or migrate both call sites): stat for presign filesize, read 5 MiB chunks from disk per part. `transcode_to_mp3` gains a path-in/path-out variant so the MP3 never round-trips through memory; callers pass the temp output path to upload.
*Acceptance:* existing upload tests adapted; new test that a large fake file (sparse, ~20 MiB) uploads in correct chunk count without holding the whole file (assert via transport stub on per-call body sizes).

**D3. run_cli dispatch refactor** — `src/plaud_tools/cli.py:154-459`
Break the if-chain into per-command functions in a `dict[str, Callable]` registry. **Zero behavior change** — `tests/test_interfaces.py` (1,065 lines) is the safety net and must pass unmodified.
*Acceptance:* `tests/test_interfaces.py` untouched and green; no output-shape diffs.
*Sequencing:* after E5 or before — they both touch cli.py; serialize (D3 first, then E5 is trivial).

**D4. Robust TOML editing** — `src/plaud_tools/ai_clients.py:85-121`
Replace the `[^\[]*` regex section editing with `tomlkit` (style-preserving round-trip; add to deps) or a line-aware parser that treats the section as ending at the next `^\[` line start — current regex corrupts sections containing arrays. Preserve the single-quote-literal path quoting behavior.
*Acceptance:* tests: section with `args = ["-m", "x"]` survives connect/disconnect; existing `tests/test_ai_clients.py` green; user comments/other sections byte-preserved.

**D5. Session cache freshness** — `src/plaud_tools/session.py:554-590`
`SessionManager.require()` revalidates when the backing store changed: cheap mtime probe on the DPAPI shadow / session file (when the store exposes one), or a 5-minute TTL on the cached session as the general mechanism. Keep the hot path allocation-free (store a `(session, loaded_at)` tuple).
*Acceptance:* test: save new session via a second store instance → first manager picks it up after TTL/mtime trip; no extra keyring reads in the within-TTL hot path (count via fake keyring).

**D6. Updater/toasts hardening** — `src/plaud_tools/tray/updater.py`, `src/plaud_tools/tray/toasts.py` **[BUNDLE-GATE]**
(a) Allowlist update download hosts: `zip_url` must parse to `github.com` or `objects.githubusercontent.com`, else refuse.
(b) Invoke PowerShell by absolute path (`%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe`) in both files.
*Acceptance:* unit tests for the allowlist (good/evil URLs); grep shows no bare `"powershell"` argv entries remain in `src/`.

---

### Wave 4 — Polish & low-priority (all S, parallel except noted)

**E1. layout.py contract fix** — `src/plaud_tools/layout.py:128-141`, `src/plaud_tools/doctor.py`, callers
Bundle `mcp_exe`/`ffmpeg_exe` return `None` when the candidate path doesn't exist, matching the documented contract. Audit all callers (`doctor.py`, `tray/` wiring, `tests/test_layout.py`) and adapt — doctor should still print the *expected* path with `exists: false`, so it may need the candidate exposed separately (e.g. keep fields as candidates but rename docs, OR add `mcp_exe_expected`). Choose the smaller diff; the requirement is docs == behavior.
*Acceptance:* `tests/test_layout.py` updated deliberately; doctor output schema preserved or CHANGELOG-noted.

**E2. Mutex hardening** — `src/plaud_tools/tray/setup.py:116-130`
Use `ctypes.WinDLL("kernel32", use_last_error=True)` + `ctypes.get_last_error()` after `CreateMutexW`; handle NULL handle return (treat as lock-acquired-failed → allow startup rather than blocking the user).
*Acceptance:* logic unit-testable via monkeypatched ctypes; existing tray tests green.

**E3. Remove retry-delay truthiness hack** — `src/plaud_tools/session.py:260-309`, `tests/conftest.py:22-38`
Delete `_KEYRING_RETRY_DELAY_S` alias; tests zero retries via a clean hook (e.g. monkeypatch `_KEYRING_RETRY_DELAYS_S = ()`). Update conftest fixture accordingly.
*Acceptance:* retry-shape tests in `test_auth.py` still assert attempt counts; suite wall-clock unchanged.

**E4. Pre-release-safe version compare** — `scripts/install.ps1`
Compare installed vs latest using `[version]` casts on the numeric prefix (strip `-suffix`); a pre-release tag must not be reported as newer than its release.
*Acceptance:* cases covered in `tests/test_install_ps1.py` (it tests the script text/behavior patterns already — extend).

**E5. login --password deprecation guidance** — `src/plaud_tools/cli.py`, `docs/CLI.md`
Help text for `--password` warns it leaks via process listings/shell history; document `PLAUD_ACCESS_TOKEN` env and `session set --token` as the scripting paths. Do not remove the flag.
*Acceptance:* help-text test; docs updated. (Serialize after D3 — same file.)

**E6. doctor improvements** — `src/plaud_tools/doctor.py`
(a) Platform-aware dev fallback for `_mcp_exe_path` (no hardcoded `.exe` on POSIX).
(b) Add an `mcp_lifecycle` health field: which enumerator (psutil/fallback) is active.
*Acceptance:* `tests/test_doctor.py` extended; JSON shape additions only (no removals).

**E7. CONTEXT.md + ADR sync pass** — docs only, run last
One agent reviews everything merged in Waves 0-4 and updates `CONTEXT.md`, ADR 003/004 amendments, `docs/TROUBLESHOOTING.md` (hash-verification failure messages), and `CHANGELOG.md` consolidation.
*Acceptance:* human review.

---

## 5. New-task summary from owner decisions

- D2 → B5 (matrix) + C3 (tri-platform lockfiles)
- D3 → C5 (429 backoff + polite polling)
- D4 → C6 (annotations + confirm param + CONTEXT.md doc)

## 6. Conflict matrix (serialize within a column)

| File | Tasks touching it | Order |
|---|---|---|
| `client.py` | A2 → C5 → D1, D2 | A2 first; C5 before D1/D2; D1 ∥ D2 only with worktrees + careful merge (they touch different methods — `browse` path vs `upload` path — but both edit imports; prefer serial) |
| `release.yml` | A3 → C3 | serial |
| `ci.yml` | A3(pins), B1, B2, B4, B5 | A3 first, then B-wave serial-ish (small file; rebase is cheap) |
| `cli.py` | D3 → E5 (D1/D2 touch list/search/upload blocks — land before D3 or rebase D3 last) | D1, D2, then D3, then E5 |
| `server.py` | A6 → C2 → C6 | serial |
| `mcp.py` | A4, D1, C6 | A4 first; D1 and C6 touch different regions but serialize to be safe |
| `session.py` | D5, E3 | either order, serial |
| `tray/updater.py` | C1, D6 | serial |
| `pyproject.toml` | B1, B2, C3, C4, D4(tomlkit) | rebase-friendly; serialize merges |
| repo-wide | B1 (ruff format) | **exclusive** — nothing else in flight when B1 lands |

## 7. Verification gates & release sequencing

**Per-PR gates (orchestrator runs):**
1. `pytest -q` on the local platform; CI matrix must be green before merge.
2. `ruff check` + `ruff format --check` (post-B1), `mypy src/plaud_tools` (post-B2).
3. For **[LIVE-GATE]** tasks: stop at PR-ready; request human live validation with sacrificial Plaud data.
4. For **[BUNDLE-GATE]** tasks: request human local PyInstaller build + bundle swap validation (documented workflow exists in the owner's memory/notes).

**Release sequencing (hash rollout):**
1. Release N (first tag after Wave 0): ships A3 — `SHA256SUMS` published, ffmpeg pinned, actions pinned.
2. Release N+1: ships C1 — verification active, soft-fail when sums absent (covers users updating from pre-N).
3. Release N+2 or later: remove the soft-fail branch (file a tracking issue when C1 merges).

## 8. Out of scope (per D5 — reject if a subagent proposes)

WT/WRT refresh loop; tray UI automation; password memory-zeroing; httpx migration; A/B update rollback; HPKP/cert pinning.

## 9. Reporting format

After each wave, the orchestrator posts a summary: tasks merged (PR links), gates passed, deviations from this plan with rationale, and anything discovered that warrants a new task card (append to this file under a "Discovered" section rather than improvising).
