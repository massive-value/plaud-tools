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

import jsonschema
import mcp.server.stdio
import mcp.types as types
from mcp.server.caching import CacheableMethod, CacheHint
from mcp.server.lowlevel import Server
from mcp_types.version import MODERN_PROTOCOL_VERSIONS

from .. import __version__
from ..core.appdata import mcp_log as _mcp_log_path
from ..core.client import DEFAULT_TRANSCRIPT_BLOCK, TRANSCRIPT_BLOCKS, PlaudClient
from ..core.session import SessionManager, SessionStore
from .mcp import (
    DEFAULT_TRANSCRIPT_UTTERANCES,
    MAX_BROWSE_LIMIT,
    MAX_TRANSCRIPT_UTTERANCES,
    build_handlers,
)

log = logging.getLogger(__name__)


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
# open_world_hint=True is set on every tool: all calls interact with the external
# Plaud service and may observe or affect state not visible in this conversation.
# ---------------------------------------------------------------------------

# outputSchema for the read tools, so clients on newer protocol versions get
# typed structuredContent.  The same JSON also goes out as text for older
# clients (see mcp._json_result).  Loose on purpose: only the fields a caller
# relies on are described, and extra fields are allowed.
_NULLABLE_STRING = {"type": ["string", "null"]}
_NULLABLE_INT = {"type": ["integer", "null"]}

_BROWSE_OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "items": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "title": {"type": "string"},
                    "date": {"type": "string"},
                    "duration_minutes": {"type": "integer"},
                    "has_transcript": {"type": "boolean"},
                    "has_summary": {"type": "boolean"},
                    "folder_id": _NULLABLE_STRING,
                },
                "required": ["id", "title"],
            },
        },
        "next_after": _NULLABLE_INT,
    },
    "required": ["items", "next_after"],
}

_GET_RECORDING_OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "id": {"type": "string"},
        "title": {"type": "string"},
        "date": {"type": "string"},
        "duration_minutes": {"type": "integer"},
        "folder_id": _NULLABLE_STRING,
        "is_trans": {"type": "boolean"},
        "is_summary": {"type": "boolean"},
        "is_trash": {"type": "boolean"},
        "headline": _NULLABLE_STRING,
        "language": _NULLABLE_STRING,
        "speakers": {"type": "array", "items": {"type": "string"}},
        "audio_url": _NULLABLE_STRING,
        "transcript": {"type": "string"},
        "transcript_segments": {"type": "array", "items": {"type": "object"}},
        "transcript_has_more": {"type": "boolean"},
        "transcript_next_after": _NULLABLE_INT,
        "transcript_fingerprint": _NULLABLE_STRING,
        "summary": _NULLABLE_STRING,
        "notes": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["id"],
}

_LIST_FOLDERS_OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "folders": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "name": {"type": "string"},
                    "color": {"type": "string"},
                    "icon": {"type": "string"},
                },
                "required": ["id", "name"],
            },
        },
    },
    "required": ["folders"],
}

