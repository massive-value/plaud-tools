from __future__ import annotations

import tomllib
from pathlib import Path

from plaud_tools import ai_clients


def test_codex_connect_writes_parsable_toml_with_windows_path(tmp_path: Path, monkeypatch):
    config = tmp_path / "config.toml"
    config.write_text(
        '[mcp_servers.plaud]\ncommand = "C:\\\\Users\\\\old\\\\plaud-mcp.exe"\nenabled = false\n',
        encoding="utf-8",
    )
    paths = {"codex": config, "claude-desktop": tmp_path / "x.json", "claude-code": tmp_path / "y.json"}
    monkeypatch.setattr(ai_clients, "_client_paths", lambda: paths)

    new_path = r"C:\Users\example\AppData\Local\Programs\PlaudTools\mcp\plaud-mcp.exe"
    ai_clients.connect("codex", new_path)

    parsed = tomllib.loads(config.read_text(encoding="utf-8"))
    assert parsed["mcp_servers"]["plaud"]["command"] == new_path


def test_codex_status_round_trips_through_connect(tmp_path: Path, monkeypatch):
    config = tmp_path / "config.toml"
    config.write_text("", encoding="utf-8")
    paths = {"codex": config, "claude-desktop": tmp_path / "x.json", "claude-code": tmp_path / "y.json"}
    monkeypatch.setattr(ai_clients, "_client_paths", lambda: paths)

    new_path = r"C:\Users\example\AppData\Local\Programs\PlaudTools\mcp\plaud-mcp.exe"
    ai_clients.connect("codex", new_path)
    assert ai_clients.get_status("codex", new_path) == "connected"


def test_codex_stale_when_path_differs(tmp_path: Path, monkeypatch):
    config = tmp_path / "config.toml"
    config.write_text("", encoding="utf-8")
    paths = {"codex": config, "claude-desktop": tmp_path / "x.json", "claude-code": tmp_path / "y.json"}
    monkeypatch.setattr(ai_clients, "_client_paths", lambda: paths)

    ai_clients.connect("codex", r"C:\Users\old\plaud-mcp.exe")
    assert ai_clients.get_status("codex", r"C:\Users\new\plaud-mcp.exe") == "stale"


def test_codex_disconnect_removes_plaud_section_only(tmp_path: Path, monkeypatch):
    config = tmp_path / "config.toml"
    config.write_text(
        '[mcp_servers.other]\ncommand = "other.exe"\n\n[mcp_servers.plaud]\ncommand = "/x/plaud-mcp"\n',
        encoding="utf-8",
    )
    paths = {"codex": config, "claude-desktop": tmp_path / "x.json", "claude-code": tmp_path / "y.json"}
    monkeypatch.setattr(ai_clients, "_client_paths", lambda: paths)

    ai_clients.disconnect("codex")
    parsed = tomllib.loads(config.read_text(encoding="utf-8"))
    assert "plaud" not in parsed.get("mcp_servers", {})
    assert parsed["mcp_servers"]["other"]["command"] == "other.exe"


def test_toml_string_falls_back_to_basic_when_value_has_apostrophe():
    out = ai_clients._toml_string("C:\\Path\\with'quote\\app.exe")
    parsed = tomllib.loads(f"command = {out}\n")
    assert parsed["command"] == "C:\\Path\\with'quote\\app.exe"
