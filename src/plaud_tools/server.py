"""Python MCP server entry point for plaud-tools."""
from __future__ import annotations

import asyncio
import json
from typing import Any

import mcp.server.stdio
import mcp.types as types
from mcp.server.lowlevel import NotificationOptions, Server
from mcp.server.models import InitializationOptions

from .client import PlaudClient
from .mcp import build_handlers
from .session import SessionManager, SessionStore

_TOOLS: list[types.Tool] = [
    types.Tool(
        name="browse_recordings",
        description=(
            "List and search Plaud recordings with optional filters. "
            "Returns a curated summary for each match."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "limit": {
                    "type": "integer",
                    "default": 50,
                    "description": "Max results to return",
                },
                "since": {
                    "type": "string",
                    "description": "ISO 8601 start-date filter (e.g. 2025-01-01)",
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
                    "description": "Folder ID filter; pass empty string to match unfiled recordings",
                },
                "after": {
                    "type": "integer",
                    "default": 0,
                    "description": "Pagination offset (number of results to skip)",
                },
            },
        },
    ),
    types.Tool(
        name="get_recording",
        description=(
            "Fetch full detail for one recording. "
            "Pass `include` to opt in to large fields: transcript, speakers, summary."
        ),
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
                    "description": "Extra fields to include in the response",
                },
            },
            "required": ["recording_id"],
        },
    ),
    types.Tool(
        name="mutate_recording",
        description=(
            "Apply a state change to a recording: rename, trash, restore, delete, "
            "move to a folder, or rename a speaker in the transcript."
        ),
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
                    "description": "Required for move; use '-' to clear folder assignment",
                },
                "original_label": {
                    "type": "string",
                    "description": "Required for rename_speaker — the existing speaker label to replace",
                },
            },
            "required": ["recording_id", "mutation"],
        },
    ),
    types.Tool(
        name="upload_recording",
        description=(
            "Upload a local audio file to Plaud. "
            "Non-native formats (e.g. WAV, FLAC) are transcoded to MP3 via ffmpeg."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "file_path": {
                    "type": "string",
                    "description": "Absolute path to the audio file",
                },
                "title": {
                    "type": "string",
                    "description": "Recording title; defaults to the file stem",
                },
                "folder_id": {
                    "type": "string",
                    "description": "Folder to assign the recording to after upload",
                },
            },
            "required": ["file_path"],
        },
    ),
    types.Tool(
        name="process_recording",
        description=(
            "Trigger transcription and summarization for a recording, "
            "then block until the job completes."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "recording_id": {"type": "string"},
                "template_type": {
                    "type": "string",
                    "description": "Summary template name",
                },
                "language": {
                    "type": "string",
                    "description": "Transcript language code (e.g. en-US)",
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
                server_version="0.2.0",
                capabilities=server.get_capabilities(
                    notification_options=NotificationOptions(),
                    experimental_capabilities={},
                ),
            ),
        )


def main() -> None:
    asyncio.run(_run())
