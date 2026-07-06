# Full Repo Audit & Remediation Plan — 2026-07-06

Senior-engineer audit of plaud-tools at v0.6.0 (commit 8afdcd0). Seven parallel audit passes
(core protocol bugs, MCP-layer bugs, CLI/tray/installer bugs, MCP+CLI surface & token
efficiency, testing process, CI/CD, UX + simplification), with every bug finding re-verified
against the code before filing. **27 issues filed: #138–#164.**

Overall assessment: the codebase is in better shape than most inherited projects — the test
suite is genuinely strong (851 tests, 14s, correct seams), actions are SHA-pinned, PyPI uses
trusted publishing, SHA256SUMS verification is fail-closed, and the fragile Plaud protocol
behavior is well pinned. The real problems cluster in four places:

1. **The release pipeline can ship a broken bundle** — tags build+publish with zero tests.
2. **A family of session/expiry bugs reintroduces the "bricked MCP" class** v0.5.0 fixed.
3. **The Windows tray/installer has ~10 latent bugs in exactly the classes that already
   shipped incidents** (PS 5.1 encoding, DETACHED_PROCESS, destroyed widgets).
4. **~900 lines (≈10% of src/) is dead or duplicated** and every user-facing doc is stale.

---

## 1. Bugs (all filed)

### High severity

| # | Issue | Summary |
|---|-------|---------|
| #138 | session.py:645-664 | Session cache mtime probe makes the expiry re-check unreachable — long-lived MCP serves an expired token; errors misclassify as `api_error` so the tray is never told to re-auth. The v0.5.0 brick, reintroduced via the cache path. |
| #139 | server.py:496-525 | `call_tool` drops `isError` — **every** tool error (including refused deletes and session-expired) is delivered to MCP clients as a successful call. |
| #140 | mcp.py:284-289 | `mutate_recording(move)` with `folder_id` omitted silently unfiles the recording instead of erroring. |
| #141 | install.ps1:286-288 | Single-entry user PATH destroyed by string concatenation (reproduced) — corrupts the WindowsApps entry on fresh profiles. |
| #142 | uninstaller.py:162-166, toasts.py:106-109 | `DETACHED_PROCESS` + no stdio from the no-console frozen tray kills PowerShell instantly (the d33c401 class) — uninstall silently no-ops; **no toast ever appears in the shipped bundle**, including session-expired. |
| #143 | transport.py:83-89, errors.py:143-160 | Network timeouts/connection failures classified non-retryable — a 30s blip aborts a 5-minute merge/transcription wait that succeeds server-side. |

### Medium severity

| # | Issue | Summary |
|---|-------|---------|
| #144 | session.py:552-567 | `clear()` doesn't delete the legacy session file — sign-out resurrects the old token. |
| #145 | session.py:122-144 | `FileSessionStore.load()` unguarded (BOM/corrupt/partial JSON crashes instead of falling through); `save()` non-atomic. |
| #146 | client.py:872, 1013-1034 | 2xx non-JSON body escapes as `JSONDecodeError` → MCP misreports a server outage as caller `validation` error. |
| #147 | client.py:825-870 | Non-idempotent POSTs blindly retried on 429/5xx — duplicate merges / "folder exists" false failures. *(needs-triage: retry-policy decision)* |
| #148 | mcp.py:220-221 | `browse_recordings(limit<=0)` → cursor never advances → agent infinite loop; schema lacks minimums. |
| #149 | mcp.py:422-423 | `upload_recording` loses the recording ID when post-upload folder move fails → duplicate uploads. |
| #150 | mcp.py:404, 134-154 | Structured-error contract violations: ffmpeg error shape; `OSError` escapes the error layer. |
| #151 | server.py:511 | Long-blocking handlers (up to ~20 min) orphan plaud-mcp after client disconnect, holding the exe lock the updater fights. *(needs-triage: cancellation strategy)* |
| #152 | background.py:266-269 | Repair-failure lambda `NameError` (deferred `exc`; reproduced) — Home window stuck on "Repairing setup…". |
| #153 | updater.py:477, uninstaller.py:160 | Runtime dispatcher .ps1 written BOM-less UTF-8 → update/uninstall broken for non-ASCII usernames under PS 5.1 (v0.3.4 family). |
| #154 | setup.py, uninstaller.py:104 | Profile handling: `UnicodeDecodeError` escapes `except OSError`; rewrite strips the BOM from the **user's own profile**. |
| #155 | cli.py:684-685 | `transcript` crashes `UnicodeEncodeError` on redirected stdout (cp1252). |
| #156 | uninstall.ps1:44-60 | No respawn-retry kill loop → partial delete with Claude Desktop running. |
| #157 | home.py:270,292; updater.py:398 | Destroyed-widget async callbacks (v0.3.3 class) — the `winfo_exists()` fix was applied to 1 of 4 sites. |
| #158 | cli.py:301-314 | `update` command broken in frozen bundle (spawns itself with `-m pip`). |
| #159 | install.ps1:172-180 | Installed>latest falls into the -Force wipe branch (silent downgrade); stuck ≤0.3.3 users routed to the broken update path with no `-Repair` mention. |

