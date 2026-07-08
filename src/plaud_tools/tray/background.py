"""Background-thread methods used by :class:`TrayApp`.

Pulled out of ``tray/app.py`` as a mixin to keep that file under 400 lines.
``BackgroundMixin`` is otherwise inert and depends only on ``self`` attributes
defined by :class:`TrayApp`.
"""

from __future__ import annotations

import json
import logging
import random
import sys
import threading
import time
from collections.abc import Callable
from typing import TYPE_CHECKING

from ..core.ai_clients import connect_all, status_all
from .setup import (
    _ACTIVATE_EVENT,
    EnvStatus,
    _autostart_enabled,
    _events_path,
    _mcp_exe,
    _set_autostart,
    _setup_cli_path,
    _setup_ps_completions,
    _verify_env,
)
from .toasts import _show_session_expired_toast, _show_update_available_toast
from .updater import _check_for_update

if TYPE_CHECKING:
    import tkinter as tk

    from ..core.session import PlaudSession
    from .windows.home import HomeWindow
    from .windows.login import LoginWindow


def _format_session_expired_diag(event: dict) -> str:
    """Format the diagnostic fields of a session_expired event for log output.

    Strips ``type`` and ``ts`` (already in the log timestamp / message prefix);
    sorts the rest for stable output. See issue #78.
    """
    diag_keys = sorted(k for k in event if k not in {"type", "ts"})
    return " ".join(f"{k}={event[k]!r}" for k in diag_keys)


