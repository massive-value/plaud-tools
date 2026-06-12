"""AI client config detection and MCP wiring for Claude Desktop, Claude Code, Codex CLI."""

from __future__ import annotations

import json
import os
import shutil
import tomllib
from datetime import datetime
from pathlib import Path
from typing import Literal

import tomlkit

ClientStatus = Literal["not-detected", "not-connected", "connected", "stale"]

CLIENTS: dict[str, str] = {
    "claude-desktop": "Claude Desktop",
    "claude-code": "Claude Code",
    "codex": "Codex",
}


def _client_paths() -> dict[str, Path]:
    home = Path.home()
    appdata = Path(os.environ.get("APPDATA") or home / "AppData" / "Roaming")
    localappdata = Path(os.environ.get("LOCALAPPDATA") or home / "AppData" / "Local")
    return {
        "claude-desktop": _resolve_claude_desktop(localappdata, appdata),
        "claude-code": home / ".claude.json",
        "codex": home / ".codex" / "config.toml",
    }


def _resolve_claude_desktop(localappdata: Path, appdata: Path) -> Path:
    # Microsoft Store version uses a sandboxed Packages path.
    packages = localappdata / "Packages"
    if packages.exists():
        for d in packages.iterdir():
            if d.name.startswith("Claude_"):
                return d / "LocalCache" / "Roaming" / "Claude" / "claude_desktop_config.json"
    return appdata / "Claude" / "claude_desktop_config.json"


def _same_path(a: str, b: str) -> bool:
    return Path(a).resolve().as_posix().lower() == Path(b).resolve().as_posix().lower()


def _backup_once(config_path: Path) -> None:
    if not config_path.exists():
        return
    stamp = datetime.now().strftime("%Y%m%d")
    if not list(config_path.parent.glob(f"{config_path.name}.plaud-backup-*")):
        shutil.copy2(config_path, f"{config_path}.plaud-backup-{stamp}")


# ---------------------------------------------------------------------------
# JSON helpers (Claude Desktop, Claude Code)
# ---------------------------------------------------------------------------


def _read_json(config_path: Path) -> dict:
    if not config_path.exists():
        return {}
    text = config_path.read_text(encoding="utf-8").strip()
    return json.loads(text) if text else {}


def _write_atomic_json(config_path: Path, data: dict) -> None:
    config_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = config_path.with_suffix(".plaud-tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    tmp.replace(config_path)


# ---------------------------------------------------------------------------
# TOML helpers (Codex CLI)
# ---------------------------------------------------------------------------


def _read_toml(config_path: Path) -> dict:
    if not config_path.exists():
        return {}
    text = config_path.read_text(encoding="utf-8").strip()
    return tomllib.loads(text) if text else {}


def _toml_string(value: str) -> str:
    # Prefer a single-quoted TOML literal string so Windows backslashes don't
    # get interpreted as escape sequences. Fall back to a basic string with
    # escaped backslashes if the value itself contains a single quote.
    if "'" not in value:
        return f"'{value}'"
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _write_toml_mcp(config_path: Path, command: str | None) -> None:
    """Add/update or remove [mcp_servers.plaud] in a TOML file without touching other content.

    Uses tomlkit for style-preserving round-trips so that array values like
    ``args = ["-m", "x"]`` in other sections are never corrupted by section
    boundary detection (the old regex ``[^\\[]*`` broke on inline arrays).
    """
    config_path.parent.mkdir(parents=True, exist_ok=True)
    text = config_path.read_text(encoding="utf-8") if config_path.exists() else ""

    doc = tomlkit.loads(text)

    if command is not None:
        # Build the [mcp_servers.plaud] table using a TOML literal string so
        # that Windows backslashes are preserved byte-for-byte (same quoting
        # behavior as _toml_string).
        plaud_table = tomlkit.table()
        if "'" not in command:
            item = tomlkit.string(command, literal=True)
        else:
            item = tomlkit.string(command)
        plaud_table.add("command", item)

        mcp_servers = doc.get("mcp_servers")
        if mcp_servers is None:
            mcp_servers = tomlkit.table(is_super_table=True)
            doc.add("mcp_servers", mcp_servers)
        mcp_servers["plaud"] = plaud_table  # type: ignore[index]
    else:
        mcp_servers = doc.get("mcp_servers")
        if mcp_servers is not None and "plaud" in mcp_servers:
            del mcp_servers["plaud"]  # type: ignore[attr-defined]
            # Remove the mcp_servers super-table entirely when it is now empty
            # so the file stays clean (matches prior regex-strip behavior).
            if not mcp_servers:  # type: ignore[truthy-iterable]
                del doc["mcp_servers"]

    out = tomlkit.dumps(doc)
    tmp = config_path.with_suffix(".plaud-tmp")
    tmp.write_text(out, encoding="utf-8")
    tmp.replace(config_path)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def get_status(client_id: str, mcp_exe: str) -> ClientStatus:
    paths = _client_paths()
    config_path = paths.get(client_id)
    if config_path is None or not config_path.exists():
        return "not-detected"

    if config_path.suffix == ".toml":
        try:
            config = _read_toml(config_path)
        except Exception:
            return "not-connected"
        entry = (config.get("mcp_servers") or {}).get("plaud")
        if not entry or not isinstance(entry.get("command"), str):
            return "not-connected"
        return "connected" if _same_path(entry["command"], mcp_exe) else "stale"

    try:
        config = _read_json(config_path)
    except Exception:
        return "not-connected"
    entry = (config.get("mcpServers") or {}).get("plaud")
    if not entry or not isinstance(entry.get("command"), str):
        return "not-connected"
    return "connected" if _same_path(entry["command"], mcp_exe) else "stale"


def connect(client_id: str, mcp_exe: str) -> None:
    paths = _client_paths()
    config_path = paths[client_id]
    _backup_once(config_path)

    if config_path.suffix == ".toml":
        _write_toml_mcp(config_path, mcp_exe)
        return

    config = _read_json(config_path)
    config.setdefault("mcpServers", {})["plaud"] = {"command": mcp_exe}
    _write_atomic_json(config_path, config)


def disconnect(client_id: str) -> None:
    paths = _client_paths()
    config_path = paths.get(client_id)
    if config_path is None or not config_path.exists():
        return

    if config_path.suffix == ".toml":
        _write_toml_mcp(config_path, None)
        return

    config = _read_json(config_path)
    (config.get("mcpServers") or {}).pop("plaud", None)
    _write_atomic_json(config_path, config)


def status_all(mcp_exe: str) -> dict[str, ClientStatus]:
    return {cid: get_status(cid, mcp_exe) for cid in CLIENTS}


def connect_all(mcp_exe: str) -> None:
    for cid in CLIENTS:
        if _client_paths()[cid].exists():
            connect(cid, mcp_exe)


def disconnect_all() -> None:
    for cid in CLIENTS:
        disconnect(cid)
