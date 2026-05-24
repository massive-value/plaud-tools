"""Self-diagnosis command for plaud-tools.

Collects local install state and outputs a single JSON document useful for
filing support issues. Token values are never included — only masked metadata.
"""
from __future__ import annotations

import json
import os
import shutil
import sys
from pathlib import Path
from typing import Any

from . import __version__
from . import ai_clients as _ai_clients_mod
from .ai_clients import CLIENTS
from .appdata import tray_log as _log_path
from .errors import PlaudSessionExpiredError
from .layout import InstallLayout
from .session import SessionManager, SessionStore


# ---------------------------------------------------------------------------
# Install-dir / executable resolution (delegated to InstallLayout)
# ---------------------------------------------------------------------------

def _install_dir() -> Path:
    """Return the root directory of the current installation.

    Delegates to InstallLayout.detect() so that both doctor and tray use
    the same running-install semantics (derived from sys.executable).
    Returns the executable's parent for pip/dev channels where install_root
    is None.
    """
    layout = InstallLayout.detect()
    if layout.install_root is not None:
        return layout.install_root
    return Path(sys.executable).parent


def _cli_exe_path() -> Path:
    """Absolute path to the plaud-tools executable."""
    return InstallLayout.detect().cli_exe


def _mcp_exe_path() -> Path | None:
    """Absolute path to the plaud-mcp executable, or a dev-fallback path."""
    layout = InstallLayout.detect()
    if layout.mcp_exe is not None:
        return layout.mcp_exe
    # Dev fallback: PyInstaller onedir output next to repo root
    return (
        Path(__file__).parent.parent.parent / "out" / "plaud-mcp" / "plaud-mcp" / "plaud-mcp.exe"
    )


def _ffmpeg_path() -> Path:
    """Absolute path to ffmpeg.

    In a frozen bundle ffmpeg.exe lives next to plaud-mcp.exe; for pip users
    ffmpeg is expected on the system PATH.
    """
    layout = InstallLayout.detect()
    if layout.ffmpeg_exe is not None:
        return layout.ffmpeg_exe
    return Path("ffmpeg")


# ---------------------------------------------------------------------------
# Individual section builders
# ---------------------------------------------------------------------------

def _executables_section() -> dict[str, Any]:
    cli = _cli_exe_path()
    mcp = _mcp_exe_path()
    ffmpeg = _ffmpeg_path()

    # on_path: check if the directory containing the exe is on PATH
    path_dirs = {Path(p).resolve() for p in os.environ.get("PATH", "").split(os.pathsep) if p}
    cli_on_path = cli.parent.resolve() in path_dirs or bool(shutil.which("plaud-tools"))

    return {
        "plaud-tools": {
            "path": str(cli),
            "exists": cli.exists(),
            "on_path": cli_on_path,
        },
        "plaud-mcp": {
            "path": str(mcp),
            "exists": mcp.exists(),
        },
        "ffmpeg": {
            "path": str(ffmpeg),
            "exists": ffmpeg.exists(),
        },
    }


def _session_section(store: SessionStore) -> dict[str, Any]:
    session, source = store.load_with_source()
    if session is None:
        return {"present": False, "source": source}
    manager = SessionManager(store)
    try:
        manager.require()
        status = "valid"
    except PlaudSessionExpiredError as exc:
        status = exc.code
    days = manager.days_until_expiry()
    return {
        "present": True,
        "source": source,
        "region": session.region,
        "days_until_expiry": days,
        "status": status,
    }


def _get_mcp_command_from_config(client_id: str) -> str | None:
    """Read the raw mcp_command string stored in the AI client config, or None.

    Calls through ``_ai_clients_mod`` so tests can monkeypatch
    ``plaud_tools.ai_clients._client_paths`` and have it take effect here.
    """
    paths = _ai_clients_mod._client_paths()
    config_path = paths.get(client_id)
    if config_path is None or not config_path.exists():
        return None
    try:
        if config_path.suffix == ".toml":
            config = _ai_clients_mod._read_toml(config_path)
            entry = (config.get("mcp_servers") or {}).get("plaud")
            if entry and isinstance(entry.get("command"), str):
                return entry["command"]
        else:
            config = _ai_clients_mod._read_json(config_path)
            entry = (config.get("mcpServers") or {}).get("plaud")
            if entry and isinstance(entry.get("command"), str):
                return entry["command"]
    except Exception:
        pass
    return None


def _ai_clients_section() -> dict[str, Any]:
    mcp_exe = str(_mcp_exe_path())
    result: dict[str, Any] = {}
    paths = _ai_clients_mod._client_paths()
    for client_id in CLIENTS:
        config_path = paths.get(client_id)
        detected = config_path is not None and config_path.exists()
        status = _ai_clients_mod.get_status(client_id, mcp_exe)
        mcp_command = _get_mcp_command_from_config(client_id)
        entry: dict[str, Any] = {
            "detected": detected,
            "status": status,
        }
        if mcp_command is not None:
            entry["mcp_command"] = mcp_command
        result[client_id] = entry
    return result


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def run_doctor(store: SessionStore | None = None) -> dict[str, Any]:
    """Return the doctor JSON document as a dict (no I/O)."""
    if store is None:
        store = SessionStore()

    install_dir = _install_dir()

    return {
        "version": __version__,
        "frozen": getattr(sys, "frozen", False),
        "install_dir": str(install_dir),
        "executables": _executables_section(),
        "session": _session_section(store),
        "ai_clients": _ai_clients_section(),
        "log_path": str(_log_path()),
    }


def run_doctor_json(store: SessionStore | None = None) -> str:
    """Return the doctor document serialised as indented JSON."""
    return json.dumps(run_doctor(store), indent=2)