_TOOLS: list[types.Tool] = [
    types.Tool(
        name="browse_recordings",
        description="Page through Plaud recordings with optional filters.",
        input_schema={
            "type": "object",
            "properties": {
                "limit": {
                    "type": "integer",
                    "default": 20,
                    "minimum": 1,
                    "maximum": MAX_BROWSE_LIMIT,
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
        output_schema=_BROWSE_OUTPUT_SCHEMA,
        # Pure read — no writes, no side-effects.
        # idempotent_hint omitted: redundant when read_only_hint=True (reads are
        # inherently idempotent; stating it again adds noise without value).
        annotations=types.ToolAnnotations(
            title="Browse recordings",
            read_only_hint=True,
            open_world_hint=True,
        ),
    ),
    types.Tool(
        name="get_recording",
        description="Fetch full detail for one recording. Transcripts are paged: while transcript_has_more, call again with transcript_after=transcript_next_after. A changed transcript_fingerprint between pages means it was edited; restart from 0.",  # noqa: E501
        input_schema={
            "type": "object",
            "properties": {
                "recording_id": {"type": "string"},
                "include": {
                    "type": "array",
                    "items": {
                        "type": "string",
                        "enum": ["transcript", "segments", "speakers", "summary", "audio_url"],
                    },
                    "description": "Extra fields: transcript (text page), segments (structured page: index, speaker, text, start_ms/end_ms from recording start), speakers, summary, audio_url",  # noqa: E501
                },
                "transcript_after": {
                    "type": "integer",
                    "default": 0,
                    "minimum": 0,
                    "description": "Utterance index to start the transcript from; pass the transcript_next_after of a previous response to continue",  # noqa: E501
                },
                "transcript_limit": {
                    "type": "integer",
                    "default": DEFAULT_TRANSCRIPT_UTTERANCES,
                    "minimum": 1,
                    "maximum": MAX_TRANSCRIPT_UTTERANCES,
                    "description": f"Max utterances per page (default {DEFAULT_TRANSCRIPT_UTTERANCES})",  # noqa: E501
                },
                "transcript_block": {
                    "type": "string",
                    "enum": list(TRANSCRIPT_BLOCKS),
                    "default": DEFAULT_TRANSCRIPT_BLOCK,
                    "description": "Which transcript to return: 'transaction' (default; raw diarized transcript) or 'transaction_polish' (Plaud's AI-cleaned pass — filler words removed, punctuation repaired; same speakers and timestamps). Not every recording has a polished block; the response names what is available when the requested block is missing.",  # noqa: E501
                },
            },
            "required": ["recording_id"],
        },
        output_schema=_GET_RECORDING_OUTPUT_SCHEMA,
        # Pure read — same rationale as browse_recordings.
        annotations=types.ToolAnnotations(
            title="Get recording",
            read_only_hint=True,
            open_world_hint=True,
        ),
    ),
    types.Tool(
        name="mutate_recording",
        description="Apply a reversible state change to one or more recordings: rename, trash, restore, or move.",  # noqa: E501
        input_schema={
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
        # undoable).  destructive_hint=False signals the client that no
        # data is permanently lost.  idempotent_hint omitted: repeated
        # renames with a different new_name have different outcomes.
        annotations=types.ToolAnnotations(
            title="Rename, move, or trash recordings",
            destructive_hint=False,
            open_world_hint=True,
        ),
    ),
    types.Tool(
        name="delete_recording",
        description="Permanently and irreversibly delete a recording.",
        input_schema={
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
        # Hard delete: irreversible.  destructive_hint=True + idempotent_hint=False
        # because deleting an already-deleted ID will raise an error from Plaud
        # (not a no-op), so clients must not retry blindly.
        annotations=types.ToolAnnotations(
            title="Permanently delete recording",
            destructive_hint=True,
            idempotent_hint=False,
            open_world_hint=True,
        ),
    ),
    types.Tool(
        name="edit_transcript",
        description="Edit a recording's transcript. action='rename_speaker' relabels a speaker across all segments; action='correct' does a literal find-and-replace on transcript text (dry_run=true previews the match count).",  # noqa: E501
        input_schema={
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
        # Destructive: a correct can't always be undone by swapping find and
        # replace (replacing "Bob" with "Rob" merges with any "Rob" already
        # there, and an empty replace deletes text), and the old text is not
        # kept anywhere.  idempotent_hint omitted: rename_speaker is a no-op on
        # rerun, but a repeated correct errors since the text is already gone.
        annotations=types.ToolAnnotations(
            title="Edit transcript",
            destructive_hint=True,
            open_world_hint=True,
        ),
    ),
    types.Tool(
        name="upload_recording",
        description="Upload a local audio file to Plaud; returns the new recording_id.",
        input_schema={
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
        # idempotent_hint omitted: uploading the same file twice creates two
        # separate recordings, so the operation is not idempotent.
        annotations=types.ToolAnnotations(
            title="Upload audio file",
            destructive_hint=False,
            open_world_hint=True,
        ),
    ),
    types.Tool(
        name="list_folders",
        description="List Plaud folders, returning id, name, color, and icon for each.",
        input_schema={
            "type": "object",
            "properties": {},
        },
        output_schema=_LIST_FOLDERS_OUTPUT_SCHEMA,
        # Pure read — same rationale as browse_recordings / get_recording.
        annotations=types.ToolAnnotations(
            title="List folders",
            read_only_hint=True,
            open_world_hint=True,
        ),
    ),
    types.Tool(
        name="process_recording",
        description="Trigger transcription and summarization for a recording; the `wait` mode controls how long to block (default: transcript). A processed recording is left unchanged (returns already_processed=true).",  # noqa: E501
        input_schema={
            "type": "object",
            "properties": {
                "recording_id": {"type": "string"},
                "wait": {
                    "type": "string",
                    "enum": ["none", "transcript", "summary"],
                    "default": "transcript",
                    "description": "How long to block: none/transcript/summary. One ~90s budget; if unfinished returns status='still_processing' with a job handle. Then poll get_recording instead of calling again.",  # noqa: E501
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
        # idempotent_hint=True: re-triggering on an already-processed recording
        # is a no-op on the Plaud side (verified live: Plaud answers status 1
        # with the existing transcript/summary and ignores the new template),
        # and the handler reports it as already_processed=true.
        annotations=types.ToolAnnotations(
            title="Transcribe and summarize",
            destructive_hint=False,
            idempotent_hint=True,
            open_world_hint=True,
        ),
    ),
    types.Tool(
        name="merge_recordings",
        description="Merge two or more recordings into one new recording. If not done within ~90s, returns status='still_processing' with the merge job id; do not call again (it would merge twice).",  # noqa: E501
        input_schema={
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
        # destructive_hint=False: sources are not deleted by the merge itself.
        # idempotent_hint omitted: merging the same IDs twice creates two
        # separate merged recordings (not idempotent).
        annotations=types.ToolAnnotations(
            title="Merge recordings",
            destructive_hint=False,
            open_world_hint=True,
        ),
    ),
    types.Tool(
        name="edit_summary",
        description="Edit a recording's AI summary (must already have a generated summary). action='correct' does a literal find-and-replace; action='replace' overwrites the whole summary with new markdown.",  # noqa: E501
        input_schema={
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
        # Destructive: 'replace' overwrites the whole summary and Plaud keeps
        # no prior version, so unless the agent saved the old text it is gone;
        # 'correct' has the same can't-always-swap-back problem as
        # edit_transcript.  idempotent_hint omitted: a second 'correct' with the
        # same find returns "no occurrences" (an error), so it is not a no-op.
        annotations=types.ToolAnnotations(
            title="Edit summary",
            destructive_hint=True,
            open_world_hint=True,
        ),
    ),
    types.Tool(
        name="mutate_folder",
        description="Manage Plaud folders: create a new folder, edit an existing one's name/color/icon, or delete one. To move a recording into a folder, use mutate_recording(action='move') instead.",  # noqa: E501
        input_schema={
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
        # in the handler.  destructive_hint=True flags the delete path to clients;
        # idempotent_hint=False because deleting a missing folder errors, not no-ops.
        annotations=types.ToolAnnotations(
            title="Create, edit, or delete folder",
            destructive_hint=True,
            idempotent_hint=False,
            open_world_hint=True,
        ),
    ),
]


# Reject arguments a tool doesn't declare.  Without this an unexpected name
# reached the handler as a Python TypeError; now the schema check below
# reports it by name.
for _tool in _TOOLS:
    _tool.input_schema.setdefault("additionalProperties", False)

# One compiled validator per tool; the schemas never change at runtime.
_VALIDATORS: dict[str, jsonschema.protocols.Validator] = {
    tool.name: jsonschema.Draft202012Validator(tool.input_schema) for tool in _TOOLS
}

# tools/list never changes while the process runs, so clients that honour
# SEP-2549 cache hints (protocol 2026-07-28) may reuse it for an hour.  The
# listing holds nothing user-specific, hence "public".
_CACHE_HINTS: dict[CacheableMethod, CacheHint] = {
    "tools/list": CacheHint(ttl_ms=60 * 60 * 1000, scope="public")
}


def _error_payload(message: str, error_code: str) -> types.CallToolResult:
    """A tool-level error in the same {error, error_code, retryable} shape mcp.py uses."""
    payload = {"error": message, "error_code": error_code, "retryable": False}
    return types.CallToolResult(
        content=[types.TextContent(type="text", text=json.dumps(payload, separators=(",", ":")))],
        is_error=True,
    )


def _argument_error(name: str, arguments: dict[str, Any]) -> str | None:
    """Return a readable message for the first schema violation, or None if valid."""
    error = jsonschema.exceptions.best_match(_VALIDATORS[name].iter_errors(arguments))
    if error is None:
        return None
    where = ".".join(str(part) for part in error.absolute_path)
    return f"Invalid arguments for tool '{name}': {f'{where}: ' if where else ''}{error.message}"


def _make_server() -> Server:
    store = SessionStore()
    # One SessionManager per server process so the in-memory session cache
    # applies to every tool call.  Signed-out detection happens inside
    # require() (PlaudSessionExpiredError), so there is no per-call
    # Credential Manager probe here.
    manager = SessionManager(store)

    def get_client() -> PlaudClient | None:
        return PlaudClient(manager)

    handlers = build_handlers(get_client)
    seen_versions: set[str] = set()

    def _log_protocol_version(ctx: Any) -> None:
        """Log each protocol version a client speaks, once per process.

        Shows in mcp.log when Claude clients move from the handshake era
        (2025-11-25 and earlier) to the stateless 2026-07-28 era.
        """
        version = getattr(ctx, "protocol_version", None)
        if not isinstance(version, str) or version in seen_versions:
            return
        seen_versions.add(version)
        era = "modern" if version in MODERN_PROTOCOL_VERSIONS else "legacy"
        client_params = getattr(getattr(ctx, "session", None), "client_params", None)
        client_info = getattr(client_params, "client_info", None)
        client = f"{getattr(client_info, 'name', '?')}/{getattr(client_info, 'version', '?')}"
        log.info("MCP client protocol_version=%s era=%s client=%s", version, era, client)

    async def list_tools(ctx: Any, params: Any) -> types.ListToolsResult:
        _log_protocol_version(ctx)
        return types.ListToolsResult(tools=_TOOLS)

    async def call_tool(ctx: Any, params: types.CallToolRequestParams) -> types.CallToolResult:
        # #139 (and the mcp v2 migration, #200): the SDK never auto-wraps a
        # bare return into a CallToolResult, so every path below builds one
        # itself.  Tool-level failures (refused deletes, session expired, bad
        # arguments, bugs) all come back as is_error=True results carrying our
        # own message, never as a protocol error or the SDK's generic
        # "Error executing tool" text.
        _log_protocol_version(ctx)
        name = params.name
        # Many LLM clients send null for optional fields they aren't using.
        # Treat that as "not passed" so the handler default applies and the
        # schema check doesn't reject it as the wrong type.
        arguments = {k: v for k, v in (params.arguments or {}).items() if v is not None}
        handler = handlers.get(name)
        if handler is None:
            return _error_payload(f"Unknown tool: {name}", "invalid_arguments")
        problem = _argument_error(name, arguments)
        if problem is not None:
            return _error_payload(problem, "invalid_arguments")
        try:
            # Wave 2 / C2: run the synchronous handler in a worker thread so
            # blocking network I/O (PlaudClient HTTP calls, keyring reads,
            # wait_for_transcription polling) does not stall the asyncio event
            # loop.  Other in-flight requests (e.g. list_tools) remain
            # responsive while a long upload or transcode waits in its thread.
            result = await asyncio.to_thread(handler, **arguments)
        except Exception:  # noqa: BLE001 — last line of defence, see below
            # mcp.py maps every expected failure to a structured error, so
            # reaching here means a bug.  Log the traceback and still answer
            # with a structured result the model can read.
            log.exception("tool %s raised an unexpected exception", name)
            return _error_payload(
                f"Internal error in tool '{name}'. Details are in the plaud-mcp log.", "internal"
            )
        return types.CallToolResult(
            content=[types.TextContent(type="text", text=result["content"][0]["text"])],
            structured_content=result.get("structuredContent"),
            is_error=bool(result.get("isError")),
        )

    return Server(
        "plaud-mcp",
        version=__version__,
        on_list_tools=list_tools,
        on_call_tool=call_tool,
        cache_hints=_CACHE_HINTS,
    )


async def _run() -> None:
    server = _make_server()
    async with mcp.server.stdio.stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


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
