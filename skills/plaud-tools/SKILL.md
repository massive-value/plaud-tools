---
name: plaud-tools
description: Read this before using the PlaudTools MCP (browse_recordings, search_recordings, get_recording, mutate_recording, edit_transcript, edit_summary, upload_recording, process_recording, merge_recordings, list_folders, mutate_folder, delete_recording). Covers transcript pagination, dry-run edits, confirm gates, error handling, and date filters. Use when the user mentions Plaud, their recordings, meetings, or transcripts for the first time in a session.
---

# plaud-tools

Eleven tools over a Plaud account. Three read, eight write. Auth lives in the
PlaudTools tray app, not here — there is no login tool, by design.

## The three things that go wrong most

**1. Transcripts are paginated.** `get_recording(include=["transcript"])` returns
`transcript_limit` utterances (default 200), not the whole thing. While
`transcript_has_more` is `true`, call again with `transcript_after` set to the
returned `transcript_next_after`. At the end `transcript_has_more` is `false` and
`transcript_next_after` is `null`. **Summarizing a partial page as if it were the
full meeting is the single worst failure mode here** — you will confidently
summarize the first third of a call. `transcript_truncated: true` means "this
page is not the whole transcript" (a last page is still truncated), so don't use
it to decide whether to keep paging. `transcript_utterance_count` gives the
total up front, and `transcript_page_start`/`transcript_page_end` (end-exclusive)
say which utterances you got.

Every page carries `transcript_fingerprint`, a hash of the whole transcript. If
it changes between pages, someone edited the transcript mid-read. Start over
from `transcript_after=0` rather than stitching two versions together.
Utterance indexes only hold within one fingerprint.

Need to cite where something was said? Add `"segments"` to `include` to get
`transcript_segments`: each utterance's absolute `index`, `speaker`, verbatim
`text`, and `start_ms`/`end_ms` (milliseconds from recording start, `null` when
Plaud has no timing). `transcript_block="transaction"` (default) is the raw
transcript; `"transaction_polish"` is Plaud's cleaned-up pass, and the two have
different fingerprints. If the requested block doesn't exist, the response
has `transcript_fingerprint: null` and an entry in `notes` naming the blocks that do.

For a full export to disk, use the CLI instead of paging through MCP:
`plaud-tools transcript <id> --segments > file.json` (see docs/CLI.md).

**2. Preview text edits with `dry_run`.** `edit_transcript(action="correct")` and
`edit_summary(action="correct")` are literal, case-sensitive find-and-replace
across the whole recording. Run with `dry_run=true` first and check `matches`.
A find string like `"the"` matches hundreds of times; that is rarely intended.

**3. Don't pull the library to search it.** `browse_recordings` filters
server-side-ish (`query`, `since`, `until`, `folder`). Use the filters. Do not
page through everything and filter yourself. To find what was *said* ("the
meeting where we covered the Johnson rollover"), call `search_recordings`
instead of opening recordings one by one. It returns 20 matches at most. When
`capped` is true, other recordings may match too, so run it again over a
narrower `since`/`until` window.

## Tools

| Tool | Notes |
|---|---|
| `browse_recordings` | Filters: `query` (title substring), `since`/`until` (ISO 8601), `folder`, `trash`. Paginate with `after` ← `next_after`. |
| `search_recordings` | Searches transcripts and summaries. Returns a snippet per hit, best match first. `source` says where the hit is, and `start_ms` gives its position in the audio. Max 20 results per search. |
| `get_recording` | `include=["transcript","segments","speakers","summary","audio_url"]` — ask only for what you need; each is a large field or an extra request. |
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
| `validation` / `invalid_arguments` | Your arguments were wrong. Read `error` and correct them; don't retry unchanged. |
| `not_found` | Bad recording/folder ID. Don't retry. |
| `transient` | `retryable: true` — retry once or twice with a pause. |
| `io_error` | Local filesystem problem (usually `upload_recording`). |
| `internal` | A bug in the server. Tell the user; details are in the plaud-mcp log. |

A response with `status: "still_processing"` is not an error: a transcribe,
summary, or merge job outlived the ~90s wait and is still running on Plaud. Its
`job` says what is running and which tool reports on it (`poll_with`): poll that
tool rather than re-issuing the call, because re-issuing starts a *second* job.
`upload_recording` never returns this; it returns the new `recording_id` or an
error.

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
