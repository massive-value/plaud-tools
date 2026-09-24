from __future__ import annotations

import json
import tomllib
from pathlib import Path

from plaud_tools.core import ai_clients


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


# ---------------------------------------------------------------------------
# Regression: array values in other sections must not be corrupted
# ---------------------------------------------------------------------------


def test_connect_disconnect_roundtrip_preserves_section_with_inline_array(tmp_path: Path, monkeypatch):
    """connect() then disconnect() must leave a section containing an inline TOML array intact.

    The old regex ``[^\\[]*`` treated the ``[`` inside ``args = ["-m", "x"]`` as
    the start of a new section header, truncating the array value and corrupting
    the file.  tomlkit parses the document correctly so this can never happen.
    """
    original = '# Top-level user comment\n[other_tool]\nargs = ["-m", "some.module"]\ntimeout = 30\n'
    config = tmp_path / "config.toml"
    config.write_text(original, encoding="utf-8")

    paths = {"codex": config, "claude-desktop": tmp_path / "x.json", "claude-code": tmp_path / "y.json"}
    monkeypatch.setattr(ai_clients, "_client_paths", lambda: paths)

    mcp_exe = r"C:\Users\example\plaud-mcp.exe"
    ai_clients.connect("codex", mcp_exe)

    # After connect: other_tool section must still be intact.
    mid = config.read_text(encoding="utf-8")
    mid_parsed = tomllib.loads(mid)
    assert mid_parsed["other_tool"]["args"] == ["-m", "some.module"], (
        "connect() corrupted the inline array in [other_tool]"
    )
    assert mid_parsed["other_tool"]["timeout"] == 30

    # Disconnect and verify other_tool is still intact.
    ai_clients.disconnect("codex")
    final = config.read_text(encoding="utf-8")
    final_parsed = tomllib.loads(final)
    assert "plaud" not in final_parsed.get("mcp_servers", {})
    assert final_parsed["other_tool"]["args"] == ["-m", "some.module"], (
        "disconnect() corrupted the inline array in [other_tool]"
    )
    assert final_parsed["other_tool"]["timeout"] == 30


def test_connect_preserves_user_comments_and_unrelated_sections(tmp_path: Path, monkeypatch):
    """User comments and byte content of unrelated sections are preserved by connect()."""
    original = (
        "# My Codex config — do not edit\n"
        "\n"
        "[model]\n"
        'provider = "openai"\n'
        "\n"
        "[mcp_servers.other]\n"
        'command = "other-mcp"\n'
    )
    config = tmp_path / "config.toml"
    config.write_text(original, encoding="utf-8")

    paths = {"codex": config, "claude-desktop": tmp_path / "x.json", "claude-code": tmp_path / "y.json"}
    monkeypatch.setattr(ai_clients, "_client_paths", lambda: paths)

    mcp_exe = r"C:\plaud-mcp.exe"
    ai_clients.connect("codex", mcp_exe)

    after = config.read_text(encoding="utf-8")
    # User comment must survive.
    assert "# My Codex config — do not edit" in after
    # Other unrelated keys must survive.
    parsed = tomllib.loads(after)
    assert parsed["model"]["provider"] == "openai"
    assert parsed["mcp_servers"]["other"]["command"] == "other-mcp"
    assert parsed["mcp_servers"]["plaud"]["command"] == mcp_exe


# ---------------------------------------------------------------------------
# JSON clients (Claude Desktop / Claude Code) -- previously untested.
# All paths below are tmp_path-only; _client_paths is monkeypatched for
# every client so a bug here can never touch a real ~/.claude.json.
# ---------------------------------------------------------------------------


def _json_paths(tmp_path: Path, **overrides: Path) -> dict[str, Path]:
    paths = {
        "claude-desktop": tmp_path / "claude_desktop_config.json",
        "claude-code": tmp_path / "claude.json",
        "codex": tmp_path / "config.toml",
    }
    paths.update(overrides)
    return paths


def test_claude_desktop_connect_writes_mcp_server(tmp_path: Path, monkeypatch):
    paths = _json_paths(tmp_path)
    monkeypatch.setattr(ai_clients, "_client_paths", lambda: paths)

    mcp_exe = r"C:\Users\example\AppData\Local\Programs\PlaudTools\mcp\plaud-mcp.exe"
    ai_clients.connect("claude-desktop", mcp_exe)

    parsed = json.loads(paths["claude-desktop"].read_text(encoding="utf-8"))
    assert parsed["mcpServers"]["plaud"]["command"] == mcp_exe
    assert ai_clients.get_status("claude-desktop", mcp_exe) == "connected"


