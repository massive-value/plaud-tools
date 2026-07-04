"""Tests for the MCP server module."""

from __future__ import annotations

import asyncio
import json
import logging
import sys
import time

import mcp.types as mcp_types

from plaud_tools.server import _TOOLS, _make_server, _mcp_log_path, _setup_mcp_logging

_EXPECTED_TOOL_NAMES = {
    "browse_recordings",
    "get_recording",
    "mutate_recording",
    "delete_recording",
    "rename_speaker",
    "correct_transcript",
    "upload_recording",
    "process_recording",
    "list_folders",
    "merge_recordings",
    "edit_summary",
    "mutate_folder",
}


def test_server_exposes_expected_tools():
    assert {t.name for t in _TOOLS} == _EXPECTED_TOOL_NAMES


def test_server_edit_summary_requires_recording_id_and_operation():
    tool = next(t for t in _TOOLS if t.name == "edit_summary")
    assert set(tool.inputSchema["required"]) == {"recording_id", "operation"}
    assert tool.inputSchema["properties"]["operation"]["enum"] == ["correct", "replace"]


def test_server_mutate_folder_requires_only_action():
    tool = next(t for t in _TOOLS if t.name == "mutate_folder")
    assert tool.inputSchema["required"] == ["action"]
    assert set(tool.inputSchema["properties"]["action"]["enum"]) == {"create", "edit", "delete"}


def test_server_mutate_folder_is_flagged_destructive():
    # The delete path is irreversible for the folder; the annotation warns clients.
    tool = next(t for t in _TOOLS if t.name == "mutate_folder")
    assert tool.annotations.destructiveHint is True


def test_server_list_folders_has_no_required_fields():
    tool = next(t for t in _TOOLS if t.name == "list_folders")
    assert "required" not in tool.inputSchema


def test_server_browse_recordings_has_no_required_fields():
    tool = next(t for t in _TOOLS if t.name == "browse_recordings")
    assert "required" not in tool.inputSchema


def test_server_get_recording_requires_recording_id():
    tool = next(t for t in _TOOLS if t.name == "get_recording")
    assert "recording_id" in tool.inputSchema["required"]


def test_server_mutate_recording_requires_recording_id_and_mutation():
    tool = next(t for t in _TOOLS if t.name == "mutate_recording")
    assert set(tool.inputSchema["required"]) == {"recording_id", "mutation"}


def test_server_mutate_recording_enum_excludes_delete_and_rename_speaker():
    tool = next(t for t in _TOOLS if t.name == "mutate_recording")
    enum_values = tool.inputSchema["properties"]["mutation"]["enum"]
    assert "delete" not in enum_values
    assert "rename_speaker" not in enum_values
    assert set(enum_values) == {"rename", "trash", "restore", "move"}


def test_server_mutate_recording_has_clear_folder_param():
    tool = next(t for t in _TOOLS if t.name == "mutate_recording")
    assert "clear_folder" in tool.inputSchema["properties"]
    assert tool.inputSchema["properties"]["clear_folder"]["type"] == "boolean"


def test_server_mutate_recording_has_no_original_label_param():
    """original_label is no longer a mutate_recording param — it moved to rename_speaker."""
    tool = next(t for t in _TOOLS if t.name == "mutate_recording")
    assert "original_label" not in tool.inputSchema["properties"]


def test_server_delete_recording_requires_recording_id():
    # D4: confirm is now a required field alongside recording_id.
    tool = next(t for t in _TOOLS if t.name == "delete_recording")
    assert set(tool.inputSchema["required"]) == {"recording_id", "confirm"}


def test_server_rename_speaker_requires_all_three_params():
    tool = next(t for t in _TOOLS if t.name == "rename_speaker")
    assert set(tool.inputSchema["required"]) == {"recording_id", "original_label", "new_name"}


def test_server_upload_recording_requires_file_path():
    tool = next(t for t in _TOOLS if t.name == "upload_recording")
    assert "file_path" in tool.inputSchema["required"]


