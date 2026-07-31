# Competitive audit: official Plaud MCP & CLI vs plaud-tools

Date: 2026-07-31
Status: **Phases 1–3 implemented** (items 1–10). Phase 4 not started.

Implementation notes, recorded after the fact:

- **Item 9 (audio download) is resolved.** The endpoint was unknown when this
  plan was written; three fresh HAR captures (`plaud-audio-play-*.har`) settled
  it: `GET /file/temp-url/{id}` → `{"status": 0, "temp_url": <presigned mp3>,
  "temp_url_opus": null}`, pointing at
  `plaud-bucket.s3.amazonaws.com/audiofiles/{id}.mp3`. Same endpoint for
  device-recorded and uploaded files. **TTL is 3600s**, not the 24h the official
  API hands out — so nothing caches it. `temp_url_opus` was null in every
  capture and is treated as a fallback only.
- **Item 8 took the breaking change in one go** rather than deprecating over a
  release: `transcript_offset`/`transcript_max_chars` are gone, replaced by
  `transcript_after`/`transcript_limit`. Cursor naming follows our own
  `after`/`next_after` idiom from `browse_recordings` rather than copying
  Plaud's opaque base64 cursor — an integer utterance index can't tear a record
  either, and it matches the surface we already have.
- **`outline` was dropped from scope** (see §2.2): it isn't an utterance list,
  so it needs a second parse+format path. Marked `ponytail:` in `client.py`.
- **Item 10 shipped as one skill, not seven.** `skills/plaud-tools/SKILL.md`,
  manually installed. Auto-installing it via the tray alongside the MCP wiring
  is a real follow-up, deliberately not done here — see Phase 4.
- Two incidental cleanups the work forced: `PlaudClient.fetch_transcript()`
  deleted (no callers left), and transcript formatting moved to
  `core/query.py::format_transcript()` since both surfaces now need it.

## What was audited

| Source | What it gave us |
|---|---|
| `docs.plaud.ai/plaud-mcp-cli/{mcp,cli,changelog}.md` | Documented surface, install, config, exit codes |
| `docs.plaud.ai/llms.txt` | Full doc index — reveals two separate developer surfaces |
| npm tarball `@plaud-ai/mcp@0.3.7` (2026-07-30) | **Real** tool descriptions, schemas, annotations, endpoints, skills, plugin manifest |
| `mcp.plaud.ai/.well-known/oauth-*` | Hosted OAuth server metadata |
| `gh repo list Plaud-AI` (16 repos) | Org inventory |

The MCP and CLI are **closed source** — no `Plaud-AI/plaud-mcp` or `plaud-cli` repo exists (verified 404). Everything below about their internals comes from the published npm bundle, which is unminified enough to read directly.

Official GA was **2026-05-12** (v1.0.0); npm `@plaud-ai/mcp` is at `0.3.7`, published 2026-07-30 — actively developed.

---

## The single most important finding

The official MCP/CLI are backed by exactly **three** user-data endpoints:

```
GET /open/third-party/users/current
GET /open/third-party/files/?page=&page_size=
GET /open/third-party/files/{file_id}
```

That is the entire official surface. **It is read-only. There are no write endpoints at all.**

Consequences:

1. Their product cannot rename, move, trash, delete, upload, merge, re-transcribe, edit transcripts, edit summaries, or touch folders — not because they didn't get to it, but because **the API they built for themselves has no verbs for it**. Our write surface is not a feature gap they'll close next sprint; it's a different architecture.
2. Their `list_files` has no server-side search. Filtering is client-side over at most `MAX_FILTER_PAGES=5 × FILTER_PAGE_SIZE=100` = **500 most recent recordings**. Anything older is unreachable by keyword. The CLI documents the same 500 cap.
3. Folders don't exist in their data model. `list_files`/`get_file` return only `id, name, created_at, start_at, duration, serial_number`.

Second finding, strategically relevant: `mcp.plaud.ai` advertises an RFC 7591 `registration_endpoint` (dynamic client registration) with S256 PKCE. So there **is** an official, ToS-blessed OAuth path to a user's own recordings — read-only, and read-only forever until they ship write endpoints. (I did not POST to `/register` to verify it accepts registrations — that's an outward-facing write to their service; flagging it as advertised-but-unverified.)

