"""Tests for the MCP server module."""
from __future__ import annotations

import logging

from plaud_tools.server import _TOOLS, _make_server, _mcp_log_path, _setup_mcp_logging

_EXPECTED_TOOL_NAMES = {
    "browse_recordings",
    "get_recording",
    "mutate_recording",
    "delete_recording",
    "rename_speaker",
    "upload_recording",
    "process_recording",
    "list_folders",
    "merge_recordings",
}


def test_server_exposes_expected_tools():
    assert {t.name for t in _TOOLS} == _EXPECTED_TOOL_NAMES


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
    tool = next(t for t in _TOOLS if t.name == "delete_recording")
    assert tool.inputSchema["required"] == ["recording_id"]


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
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    p = _mcp_log_path()
    assert p == tmp_path / "PlaudTools" / "mcp.log"


def test_setup_mcp_logging_writes_startup_banner(monkeypatch, tmp_path):
    """Pin issue #78 fix: the MCP server now leaves an on-disk audit trail."""
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    # Strip any pre-existing root handlers so basicConfig actually installs ours.
    root = logging.getLogger()
    saved_handlers = root.handlers[:]
    saved_level = root.level
    for h in saved_handlers:
        root.removeHandler(h)
    try:
        _setup_mcp_logging()
        # Force the rotating handler to flush so the assertion sees the banner.
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
