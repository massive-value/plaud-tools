"""Python MCP server entry point for plaud-tools."""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from typing import Any

import mcp.server.stdio
import mcp.types as types
from mcp.server.lowlevel import NotificationOptions, Server
from mcp.server.models import InitializationOptions

from . import __version__
from .client import PlaudClient
from .mcp import build_handlers
from .session import SessionManager, SessionStore

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
                    "description": "Folder ID (from `list_folders`); pass empty string for unfiled recordings",
                },
                "after": {
                    "type": "integer",
                    "default": 0,
                    "description": "Cursor from next_after of a previous response",
                },
            },
        },
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
    ),
    types.Tool(
        name="mutate_recording",
        description="Apply a state change to a recording: rename, trash, restore, delete, move, or rename_speaker.",
        inputSchema={
            "type": "object",
            "properties": {
                "recording_id": {"type": "string"},
                "mutation": {
                    "type": "string",
                    "enum": ["rename", "trash", "restore", "delete", "move", "rename_speaker"],
                },
                "new_name": {
                    "type": "string",
                    "description": "Required for rename and rename_speaker",
                },
                "folder_id": {
                    "type": "string",
                    "description": "Folder ID (from `list_folders`); required for move; use '-' to clear",
                },
                "original_label": {
                    "type": "string",
                    "description": "Required for rename_speaker: existing speaker label",
                },
            },
            "required": ["recording_id", "mutation"],
        },
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
    ),
    types.Tool(
        name="list_folders",
        description="List Plaud folders, returning id, name, color, and icon for each.",
        inputSchema={
            "type": "object",
            "properties": {},
        },
    ),
    types.Tool(
        name="process_recording",
        description="Trigger transcription and summarization for a recording, blocking until both jobs complete.",
        inputSchema={
            "type": "object",
            "properties": {
                "recording_id": {"type": "string"},
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
    ),
    types.Tool(
        name="merge_recordings",
        description="Merge two or more recordings into a single new recording, blocking until the merge job completes.",
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
    ),
]


def _make_server() -> Server:
    store = SessionStore()

    def get_client() -> PlaudClient | None:
        if store.load() is None:
            return None
        return PlaudClient(SessionManager(store))

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
        result = handler(**arguments)
        text = result["content"][0]["text"]
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
    try:
        asyncio.run(_run())
    except KeyboardInterrupt:
        pass
    except Exception as exc:  # noqa: BLE001
        print(f"plaud-mcp: error: {exc}", file=sys.stderr)
        sys.exit(1)