Note also: the two developer surfaces are easy to conflate and are **not** interchangeable.
- `/open/partner/*` — Plaud Embedded. Partner token → per-user tokens keyed to *your* app's users; upload + transcribe audio *you* capture, billed per minute, 112 languages. Does **not** grant access to an existing consumer's Plaud library.
- `/open/third-party/*` — what MCP/CLI use. A real Plaud consumer OAuth-grants your app read access to their library.

Only the second is relevant to us.

---

## Angle 1 — What we have that they lack

Their MCP: 7 tools, of which 3 are auth/account (`login`, `logout`, `get_current_user`) and 4 are data reads (`list_files`, `get_file`, `get_note`, `get_transcript`).
Ours: 11 tools, all data, 7 of them writes.

| Capability | plaud-tools | Official | Notes |
|---|---|---|---|
| Browse / paginate recordings | ✅ | ✅ | — |
| Get recording detail | ✅ | ✅ | — |
| Read transcript | ✅ | ✅ | — |
| Read AI summary | ✅ | ✅ | — |
| **Rename recording** | ✅ | ❌ | no write endpoint |
| **Trash / restore** | ✅ (+ batch) | ❌ | |
| **Permanent delete** | ✅ (confirm-gated) | ❌ | |
| **Folders: list** | ✅ | ❌ | not in their data model |
| **Folders: create / edit / delete** | ✅ | ❌ | |
| **Move recording to folder** | ✅ (+ batch) | ❌ | |
| **Rename speaker** (your hunch — confirmed) | ✅ | ❌ | |
| **Find-and-replace transcript text** | ✅ (+ dry-run) | ❌ | |
| **Edit AI summary** (correct or replace) | ✅ (+ dry-run) | ❌ | |
| **Upload local audio** | ✅ (+ ffmpeg transcode) | ❌ | |
| **Trigger transcription / summarization** | ✅ (template, language, diarization, LLM) | ❌ | |
| **Merge recordings** (your hunch — confirmed) | ✅ | ❌ | |
| **Search beyond 500 recent** | ✅ | ❌ | hard 5×100 cap in their code |
| **Trash browsing** | ✅ (`trash=true`) | ❌ | |
| **Batch operations** | ✅ (`recording_ids[]`) | ❌ | |
| Local-only, zero telemetry | ✅ | ❌ | they ship PostHog (`us.i.posthog.com`) on every tool call |
| Windows tray + one-click installer + auto-update | ✅ | ❌ | npm-only |
| stdio-only (data never transits a vendor server) | ✅ | ⚠️ | their HTTP mode routes your recordings through their US server |

Both your hunches were right: **no speaker naming, no merge** — and it's much broader than that.

**Our per-item response shape is also better for LLM consumption.** We return `duration_minutes` (rounded int), a pre-truncated `date`, and `has_transcript`/`has_summary` availability booleans. They return raw `duration` in milliseconds and then spend skill instructions telling the model to reformat it ("Format durations human-readable: `23s`, `5m23s`… Raw milliseconds are for logs only"). We solved at the tool boundary what they push into the prompt.

---

## Angle 2 — What they have that we lack

Only four things, and two of them are worth building.

### 2.1 Audio download — the one real functional gap ⭐

Their `get_file` returns `presigned_url` (24h TTL); the CLI exposes `plaud audio <id>`. **We have no way to get the original audio out.** We only use presigned URLs for *upload*.

Feasibility: not yet known. `/file/detail` returns `download_path_mapping` and `data_file_list`, but in all 20 of our HAR captures `download_path_mapping` contains only summary-poster PNGs and `data_file_list` is `null` — we never captured a "play recording" or "export audio" flow. Needs one fresh HAR capture before it can be scoped. This is real research, not a known endpoint.

Value: high. "Download the audio for yesterday's call" is an obvious ask, and it unblocks local re-processing with other tools.

### 2.2 `transaction_polish` — AI-cleaned transcript ⭐

Their `get_transcript` takes `block ∈ {transaction, outline, transaction_polish}`. We hardcode `data_type == "transaction"` (`client.py:1062`) and never expose the other two.