class _BackgroundMixin:
    """Background-thread helpers for TrayApp.  No __init__ — relies on TrayApp's.

    The attribute stubs below are not assigned here — they exist solely to
    satisfy the type checker.  All attributes are initialised by TrayApp.__init__.
    Declaring them on the mixin avoids per-line ``# type: ignore[attr-defined]``
    spam across every self-access without contorting the runtime code.
    """

    if TYPE_CHECKING:
        # Attributes provided by TrayApp; typed here so mypy can resolve them
        # in the mixin methods without a circular import at runtime.
        _root: tk.Tk | None
        _session: PlaudSession | None
        _home_win: HomeWindow | None
        _login_win: LoginWindow | None
        _update_info: tuple[str, str, str | None, str | None] | None
        _env_status: EnvStatus | None

        def _refresh(self) -> None: ...
        def _open_login(self) -> None: ...

    def _watch_activate_event(self) -> None:
        """Show the appropriate window whenever a second instance signals us."""
        if sys.platform != "win32":
            return
        import ctypes

        h = ctypes.windll.kernel32.CreateEventW(None, False, False, _ACTIVATE_EVENT)
        if not h:
            return
        try:
            while True:
                if ctypes.windll.kernel32.WaitForSingleObject(h, 0xFFFFFFFF) == 0:
                    if self._root:
                        if self._session and self._home_win:
                            self._root.after(0, self._home_win.show)
                        elif not self._session and self._login_win:
                            self._root.after(0, self._login_win.show)
        finally:
            ctypes.windll.kernel32.CloseHandle(h)

    def _update_poll_loop(self) -> None:
        interval_seconds = random.uniform(20 * 3600, 28 * 3600)
        _notified_version: str | None = None

        # Run the first check immediately (preserves current startup behaviour).
        def _on_update_found(result: tuple) -> None:
            nonlocal _notified_version
            self._update_info = result
            self._refresh()
            if self._root:
                self._root.after(0, lambda: self._home_win._refresh_update_btn() if self._home_win else None)
            version = result[0]
            if version != _notified_version:
                _notified_version = version
                _show_update_available_toast(version)

        try:
            result = _check_for_update()
            if result:
                _on_update_found(result)
        except Exception:
            logging.warning("update check failed", exc_info=True)
        last_check_wall_time = time.time()
        while True:
            time.sleep(300)
            if time.time() - last_check_wall_time >= interval_seconds:
                try:
                    result = _check_for_update()
                    if result:
                        _on_update_found(result)
                except Exception:
                    logging.warning("update check failed", exc_info=True)
                last_check_wall_time = time.time()
                interval_seconds = random.uniform(20 * 3600, 28 * 3600)

    def _event_poll_loop(self) -> None:
        """Poll ``appdata.events_path()`` every 5 s for tray events.

        Currently handles ``session_expired`` events written by the MCP server.
        Events are claimed by an atomic rename (to a sibling ``.processing``
        path) before being read and deleted, rather than read-then-truncate
        in place: the previous read+truncate had a window where an MCP-side
        append landing between the read and the truncate was silently wiped
        by the truncate, dropping the event (#162). A concurrent append after
        the rename lands in a fresh file at the original path and is picked
        up on the next poll instead of being lost.
        """
        events_path = _events_path()
        claim_path = events_path.with_name(events_path.name + ".processing")
        while True:
            time.sleep(5)
            try:
                if not events_path.exists():
                    continue
                try:
                    events_path.replace(claim_path)  # atomic claim
                except OSError:
                    continue
                try:
                    lines = claim_path.read_text(encoding="utf-8").splitlines()
                finally:
                    claim_path.unlink(missing_ok=True)
                for line in lines:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        event = json.loads(line)
                    except Exception:
                        continue
                    if event.get("type") == "session_expired":
                        # Log every diagnostic field from the event payload so
                        # tray.log carries the same context as mcp.log (issue
                        # #78). MCP enriches the payload with reason,
                        # store_source, env_token_present, mcp_pid, mcp_version;
                        # older MCP builds may omit them.
                        logging.warning(
                            "Tray: session_expired event received from MCP %s",
                            _format_session_expired_diag(event),
                        )
                        _show_session_expired_toast()
                        # Open the login window on the tkinter thread
                        if self._root:
                            self._root.after(0, self._open_login)
                        break  # one toast per poll cycle is enough
            except Exception:
                logging.warning("event poll error", exc_info=True)

    def _auto_heal(self) -> None:
        if not self._session:
            return
        mcp = _mcp_exe()
        statuses = status_all(mcp)
        if any(s == "stale" for s in statuses.values()):
            connect_all(mcp)

    def _run_verify_env(self) -> None:
        """Background thread: verify PATH/completions/autostart and auto-heal.

        Setup can go missing across uninstall/reinstall cycles, in-app upgrades
        that bypass ``install.ps1`` step 4, or system-level cleanup tools
        (Task Manager's Startup tab, ``msconfig``, etc.).  Rather than leave
        the user staring at the yellow setup-failure banner, the tray quietly
        restores anything it knows how to set without asking.  The autostart
        slot honors the user's explicit opt-out marker so disabling "Start
        with Windows" via the menu actually sticks across launches.

        After the auto-heal pass we re-verify and only surface the banner /
        repair button if something genuinely cannot be fixed automatically
        (e.g., a non-frozen dev build or a registry write that raised).
        """
        try:
            status = _verify_env()
            if not status.all_ok:
                self._auto_repair_env(status)
                status = _verify_env()
            self._env_status = status
            if not status.all_ok:
                missing = ", ".join(status.missing_labels())
                logging.warning(
                    "Environment check: missing %s (auto-repair did not resolve)",
                    missing,
                )
                if self._root and self._home_win:

                    def _nudge_home() -> None:
                        if self._home_win:
                            self._home_win._refresh_repair_btn()
                            self._home_win._refresh_setup_failure_row()

                    self._root.after(0, _nudge_home)
        except Exception:
            logging.warning("Environment check failed", exc_info=True)

    def _auto_repair_env(self, status: EnvStatus) -> None:
        """Silently restore missing setup entries on tray startup.

        Only runs in the frozen bundle context — the helpers it calls (PATH /
        completions / autostart) target paths that only make sense for a
        Windows install at ``%LOCALAPPDATA%\\Programs\\PlaudTools\\``.  Each
        helper is idempotent, so a partial repair on the previous launch
        cannot cause duplicate entries.
        """
        if not getattr(sys, "frozen", False):
            return
        if not status.path_ok:
            try:
                _setup_cli_path()
                logging.info("Auto-repair: added cli dir to user PATH")
            except Exception:
                logging.warning("Auto-repair: PATH restore failed", exc_info=True)
        if not status.completions_ok:
            try:
                _setup_ps_completions()
                logging.info("Auto-repair: restored PowerShell completions sourcing")
            except Exception:
                logging.warning("Auto-repair: completions restore failed", exc_info=True)
        if not status.autostart_ok:
            try:
                _set_autostart(True)
                logging.info("Auto-repair: re-registered autostart in HKCU Run")
            except Exception:
                logging.warning("Auto-repair: autostart restore failed", exc_info=True)

    def _repair_env(self, on_done: Callable[[bool, str], None]) -> None:
        """Re-run the mutating setup helpers and report success/failure via callback.

        Runs in a background thread so the UI stays responsive.
        """

        def _work() -> None:
            try:
                _setup_cli_path()
                _setup_ps_completions()
                if not _autostart_enabled():
                    _set_autostart(True)
                # Re-verify so the button hides itself on success
                self._env_status = _verify_env()
                if self._root:
                    self._root.after(0, lambda: on_done(True, "Setup repaired successfully."))
            except Exception as exc:
                logging.warning("Repair setup failed", exc_info=True)
                # Render the message into a plain string BEFORE scheduling the
                # lambda (#152): `except Exception as exc:` implicitly runs
                # `del exc` when the except block exits (CPython avoids a
                # traceback reference cycle), so a lambda that captures `exc`
                # by name raises NameError once it actually runs later via
                # root.after() -- the Home window then hangs on
                # "Repairing setup..." with no shown error and no log clue.
                msg = f"Repair failed: {exc}"
                if self._root:
                    self._root.after(0, lambda: on_done(False, msg))

        threading.Thread(target=_work, daemon=True).start()


__all__ = ["_BackgroundMixin", "_format_session_expired_diag"]
