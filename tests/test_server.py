"""Tests for the MCP server module."""
from __future__ import annotations

from plaud_tools.server import _TOOLS, _make_server

_EXPECTED_TOOL_NAMES = {
    "browse_recordings",
    "get_recording",
    "mutate_recording",
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


def test_server_upload_recording_requires_file_path():
    tool = next(t for t in _TOOLS if t.name == "upload_recording")
    assert "file_path" in tool.inputSchema["required"]


def test_server_process_recording_requires_recording_id():
    tool = next(t for t in _TOOLS if t.name == "process_recording")
    assert "recording_id" in tool.inputSchema["required"]


def test_server_constructs_without_error():
    server = _make_server()
    assert server is not None
