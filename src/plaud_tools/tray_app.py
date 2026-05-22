"""Plaud Tools system tray application — compatibility shim.

The tray code lives under :mod:`plaud_tools.tray` (split across ``setup``,
``updater``, ``uninstaller``, ``windows.*``, and ``app``).  This module
re-exports every public symbol the rest of the codebase, the test suite, and
the PyInstaller entry script expect to find on ``plaud_tools.tray_app``.

Several tests use ``monkeypatch.setattr(tray_app, "_foo", value)`` and then
exercise code paths that live in the submodules.  To make those patches reach
the submodule globals (which is where the function bodies look up ``_foo``),
this module installs a custom ``__class__`` on itself that propagates every
attribute assignment to the submodules that originally defined the name.

Requires the ``[tray]`` optional dependencies::

    pip install plaud-tools[tray]

Entry point: ``plaud-tray`` (see ``pyproject.toml``).
"""
from __future__ import annotations

# ---------------------------------------------------------------------------
# Module-level imports that tests monkeypatch via ``tray_app.<name>``.
# Keep these in the shim's namespace too so e.g. ``tray_app.subprocess``,
# ``tray_app.tempfile``, ``tray_app.time``, ``tray_app.Path`` all resolve.
# ---------------------------------------------------------------------------
import json  # noqa: F401
import logging  # noqa: F401
import logging.handlers  # noqa: F401
import os  # noqa: F401
import random  # noqa: F401
import subprocess
import sys
import tempfile
import threading  # noqa: F401
import time
import tkinter as tk  # noqa: F401
import urllib.request  # noqa: F401
from pathlib import Path
from types import ModuleType
from typing import Callable  # noqa: F401

from . import __version__ as APP_VERSION
from .ai_clients import CLIENTS, connect, connect_all, disconnect, status_all  # noqa: F401
from .auth import PlaudAuth  # noqa: F401
from .client import PlaudClient
from .errors import PlaudApiError, PlaudSessionExpiredError  # noqa: F401
from .ps1_templates import render_uninstall_ps1, render_update_ps1  # noqa: F401
from .session import PlaudSession, SessionManager, SessionStore  # noqa: F401

# ---------------------------------------------------------------------------
# Re-export submodules so they are addressable via ``tray_app.<submodule>``
# (some tests reach into them) and so the shim can propagate setattr to them.
# ---------------------------------------------------------------------------
from .tray import app as _app_mod
from .tray import background as _background_mod
from .tray import icons as _icons_mod
from .tray import setup as _setup_mod
from .tray import toasts as _toasts_mod
from .tray import uninstaller as _uninstaller_mod
from .tray import updater as _updater_mod
from .tray.windows import home as _home_mod
from .tray.windows import login as _login_mod
from .tray.windows import wizard as _wizard_mod

# ---------------------------------------------------------------------------
# Explicit re-exports.  These give every consumer a stable
# ``plaud_tools.tray_app.<name>`` import path.
# ---------------------------------------------------------------------------
from .tray.app import (
    TrayApp,
    _TEST_CONNECTION_TIMEOUT,
    main,
)
from .tray.icons import _load_icon, _load_icons
from .tray.toasts import _show_install_toast, _show_session_expired_toast
from .tray.setup import (
    APP_NAME,
    EnvStatus,
    _ACTIVATE_EVENT,
    _AUTOSTART_KEY,
    _AUTOSTART_NAME,
    _MUTEX_HANDLE,
    _acquire_instance_lock,
    _apply_theme,
    _assets_path,
    _autostart_enabled,
    _autostart_opt_out_marker_path,
    _autostart_opted_out,
    _check_cli_path,
    _check_ps_completions,
    _cli_dir,
    _completions_dir,
    _events_path,
    _install_completions_dir,
    _install_dir,
    _mcp_exe,
    _set_app_icon,
    _set_autostart,
    _setup_cli_path,
    _setup_logging,
    _setup_ps_completions,
    _stale_sourcing_re,
    _verify_env,
)
from .tray.uninstaller import (
    UninstallDialog,
    _delete_log_files,
    _delete_session_files,
    _launch_uninstall_helper,
    _remove_cli_path,
    _remove_ps_completions,
)
from .tray.updater import (
    GITHUB_REPO,
    UpdateDialog,
    _check_for_update,
    _version_gt,
)
from .tray.windows.home import HomeWindow
from .tray.windows.login import LoginWindow
from .tray.windows.wizard import (
    WizardWindow,
    _STATUS_BADGE,
)

