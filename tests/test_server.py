"""Tests for the MCP server module."""
from __future__ import annotations

from plaud_tools.server import _TOOLS, _make_server

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


def test_server_constructs_without_error():
    server = _make_server()
    assert server is not None
