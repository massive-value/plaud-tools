# `plaud-tools` CLI reference

`plaud-tools` is the human-facing terminal entry point. `pt` is a short alias for the same tool — every command below works with either name.

```
plaud-tools --version
plaud-tools --help
plaud-tools <subcommand> --help
```

All read commands return JSON. The output is suitable for piping into `jq` or capturing for scripts.

## Exit codes

Errors go to stderr, keeping stdout clean for piping. The exit code tells a
script *why* it failed without having to match on the message:

| Code | Meaning | What a script should do |
|---|---|---|
| `0` | Success | — |
| `1` | Invalid arguments, recording not found, or an unclassified error | Fail; the message is on stderr |
| `2` | Authentication failed — no session, expired session, or HTTP 401 | Run `plaud-tools refresh`, or sign in via the tray |
| `3` | Transient network or server error (connection failure, 429, 5xx) | Retry with backoff |
| `4` | Timed out waiting on a job that is **still running** on Plaud's side | Poll with `status <id>` rather than re-running the command |

Code `4` matters for the long-running commands (`transcribe --wait`, `merge`,
`upload`): the work has not failed, it just outlived the client-side wait.
Re-running the command would start a *second* job.

One wrinkle: a malformed command line exits `2` as well, because that is
argparse's own convention for a usage error. A usage message on stderr
distinguishes it from an auth failure.

Two more outside the `0`-`4` taxonomy: Ctrl+C mid-command exits `130`, the
conventional shell code for SIGINT. Piping into something that closes early
(`plaud-tools list | head`) exits `0` — the command did its job; the reader
just stopped listening — instead of a broken-pipe traceback.

---

## Sign-in and session

### `login`

```
plaud-tools login --email you@example.com --region us
```

