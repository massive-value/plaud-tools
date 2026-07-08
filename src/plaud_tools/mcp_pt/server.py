"""Python MCP server entry point for plaud-tools."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import logging.handlers
import os
import sys
from typing import Any

import mcp.server.stdio
import mcp.types as types
from mcp.server.lowlevel import NotificationOptions, Server
from mcp.server.models import InitializationOptions

from .. import __version__
from ..core.appdata import mcp_log as _mcp_log_path
from ..core.client import PlaudClient
from ..core.session import SessionManager, SessionStore
from .mcp import build_handlers


def _setup_mcp_logging() -> None:
    """Configure rotating file logging for the MCP server.

    Without this, every ``logging`` call in the MCP code path goes nowhere
    (the MCP server has no console; Claude Desktop captures stderr but Codex
    does not, and neither persists across sessions). Issue #78 traced spurious
    session_expired events to this observability gap.
    """
    path = _mcp_log_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except OSError:
        return
    handler = logging.handlers.RotatingFileHandler(
        str(path),
        maxBytes=1_000_000,
        backupCount=3,
        encoding="utf-8",
    )
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    # Don't use logging.basicConfig — it is a no-op when the root logger
    # already has handlers. Observed in the v0.2.2 follow-up: the
    # pip-installed plaud-mcp never wrote its startup banner because some
    # earlier import in the pip-launch path had already configured a root
    # handler. Attach directly so we are immune to import-order surprises.
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.addHandler(handler)
    logging.info(
        "plaud-mcp %s starting pid=%d localappdata=%s",
        __version__,
        os.getpid(),
        os.environ.get("LOCALAPPDATA"),
    )


# ---------------------------------------------------------------------------
# Annotation policy — Decision D4 (Wave 2 / C6)
#
# Servers DECLARE capability hints via ToolAnnotations; clients ENFORCE policy
# (e.g. confirmation dialogs, audit trails).  This server never shows
# interactive prompts — it runs over stdio — so destructive safety is achieved
# by two complementary mechanisms:
#
#   1. ToolAnnotations: machine-readable hints that well-behaved clients use to
#      surface warnings or gate execution.
#   2. A required `confirm: true` parameter on delete_recording (the only truly
#      irreversible tool) so the LLM must explicitly pass the flag after the
#      human confirms; the handler rejects the call if confirm is absent or false.
#
# openWorldHint=True is set on every tool: all calls interact with the external
# Plaud service and may observe or affect state not visible in this conversation.
# ---------------------------------------------------------------------------
_TOOLS: list[types.Tool] = [
    types.Tool(
        name="browse_recordings",
        description="Page through Plaud recordings with optional filters.",
        inputSchema={
            "type": "object",
            "properties": {
                "limit": {
                    "type": "integer",
                    "default": 20,
                    "minimum": 1,
                    "description": "Max results per page",
                },
                "since": {
                    "type": "string",
                    "description": "ISO 8601 start-date filter",
                },
                "until": {
                    "type": "string",
                    "description": "ISO 8601 end-date filter",
                },
                "query": {
                    "type": "string",
                    "description": "Title substring filter",
                },
                "folder": {
                    "type": "string",
                    "description": "Folder ID (from `list_folders`); pass empty string for unfiled recordings",  # noqa: E501
                },
                "after": {
                    "type": "integer",
                    "default": 0,
                    "minimum": 0,
                    "description": "Cursor from next_after of a previous response",
                },
                "trash": {
                    "type": "boolean",
                    "default": False,
                    "description": "List trashed recordings instead of active ones",
                },
            },
        },
        # Pure read — no writes, no side-effects.
        # idempotentHint omitted: redundant when readOnlyHint=True (reads are
        # inherently idempotent; stating it again adds noise without value).
        annotations=types.ToolAnnotations(
            readOnlyHint=True,
            openWorldHint=True,
        ),
    ),
    types.Tool(
        name="get_recording",
        description="Fetch full detail for one recording.",
        inputSchema={
            "type": "object",
            "properties": {
                "recording_id": {"type": "string"},
                "include": {
                    "type": "array",
                    "items": {
                        "type": "string",
                        "enum": ["transcript", "speakers", "summary"],
                    },
                    "description": "Large fields to include: transcript, speakers, summary",
                },
                "transcript_offset": {
                    "type": "integer",
                    "default": 0,
                    "minimum": 0,
                    "description": "Character offset to start the transcript from (with include=[transcript])",  # noqa: E501
                },
                "transcript_max_chars": {
                    "type": "integer",
                    "minimum": 1,
                    "description": "Max transcript characters to return; sets transcript_truncated when cut off",  # noqa: E501
                },
            },
            "required": ["recording_id"],
        },
        # Pure read — same rationale as browse_recordings.
        annotations=types.ToolAnnotations(
            readOnlyHint=True,
            openWorldHint=True,
        ),
    ),
    types.Tool(
        name="mutate_recording",
        description="Apply a reversible state change to one or more recordings: rename, trash, restore, or move.",  # noqa: E501
        inputSchema={
            "type": "object",
            "properties": {
                "recording_id": {"type": "string", "description": "Single recording ID"},
                "recording_ids": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Batch of recording IDs (trash/restore/move only, not rename); use instead of recording_id",  # noqa: E501
                },
                "action": {
                    "type": "string",
                    "enum": ["rename", "trash", "restore", "move"],
                },
                "new_name": {
                    "type": "string",
                    "description": "Required for rename",
                },
                "folder_id": {
                    "type": "string",
                    "description": "Folder ID (from `list_folders`); required for move unless clear_folder is true",  # noqa: E501
                },
                "clear_folder": {
                    "type": "boolean",
                    "description": "When true, removes the recording from its current folder (use instead of a magic folder_id value)",  # noqa: E501
                },
            },
            "required": ["action"],
        },
        # Reversible write (trash/restore are inverses; rename/move are
        # undoable).  destructiveHint=False signals the client that no
        # data is permanently lost.  idempotentHint omitted: repeated
        # renames with a different new_name have different outcomes.
        annotations=types.ToolAnnotations(
            destructiveHint=False,
            openWorldHint=True,
        ),
    ),
    types.Tool(
        name="delete_recording",
        description="Permanently and irreversibly delete a recording.",
        inputSchema={
            "type": "object",
            "properties": {
                "recording_id": {"type": "string"},
                "confirm": {
                    "type": "boolean",
                    "description": "Must be true; set only after the human has explicitly confirmed the permanent deletion.",  # noqa: E501
                },
            },
            "required": ["recording_id", "confirm"],
        },
        # Hard delete: irreversible.  destructiveHint=True + idempotentHint=False
        # because deleting an already-deleted ID will raise an error from Plaud
        # (not a no-op), so clients must not retry blindly.
        annotations=types.ToolAnnotations(
            destructiveHint=True,
            idempotentHint=False,
            openWorldHint=True,
        ),
    ),
    types.Tool(
        name="edit_transcript",
        description="Edit a recording's transcript. action='rename_speaker' relabels a speaker across all segments; action='correct' does a literal find-and-replace on transcript text (dry_run=true previews the match count).",  # noqa: E501
        inputSchema={
            "type": "object",
            "properties": {
                "recording_id": {"type": "string"},
                "action": {
                    "type": "string",
                    "enum": ["rename_speaker", "correct"],
                },
                "original_label": {
                    "type": "string",
                    "description": "Existing speaker label; required for action=rename_speaker",
                },
                "new_name": {
                    "type": "string",
                    "description": "Replacement speaker name; required for action=rename_speaker",
                },
                "find": {
                    "type": "string",
                    "description": "Exact literal text to find (case-sensitive); required for action=correct",  # noqa: E501
                },
                "replace": {
                    "type": "string",
                    "description": "Replacement text, may be empty to delete; required for action=correct",
                },
                "dry_run": {
                    "type": "boolean",
                    "description": "action=correct only: return the match count without editing",
                },
            },
            "required": ["recording_id", "action"],
        },
        # Reversible content edit — rerun with swapped args to undo.
        # idempotentHint omitted: the two actions have different idempotency
        # (rename_speaker is a no-op on rerun; correct errors on a second
        # identical rerun since the text is already replaced).
        annotations=types.ToolAnnotations(
            destructiveHint=False,
            openWorldHint=True,
        ),
    ),
    types.Tool(
        name="upload_recording",
        description="Upload a local audio file to Plaud.",
        inputSchema={
            "type": "object",
            "properties": {
                "file_path": {
                    "type": "string",
                    "description": "Absolute path to the audio file",
                },
                "title": {
                    "type": "string",
                    "description": "Recording title; defaults to file stem",
                },
                "folder_id": {
                    "type": "string",
                    "description": "Folder ID (from `list_folders`) to assign after upload",
                },
                "start_time": {
                    "type": ["integer", "string"],
                    "description": "Millisecond epoch integer or ISO 8601 string; defaults to now",
                },
                "timezone_offset": {
                    "type": "number",
                    "description": "UTC offset in hours; defaults to local system offset",
                },
            },
            "required": ["file_path"],
        },
        # Additive — creates a new recording; does not modify existing data.
        # idempotentHint omitted: uploading the same file twice creates two
        # separate recordings, so the operation is not idempotent.
        annotations=types.ToolAnnotations(
            destructiveHint=False,
            openWorldHint=True,
        ),
    ),
    types.Tool(
        name="list_folders",
        description="List Plaud folders, returning id, name, color, and icon for each.",
        inputSchema={
            "type": "object",
            "properties": {},
        },
        # Pure read — same rationale as browse_recordings / get_recording.
        annotations=types.ToolAnnotations(
            readOnlyHint=True,
            openWorldHint=True,
        ),
    ),
    types.Tool(
        name="process_recording",
        description="Trigger transcription and summarization for a recording; the `wait` mode controls how long to block (default: transcript).",  # noqa: E501
        inputSchema={
            "type": "object",
            "properties": {
                "recording_id": {"type": "string"},
                "wait": {
                    "type": "string",
                    "enum": ["none", "transcript", "summary"],
                    "default": "transcript",
                    "description": "How long to block: none/transcript/summary; waits are bounded and return status='still_processing' if not done in time — poll get_recording or retry.",  # noqa: E501
                },
                "template_type": {
                    "type": "string",
                    "description": "Summary template (e.g. 'AUTO-SELECT', 'MEETING')",
                },
                "language": {
                    "type": "string",
                    "description": "BCP-47 primary subtag (e.g. 'en', 'zh'); use 'auto' to detect",
                },
                "diarization": {
                    "type": "boolean",
                    "description": "Enable speaker diarization",
                },
                "llm": {
                    "type": "string",
                    "description": "LLM identifier for summarization",
                },
            },
            "required": ["recording_id"],
        },
        # Additive compute — triggers AI processing; does not delete or
        # overwrite existing user data (the transcript/summary are new artifacts).
        # idempotentHint=True: re-triggering on an already-processed recording
        # is a no-op on the Plaud side (the existing transcript is kept).
        annotations=types.ToolAnnotations(
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=True,
        ),
    ),
    types.Tool(
        name="merge_recordings",
        description="Merge two or more recordings into a single new recording, blocking until the merge job completes.",  # noqa: E501
        inputSchema={
            "type": "object",
            "properties": {
                "recording_ids": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "IDs to merge in order (minimum 2)",
                },
                "title": {
                    "type": "string",
                    "description": "Title for the merged recording",
                },
            },
            "required": ["recording_ids", "title"],
        },
        # Creates a new merged recording; the source recordings remain.
        # destructiveHint=False: sources are not deleted by the merge itself.
        # idempotentHint omitted: merging the same IDs twice creates two
        # separate merged recordings (not idempotent).
        annotations=types.ToolAnnotations(
            destructiveHint=False,
            openWorldHint=True,
        ),
    ),
    types.Tool(
        name="edit_summary",
        description="Edit a recording's AI summary (must already have a generated summary). action='correct' does a literal find-and-replace; action='replace' overwrites the whole summary with new markdown.",  # noqa: E501
        inputSchema={
            "type": "object",
            "properties": {
                "recording_id": {"type": "string"},
                "action": {
                    "type": "string",
                    "enum": ["correct", "replace"],
                },
                "find": {
                    "type": "string",
                    "description": "Exact text to find (case-sensitive, literal); required for action=correct",  # noqa: E501
                },
                "replace": {
                    "type": "string",
                    "description": "Replacement text (may be empty to delete); required for action=correct",
                },
                "content": {
                    "type": "string",
                    "description": "Full replacement summary markdown; required for action=replace",
                },
                "dry_run": {
                    "type": "boolean",
                    "description": "action=correct only: return the match count without editing",
                },
            },
            "required": ["recording_id", "action"],
        },
        # Reversible content edit — a 'correct' can be undone by re-running with
        # swapped find/replace; a 'replace' overwrites, but the prior text can be
        # re-supplied.  idempotentHint omitted: a second 'correct' with the same
        # find returns "no occurrences" (an error), so it is not a no-op.
        annotations=types.ToolAnnotations(
            destructiveHint=False,
            openWorldHint=True,
        ),
    ),
    types.Tool(
        name="mutate_folder",
        description="Manage Plaud folders: create a new folder, edit an existing one's name/color/icon, or delete one. To move a recording into a folder, use mutate_recording(action='move') instead.",  # noqa: E501
        inputSchema={
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["create", "edit", "delete"],
                },
                "folder_id": {
                    "type": "string",
                    "description": "Folder ID (from `list_folders`); required for edit and delete",
                },
                "name": {
                    "type": "string",
                    "description": "Folder name; required for create, optional for edit",
                },
                "color": {
                    "type": "string",
                    "description": "Hex color (e.g. '#4c8eff'); optional",
                },
                "icon": {
                    "type": "string",
                    "description": "Icon glyph codepoint (e.g. 'e627'); optional",
                },
                "confirm": {
                    "type": "boolean",
                    "description": "Required for action=delete; confirm only after the human agrees the folder should be deleted.",  # noqa: E501
                },
            },
            "required": ["action"],
        },
        # create/edit are reversible; delete is irreversible for the folder
        # itself (recordings survive), so it is gated by a required confirm=true
        # in the handler.  destructiveHint=True flags the delete path to clients;
        # idempotentHint=False because deleting a missing folder errors, not no-ops.
        annotations=types.ToolAnnotations(
            destructiveHint=True,
            idempotentHint=False,
            openWorldHint=True,
        ),
    ),
]


def _make_server() -> Server:
    store = SessionStore()
    # One SessionManager per server process so the in-memory keyring cache
    # added in v0.1.22 actually applies to MCP tool calls.  Previously this
    # constructed a fresh SessionManager (and thus a fresh empty cache) on
    # every tool invocation, defeating the cache and doubling keyring reads.
    manager = SessionManager(store)

    def get_client() -> PlaudClient | None:
        # store.load() also fronts the keyring; we keep it as the cheap
        # "is there any session at all?" probe before constructing the client.
        # SessionManager.require() (called inside PlaudClient.request paths)
        # validates expiry against the in-memory cache after the first hit.
        if store.load() is None:
            return None
        return PlaudClient(manager)

    handlers = build_handlers(get_client)
    server = Server("plaud-mcp")

    @server.list_tools()
    async def list_tools() -> list[types.Tool]:
        return _TOOLS

    @server.call_tool()
    async def call_tool(name: str, arguments: dict[str, Any]) -> types.CallToolResult:
        # #139: the MCP SDK's call_tool decorator hard-codes isError=False
        # whenever the registered handler returns a plain content list (see
        # mcp.server.lowlevel.server.Server.call_tool) — only an explicit
        # types.CallToolResult lets us control isError.  Every one of the 3
        # return paths below must therefore build the CallToolResult itself so
        # that refused deletes, session-expired, and other tool-level errors
        # are delivered to clients as errors instead of silent "successes".
        handler = handlers.get(name)
        if handler is None:
            payload: dict[str, Any] = {"error": f"Unknown tool: {name}"}
            return types.CallToolResult(
                content=[types.TextContent(type="text", text=json.dumps(payload, separators=(",", ":")))],
                isError=True,
            )
        try:
            # Wave 2 / C2: run the synchronous handler in a worker thread so
            # blocking network I/O (PlaudClient HTTP calls, keyring reads,
            # wait_for_transcription polling) does not stall the asyncio event
            # loop.  Other in-flight requests (e.g. list_tools) remain
            # responsive while a long upload or transcode waits in its thread.
            #
            # TypeError propagation: a TypeError raised *inside* the thread
            # (bad kwargs forwarded by the MCP framework) propagates out of the
            # ``await`` and is caught by the except clause below — identical
            # behaviour to the previous synchronous call.
            result = await asyncio.to_thread(handler, **arguments)
            text = result["content"][0]["text"]
            is_error = bool(result.get("isError"))
        except TypeError as exc:
            # The MCP framework or a misbehaving client passed unexpected / missing
            # keyword arguments.  Returning a structured validation error keeps the
            # raw TypeError inside the server process and lets the caller
            # self-correct.  Shape mirrors _error_result() in mcp.py exactly:
            # {"error": ..., "error_code": "validation", "retryable": false}.
            payload = {
                "error": f"Invalid arguments for tool '{name}': {exc}",
                "error_code": "validation",
                "retryable": False,
            }
            return types.CallToolResult(
                content=[types.TextContent(type="text", text=json.dumps(payload, separators=(",", ":")))],
                isError=True,
            )
        return types.CallToolResult(
            content=[types.TextContent(type="text", text=text)],
            isError=is_error,
        )

    return server


async def _run() -> None:
    server = _make_server()
    async with mcp.server.stdio.stdio_server() as (read_stream, write_stream):
        await server.run(
            read_stream,
            write_stream,
            InitializationOptions(
                server_name="plaud-mcp",
                server_version=__version__,
                capabilities=server.get_capabilities(
                    notification_options=NotificationOptions(),
                    experimental_capabilities={},
                ),
            ),
        )


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="plaud-mcp",
        description="Plaud Tools MCP server (stdio transport).",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {__version__}",
    )
    parser.parse_args()
    _setup_mcp_logging()
    try:
        asyncio.run(_run())
    except KeyboardInterrupt:
        pass
    except Exception as exc:  # noqa: BLE001
        logging.exception("plaud-mcp crashed")
        print(f"plaud-mcp: error: {exc}", file=sys.stderr)
        sys.exit(1)
