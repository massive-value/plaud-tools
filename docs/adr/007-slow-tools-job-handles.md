# ADR 007: Slow MCP tools return job handles, never "retry me"

**Status:** accepted (2026-09-24). Replaces the #151 "bounded wait" section of ADR 006.

## Problem

Three MCP tools can outlast a client's patience: `upload_recording`,
`merge_recordings`, and `process_recording(wait="summary")`. ADR 006 capped each
at a 90 s soft deadline and answered a timeout with `status: "still_processing"`.
That fixed orphaned handlers but invited duplicates:

- `upload_recording` put a 90 s deadline on the S3 transfer itself. A slow link
  aborted a transfer halfway and answered `still_processing, retryable: true`,
  for an upload that never happened. There was nothing to check back on, so the
  only move left was to upload again.
- `merge_recordings` timed out without the combine `task_id`, so the agent could
  not tell "still merging" from "lost", and a re-call starts a second merge.
- `process_recording(wait="summary")` gave each wait its own 90 s, so one call
  could block for 180 s.
- The soft-deadline check matched any error text containing "timed out". A 30 s
  socket timeout on one poll looked like "job still running" (CLI exit 4, MCP
  `still_processing`) when nothing was known to be running.

## Decision

A timeout is only reported as `still_processing` when a Plaud job is known to be
running, and the result says what that job is and how to check on it.

- Poll loops raise `PlaudWaitTimeoutError` (a `PlaudApiError` subclass carrying
  an optional `task_id`). `is_soft_deadline_timeout()` is true only for that
  class. Network timeouts stay plain transient `PlaudApiError`s.
- `upload_recording` has no deadline. It returns `recording_id` as soon as
  Plaud confirms the upload; any failure is a normal error. If the connection
  dies during `confirm_upload`, the error is non-retryable and says to check
  `browse_recordings` first, because the recording may exist.
- `process_recording` spends one 90 s budget across both waits.
- A still-running job comes back as a success-shaped result:

```json
{
  "status": "still_processing",
  "job": {"kind": "merge", "id": "<combine task_id>", "poll_with": "browse_recordings"},
  "retryable": false,
  "message": "Plaud is still merging. Do not call merge_recordings again; ...",
  "recording_ids": ["..."], "title": "..."
}
```

`kind` is `transcription`, `summary`, or `merge`. For process_recording `id` is
the recording id and `poll_with` is `get_recording`. `retryable: false` means
"calling this tool again starts a new job", not "the job failed".

## Why this shape

The MCP tasks extension (SEP-2663) models a long call as a task with a `taskId`,
a `status`, and a poll method. The Python SDK doesn't implement tasks yet, so we
can't return one. `job` is the same three facts in our own JSON:
`job.id` becomes `taskId`, `status: "still_processing"` becomes `working`, and
`poll_with` becomes the task poll. When the SDK ships tasks, the handlers can
return a real task for the same cases without changing when they fire.

## Rejected

- **Keep a deadline on upload and resume the multipart transfer later.** Plaud's
  presigned part URLs expire and there is no resume endpoint we know of. Stopping
  midway can only waste the bytes already sent.
- **Return an error on timeout.** Clients and models read `isError` as failure
  and retry, which is exactly the duplicate we are avoiding.
- **Poll combine-tasks from `browse_recordings`.** A merge job id isn't a
  recording until it finishes; `browse_recordings` shows the new recording by
  title when it does, which is enough.
