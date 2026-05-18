"""AI client config detection and MCP wiring for Claude Desktop, Claude Code."""
from __future__ import annotations

import json
import os
import shutil
from datetime import datetime
from pathlib import Path
from typing import Literal

ClientStatus = Literal["not-detected", "not-connected", "connected", "stale"]

CLIENTS: dict[str, str] = {
    "claude-desktop": "Claude Desktop",
    "claude-code": "Claude Code",
}


def _client_paths() -> dict[str, Path]:
    home = Path.home()
    appdata = Path(os.environ.get("APPDATA") or home / "AppData" / "Roaming")
    localappdata = Path(os.environ.get("LOCALAPPDATA") or home / "AppData" / "Local")
    return {
        "claude-desktop": _resolve_claude_desktop(localappdata, appdata),
        "claude-code": home / ".claude.json",
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


def _read_config(config_path: Path) -> dict:
    if not config_path.exists():
        return {}
    text = config_path.read_text(encoding="utf-8").strip()
    return json.loads(text) if text else {}


def _write_atomic(config_path: Path, data: dict) -> None:
    config_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = config_path.with_suffix(".plaud-tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    tmp.replace(config_path)


def get_status(client_id: str, mcp_exe: str) -> ClientStatus:
    paths = _client_paths()
    config_path = paths.get(client_id)
    if config_path is None or not config_path.exists():
        return "not-detected"
    try:
        config = _read_config(config_path)
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
    config = _read_config(config_path)
    config.setdefault("mcpServers", {})["plaud"] = {"command": mcp_exe}
    _write_atomic(config_path, config)


def disconnect(client_id: str) -> None:
    paths = _client_paths()
    config_path = paths.get(client_id)
    if config_path is None or not config_path.exists():
        return
    config = _read_config(config_path)
    (config.get("mcpServers") or {}).pop("plaud", None)
    _write_atomic(config_path, config)


def status_all(mcp_exe: str) -> dict[str, ClientStatus]:
    return {cid: get_status(cid, mcp_exe) for cid in CLIENTS}


def connect_all(mcp_exe: str) -> None:
    for cid in CLIENTS:
        if _client_paths()[cid].exists():
            connect(cid, mcp_exe)


def disconnect_all() -> None:
    for cid in CLIENTS:
        disconnect(cid)