`transaction_polish` is Plaud's AI-cleaned transcript (filler words removed, punctuation fixed) with speaker + timestamps preserved. That's strictly nicer to read than the raw block for most purposes, and users are paying for it already. `outline` is a third view.

Feasibility: high — same `content_list` walk we already do, just a different `data_type`. Small diff.

### 2.3 Exit-code taxonomy (CLI)

Theirs: `0` ok, `1` bad args/unknown, `2` auth failed, `3` network, `4` timeout, with `✖ [AUTH_FAILED] …` prefixes on stderr.

Ours: **everything collapses to `1`** (`cli.py:846-865`). Scripts can't distinguish "log in again" from "network flaked" without string-matching stderr.

We already compute the right answer — `exc.classify()` returns an error code. We just throw it away at the exit boundary. Cheapest real win in this document.

### 2.4 Convenience niceties (low value)

- `plaud recent --days 7`, `plaud today` — sugar over our more general `--since/--until`. Genuinely nicer to type.
- `-o <file>` on `transcript`/`summary` — we rely on shell redirection, which is fine on POSIX and mildly annoying on PowerShell.
- `plaud me` — account info. We have `get_user_info()` in the client already (`/user/me`) but only surface it via `status`/`ping`.

### Explicitly NOT worth copying

- **`login`/`logout` as MCP tools.** They need them because OAuth is per-client. Our auth lives in the tray, deliberately. An MCP tool that pops a browser is worse for us, and we already settled "no MCP refresh tool by design" in v0.5.0. Skip.
- **OAuth / official API migration.** Tempting as ToS de-risking — our biggest structural risk is that we drive the private web API. But the official API can serve **4 of our 11 tools** and caps search at 500 recordings. Migrating reads would mean maintaining two auth systems and two clients to *lose* capability on the read path. No.
- **Their npm/`npx` distribution.** Our tray bundle is a better answer for the Windows-first audience.

---

## Angle 3 — Shared functionality: is their implementation better?

Four things they do better. One is worth stealing outright.

### 3.1 Cursor-paginated transcripts ⭐ (they're better)

Theirs: `get_transcript(file_id, block, cursor, limit)` — 50 utterances/page, opaque base64url cursor `{o: offset}`, returns `next_cursor`.

Ours: `get_recording(include=["transcript"], transcript_offset, transcript_max_chars)` — **character** offsets.

Theirs is better on a real failure mode: character slicing cuts mid-word and mid-utterance, so a paged read hands the model a fragment like `…and then Sar` and the model has to guess. Utterance-boundary paging never produces a torn record, and `next_cursor` is self-describing — the model doesn't have to do offset arithmetic to continue (a classic place for LLMs to fumble).

Recommendation: keep `include=[...]` (our composability is better than their one-tool-per-field split — see 3.5), but move transcript paging to utterance boundaries with a `next_cursor`. Character params can stay accepted-but-deprecated for one release.

### 3.2 `annotations.title` (they're better, trivially)

They set a human-readable `title` on every tool (`"List recordings"`, `"Get recording transcript"`). We set `readOnlyHint`/`destructiveHint`/`idempotentHint`/`openWorldHint` — **more rigorously than they do**, with per-tool reasoning comments — but never `title`. Clients use `title` for display. One line per tool.

### 3.3 Inline remediation hints on degraded results ⭐ (they're better)

When `presigned_url` is null, they append prose to the result explaining *why* and *what to do*:

> `Note: presigned_url … is null for this otherwise-synced recording. This is usually a transient backend signing issue — retry get_file in a few minutes to obtain the URL.`

And when a transcript block is missing they name what *is* available: `Block "outline" not available. Available blocks: transaction, transaction_polish.`

This is good LLM-facing design: it converts a dead end into a next action. We do this well for *errors* (`_SESSION_EXPIRED_HINT`, structured `{error, error_code, retryable}` — better than their string-matching, see 3.6) but not for **successful-but-degraded** results. Our closest equivalent is the bare `"(summary exists on Plaud but could not be fetched)"` at `mcp.py:415`, which tells the model nothing about what to do next.

### 3.4 Bundled skills + Claude Code plugin (they're better on distribution)

