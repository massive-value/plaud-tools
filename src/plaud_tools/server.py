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

from . import __version__
from .appdata import mcp_log as _mcp_log_path
from .client import PlaudClient
from .mcp import build_handlers
from .session import SessionManager, SessionStore


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
                    "default": 50,
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
                    "description": "Cursor from next_after of a previous response",
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
        description="Apply a reversible state change to a recording: rename, trash, restore, or move.",
        inputSchema={
            "type": "object",
            "properties": {
                "recording_id": {"type": "string"},
                "mutation": {
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
            "required": ["recording_id", "mutation"],
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
                    "description": (
                        "Must be true to proceed. Set this only after the human has "
                        "explicitly confirmed they want to permanently delete the recording. "
                        "Re-invoke with confirm=true after obtaining confirmation."
                    ),
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
        name="rename_speaker",
        description="Rename a speaker label across all transcript segments for a recording.",
        inputSchema={
            "type": "object",
            "properties": {
                "recording_id": {"type": "string"},
                "original_label": {
                    "type": "string",
                    "description": "Existing speaker label to replace",
                },
                "new_name": {
                    "type": "string",
                    "description": "Replacement speaker name",
                },
            },
            "required": ["recording_id", "original_label", "new_name"],
        },
        # Additive label edit — reversible by calling again with swapped args.
        # idempotentHint=True: applying the same rename twice leaves the
        # transcript in the same final state (all occurrences of original_label
        # become new_name; the second call is a no-op if the label no longer exists).
        annotations=types.ToolAnnotations(
            destructiveHint=False,
            idempotentHint=True,
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
                    "description": (
                        "How long to block after the transcribe/summarize request is accepted: "
                        "'none' returns immediately with {recording_id, accepted}, "
                        "'transcript' waits only for transcript readiness, and "
                        "'summary' waits for both transcript and summary."
                    ),
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
    async def call_tool(name: str, arguments: dict[str, Any]) -> list[types.TextContent]:
        handler = handlers.get(name)
        if handler is None:
            return [types.TextContent(type="text", text=json.dumps({"error": f"Unknown tool: {name}"}))]
        try:
            result = handler(**arguments)
            text = result["content"][0]["text"]
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
            return [types.TextContent(type="text", text=json.dumps(payload, indent=2))]
        return [types.TextContent(type="text", text=text)]

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