def test_server_process_recording_requires_recording_id():
    tool = next(t for t in _TOOLS if t.name == "process_recording")
    assert "recording_id" in tool.inputSchema["required"]


def test_server_process_recording_wait_schema_defaults_to_transcript():
    tool = next(t for t in _TOOLS if t.name == "process_recording")
    wait_schema = tool.inputSchema["properties"]["wait"]
    assert wait_schema["enum"] == ["none", "transcript", "summary"]
    assert wait_schema["default"] == "transcript"


def test_server_constructs_without_error():
    server = _make_server()
    assert server is not None


def test_mcp_log_path_uses_localappdata(monkeypatch, tmp_path):
    # appdata.data_dir() branches on sys.platform; pin to win32 so the
    # LOCALAPPDATA env-var override is honoured on Linux CI as well. This
    # test pins the Windows-only LOCALAPPDATA behaviour by name.
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    p = _mcp_log_path()
    assert p == tmp_path / "PlaudTools" / "mcp.log"


def test_setup_mcp_logging_writes_startup_banner(monkeypatch, tmp_path):
    """Pin issue #78 fix: the MCP server now leaves an on-disk audit trail."""
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    root = logging.getLogger()
    saved_handlers = root.handlers[:]
    saved_level = root.level
    for h in saved_handlers:
        root.removeHandler(h)
    try:
        _setup_mcp_logging()
        for h in root.handlers:
            h.flush()
        log_path = tmp_path / "PlaudTools" / "mcp.log"
        assert log_path.exists(), f"expected {log_path} to be created"
        contents = log_path.read_text(encoding="utf-8")
        assert "plaud-mcp" in contents
        assert "starting" in contents
        assert "pid=" in contents
    finally:
        for h in root.handlers[:]:
            root.removeHandler(h)
        for h in saved_handlers:
            root.addHandler(h)
        root.setLevel(saved_level)


def test_setup_mcp_logging_attaches_even_when_root_already_has_handlers(monkeypatch, tmp_path):
    """Regression for the v0.2.2 pip-install path: an earlier import
    (or pip's own logging) can leave the root logger pre-configured.
    ``logging.basicConfig`` is a silent no-op in that case, which made the
    pip-installed plaud-mcp v0.2.2 never write its mcp.log banner even
    though _setup_mcp_logging ran.  We now attach directly.
    """
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    root = logging.getLogger()
    saved_handlers = root.handlers[:]
    saved_level = root.level
    for h in saved_handlers:
        root.removeHandler(h)
    # Simulate "some earlier import configured logging" by attaching a dummy handler.
    dummy = logging.StreamHandler()
    root.addHandler(dummy)
    try:
        _setup_mcp_logging()
        for h in root.handlers:
            h.flush()
        log_path = tmp_path / "PlaudTools" / "mcp.log"
        assert log_path.exists(), (
            "_setup_mcp_logging must attach the file handler even when the "
            "root logger already has handlers (the v0.2.2 pip-install regression)."
        )
        assert "plaud-mcp" in log_path.read_text(encoding="utf-8")
        # And the pre-existing handler must still be there — we add, not replace.
        assert dummy in root.handlers
    finally:
        for h in root.handlers[:]:
            root.removeHandler(h)
        for h in saved_handlers:
            root.addHandler(h)
        root.setLevel(saved_level)


def test_make_server_constructs_one_session_manager(monkeypatch):
    """Pin per-process SessionManager sharing.

    Regression for the v0.2.0 bug where ``get_client`` constructed a fresh
    ``SessionManager(store)`` on every MCP tool call — defeating the
    in-memory keyring cache added in v0.1.22 and forcing the 30-day buffer
    check to re-validate from cold state every call.

    The fix hoists construction into ``_make_server`` itself, so the count
    is exactly one immediately after the function returns.
    """
    from plaud_tools import server as srv_mod

    instances: list[object] = []
    original_init = srv_mod.SessionManager.__init__

    def counting_init(self, store):  # type: ignore[no-untyped-def]
        instances.append(self)
        original_init(self, store)

    monkeypatch.setattr(srv_mod.SessionManager, "__init__", counting_init)

    _make_server()
    assert len(instances) == 1, (
        f"_make_server() must construct exactly one SessionManager per "
        f"process; got {len(instances)}.  This regression guards against "
        f"re-introducing per-call SessionManager(store) inside get_client."
    )