Prompts for your Plaud password and stores the resulting access token in your OS keyring. On Windows, a DPAPI-encrypted shadow copy is also written to `%LOCALAPPDATA%\PlaudTools\session.dat` so the token can still be read if the Credential Manager is having a bad day; a plaintext file at `%LOCALAPPDATA%\PlaudTools\session.json` (macOS/Linux: your platform's per-user data directory) is the last-resort fallback, used only when both the keyring and DPAPI are unavailable. `--region` is `us` or `eu`; if you pick the wrong one, the client auto-detects and switches on the first API call.

If you signed up for Plaud with Google, use "Forgot password" on [web.plaud.ai](https://web.plaud.ai) first to set a password — `plaud-tools login` is password-based.

### `refresh`

```
plaud-tools refresh
plaud-tools refresh --email you@example.com --region us
```

Re-authenticates using the email/region already saved in the stored session (no need to retype them) and prompts for the password. This is the designated way to unbrick an expired or soon-to-expire session — Plaud has no refresh-token grant, so it's still a full credential re-auth under the hood, just without retyping email/region. `--email` and `--region` override the stored values if given.

**Avoid `--password` in scripts.** Passing `--password` on the command line exposes it via process listings (`ps`, Task Manager) and shell history. Omit the flag to be prompted securely instead. For scripting and CI, use the safer alternatives described below.

### `session show`

```
plaud-tools session show
```

Prints the stored email, region, masked token, source, and days until expiry. Use this to debug session-loading issues. Pass `--show-token` to print the full token (handle with care).

`source` is one of:

| Source | Meaning |
|---|---|
| `env` | `PLAUD_ACCESS_TOKEN` (see below) — never persisted |
| `keyring` | OS credential store (Windows Credential Manager, macOS Keychain, ...) |
| `legacy_keyring` | Found under the predecessor tool's keyring entry and migrated on the spot |
| `dpapi_file` | Windows DPAPI-encrypted shadow file, used when the keyring read failed |
| `file` | Plaintext fallback file, used when both the keyring and DPAPI are unavailable |
| `missing` | No session found anywhere |

### `session set`

```
plaud-tools session set --token <token> --region us --email you@example.com
```

Write a session entry without going through `login`. Useful for CI or for transferring a session between machines. Like `login`, this goes through the keyring/DPAPI/file fallback chain above; the output's `source` field says where it actually landed, and `path` is only included when that source has one (`dpapi_file` or `file` — `keyring` has none to report).

### `session clear`

```
plaud-tools session clear
```

Removes the stored session from the keyring, the DPAPI shadow file, and the plaintext file store — wherever it might be.

### Environment variable injection

```
PLAUD_ACCESS_TOKEN=<token> PLAUD_REGION=us plaud-tools list
```

Both the CLI and `plaud-mcp` server honour `PLAUD_ACCESS_TOKEN` and `PLAUD_REGION` — they take precedence over any stored session and are never written to disk.

### Recommended scripting and CI auth paths

When automating `plaud-tools` in scripts or CI pipelines, avoid `login --password`: the password is visible in process listings and shell history. Use one of these safer alternatives instead:

**Environment variable (one-shot or ephemeral):**

```
PLAUD_ACCESS_TOKEN=<token> PLAUD_REGION=us plaud-tools list
```

The token is injected per-invocation and never stored.

**Stored session without a password (persistent):**

```
plaud-tools session set --token <token> --region us --email you@example.com
```

Writes a session entry directly to the keyring / file store without going through `login` at all. Obtain a token by running `plaud-tools login` interactively once and then copying it with `plaud-tools session show --show-token`.

---

## Browsing recordings

### `list`

```
plaud-tools list
plaud-tools list --limit 10
plaud-tools list --since 2025-01-01 --until 2025-02-01
plaud-tools list --query "tax planning"
plaud-tools list --folder-id <folder-id>
plaud-tools list --unfiled
```

Defaults to 20 most recent recordings. `--since` and `--until` accept ISO 8601 dates (`2025-01-01`) or datetimes (`2025-01-01T09:30`) — no relative offsets like `7d`. `--query` is case-insensitive substring matching against titles.

`--all` returns every matching recording instead of one page. It fetches the library 200 recordings per request and stops when Plaud returns a short page, so the result is complete. A large `--limit` is not the same thing; it only caps the count. `--all` and `--limit` can't be combined. `search` takes `--all` too.

```
plaud-tools list --all --folder-id <folder-id> > recordings.json
```

### `search`

```
plaud-tools search "henderson account"
plaud-tools search "tax" --since 2025-01-01
```

Shorthand for `list --query QUERY` — identical filtering, just with `query` as a positional argument instead of a flag. No separate ranking.

Add `--content` to search what was said instead of titles:

```
plaud-tools search --content "rollover"
plaud-tools search --content "roth conversion" --since 2026-01-01 --until 2026-03-31
```

This uses Plaud's own full-text search over transcripts and summaries. Each result has a `snippet` around the hit, a `source` (`transcript` or `summary`), and `start_ms`, the hit's position in the audio (transcript hits only). Results come best match first. Plaud stems words ("retire" also finds "retirement") and does not match exact phrases.

Plaud returns 20 matches at most. If you get exactly 20, older or less relevant recordings may match too. Narrow `--since`/`--until` to see them. `--folder-id` and `--unfiled` don't work with `--content`.

### `show`

```
plaud-tools show <recording-id>
```

Compact summary: title, date, duration, folder, speakers, headline.

### `detail`

```
plaud-tools detail <recording-id>
plaud-tools detail <recording-id> --include-transcript
```

Lower-level dump of the recording's API fields. Always includes the AI summary (`null` if none exists yet). Use `--include-transcript` to also fetch the linked transcript content. `transcript` is `null` when the recording has no transcript, and `""` only when it has one that is empty.

### `transcript`

```
plaud-tools transcript <recording-id>
plaud-tools transcript <recording-id> --polish
plaud-tools transcript <recording-id> --segments
```

Prints the full transcript text. By default this is the raw diarized transcript.

`--polish` returns Plaud's AI-cleaned pass instead — filler words removed and
punctuation repaired, with the same speakers and timestamps. Not every recording
has one; when it's missing the command errors and names the blocks that are
available rather than printing nothing.

`--segments` prints JSON instead of text, with one record per utterance and a
fingerprint of the whole transcript:

```json
{
  "recording_id": "<recording-id>",
  "transcript_block": "transaction",
  "utterance_count": 2,
  "fingerprint": "sha256:9f2c...",
  "segments": [
    {"index": 0, "speaker": "Speaker 1", "text": "Morning, let's start.", "start_ms": 1510, "end_ms": 11430},
    {"index": 1, "speaker": "Speaker 2", "text": "Sounds good.", "start_ms": 11430, "end_ms": null}
  ]
}
```

`start_ms`/`end_ms` are milliseconds from the start of the recording, as Plaud
sent them. They are `null` when Plaud sent none. `index` counts utterances from
0 and matches the MCP `get_recording` `transcript_segments` indexes. The
fingerprint is a SHA-256 of the transcript content (not a Plaud revision
number), so any edit changes it. Indexes stay valid only while the fingerprint
stays the same; after an edit, re-export.

Note that the editing commands (`rename-speaker`, `correct-transcript`) always
operate on the raw transcript, whichever block you read.

#### Exporting transcripts from a script

Redirect stdout to a file. Output is UTF-8 on every platform, including a
Windows console redirect, and errors go to stderr, so the file holds only the
transcript.

```
plaud-tools transcript <recording-id> > meeting.txt
plaud-tools transcript <recording-id> --segments > meeting.json
```

| Outcome | Exit code | stdout |
|---|---|---|
| Transcript exported | `0` | The transcript |
| Transcript exists but is empty | `0` | Empty |
| No transcript yet, or `--polish` with no polished block | `1` | Empty (reason on stderr) |
| Unknown recording ID | `1` | Empty |
| Session expired | `2` | Empty |
| Network or Plaud server error, including a failed transcript download | `3` (or `1` for a non-retryable HTTP error) | Empty |

To export a whole folder, list it with `--all` and loop over the IDs:

```
plaud-tools list --all --folder-id <folder-id> | jq -r '.[] | select(.has_transcript) | .id' |
  while read -r id; do plaud-tools transcript "$id" --segments > "$id.json" || echo "failed: $id" >&2; done
```

### `summary`

```
plaud-tools summary <recording-id>
```

Prints the AI-generated summary if one exists. Returns `null` for recordings that haven't been processed yet.

### `audio`

```
plaud-tools audio <recording-id>
plaud-tools audio <recording-id> -o meeting.mp3
plaud-tools audio <recording-id> -o ./downloads/
```

Without `-o`, prints a temporary download URL for the recording's audio plus its
lifetime in seconds. **The URL expires after one hour** — don't store it or reuse
it later in a script; ask for a fresh one.

With `-o`, downloads the audio to that path instead. Pass a directory to save as
`<recording-id>.mp3` inside it. Errors if Plaud has no audio for the recording,
which usually means it hasn't finished syncing from the device.

---

## Folders

### `folders`

```
plaud-tools folders
```

Lists all folders with `id`, `name`, `color`, `icon`.

### `folder create` / `folder edit` / `folder delete`

```
plaud-tools folder create "Client Calls"
plaud-tools folder create "Client Calls" --color "#4c8eff" --icon e627
plaud-tools folder edit <folder-id> --name "Renamed folder"
plaud-tools folder edit <folder-id> --color "#4c8eff"
plaud-tools folder delete <folder-id> --yes
```

`create` requires a name; `--color` (hex, e.g. `#4c8eff`) and `--icon` (glyph codepoint, e.g. `e627`) are optional. `edit` requires at least one of `--name`, `--color`, `--icon`. `delete` is irreversible for the folder itself — recordings inside are kept but become unfiled — and requires `--yes`.

### `move`

```
plaud-tools move <recording-id> <folder-id>
plaud-tools move <recording-id> -
```

Moves a recording into a folder. Use `-` as the folder ID to clear (move out of any folder).

`move-to-folder` is an alias for `move`.

---

## Editing recordings

### `rename`

```
plaud-tools rename <recording-id> "New title"
```

### `rename-speaker`

```
plaud-tools rename-speaker <recording-id> <original-label> "New name"
```

`<original-label>` is the speaker label as shown in the transcript. This can
be a generic label (`Speaker 1`) **or** a name Plaud already resolved from an
enrolled voice or a prior rename (e.g. `Benjamin Everitt`) — either matches.

### `correct-transcript`

```
plaud-tools correct-transcript <recording-id> "<find>" "<replace>"
```

Literal (case-sensitive) find-and-replace across every transcript segment —
useful for fixing a misheard word or name. Reports the number of occurrences
replaced and segments changed. Speaker labels are not affected; use
`rename-speaker` for those. Pass an empty `<replace>` to delete the matched
text.

### `correct-summary`

```
plaud-tools correct-summary <recording-id> "<find>" "<replace>"
```

Literal (case-sensitive) find-and-replace on a recording's AI summary text. Requires a summary to already exist. Pass an empty `<replace>` to delete the matched text.

### `set-summary`

```
plaud-tools set-summary <recording-id> --content "New summary markdown"
plaud-tools set-summary <recording-id> --content-file summary.md
```

Overwrites a recording's AI summary entirely with new markdown. `--content` and `--content-file` are mutually exclusive; exactly one is required.

### `trash` / `restore` / `delete`

```
plaud-tools trash <recording-id>
plaud-tools trash --list
plaud-tools restore <recording-id>
plaud-tools delete <recording-id> --yes
```

`trash` (with a recording ID) is reversible — it moves the recording to the trash folder. `trash --list` lists recordings currently in trash; a bare `trash` with neither a recording ID nor `--list` is rejected (a dropped argument used to silently turn the mutation into a listing). `delete` is permanent and requires `--yes`.

### `trash-move` / `trash-restore`

```
plaud-tools trash-move <id1> <id2> <id3>
plaud-tools trash-restore <id1> <id2> <id3>
```

Bulk variants of `trash` and `restore`.

---

## Audio

### `upload`

```
plaud-tools upload /path/to/file.m4a
plaud-tools upload file.wav --title "Client meeting"
plaud-tools upload file.mp3 --folder-id <folder-id>
plaud-tools upload file.wav --detach
plaud-tools upload file.wav --skip-summary
plaud-tools upload file.wav --start-time 2025-03-15T14:30 --timezone-offset -7
```

Uploads a local audio file. Native formats: `.mp3`, `.opus`, `.ogg`, `.oga`. Other formats (`.m4a`, `.mp4`, `.wav`, `.aac`, `.flac`, `.wma`, `.amr`) are transcoded to MP3 via ffmpeg.

By default the command waits for transcription and summary to finish. `--detach` returns immediately. `--skip-summary` waits for transcript only.

### `transcribe`

```
plaud-tools transcribe <recording-id>
plaud-tools transcribe <recording-id> --template <template-type>
plaud-tools transcribe <recording-id> --language en --diarization --llm <llm-id>
plaud-tools transcribe <recording-id> --wait transcript
plaud-tools transcribe <recording-id> --wait summary
```

Triggers transcription + summarization on an existing recording. `--language` is a language code (default: auto-detect); `--diarization`/`--no-diarization` enables/disables speaker diarization (default: Plaud's default); `--llm` selects the summarization model (default: auto). `--wait` controls how long to block before returning: `none` (default — accept and return immediately), `transcript`, or `summary`.

If the recording already has a transcript, Plaud keeps the existing transcript and summary and ignores `--template`/`--language`. The command then exits `0` with `"accepted": false, "already_processed": true`. To change the summary text, use `set-summary` or `correct-summary`.

### `status`

```
plaud-tools status <recording-id>
plaud-tools status
```

Returns the task list for a recording (transcription, summary, etc.). With no argument, returns all in-flight tasks.

---

## Merging

### `merge`

```
plaud-tools merge <id1> <id2> --title "Combined call"
plaud-tools merge <id1> <id2> <id3> <id4> --title "Day-long session"
```

Merges two or more recordings into a single new recording. The source recordings are not modified.

---

## Diagnostics

### `doctor`

```
plaud-tools doctor
```

Prints a JSON self-diagnosis document: version, install mode (frozen vs pip), executable paths, session status (masked), AI client wiring, and tray log path. Attach the output to bug reports.

### `ping`

```
plaud-tools ping
```

Hits Plaud's user-info endpoint and confirms the session is live.

### `dump`

```
plaud-tools dump <recording-id>
```

Prints the raw `/file/detail` API response. For debugging — not part of the stable interface.

---

## Updating

### `update`

```
plaud-tools update
```

Runs `pip install --upgrade plaud-tools` in the current Python environment and prints a reminder that pipx, uv, and conda users should use their own upgrade command. Windows tray-bundle users should use the in-tray updater instead — see the main [README](../README.md#keeping-plaudtools-up-to-date).
