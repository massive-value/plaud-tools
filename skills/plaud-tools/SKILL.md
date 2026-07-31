---
name: plaud-tools
description: Read this before using the PlaudTools MCP (browse_recordings, get_recording, mutate_recording, edit_transcript, edit_summary, upload_recording, process_recording, merge_recordings, list_folders, mutate_folder, delete_recording). Covers transcript pagination, dry-run edits, confirm gates, error handling, and date filters. Use when the user mentions Plaud, their recordings, meetings, or transcripts for the first time in a session.
---

# plaud-tools

Eleven tools over a Plaud account. Four read, seven write. Auth lives in the
PlaudTools tray app, not here — there is no login tool, by design.

## The three things that go wrong most

**1. Transcripts are paginated.** `get_recording(include=["transcript"])` returns
`transcript_limit` utterances (default 200), not the whole thing. Check
`transcript_truncated`; if it's `true`, call again with
`transcript_after` set to the returned `transcript_next_after` and keep going
until there's no cursor. **Summarizing a `transcript_truncated: true` response as
if it were the full meeting is the single worst failure mode here** — you will
confidently summarize the first third of a call. `transcript_utterance_count`
tells you the total up front.

**2. Preview text edits with `dry_run`.** `edit_transcript(action="correct")` and
`edit_summary(action="correct")` are literal, case-sensitive find-and-replace
across the whole recording. Run with `dry_run=true` first and check `matches`.
A find string like `"the"` matches hundreds of times; that is rarely intended.

**3. Don't pull the library to search it.** `browse_recordings` filters
server-side-ish (`query`, `since`, `until`, `folder`). Use the filters. Do not
page through everything and filter yourself.

## Tools

| Tool | Notes |
|---|---|
| `browse_recordings` | Filters: `query` (title substring), `since`/`until` (ISO 8601), `folder`, `trash`. Paginate with `after` ← `next_after`. |
| `get_recording` | `include=["transcript","speakers","summary","audio_url"]` — ask only for what you need; each is a large field or an extra request. |
| `mutate_recording` | `action=` rename / trash / restore / move. Accepts `recording_ids` for batch (not for rename). |
| `delete_recording` | Permanent. Requires `confirm=true` — see below. |
| `edit_transcript` | `action=` rename_speaker / correct. |
| `edit_summary` | `action=` correct / replace. Recording must already have a summary. |
| `upload_recording` | Local audio file → new recording. Transcodes via ffmpeg when needed. |
| `process_recording` | Trigger transcription/summarization. `wait=` none / transcript / summary. |
| `merge_recordings` | Two or more → one new recording. Sources survive. |
| `list_folders` | Get folder IDs before any folder-scoped call. |
| `mutate_folder` | `action=` create / edit / delete. Delete requires `confirm=true`. |

To move a recording into a folder, use `mutate_recording(action="move")` — not
`mutate_folder`.

## Confirm gates

`delete_recording` and `mutate_folder(action="delete")` refuse to run without
`confirm=true`. That flag means *the human has said yes to this specific
irreversible thing*. Ask, get an answer, then pass it. Never set it
pre-emptively to save a turn.

Prefer `mutate_recording(action="trash")` over `delete_recording` — trash is
reversible with `action="restore"`, and it's almost always what the user meant.

## Transcript blocks

`transcript_block="transaction"` (default) is the raw diarized transcript.
`transcript_block="transaction_polish"` is Plaud's AI-cleaned pass — filler words
removed, punctuation repaired, same speakers and timestamps. Polish reads better
for quoting; raw is truthful about what was said. Not every recording has a
polished block, and the response names what's available when it's missing.

Edits always apply to the raw block regardless of what you read.

## Errors

Every failure returns structured JSON — read the fields, don't pattern-match the
message:

| `error_code` | Meaning |
|---|---|
| `session_expired` | Tell the user to open the PlaudTools tray and sign in. You cannot fix this yourself. |
| `validation` | Your arguments were wrong. Read `error` and correct them; don't retry unchanged. |
| `not_found` | Bad recording/folder ID. Don't retry. |
| `transient` | `retryable: true` — retry once or twice with a pause. |
| `io_error` | Local filesystem problem (usually `upload_recording`). |

A response with `status: "still_processing"` is not an error: a long job
(transcribe, merge, upload) outlived the server-side wait and is still running.
Poll `get_recording` rather than re-issuing the call — re-issuing starts a
*second* job.

## Dates

Resolve relative dates against the current date from context, never the training
cutoff. `since`/`until` take ISO 8601 and `until` is inclusive to end-of-day.

| User says | Filter |
|---|---|
| "today" | `since` = today, `until` = today |
| "yesterday" | both = yesterday |
| "this week" | Monday of this week → today |
| "last week" | Monday → Sunday of last week |
| "last month" | 1st → last day of previous month |

## Presenting results

- Show title, date, and duration; include the recording ID when the user may
  want a follow-up action on it.
- `duration_minutes` is already minutes and `date` is already trimmed — no
  reformatting needed.
- `has_transcript` / `has_summary` tell you whether it's worth fetching those
  before you try.
- `audio_url` expires in an hour (`audio_url_expires_in_s`). Don't save it or
  reuse it later in a long session — re-fetch it.
