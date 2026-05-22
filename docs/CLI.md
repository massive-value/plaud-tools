# `plaud-tools` CLI reference

`plaud-tools` is the human-facing terminal entry point. `pt` is a short alias for the same tool — every command below works with either name.

```
plaud-tools --version
plaud-tools --help
plaud-tools <subcommand> --help
```

All read commands return JSON. The output is suitable for piping into `jq` or capturing for scripts.

---

## Sign-in and session

### `login`

```
plaud-tools login --email you@example.com --region us
```

Prompts for your Plaud password and stores the resulting access token in your OS keyring (with a file-store fallback at `~/.config/plaud-tools/session.json`). `--region` is `us` or `eu`; if you pick the wrong one, the client auto-detects and switches on the first API call.

If you signed up for Plaud with Google, use "Forgot password" on [web.plaud.ai](https://web.plaud.ai) first to set a password — `plaud-tools login` is password-based.

### `session show`

```
plaud-tools session show
```

Prints the stored email, region, masked token, source (`env` / `keyring` / `file` / `missing`), and days until expiry. Use this to debug session-loading issues. Pass `--show-token` to print the full token (handle with care).

### `session set`

```
plaud-tools session set --token <token> --region us --email you@example.com
```

Write a session entry without going through `login`. Useful for CI or for transferring a session between machines.

### `session clear`

```
plaud-tools session clear
```

Removes the stored session from both the keyring and the file store.

### Environment variable injection

```
PLAUD_ACCESS_TOKEN=<token> PLAUD_REGION=us plaud-tools list
```

Both the CLI and `plaud-mcp` server honour `PLAUD_ACCESS_TOKEN` and `PLAUD_REGION` — they take precedence over any stored session and are never written to disk.

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

Defaults to 20 most recent recordings. `--since` and `--until` accept dates (`2025-01-01`), datetimes (`2025-01-01T09:30`), or relative offsets. `--query` is case-insensitive substring matching against titles.

### `search`

```
plaud-tools search "henderson account"
plaud-tools search "tax" --since 2025-01-01
```

Like `list --query` but with `query` as a positional argument and ranking optimized for finding a specific recording.

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

Lower-level dump of the recording's API fields. Use `--include-transcript` to fetch the linked transcript content.

### `transcript`

```
plaud-tools transcript <recording-id>
```

Prints the full transcript text.

### `summary`

```
plaud-tools summary <recording-id>
```

Prints the AI-generated summary if one exists. Returns `null` for recordings that haven't been processed yet.

---

## Folders

### `folders`

```
plaud-tools folders
```

Lists all folders with `id`, `name`, `color`, `icon`.

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

`<original-label>` is the speaker label from the transcript (e.g., `Speaker 0`, `Speaker 1`).

### `trash` / `restore` / `delete`

```
plaud-tools trash <recording-id>
plaud-tools restore <recording-id>
plaud-tools delete <recording-id> --yes
```

`trash` is reversible (moves to the trash folder). `delete` is permanent and requires `--yes`. Running `trash` with no argument lists trashed recordings.

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
```

Triggers transcription + summarization on an existing recording.

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