# ---------------------------------------------------------------------------
# A6: Structured TypeError guard in call_tool (Wave 0 audit)
# ---------------------------------------------------------------------------


class TestCallToolTypeErrorGuard:
    """call_tool must return a structured validation error — not raise — when the
    MCP framework passes an argument name that the underlying handler does not
    accept.  The error payload must match the project-standard shape used by
    _error_result() in mcp.py: {error, error_code, retryable}.

    The test exercises the full call_tool path by invoking the handler registered
    with the MCP SDK (via server.request_handlers[CallToolRequest]) so that the
    exact same code path exercised by the live server is under test.
    """

    def _invoke(self, tool_name: str, arguments: dict) -> str:
        """Build a CallToolRequest, run it through the SDK handler, and return the
        text of the first TextContent in the result."""
        server = _make_server()
        sdk_handler = server.request_handlers[mcp_types.CallToolRequest]
        req = mcp_types.CallToolRequest(
            method="tools/call",
            params=mcp_types.CallToolRequestParams(name=tool_name, arguments=arguments),
        )
        result = asyncio.run(sdk_handler(req))
        return result.root.content[0].text

    def test_bogus_kwarg_returns_validation_error_code(self):
        """A kwarg unknown to the handler must produce error_code='validation'."""
        text = self._invoke("list_folders", {"bogus_kwarg": "unexpected"})
        payload = json.loads(text)
        assert payload["error_code"] == "validation", (
            f"Expected error_code='validation', got {payload.get('error_code')!r}. Full payload: {payload}"
        )

    def test_bogus_kwarg_retryable_is_false(self):
        """A validation error from a bad kwarg must not be marked as retryable."""
        text = self._invoke("list_folders", {"bogus_kwarg": "unexpected"})
        payload = json.loads(text)
        assert payload["retryable"] is False

    def test_bogus_kwarg_error_message_names_tool(self):
        """The human-readable error string must include the tool name so agents can self-correct."""
        text = self._invoke("browse_recordings", {"not_a_real_param": 42})
        payload = json.loads(text)
        assert "browse_recordings" in payload["error"], (
            f"Expected tool name in error message; got: {payload.get('error')!r}"
        )

    def test_bogus_kwarg_does_not_raise(self):
        """call_tool must never propagate a raw TypeError to the caller."""
        # If this assertion fails the SDK catches the exception and wraps it in a
        # plain-text error message — the test below would also fail — but this
        # assertion documents the requirement explicitly.
        try:
            self._invoke("get_recording", {"recording_id": "abc", "unknown_field": True})
        except TypeError:
            raise AssertionError("call_tool raised TypeError instead of returning a structured error payload")

    def test_bogus_kwarg_text_is_valid_json(self):
        """The TextContent text for a bad-kwarg call must be parseable JSON."""
        text = self._invoke("list_folders", {"garbage": "value"})
        try:
            json.loads(text)
        except json.JSONDecodeError as exc:
            raise AssertionError(f"call_tool returned non-JSON text for bogus kwarg: {text!r}") from exc


# ---------------------------------------------------------------------------
# C2: Non-blocking MCP server — responsiveness test (Wave 2)
# ---------------------------------------------------------------------------


