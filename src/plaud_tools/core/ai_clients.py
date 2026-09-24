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

ClientStatus = Literal["not-detected", "not-connected", "connected", "stale", "invalid-config"]

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


def _is_bare_command(command: str) -> bool:
    """True when *command* has no path component, e.g. a pip-installed ``plaud-mcp``."""
    return os.sep not in command and (os.altsep is None or os.altsep not in command)


def _resolve_command_path(command: str) -> Path:
    """Resolve *command* to an absolute path the way a shell would launch it.

    A bare command (no directory component) is a PATH lookup, not a path
    relative to the current working directory -- resolving it with
    ``Path.resolve()`` alone silently produces a path under cwd that can
    never match the real executable, permanently misreporting a healthy pip
    install (which stores the bare command "plaud-mcp") as "stale". Look it
    up with ``shutil.which`` first, and only fall back to plain path
    resolution when that fails (e.g. a stale entry pointing at a command no
    longer on PATH).
    """
    if _is_bare_command(command):
        found = shutil.which(command)
        if found is not None:
            return Path(found).resolve()
    return Path(command).resolve()


def _same_path(a: str, b: str) -> bool:
    return _resolve_command_path(a).as_posix().lower() == _resolve_command_path(b).as_posix().lower()


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
    # utf-8-sig: some editors (and Windows tools in general) write
    # claude_desktop_config.json / .claude.json with a leading BOM, which
    # plain "utf-8" decoding leaves in the string and json.loads() then
    # rejects as invalid.
    text = config_path.read_text(encoding="utf-8-sig").strip()
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
    text = config_path.read_text(encoding="utf-8-sig").strip()  # BOM-tolerant; see _read_json
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
    text = config_path.read_text(encoding="utf-8-sig") if config_path.exists() else ""

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


def _load_mcp_entry(config_path: Path) -> dict | None:
    """Parse *config_path* and return its ``[mcp_servers.plaud]``/``mcpServers.plaud`` entry.

    Returns ``None`` when the config parses fine but has no (valid) plaud
    entry. Raises whatever the underlying parser raises (``json.JSONDecodeError``,
    ``tomllib.TOMLDecodeError``, ...) when the file itself is malformed --
    callers that need to tell "broken file" apart from "valid file, nothing
    configured" (``get_status``) catch that themselves.
    """
    if config_path.suffix == ".toml":
        config = _read_toml(config_path)
        entry = (config.get("mcp_servers") or {}).get("plaud")
    else:
        config = _read_json(config_path)
        entry = (config.get("mcpServers") or {}).get("plaud")
    if entry and isinstance(entry.get("command"), str):
        return entry
    return None


def get_mcp_command(client_id: str) -> str | None:
    """Return the raw mcp_command string stored in *client_id*'s config, or None.

    Used by ``doctor`` to surface the configured command alongside the
    connection status ``get_status`` computes -- shares ``_load_mcp_entry``
    with it instead of re-parsing the config a second time.
    """
    paths = _client_paths()
    config_path = paths.get(client_id)
    if config_path is None or not config_path.exists():
        return None
    try:
        entry = _load_mcp_entry(config_path)
    except Exception:
        return None
    return entry["command"] if entry else None


def get_status(client_id: str, mcp_exe: str) -> ClientStatus:
    paths = _client_paths()
    config_path = paths.get(client_id)
    if config_path is None or not config_path.exists():
        return "not-detected"

    try:
        entry = _load_mcp_entry(config_path)
    except Exception:
        # The file exists but couldn't be parsed (bad JSON/TOML) -- distinct
        # from "not-connected" (parses fine, plaud just isn't set up) so a
        # user isn't told to "connect" a client whose config is actually broken.
        return "invalid-config"
    if entry is None:
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
