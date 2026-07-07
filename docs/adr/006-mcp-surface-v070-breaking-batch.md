# ADR 006 — MCP Surface v0.7.0: Deliberate Breaking Batch

**Status:** accepted (2026-07-06, Wave 4)

## Problem

The 2026-07-06 audit (§2, "MCP surface — token efficiency & functionality")
found the 12-tool listing lean by MCP standards but carrying avoidable cost and
several real functional gaps:

- **Whitespace tax on every response.** `_json_result` (mcp.py) and the
  `call_tool` error paths (server.py) serialized with `json.dumps(indent=2)` —
  a 20-35% token surcharge paid on every tool result, compounding over a long
  agent session.
- **Redundant payload fields.** `correct_transcript`/`edit_summary` echoed the
  `find`/`replace` the agent had just sent; `merge_recordings` returned a full
  detail dict that is all-nulls on a fresh merge; browse omitted `has_summary`
  (forcing per-item `get_recording` calls the README's own first-prompt needs).
- **No way to take less of a big transcript.** A 2-hour meeting was a
  10-20k-token `get_recording` response with no slice control.
- **Inconsistent discriminator names.** The action-selector parameter was
  `mutation` on `mutate_recording`, `operation` on `edit_summary`, and `action`
  on `mutate_folder` — three names for one concept.
- **Two near-identical transcript tools.** `rename_speaker` and
  `correct_transcript` were separate tools with cross-referencing prose, while
  `edit_summary` already unified correct/replace under one tool.
- **Functional gaps.** No way to browse trash (so the MCP could restore but not
  discover trashed IDs), no batch mutate, no dry-run preview before a
  find-and-replace, and an unbounded `wait="summary"` that could block a handler
  ~20 minutes — long past most clients' 60-120s timeout, orphaning the process
  and the exe lock the updater fights (#151).

Because several fixes are breaking, the audit plan batched them into one minor
bump (v0.7.0) rather than dribbling breakage across patches.

## Decisions

### Discriminator standardized to `action` (breaking)

Every action-selector parameter is now `action`:

| Tool | Before | After |
|---|---|---|
| `mutate_recording` | `mutation` | `action` |
| `edit_summary` | `operation` | `action` |
| `mutate_folder` | `action` (already) | `action` |

`delete_recording` stays a **separate tool** (not an `action` on
`mutate_recording`): its `destructiveHint=True` annotation and required
`confirm: true` schema are load-bearing and must not be diluted into a
reversible-mutation tool (Decision D4, ADR carried from Wave 2).

### `rename_speaker` + `correct_transcript` merged into `edit_transcript` (breaking)

One tool mirroring `edit_summary`:

- `edit_transcript(action="rename_speaker", original_label=…, new_name=…)`
- `edit_transcript(action="correct", find=…, replace=…, dry_run=…)`

The two old tools and their cross-referencing prose are deleted. Net: one fewer
tool in the listing, and the "use the *other* tool for speaker labels" prose
disappears.

### Response-shape changes (breaking)

- **Compact JSON everywhere.** `separators=(",", ":")`, no `indent`.
- **`merge_recordings`** returns `{ok, recording_id, title}` only.
- **`correct`/`rename_speaker`/`edit_summary`** no longer echo `find`/`replace`
  or `original_label`/`new_name` — counts only.
- **`mutate_recording`** trash/restore success now reports `action` (was
  `mutation`).
- **`browse_recordings`** items gain `has_summary`; default `limit` 50 → 20.
- **`get_recording`** transcript responses gain a `transcript_truncated` flag
  whenever slicing params are used.
- **`process_recording`** may now return `{status: "still_processing"}` at a
  soft deadline instead of always blocking to completion.

### New functionality (additive)

- `browse_recordings(trash=true)` — lists trashed recordings (is_trash=1).
- `mutate_recording(recording_ids=[…])` — batch trash/restore/move (rename stays
  single-ID; the client methods already accept lists).
- `get_recording(transcript_offset, transcript_max_chars)` — pure client-side
  slice of the transcript already in hand; sets `transcript_truncated`.
- `edit_transcript(action="correct", dry_run=true)` and
  `edit_summary(action="correct", dry_run=true)` — return a match count without
  mutating (the client already counts matches internally).

### Bounded `wait="summary"` (#151)

`process_recording` wraps each `client.wait_for_*` call at a soft
`_WAIT_TIMEOUT_S` (90 s) deadline and returns `{status: "still_processing"}`
rather than blocking ~20 minutes. This is the minimum viable fix: it stops a
disconnected client from orphaning a 20-minute handler. **Follow-up:** true
cancellation on client disconnect (a shutdown `Event` threaded through the
handler) is deeper and deferred; the soft-deadline return is the load-bearing
mitigation.

> **Update (2026-07-07, #151/#185):** the soft deadline now also bounds
> `merge_recordings` (300 s poll loop) and `upload_recording` (multipart S3
> loop), covering all three long-blocking handlers. Full cancel-on-disconnect
> was deemed **YAGNI** and closed, not deferred: the ~90 s cap is already below
> the exe-lock contention window the updater fights, so wiring a shutdown
> `Event` through `server.py`'s `asyncio.to_thread` buys nothing measurable.
> The soft-deadline return is the accepted resolution for #151; the `Event`
> path is the documented upgrade if orphan telemetry ever shows 90 s matters.

### Session-expired remediation strings (§6.2)

MCP session-expired errors now append "Tell the user to open the PlaudTools
tray and sign in, then retry." so the assistant can self-serve the fix. A 401
reaching the generic `PlaudApiError` branch (reclassified to `session_expired`
in Wave 1) now also fires `_emit_session_expired("http_401")` — previously only
the dedicated `PlaudSessionExpiredError` branch fired the tray re-auth event, so
a mid-session 401 produced no toast.

## Migration

For MCP client integrations pinned to the old surface:

- `mutate_recording(mutation=…)` → `mutate_recording(action=…)`.
- `edit_summary(operation=…)` → `edit_summary(action=…)`.
- `rename_speaker(recording_id, original_label, new_name)` →
  `edit_transcript(recording_id, action="rename_speaker", original_label, new_name)`.
- `correct_transcript(recording_id, find, replace)` →
  `edit_transcript(recording_id, action="correct", find, replace)`.
- Parsers that read `find`/`replace`/`original_label` back out of a
  correct/rename response must stop — those echoes are gone.
- Parsers reading the full detail dict from `merge_recordings` must switch to
  `{ok, recording_id, title}`.
- Handle a possible `{status: "still_processing"}` from `process_recording`
  (poll `get_recording` or retry).

The golden tool-description fixture (`tests/data/tool_descriptions.golden.json`)
and the token-budget assertion in `tests/test_mcp_golden.py` are regenerated
alongside this change.