class TestCallToolNonBlocking:
    """Prove that call_tool runs handlers via asyncio.to_thread, keeping the
    event loop responsive while a slow handler blocks in its worker thread.

    Strategy: register a stub handler that sleeps for SLOW_SLEEP_S.  Fire it
    as an asyncio task, then immediately call list_tools() (a fast coroutine).
    Assert list_tools() returns well inside FAST_DEADLINE_S — i.e. before the
    slow handler finishes — proving the event loop was not blocked.
    """

    # The "slow" handler blocks for this long in its worker thread.
    SLOW_SLEEP_S = 0.5
    # The fast op must complete well before the slow handler finishes.
    # Set to half the slow sleep so there's a comfortable margin.
    FAST_DEADLINE_S = 0.25

    def _build_server_with_slow_tool(self):
        """Build a minimal MCP server with a slow stub 'list_folders' handler.

        The stub sleeps synchronously for SLOW_SLEEP_S, simulating a blocking
        PlaudClient network call.  The call_tool handler uses asyncio.to_thread
        (the production code path) so the event loop stays free while the stub
        blocks in its worker thread.
        """
        from mcp.server.lowlevel import Server

        inner_server = Server("plaud-mcp-test")

        def slow_list_folders() -> dict:
            """Synchronous handler that blocks for SLOW_SLEEP_S."""
            time.sleep(TestCallToolNonBlocking.SLOW_SLEEP_S)
            return {"content": [{"type": "text", "text": '{"ok": true}'}]}

        handlers = {"list_folders": slow_list_folders}

        @inner_server.list_tools()
        async def list_tools_handler() -> list[mcp_types.Tool]:
            return _TOOLS

        @inner_server.call_tool()
        async def call_tool_handler(name: str, arguments: dict) -> list[mcp_types.TextContent]:
            handler = handlers.get(name)
            if handler is None:
                return [
                    mcp_types.TextContent(
                        type="text",
                        text=json.dumps({"error": f"Unknown tool: {name}"}),
                    )
                ]
            try:
                # Production code path under test: asyncio.to_thread keeps the
                # event loop unblocked while the handler does its work.
                result = await asyncio.to_thread(handler, **arguments)
                text = result["content"][0]["text"]
            except TypeError as exc:
                payload = {
                    "error": f"Invalid arguments for tool '{name}': {exc}",
                    "error_code": "validation",
                    "retryable": False,
                }
                return [mcp_types.TextContent(type="text", text=json.dumps(payload, indent=2))]
            return [mcp_types.TextContent(type="text", text=text)]

        return inner_server

    def test_event_loop_stays_responsive_during_slow_handler(self):
        """list_tools() must return well before the slow handler finishes.

        Timeline (approximate):
          t=0.00  slow call_tool task launched (handler sleeps SLOW_SLEEP_S=0.5s in thread)
          t=0.00  list_tools coroutine starts concurrently
          t<0.25  list_tools must return (FAST_DEADLINE_S)
          t=0.50  slow handler thread wakes, call_tool task resolves
        """

        async def _run() -> None:
            server = self._build_server_with_slow_tool()
            call_tool_sdk = server.request_handlers[mcp_types.CallToolRequest]
            list_tools_sdk = server.request_handlers[mcp_types.ListToolsRequest]

            slow_req = mcp_types.CallToolRequest(
                method="tools/call",
                params=mcp_types.CallToolRequestParams(name="list_folders", arguments={}),
            )

            # Launch the slow call_tool in the background.
            slow_task = asyncio.create_task(call_tool_sdk(slow_req))

            # Give the event loop one iteration so the task starts and the
            # worker thread is actually running before we time the fast op.
            await asyncio.sleep(0)

            # Measure how long list_tools takes while the slow task is in flight.
            t0 = time.perf_counter()
            fast_result = await asyncio.wait_for(
                list_tools_sdk(mcp_types.ListToolsRequest(method="tools/list", params=None)),
                timeout=TestCallToolNonBlocking.FAST_DEADLINE_S,
            )
            fast_elapsed = time.perf_counter() - t0

            # The fast op must have completed well within the deadline.
            assert fast_elapsed < TestCallToolNonBlocking.FAST_DEADLINE_S, (
                f"list_tools took {fast_elapsed:.3f}s — event loop was blocked "
                f"(deadline {TestCallToolNonBlocking.FAST_DEADLINE_S}s, "
                f"slow handler sleep {TestCallToolNonBlocking.SLOW_SLEEP_S}s)"
            )

            # Sanity: the fast result actually contains tools.
            assert fast_result.root.tools, "list_tools returned empty tool list"

            # Clean up: wait for the slow task to finish (it will, just slowly).
            await slow_task

        asyncio.run(_run())