They ship 7 skills (`plaud-shared`, `-browse`, `-find`, `-read`, `-digest`, `-followup`, `-export`) inside the npm package, auto-installed, plus a `plugin.json` + `.mcp.json` so `/plugin install plaud` works in Claude Code.

The skills are genuinely well-built. `plaud-shared` is a read-this-first covering auth, error semantics, output conventions, and a data-model quick reference. `plaud-find` carries a relative-date resolution table ("this week" → Monday→today) and an explicit *"Resolve relative dates against the current date, not the model's training cutoff."* `plaud-digest` sets a hard budget (50 `get_note` calls max) and lists anti-patterns ("do not load transcripts just to pad the digest").

We ship zero skills. A `plaud-shared` equivalent is where we'd document the one thing agents most often get wrong about our surface: reach for `browse_recordings` filters instead of pulling everything, and use `dry_run` before `correct`.

Worth noting we'd get more out of skills than they do — they have 4 read tools and an LLM barely needs guidance; we have 11 tools with `action=` discriminators and confirm gates, which is exactly where a skill earns its keep.

### 3.5 Where we're better on shared ground

- **Tool consolidation.** They split `get_file`/`get_note`/`get_transcript` into three tools that all call the same `getFile(file_id)` under the hood — three round trips and three tool definitions in the context window for one API call. Our `get_recording(include=[...])` is one tool, one call, caller-selected payload. Ours is more token-efficient both in schema size and in calls.
- **Structured errors.** We return `{error, error_code, retryable, http_status}`. They return `Failed to get note: <raw err>` and their own skill instructs the model to **string-match** it (`"Pattern in error message: 401 / Not authenticated"`). Ours is machine-readable; theirs is a prompt-engineering workaround for a missing contract.
- **Compact JSON.** We use `json.dumps(separators=(",",":"))`; they use `JSON.stringify(x, null, 2)` on every response. Their pretty-printing is a 20–35% whitespace tax on every payload, which is exactly the tax we removed deliberately.
- **Destructive-op safety.** We require `confirm: true` server-side on `delete_recording` and `mutate_folder(delete)`. They have nothing destructive, so no credit either way — but our pattern is the right one for when they do.
- **Annotation rigor.** Noted above: we reason about `idempotentHint` per tool; they set the same three hints on all seven.

### 3.6 Naming

Their names are flatter and more conventional (`list_files`, `get_file`); ours are domain-shaped with `action=` discriminators (`mutate_recording`, `edit_transcript`). Theirs reads slightly more natural in isolation; ours scales — 7 tools with `action=` beats 20 flat tools for context cost, and we already settled this in ADR 006. **No change recommended.** One quibble: `mutate_recording` is jargon where `update_recording` would be plainer, but renaming it is a breaking change for no functional gain. Not worth it.

---

## GitHub org audit — 16 repos

Bluntly: **almost nothing here is useful to us.**

- **9 are forks of third-party OSS** they run internally: `langfuse`, `plaud-opik`, `plaud-memU-{server,ui}`, `goreplay`, `xiaozhi-esp32`, `live-agent`, `live-agent-memory`, `yt-DeepResearch-Backend`, `client-sdk-esp32`. Interesting as a read on their stack (LLM observability, agent memory, ESP32/LiveKit realtime audio); zero reuse for us.
- **6 are Plaud Embedded SDKs**: `plaud-sdk-public` (Swift, 22★), `embedded-react-native`, `embedded-flutter`, `embedded-capacitor`, `vite-react-template`, `plaud-embedded-skills`. These are for building *your own product* on Plaud hardware — wrong axis from ours entirely.
- **The MCP and CLI are not open source.** Confirmed 404 on every plausible repo name.
- None carry a license (`licenseInfo: null` across the board), so nothing is safely vendorable even if it were relevant.

The one mildly useful artifact is `plaud-embedded-skills` as a second sample of their skill-authoring style — but the 7 skills inside the MCP npm tarball are a better and more relevant sample, and I've already extracted them.

**Conclusion: no dependency, no vendoring, no SDK adoption.** The genuinely valuable resource in their ecosystem was the npm tarball, not the GitHub org.

---