# ---------------------------------------------------------------------------
# Monkeypatch propagation
#
# Tests do things like ``monkeypatch.setattr(tray_app, "_cli_dir", X)`` and
# then call a function that lives in ``plaud_tools.tray.setup``.  The function
# body looks up ``_cli_dir`` in its own module globals (``tray.setup``), so the
# patch on this shim wouldn't reach it.  To preserve the contract, we install
# a custom module class that mirrors every attribute assignment into the
# submodule(s) that defined the name.
# ---------------------------------------------------------------------------

# Order matters: submodules earlier in the list are checked first when looking
# up the original module for a name.  ``app`` is checked first because it
# (re-)defines symbols that also exist in ``setup`` (e.g. ``Path``).
_FORWARD_TARGETS = (
    _app_mod,
    _background_mod,
    _toasts_mod,
    _updater_mod,
    _uninstaller_mod,
    _home_mod,
    _login_mod,
    _wizard_mod,
    _icons_mod,
    _setup_mod,
)


class _TrayAppShim(ModuleType):
    """Module subclass that mirrors attribute writes into the tray submodules.

    monkeypatch.setattr(tray_app, "_foo", value) becomes:
        tray_app._foo = value
        # plus, for every submodule that already has a "_foo" attribute,
        tray.setup._foo = value  (etc.)
    On teardown monkeypatch reapplies setattr(tray_app, "_foo", original_value),
    which restores all of them.
    """

    def __setattr__(self, name: str, value: object) -> None:  # type: ignore[override]
        super().__setattr__(name, value)
        # Skip dunder names — propagating __name__, __loader__, etc. into the
        # submodules would corrupt their identities (especially during reload).
        if name.startswith("__") and name.endswith("__"):
            return
        for submod in _FORWARD_TARGETS:
            # Only mirror names that the submodule already exposes — avoids
            # spraying unrelated globals across modules.
            if hasattr(submod, name):
                try:
                    setattr(submod, name, value)
                except (AttributeError, TypeError):
                    pass

    def __delattr__(self, name: str) -> None:  # type: ignore[override]
        super().__delattr__(name)
        if name.startswith("__") and name.endswith("__"):
            return
        for submod in _FORWARD_TARGETS:
            if hasattr(submod, name):
                try:
                    delattr(submod, name)
                except (AttributeError, TypeError):
                    pass


sys.modules[__name__].__class__ = _TrayAppShim


__all__ = [
    # Module-level imports tests reach into (subprocess, time, tempfile, Path)
    "subprocess",
    "sys",
    "tempfile",
    "time",
    "Path",
    # Re-exports from .tray.setup
    "APP_NAME",
    "EnvStatus",
    "_ACTIVATE_EVENT",
    "_AUTOSTART_KEY",
    "_AUTOSTART_NAME",
    "_acquire_instance_lock",
    "_apply_theme",
    "_assets_path",
    "_autostart_enabled",
    "_autostart_opt_out_marker_path",
    "_autostart_opted_out",
    "_check_cli_path",
    "_check_ps_completions",
    "_cli_dir",
    "_completions_dir",
    "_events_path",
    "_install_completions_dir",
    "_install_dir",
    "_mcp_exe",
    "_set_app_icon",
    "_set_autostart",
    "_setup_cli_path",
    "_setup_logging",
    "_setup_ps_completions",
    "_stale_sourcing_re",
    "_verify_env",
    # Re-exports from .tray.uninstaller
    "UninstallDialog",
    "_delete_log_files",
    "_delete_session_files",
    "_launch_uninstall_helper",
    "_remove_cli_path",
    "_remove_ps_completions",
    # Re-exports from .tray.updater
    "GITHUB_REPO",
    "UpdateDialog",
    "_check_for_update",
    "_version_gt",
    # Re-exports from .tray.windows
    "HomeWindow",
    "LoginWindow",
    "WizardWindow",
    # Re-exports from .tray.app
    "TrayApp",
    "_TEST_CONNECTION_TIMEOUT",
    "_load_icon",
    "_load_icons",
    "_show_install_toast",
    "_show_session_expired_toast",
    "main",
    # Top-level imports used by tests
    "APP_VERSION",
    "PlaudClient",
    "SessionStore",
]