def test_claude_code_connect_preserves_unrelated_keys(tmp_path: Path, monkeypatch):
    config = tmp_path / "claude.json"
    config.write_text(
        json.dumps({"mcpServers": {"other": {"command": "other-mcp"}}, "theme": "dark"}),
        encoding="utf-8",
    )
    paths = _json_paths(tmp_path, **{"claude-code": config})
    monkeypatch.setattr(ai_clients, "_client_paths", lambda: paths)

    mcp_exe = r"C:\plaud-mcp.exe"
    ai_clients.connect("claude-code", mcp_exe)

    parsed = json.loads(config.read_text(encoding="utf-8"))
    assert parsed["theme"] == "dark"
    assert parsed["mcpServers"]["other"]["command"] == "other-mcp"
    assert parsed["mcpServers"]["plaud"]["command"] == mcp_exe


def test_claude_code_disconnect_removes_plaud_section_only(tmp_path: Path, monkeypatch):
    config = tmp_path / "claude.json"
    config.write_text(
        json.dumps(
            {
                "mcpServers": {"other": {"command": "other-mcp"}, "plaud": {"command": "/x/plaud-mcp"}},
                "theme": "dark",
            }
        ),
        encoding="utf-8",
    )
    paths = _json_paths(tmp_path, **{"claude-code": config})
    monkeypatch.setattr(ai_clients, "_client_paths", lambda: paths)

    ai_clients.disconnect("claude-code")

    parsed = json.loads(config.read_text(encoding="utf-8"))
    assert "plaud" not in parsed["mcpServers"]
    assert parsed["mcpServers"]["other"]["command"] == "other-mcp"
    assert parsed["theme"] == "dark"


# ---------------------------------------------------------------------------
# BOM tolerance -- Windows tools commonly write JSON/TOML configs with a
# leading UTF-8 BOM, which plain "utf-8" decoding used to leave in the string
# and make json.loads()/tomllib.loads() reject as malformed.
# ---------------------------------------------------------------------------


def test_read_json_tolerates_leading_bom(tmp_path: Path, monkeypatch):
    config = tmp_path / "claude_desktop_config.json"
    config.write_bytes(b"\xef\xbb\xbf" + json.dumps({"mcpServers": {}}).encode("utf-8"))
    paths = _json_paths(tmp_path, **{"claude-desktop": config})
    monkeypatch.setattr(ai_clients, "_client_paths", lambda: paths)

    assert ai_clients.get_status("claude-desktop", "/x/plaud-mcp") == "not-connected"


def test_read_toml_tolerates_leading_bom(tmp_path: Path, monkeypatch):
    config = tmp_path / "config.toml"
    config.write_bytes(b"\xef\xbb\xbf[mcp_servers.plaud]\ncommand = '/x/plaud-mcp'\n")
    paths = _json_paths(tmp_path, **{"codex": config})
    monkeypatch.setattr(ai_clients, "_client_paths", lambda: paths)

    assert ai_clients.get_status("codex", "/x/plaud-mcp") == "connected"


# ---------------------------------------------------------------------------
# invalid-config status -- distinct from "not-connected" (parses fine, plaud
# just isn't configured) so a broken config isn't mistaken for a healthy one
# that simply needs `connect`.
# ---------------------------------------------------------------------------


def test_get_status_invalid_config_for_malformed_json(tmp_path: Path, monkeypatch):
    config = tmp_path / "claude_desktop_config.json"
    config.write_text("{not valid json", encoding="utf-8")
    paths = _json_paths(tmp_path, **{"claude-desktop": config})
    monkeypatch.setattr(ai_clients, "_client_paths", lambda: paths)

    assert ai_clients.get_status("claude-desktop", "/x/plaud-mcp") == "invalid-config"
    assert ai_clients.get_mcp_command("claude-desktop") is None


def test_get_status_invalid_config_for_malformed_toml(tmp_path: Path, monkeypatch):
    config = tmp_path / "config.toml"
    config.write_text("[mcp_servers.plaud\ncommand = broken", encoding="utf-8")
    paths = _json_paths(tmp_path, **{"codex": config})
    monkeypatch.setattr(ai_clients, "_client_paths", lambda: paths)

    assert ai_clients.get_status("codex", "/x/plaud-mcp") == "invalid-config"


# ---------------------------------------------------------------------------
# Bare-command resolution -- pip installs store the bare "plaud-mcp" command
# (found on PATH at run time) rather than a full path.
# ---------------------------------------------------------------------------


def test_same_path_resolves_bare_command_via_which(tmp_path: Path, monkeypatch):
    real_exe = tmp_path / "plaud-mcp"
    real_exe.touch()
    monkeypatch.setattr(
        ai_clients.shutil, "which", lambda name: str(real_exe) if name == "plaud-mcp" else None
    )

    assert ai_clients._same_path("plaud-mcp", str(real_exe)) is True
