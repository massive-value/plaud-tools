"""Per-user data directory and known file paths for plaud-tools.

Channel-agnostic: branches on ``sys.platform`` only.  Install-layout concerns
(frozen vs pip vs dev, ``sys.executable``-based) live in the sibling
``layout.py`` module — do not import it here.

On Windows ``data_dir()`` returns ``%LOCALAPPDATA%\\PlaudTools\\``, which is
exactly the path the previous inline reconstructions produced.  On macOS and
Linux the path comes from ``platformdirs.user_data_dir``.  All log and event
files share this one directory; there is no separate ``user_log_dir`` subtree
(deliberate — see ADR 004).

Public API
----------
data_dir()             -> Path   # per-user data root
tray_log()             -> Path   # tray.log inside data_dir()
mcp_log()              -> Path   # mcp.log inside data_dir()
events_path()          -> Path   # events.jsonl inside data_dir()
session_path()         -> Path   # session.json inside data_dir()
dpapi_shadow_path()    -> Path | None  # session.dat on Windows; None elsewhere
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import platformdirs


def data_dir() -> Path:
    """Return the per-user plaud-tools data directory.

    - Windows: ``%LOCALAPPDATA%\\PlaudTools`` (exact match to the previous
      inline reconstructions; falls back to
      ``Path.home() / "AppData" / "Local" / "PlaudTools"`` when the env var
      is absent).
    - macOS / Linux: ``platformdirs.user_data_dir("PlaudTools", appauthor=False)``.
    """
    if sys.platform == "win32":
        local = os.environ.get("LOCALAPPDATA")
        if local:
            return Path(local) / "PlaudTools"
        return Path.home() / "AppData" / "Local" / "PlaudTools"
    return Path(platformdirs.user_data_dir("PlaudTools", appauthor=False))


def tray_log() -> Path:
    """Return the path to the tray application log file."""
    return data_dir() / "tray.log"


def mcp_log() -> Path:
    """Return the path to the MCP server log file."""
    return data_dir() / "mcp.log"


def events_path() -> Path:
    """Return the path to the cross-process tray events file (events.jsonl)."""
    return data_dir() / "events.jsonl"


def session_path() -> Path:
    """Return the path to the JSON session fallback file."""
    return data_dir() / "session.json"


def dpapi_shadow_path() -> Path | None:
    """Return the DPAPI shadow path on Windows; None on all other platforms."""
    if sys.platform != "win32":
        return None
    return data_dir() / "session.dat"