### Low severity

| # | Issue | Summary |
|---|-------|---------|
| #160 | setup.py:315, install.ps1:365 | Autostart Run value unquoted (spaced paths; hijackable). |
| #161 | login.py:74-76 | Login runs the 30s network call on the Tk main thread — whole tray freezes. |
| #162 | background.py:143-145 | Event-file read-then-truncate race can drop `session_expired` events. |
| #163 | tray/app.py:475 | First-run welcome banner never shows for actual first-run users. |
| #164 | docs | Docs drift (token lifetime, 10-vs-12 tools, missing v0.5/v0.6 commands, false claims, `plaud login` doesn't exist). |

**Cross-cutting observation:** four issues (#141 vs setup.py, #142 vs updater.py, #153/#154 vs
ps1_templates, #157 vs `_refresh_update_btn`) are cases where a past incident was fixed at one
site while sibling sites kept the bug. The remediation below therefore prefers *shared helpers*
(one launch helper, one profile-IO helper, one widget-guard helper) over site-by-site patches.

---

## 2. MCP surface — token efficiency & functionality

Baseline: the 12-tool listing costs ~1,850–2,100 tokens at session start — already lean by MCP
standards. Findings are trims plus payload fixes, not surgery.

### Payload fixes (biggest wins)
- **Compact JSON**: `_json_result` uses `json.dumps(indent=2)` (mcp.py:102, server.py:524) —
  a 20–35% whitespace tax on every response, compounding every turn. Switch to
  `separators=(",", ":")`. **Highest-leverage one-line change in the repo.**
- **`has_summary` in browse items** (query.py:133-140): `Recording.is_summary` is already
  normalized but dropped. README's own suggested first prompt ("most recent meeting that has a
  summary") currently forces per-item `get_recording` calls. +3 tokens/item, saves whole calls.
- **Transcript truncation on `get_recording`**: a 2-hour meeting is a 10–20k-token response
  with no way to take less. Add `transcript_offset`/`transcript_max_chars` (+
  `transcript_truncated` flag) — pure client-side slicing of the segment list already in hand
  (client.py:1036-1042).
- **Drop verbatim echoes**: `correct_transcript`/`edit_summary` echo `find`/`replace` back
  (mcp.py:349-359, 514-523) — the agent just sent them. Keep only the counts.
- **Slim `merge_recordings` response** to `{ok, recording_id, title}` — the full detail dict is
  all nulls on a fresh merge.
- **Lower MCP browse default `limit` 50 → 20–25** (2–3k tokens for a browse an agent skims;
  `next_after` exists).

### Definition trims (~300–450 tokens, 15–25%)
- `process_recording.wait` description (~60 tokens of return-shape prose) → one line.
- Confirm-gate prose triplicated on `delete_recording`/`mutate_folder` → one sentence each
  (the rejection error already teaches the re-invoke protocol).
- `edit_summary` states operation semantics twice → cut param-level duplication.
- Standardize the discriminator name: `mutation` / `action` / `operation` → **`action`**
  everywhere (usability, not tokens; breaking → v0.7.0).
- Optional: merge `rename_speaker` + `correct_transcript` → `edit_transcript(action=...)`,
  mirroring `edit_summary` (~120 tokens + deletes the cross-referencing prose; breaking).
- Keep `delete_recording` separate — the destructive annotation + required-confirm schema is
  load-bearing (D4). Regenerate the golden file once at the end of the trim pass.

### Functionality gaps (all grounded in existing client code)
1. **`trash: boolean` on browse** — `client.list_trash()` exists; MCP can restore but can't
   discover trashed IDs today. Real hole.
2. **Batch trash/restore** — client methods already take lists; `mutate_recording` is
   single-ID. Accept `recording_ids: array`.
3. **`dry_run: true` on `correct_transcript`/`edit_summary(correct)`** — match-count preview
   before mutating; the client already counts matches (client.py:684-698).
4. **Bounded `wait="summary"`** — can block ~20 min today; many MCP clients time out at
   60–120s. Return `{status: "still_processing"}` at a soft deadline (pairs with #151).
5. **Task-status surface** — `client.get_task_status` backs CLI `status` but has no MCP
   surface; medium priority (browse/get already expose `is_trans`/`is_summary`).
6. **Transcript search across recordings** — nothing server-side today; before building the
   O(N)-download version, check har-captures/ for a native Plaud search endpoint (the web app
   has search).

---

## 3. CLI surface

- `detail` always emits `"transcript": null` and a misleading `summary: null` (never passes
  `include_summary=True` even though `summary <id>` would find it) — cli.py:382-394.
- `search` is byte-identical to `list --query` (no ranking) and lacks `--unfiled`; either give
  it a reason to exist or fold it into `list`.
- `transcribe` exposes only `--template` while client+MCP support `language`/`diarization`/
  `llm`; and it never waits while MCP defaults to waiting — same op, opposite defaults. Add
  `--wait {none,transcript,summary}` and the missing flags.
- `trash` overloaded: no-arg lists, with-ID mutates — a dropped argument silently changes a
  mutation into a listing. Split (`trash list` subcommand or `--list`).
- `move-to-folder` duplicate registration of `move` → argparse `aliases=`.
- Naming: entry points are `plaud-tools`/`pt`; CLAUDE.md says `plaud`/`pld`; cli.py:251 says
  `plaud login`. Decide the canonical name once (adding `plaud`/`pld` as real entry points is
  one pyproject line — or fix all the references; either way, one truth).

---

## 4. CI/CD

Strong baseline (SHA-pinned actions, trusted publishing, fail-closed SHA256SUMS, 3×3 matrix,
real lint/mypy gate, per-platform constraints). The gaps are all on the release path:

1. **Release runs zero tests** — tag push matches neither CI trigger, and release build has no
   `needs:` on any test job. An un-CI'd commit can be tagged, built, and published to GitHub +
   PyPI. Gate release on the full check suite.
2. **The tray exe is never executed at release** — only the two CLI `--version` smokes. Run
   `PlaudTools.exe --diagnose-enum` + `ffmpeg_smoke.py` against the shipped `mcp\ffmpeg.exe`
   in the assembled layout (the v0.3.x hiddenimports class ships silently today).
3. **No PS 5.1 gate** — both shipped updater regressions were PS 5.1 execution failures;
   `windows-latest` has `powershell.exe`. Add parse ([Parser]::ParseFile) + behavioral tests
   for update.ps1/install.ps1 under 5.1, ideally a frozen old→new updater simulation job.
4. **Publish can ship a stale version** — the tag→pyproject sync runs only in the Windows
   build job; the publish job builds from a fresh checkout of the committed version. Fail the
   workflow when tag ≠ pyproject version.
5. **CI smoke bundle built from floating deps, release from pins** — build the CI bundle with
   `constraints/windows.txt` too.
6. Hygiene: `permissions: contents: read` on ci.yml; `dependabot.yml` (github-actions + pip);
   single-source the ffmpeg URL+hash (currently copy-pasted in two workflows); `[tray]` extra
   on the Windows test leg; `cache: pip`; `concurrency` group; drop `--force-reinstall`.

---

## 5. Testing

Strong suite (851 tests / 14s, transport-seam protocol pins, exhaustive session fallback
matrix, per-tool MCP handler tests, golden-file tool descriptions with regen + token budget).
Gaps, in value order:

1. **No boundary test for `TOKEN_REFRESH_BUFFER_SECONDS`** — the exact v0.5.0 incident has no
   pin (tests use 300-day or already-expired tokens). Add 2d-rejected / 4d- and 29d-accepted
   cases, plus a cached-session-crosses-buffer case (pins #138).
2. **`plaud-tools refresh` has zero tests** — the designated unbrick command is the untested
   one.
3. **PS 5.1 execution tests** (see CI/CD #3) — replace substring pins with executable proof;
   keep the ASCII pins as fast local guards.
4. **StubClient signature-sync test** — `test_interfaces.py` hand-rolls the client facade and
   has already drifted once; assert each stub method exists on `PlaudClient` with a compatible
   `inspect.signature`.
5. **pytest-cov + CI coverage artifact** (non-gating) — makes gaps like `refresh` visible.
6. **Live-test usability**: clean skip when `PLAUD_SESSION_PATH` unset; teardown-trash uploaded
   live recordings; add live reads of the drift-prone surfaces (folder list, detail+summary).
7. **Weekly live-read canary workflow** (secrets-gated) — turns "user reports Plaud drift"
   into "CI reports Plaud drift"; Plaud API drift is the project's #1 stated risk.
8. Housekeeping: recreate the lost conftest trip-wire meta-test; `filterwarnings = ["error"]`;
   register a `live` marker; fix stale golden-test docstring numbers; consolidate session tests.

---

## 6. UX

1. **Expiry runway is ~zero**: MCP refuses tokens <3 days out (`session.py:658`) and the tray
   warns at ≤3 days — warning and breakage start the same day, and the HomeWindow says "Token
   valid for N days" while MCP is already refusing. **Shrink the require() buffer to 12–24h,
   widen the tray warning to 5–7 days** so warning strictly precedes breakage. (10% of every
   30-day token is currently burned by the buffer.)
2. **Error messages should name the remedy**: CLI session-expired should say "run
   `plaud-tools refresh` or open the PlaudTools tray"; MCP session errors should tell the AI
   client to relay "open the PlaudTools tray and sign in" (lets the assistant self-serve the
   fix); login window shows raw `HTTP 401` with no Google-SSO/app-password hint (that guidance
   exists only in docs, not where the stuck user is); wizard failure crams the exception into a
   12-char button.
3. **Docs**: #164 (lifetime, tool count, missing commands, wrong command names, ≤0.3.3
   caveat surfaced in TROUBLESHOOTING, "Manage AI clients…" vs "Configure AI Agents…").
4. What's already good and shouldn't be touched: install progress + fail-closed checksums,
   wizard status badges, honest updater (heartbeat, failure sentinel, version-match), uninstall
   checklist, background auto-heal.

---

## 7. Simplification (≈900 deletable lines, no user-visible change)

1. **`mcp_lifecycle.py`: ~470 of 519 lines have zero production callers** (the enumerate/
   shutdown/snippet functions are used only by tests; install.ps1 reimplements both snippets
   inline). Delete module + doctor's `active_enumerator_name` field + tests — or actually wire
   the snippets into the PS1 generators. Deleting is the honest option.
2. **`tray_app.py` (257 lines)**: a `__setattr__`-propagating module shim existing solely so
   tests can monkeypatch; repoint tests at real submodules, change the `plaud-tray` entry point
   to `plaud_tools.tray.app:main`, delete.
3. **Upload/transcode block duplicated verbatim** (cli.py:592-614 ≡ mcp.py:393-420) → one
   `upload_with_transcode()` helper. Fixing #149 once instead of twice.
4. Dead code: `_PRE_CLIENT_HANDLERS` dict (cli.py:699-705, never read); bytes-based
   `transcode_to_mp3` + bytes `upload_recording` overload (test-only); `summarize_recording_for_cli`
   re-export; winrt toast branch (~50 lines, never available in the shipped bundle — decision
   pairs with #142); `_show_session_expired_toast(on_click)` unused param; `_infer_channel`
   pip-vs-dev distinction driving nothing.
5. Duplication: folder dict built 4×, rename/correct result dicts duplicated cli↔mcp,
   `_summarize_detail` vs `_handle_show` near-twins → finish the query.py consolidation.
6. install.ps1 step 4 duplicates tray/setup.py in a second language; the tray auto-heals all
   three on every launch — delete the installer step and let first launch do it.
7. session.py mostly earns its 749 lines (each fallback carries a documented incident); trim
   only `SessionStoreProtocol`, `_decode_header_safe`, and `diagnose`'s inline expiry re-derivation.
8. `query.py` dual "unfiled" conventions (`unfiled=True` ≡ `folder_id==""`) → keep the kwarg.

---

## 8. Implementation plan

Sequenced so that (a) the release pipe is trustworthy before anything ships through it,
(b) correctness precedes surface changes, (c) breaking MCP changes batch into one minor bump.
Sizes: S <½ day, M ~1 day, L 2–3 days.

### Wave 0 — Make the pipeline trustworthy (no release) — ~2 days
| Item | Size | Refs |
|---|---|---|
| Gate release on tests + assembled-bundle proof (tray exe, shipped ffmpeg) | M | CI/CD 1–2 |
| Tag ≠ pyproject version → fail; fix publish-job sync | S | CI/CD 4 |
| CI bundle built with constraints/windows.txt | S | CI/CD 5 |
| PS 5.1 parse gate for update.ps1/install.ps1 (full simulation job can follow) | M | CI/CD 3 |
| Hygiene batch: permissions, dependabot, pip cache, concurrency, [tray] leg, ffmpeg single-source | S | CI/CD 6 |

### Wave 1 — Session & protocol correctness → **v0.6.1** — ~3 days
| Item | Size | Issues |
|---|---|---|
| Expiry check in cache hot path + 401→session_expired classification | M | #138 |
| Token-buffer boundary tests + cached-crossing test + `refresh` tests | S | #138, Testing 1–2 |
| `clear()` legacy file + hardened/atomic FileSessionStore | S | #144, #145 |
| Transport-error retryability + non-JSON 2xx wrapping | M | #143, #146 |
| Retry-policy decision for mutations (triage #147 first — small design note in the issue) | S–M | #147 |

### Wave 2 — MCP protocol correctness → ships with v0.6.1 — ~2 days
| Item | Size | Issues |
|---|---|---|
| Return `CallToolResult(isError=...)` from call_tool (all 3 paths) + test | S | #139 |
| move-without-folder validation error | S | #140 |
| browse limit/after minimums (schema + guard) + golden regen | S | #148 |
| upload partial-success payload (via the shared upload helper — do Simplification 3 here) | M | #149, Simp 3 |
| Error-shape cleanup (ffmpeg path, OSError) | S | #150 |

### Wave 3 — Windows tray/installer hardening → **v0.6.2** — ~4 days
| Item | Size | Issues |
|---|---|---|
| Shared PowerShell-launch helper; fix uninstaller + toasts | M | #142 |
| Dispatcher BOM (`utf-8-sig`) + non-ASCII-path test | S | #153 |
| Shared profile-IO helper (encoding-tolerant read, BOM-preserving write) | M | #154 |
| install.ps1: PATH array fix, version branches, quoted autostart | M | #141, #159, #160 |
| `_configure_if_alive` widget guard at all async callback sites | S | #157 |
| Repair-callback NameError; login on worker thread; event-file rename-then-read | S | #152, #161, #162 |
| uninstall.ps1 respawn-retry kill loop | S | #156 |
| CLI: stdout utf-8 reconfigure; frozen-channel `update` guard | S | #155, #158 |
| Validate per feedback memory: local PyInstaller build + end-to-end update/uninstall/toast exercise before merging | — | workflow-local-pyinstaller-build |

### Wave 4 — Surface & UX → **v0.7.0** (one deliberate breaking batch) — ~4 days
| Item | Size | Refs |
|---|---|---|
| Compact JSON everywhere; drop echoes; slim merge response | S | §2 |
| `has_summary` in browse; `trash:` param; browse default limit 20–25 | S | §2 |
| Transcript truncation params on get_recording | M | §2 |
| Batch IDs on mutate_recording; `dry_run` on correct/edit tools | M | §2 |
| Bounded `wait="summary"` + still_processing status (+ shutdown Event → resolves #151) | M | #151 |
| Description trims + discriminator → `action` + optional edit_transcript merge; golden regen; ADR for the breaking batch | M | §2 |
| Expiry runway: buffer 12–24h, tray warning 5–7 days, HomeWindow label truth | S | §6.1 |
| Remediation-bearing error strings (CLI, MCP, login window, wizard) | S | §6.2 |
| Docs overhaul (#164) + AI-CLIENTS/CLI.md regeneration; canonical command name decision | M | #164, §3 |
| CLI: transcribe flags + --wait; trash split; detail summary fix; search fold-in; move alias | M | §3 |

### Wave 5 — Deletion & test depth (rolling, no release pressure) — ~3 days
| Item | Size | Refs |
|---|---|---|
| Delete mcp_lifecycle dead code + tray_app shim (~730 lines) | M | §7.1–2 |
| Dead-code batch + cli/mcp response-shaping consolidation | M | §7.4–5, §7.7–8 |
| install.ps1 step-4 removal (after Wave 3 proves auto-heal) | S | §7.6 |
| StubClient signature-sync; pytest-cov; conftest trip-wire meta-test; filterwarnings; live-test cleanup | M | §5.4–6, 8 |
| Weekly live-read canary workflow | S | §5.7 |

Total: ~18 engineer-days across five waves; Waves 0–2 (the "stop shipping risk" half) are ~7.

### Suggested first PR
Wave 0 alone (CI-only, no runtime code) — everything else then merges through a pipeline that
would have caught the classes of bug this audit found.
