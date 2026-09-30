# Transcript export

`export_transcript` (MCP) and `plaud-tools export` (CLI) save a recording's whole transcript to a file. The tool fetches the transcript, builds the file in code, writes it to disk, and returns metadata about the file. The transcript text never appears in the tool response, so an agent can file a transcript without retyping it.

Both surfaces share one implementation, `src/plaud_tools/core/export.py`.

## Formats

| `format` | Built by | Speaker labels | Timestamps |
|---|---|---|---|
| `json` | plaud-tools, to the contract below | always | always |
| `txt` | Plaud's exporter | optional | optional |
| `srt` | Plaud's exporter | optional | always |
| `docx` | Plaud's exporter | optional | optional |
| `pdf` | Plaud's exporter | optional | optional |

`txt`, `srt`, `docx` and `pdf` are the files the Export button in Plaud's web app makes. The tool posts the utterances it just fetched to `POST /file/document/export`, and Plaud answers with a temporary S3 link to the rendered file. Because we send the same utterances we fingerprint, every format reports the fingerprint of the content inside it.

SRT ignores `with_timestamps=false`. With timestamps off, Plaud writes every cue as `00:00:00,000 --> 00:00:00,000`. That's the broken SRT the SWIRL handoff ran into.

## JSON contract, version 1

Example: [examples/transcript-export-v1.json](examples/transcript-export-v1.json) (synthetic).

| Field | Rule |
|---|---|
| `schema_version` | `1` |
| `source` | `"plaud"` |
| `recording_id` | Plaud's file ID, unchanged. Letters, digits, `-`, `_`, up to 128 characters. |
| `portion_id` | `"full"`. Portion export isn't built yet. |
| `title` | Plaud's recording title. Omitted when empty. |
| `recorded_at` | Recording start, UTC, `YYYY-MM-DDTHH:MM:SSZ`. Omitted when Plaud has none. |
| `transcript_block` | `"transaction"` (raw) or `"transaction_polish"` (Plaud's AI-cleaned pass). |
| `transcript_fingerprint` | `sha256:<hex>`, see below. |
| `segments` | Every utterance in Plaud's order. Never empty. |

Each segment:

| Field | Rule |
|---|---|
| `index` | Position in the block, `0, 1, 2, ...` with no gaps. |
| `text` | Plaud's `content`, byte for byte. We don't trim, merge or deduplicate it. |
| `speaker` | The label Plaud shows (`"Jane Doe"` or `"Speaker 1"`), or `null`. Up to 200 characters. A label doesn't prove who spoke. |
| `speaker_id` | Plaud's `original_speaker`, the diarization label the recording started with, or `null`. It's only meaningful inside one recording. Plaud has no speaker ID that spans recordings. Of the two authorized test recordings, one sends it and one doesn't. |
| `start_ms`, `end_ms` | Milliseconds from recording start. Whole numbers, `end_ms >= start_ms`, both present or both `null`. Overlapping turns are allowed. |

The file is UTF-8 JSON, 2-space indent, non-ASCII left unescaped, with a trailing newline. Serialization is deterministic, so exporting an unchanged transcript twice gives the same bytes and the same SHA-256.

## Fingerprint

Plaud has no revision number, so we compute one:

```
sha256( utf8( json.dumps({"block": <block>, "segments": <Plaud's raw utterances>},
                          sort_keys=True, ensure_ascii=False, separators=(",", ":")) ) )
```

The input is the raw utterances as Plaud sent them (text, speaker, original speaker, timings) in their original order. Correcting a word or renaming a speaker changes the fingerprint. The export time never goes into it. `get_recording` and `transcript --segments` report the same value, so a caller can review with `get_recording` and then export with `expected_transcript_fingerprint` set. If the transcript changed in between, the export fails with `revision_conflict` and writes nothing.

The fingerprint identifies the transcript revision. The response's `sha256` identifies the exact file bytes. A PDF and a JSON of the same revision share a fingerprint and have different hashes.

## Consistency

Plaud serves a whole transcript block as one download, so an export reads one revision in one request. There are no pages to stitch, and two revisions can't mix. The paging in `get_recording` happens on our side for a model reading along. The export doesn't use it.

## Failures

A failed export writes no file. Each failure returns an `error_code`, and the message never includes transcript text:

| `error_code` | When |
|---|---|
| `transcript_unavailable` | Not transcribed, the requested block doesn't exist (no fallback to the other block), or the block is empty. |
| `revision_conflict` | The fingerprint doesn't match `expected_transcript_fingerprint`. |
| `file_exists` | `output_path` already exists and `overwrite` is false. |
| `invalid_source` | An utterance breaks the contract (negative or one-sided timing, end before start, a speaker label over 200 characters), or Plaud's rendered file isn't the format we asked for. |
| `validation` | Bad arguments, such as a recording ID with path characters in it. |

Files are written to a temp file in the target folder first and renamed into place once complete, so a crash never leaves a half-written file under the real name.

## Where the file goes

Without `output_path`, the file goes to `%LOCALAPPDATA%\PlaudTools\exports\<recording_id>.<block>.<format>`. The next export of the same recording, block and format replaces it, so that folder holds at most one copy of each.

With `output_path`, the file goes where the caller says, either a full file path or a folder. The tool never replaces an existing file there unless `overwrite` is true, because the path may be a OneDrive-synced SharePoint folder.

## Getting the file into SharePoint

plaud-mcp is a local process, so it returns a local path. Host support differs:

- **Claude Code.** Works without the model touching the text. The Squire plugin already files transcripts into the advisor's OneDrive-synced client folder. It can export straight there with `output_path`, or copy the exported file with a shell command. OneDrive uploads it, and the plugin confirms the SharePoint item exists before attaching it in SWIRL.
- **The Microsoft 365 `sharepoint_upload_file` connector.** No programmatic path. It only accepts file content as an inline `content` or `contentBase64` argument, up to 1 MB. It has no parameter for a file reference, path or URL. So a model would have to paste the transcript into the call, which is what this tool exists to avoid. The fix belongs in the connector: an upload-from-URL or upload-from-local-file parameter.
- **ChatGPT.** Not tested. ChatGPT only reaches MCP servers over HTTPS, and plaud-mcp only runs locally over stdio, so ChatGPT can't call it at all without a tunnel. And even through a tunnel, the file path it returns would point at the advisor's machine.