## Roadmap

Sequenced by value-per-effort. Phases 1–2 are the ones I'd actually ship.

### Phase 1 — Claim the differentiation (docs only, no code risk)

1. **README comparison table.** Add a "How this compares to Plaud's official MCP & CLI" section. Lead with the structural fact — *their API is read-only; 7 of our 11 tools have no official equivalent* — then the table from Angle 1. Name the 500-recording search cap and the folder gap explicitly; those are concrete and verifiable.
2. **Position, don't compete.** State plainly that the official MCP is a fine read-only option and can run *alongside* ours. Credibility, and it's true. Our pitch is writes + local-only + no telemetry + Windows tray.
3. **Keep the "Unofficial" disclaimer prominent and update it.** Now that an official product exists, the honest framing is "we use the private web API to do things the official read-only API cannot" — which is both our value prop and our risk, in one sentence.

Effort: hours. Do this first regardless of everything else.

### Phase 2 — Cheap parity + quality wins

4. **CLI exit-code taxonomy** (§2.3). Map `exc.classify()` → `2` auth / `3` network / `4` timeout / `1` other. We already compute the code. ~10 lines in `main()` + a test.
5. **`annotations.title` on all 11 tools** (§3.2). One line each.
6. **`transaction_polish` + `outline` transcript blocks** (§2.2). Add `block` param to `get_recording`; same `content_list` walk, different `data_type`. Ship `transaction_polish` at minimum — it's the nicer read and users already pay for it.
7. **Inline remediation hints on degraded results** (§3.3). Start with the two we already know: the unfetchable-summary path at `mcp.py:415`, and a "block not available — available blocks: …" message from item 6.

Effort: one focused session for all four. Low risk, all additive.

### Phase 3 — The real gaps

8. **Utterance-boundary transcript pagination** (§3.1). Page on segments with `next_cursor`; keep character params accepted-but-deprecated one release. Needs a decision on whether it's breaking — recommend additive-then-deprecate, since `transcript_offset` semantics can't be preserved exactly.
9. **Audio download** (§2.1). Gated on one fresh HAR capture of "play recording"/"export audio" in the web app to find the endpoint. Then: `get_recording(include=["audio_url"])` + `plaud-tools audio <id>` + optional `--output` to save bytes locally. **Do the capture first and re-scope** — if the URL turns out to be short-TTL or per-chunk, the design changes.
10. **A `plaud-shared` skill** (§3.4), possibly plus `plaud-find`-style relative-date guidance. Ours would earn more than theirs does because our surface is bigger and has confirm gates and `dry_run` semantics an agent should be told about. Adopt their structure; write our own content.

### Phase 4 — Optional, low priority (not started)

11. `recent` / `today` CLI sugar; `-o/--output` on `transcript`/`summary`; a `me` command over the `get_user_info()` we already have.
12. Claude Code plugin manifest (`plugin.json` + `.mcp.json`) so `/plugin install` works. Only if we want a non-tray distribution path; our installer already covers the target audience better.
13. **Auto-install the agent skill** from the tray's "Configure AI Agents…" flow, which already knows where each client lives. Currently a manual copy into `~/.claude/skills/` — a skill nobody installs does nothing, so this is the highest-value item left in Phase 4. Needs a decision on Codex/Cursor, which don't share Claude's skill directory convention.

### Explicitly not doing

- OAuth / official-API migration — costs two auth systems to lose read capability (§2.4).
- `login`/`logout` MCP tools — contradicts the tray-owns-auth design (§2.4).
- Tool renames for style parity — settled in ADR 006 (§3.6).
- Any dependency on Plaud's GitHub repos — nothing relevant, nothing licensed.

---

## Open questions for Kadin

1. **Audio download** — worth spending a HAR capture session on? It's the only genuine functional gap, but the endpoint is unknown and could be awkward.
2. **Transcript pagination** — additive-then-deprecate, or take the breaking change in one go? Last breaking batch was v0.7.0 via ADR 006.
3. **Skills** — in scope for this repo at all, or a separate concern? They'd ship in the PyPI package and the tray bundle, which raises "where do skills live for Codex/Cursor users" questions their npm-only distribution doesn't have to answer.
