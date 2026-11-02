# ADR 008: Move the client to the Plaud 4.0 API

**Status:** accepted (2026-11-02)

## Problem

On 2026-11-02 Plaud moved accounts to its 4.0 app. For every moved account,
each legacy `/file/*` and `/filetag/*` route now answers status `-1800907`,
"Account migrated to 4.0. Please update and reinitialize." plaud-tools 0.12
read and wrote recordings and folders only through those routes, so browsing,
reading, editing, uploading and folder management all stopped working. Search
(`/gsearch/v1/search`) kept working, which made the outage look partial.

The 4.0 web app talks to a new API under `/file-app/v4/`. It models the
library differently, so the change touches every call in `core/client.py`.

## Decision

`PlaudClient` uses only the 4.0 routes, plus the three legacy routes that still
answer for moved accounts: `/user/me`, `/gsearch/v1/search` and
`/file/document/export`. Accounts Plaud has not moved yet are not supported.
Keeping both protocols would double the client for a population that shrinks
to zero, and the stored login token works on both, so there is nothing to
detect at login.

The MCP tools and CLI commands keep their names and inputs. Their outputs
change where the 4.0 model has no equivalent (see "Consequences").

## How 4.0 differs

**IDs.** A recording is named by its file id. A recording made before the move
is `of_<old hex id>`; a newer one is `f_<hex>`. `to_file_id()` turns any id a
caller may still hold (bare hex from old exports, notes or the search index,
or the node ids `on_f_` / `on_w_`) into the file id, so old ids keep working.

**Nodes.** Plaud stores the library as a tree of nodes. Each recording is a
file node inside its own container folder, and the container sits in a user
folder or a system folder (Recordings, Import, Trash). Container folders have
no icon; folders a person made always do, which is how `_is_container` tells
them apart. A recording's `folder_id` is the user folder it sits in, or None
when it is in a system folder. That is what "unfiled" means now.

**Versions.** Every write sends `origin_version`, the node's current
`version_ms`. A missing or stale version fails with `-1800313`. Renaming a
recording renames both the file node and its container, so the two names
never disagree.

**Batch writes.** `nodes/move`, `nodes/trash`, `nodes/restore` and
`nodes/delete` return status 0 even when an item fails, and list the failures
in `data.failed[]`. `_node_batch()` raises on any entry there. Before this
check, a rejected move or delete looked like a success.

**Listing.** `recordings/all` pages by cursor, not offset, and accepts
`page_size` up to at least 1000, so one request covers most libraries (806
recordings took 0.3 s). `list_recordings` cuts its skip/limit window from the
front of that walk. The list carries no transcript or summary flags. There is
no folder-tree endpoint. The web app's sidebar asks only for the top level
and opens a folder when you expand it. `list_folders` needs the whole tree, so
it walks `folders/children` and fetches each level in parallel. That took
about 1 s for 16 top-level folders, against 4 s one folder at a time. Each
call also returns every recording in that folder, which is most of the cost.

**Detail.** `files/detail/{file_id}` returns the node, its metadata, and one
object per artifact (AUDIO, TRANSCRIPT, POLISHED_TRANSCRIPT, OUTLINE,
SUMMARY), each with a presigned `content_url` that lasts an hour. An object is
done when it has a `content_url` and its `task_status` is null (migrated) or
1 (generated). Audio may be `audio/ogg`, so `get_audio_link` returns the MIME
type alongside the URL.

**Upload.** `upload/create`, then one S3 PUT per part with no headers, then
`upload/merge-part`, then `upload/confirm-audio-web`. The confirm request
needs an `x-timezone-iana` header, sets the target folder, and starts
transcription and a summary on its own. The client names the new file id.
Plaud's AI title replaces the recording's name once the summary finishes, so
a rename made before then is overwritten.

**Timezone.** Plaud 4.0 takes IANA zone names only. `tzlocal` maps the
Windows zone to one. An explicit offset becomes `Etc/GMT±N`, so it must be
whole hours.

**Errors.** `-1800311` (file not found) maps to `not_found`. Error messages
now carry the status code, method and path. A `-1800907` adds a hint to
update plaud-tools, so the next protocol change explains itself.

## Consequences

- Breaking release, 0.13.0.
- `browse_recordings` items lose `has_transcript` and `has_summary`. The 4.0
  list has no such flags, and fetching each detail would turn one request into
  hundreds. `get_recording` still reports `is_trans` and `is_summary`.
- `get_recording` loses `headline` and `used_template`, which 4.0 does not
  return. It gains `audio_mime_type`.
- Folder colors are Plaud color names such as `folder-color-green`, not hex.
  Folders gain `parent_id`.
- `upload_recording` loses `folder_error`. The folder is set in the confirm
  request, so a recording can no longer be uploaded but left unfiled.
- Exports and every other output name recordings by file id. A system that
  stored bare hex ids from 0.12 or earlier sees the same recording as
  `of_<hex>`.
- Deleting a folder moves its recordings to the Recordings system folder
  first, then trashes and deletes the folder. Trashing a folder in 4.0 also
  trashes everything inside it.
