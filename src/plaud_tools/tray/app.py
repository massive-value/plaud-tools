"""TrayApp — the main system tray application and ``main`` entry point.

This module also defines the two toast helpers (first-run and session-expired).
The helpers and the TrayApp methods look up several globals (``_events_path``,
``_show_session_expired_toast``, ``PlaudClient``, ``subprocess``, ``tempfile``,
etc.) so the test suite can monkeypatch them via ``plaud_tools.tray_app``.

To preserve that contract the shim module ``plaud_tools.tray_app`` overrides
``__setattr__`` and propagates assignments back into the submodules.  See
``plaud_tools.tray_app`` for the propagation logic.
"""
from __future__ import annotations

import logging
import os
import subprocess  # noqa: F401  (re-exported via the tray_app shim)
import sys
import tempfile
import threading
import tkinter as tk
import urllib.request  # noqa: F401  (kept for monkeypatch parity with old tray_app)
from pathlib import Path
from typing import Callable

import pystray
from PIL import Image

from .. import __version__ as APP_VERSION
from ..client import PlaudClient
from ..errors import PlaudSessionExpiredError
from ..session import PlaudSession, SessionManager, SessionStore
from .background import _BackgroundMixin
from .icons import _load_icon, _load_icons
from .setup import (
    APP_NAME,
    EnvStatus,
    _acquire_instance_lock,
    _apply_theme,
    _assets_path,
    _autostart_enabled,
    _set_app_icon,
    _set_autostart,
    _setup_logging,
)
from .toasts import _show_install_toast, _show_session_expired_toast
from .updater import UpdateDialog, _check_for_update
from .uninstaller import UninstallDialog
from .windows.home import HomeWindow
from .windows.login import LoginWindow
from .windows.wizard import WizardWindow

# Test-hook constant — monkeypatched by tests to a smaller value
_TEST_CONNECTION_TIMEOUT = 15  # seconds


class TrayApp(_BackgroundMixin):
    def __init__(self) -> None:
        self._store = SessionStore()
        self._manager = SessionManager(self._store)
        self._session: PlaudSession | None = None
        self._icon: pystray.Icon | None = None
        self._root: tk.Tk | None = None
        self._update_info: tuple[str, str, str | None] | None = None
        self._env_status: EnvStatus | None = None
        self._login_win: LoginWindow | None = None
        self._wizard_win: WizardWindow | None = None
        self._home_win: HomeWindow | None = None
        self._update_win: UpdateDialog | None = None
        self._uninstall_win: UninstallDialog | None = None
        self._icons: dict[str, Image.Image] = {}

    # Session helpers

    def _load_session(self) -> None:
        try:
            self._session = self._manager.require()
        except PlaudSessionExpiredError:
            self._session = self._store.load()  # keep for display even if expired

    def _tray_state(self) -> str:
        if self._session is None:
            return "signed-out"
        days = self._manager.days_until_expiry()
        if days is None or days == 0:
            return "expired"
        if days <= 30:
            return "expiring"
        return "signed-in"

    # Tray icon / menu

    def _make_menu(self) -> pystray.Menu:
        state = self._tray_state()
        items: list = [
            pystray.MenuItem("Open", self._open_home, default=True, visible=False),
        ]

        if self._update_info:
            version, url, _zip_url = self._update_info
            if getattr(sys, "frozen", False):
                items.append(pystray.MenuItem(
                    f"Update available: v{version}",
                    lambda: self._open_update(),
                ))
            else:
                items.append(pystray.MenuItem(
                    f"Update available: v{version}",
                    lambda: self._open_url(url),
                ))
            items.append(pystray.Menu.SEPARATOR)

        if state == "expiring":
            days = self._manager.days_until_expiry() or 0
            items.append(pystray.MenuItem(f"Session expires in {days} days — sign in again", self._open_login))
            items.append(pystray.Menu.SEPARATOR)

        if self._session:
            items.append(pystray.MenuItem(f"Signed in as {self._session.email}", None, enabled=False))
            items.append(pystray.Menu.SEPARATOR)
            items.append(pystray.MenuItem("Test connection", self._open_test_connection))
            items.append(pystray.MenuItem("Manage AI clients…", self._open_wizard))
            items.append(pystray.MenuItem("Open log folder", self._open_log_folder))
            items.append(pystray.Menu.SEPARATOR)
            items.append(pystray.MenuItem(
                "Start with Windows",
                self._toggle_autostart,
                checked=lambda _: _autostart_enabled(),
            ))
            items.append(pystray.MenuItem("Sign out", self._sign_out))
        else:
            items.append(pystray.MenuItem("Sign in…", self._open_login))

        items.append(pystray.Menu.SEPARATOR)
        items.append(pystray.MenuItem("Uninstall…", self._open_uninstall))
        items.append(pystray.MenuItem("Quit", self._quit))
        return pystray.Menu(*items)

    def _refresh(self) -> None:
        if self._icon is None:
            return
        state = self._tray_state()
        self._icon.icon = self._icons[state]
        self._icon.title = f"{APP_NAME} — {state.replace('-', ' ')}"
        self._icon.menu = self._make_menu()

    # Menu actions (called from pystray thread — schedule on tkinter)

    def _tk(self, fn: Callable) -> None:
        if self._root:
            self._root.after(0, fn)

    def _open_home(self) -> None:
        if self._session:
            self._tk(lambda: self._home_win.show() if self._home_win else None)
        else:
            self._open_login()

    def _open_login(self) -> None:
        self._tk(lambda: self._login_win.show() if self._login_win else None)

    def _open_update(self) -> None:
        self._tk(lambda: self._update_win.show() if self._update_win else None)

    def _open_wizard(self) -> None:
        self._tk(lambda: self._wizard_win.show() if self._wizard_win else None)

    def _open_uninstall(self) -> None:
        self._tk(lambda: self._uninstall_win.show() if self._uninstall_win else None)

    def _open_test_connection(self) -> None:
        from tkinter import messagebox

        def _show(ok: bool, msg: str) -> None:
            if ok:
                messagebox.showinfo(APP_NAME, msg, parent=self._root)
            else:
                messagebox.showerror(APP_NAME, f"Connection failed:\n{msg}", parent=self._root)

        self._tk(lambda: self._test_connection(_show))

    def _check_for_update_action(self, on_done: Callable[[bool, str], None]) -> None:
        def _worker() -> None:
            result = _check_for_update()
            if result:
                self._update_info = result
                v = result[0]
                self._tk(self._refresh)
                if self._root:
                    self._root.after(0, lambda v=v: on_done(True, f"v{v} available"))
            else:
                if self._root:
                    self._root.after(0, lambda: on_done(False, "You're up to date."))
        threading.Thread(target=_worker, daemon=True).start()

    def _test_connection(self, on_done: Callable[[bool, str], None]) -> None:
        """Run get_user_info on a worker thread and deliver result on the tkinter thread.

        If the Plaud API does not respond within _TEST_CONNECTION_TIMEOUT seconds,
        on_done is called with a timeout error message.
        """
        result_holder: list[tuple[bool, str]] = []
        done_event = threading.Event()

        def _worker() -> None:
            try:
                PlaudClient(self._manager).get_user_info()
                result_holder.append((True, "Successfully connected to Plaud."))
            except Exception as exc:
                result_holder.append((False, str(exc)))
            done_event.set()

        t = threading.Thread(target=_worker, daemon=True)
        t.start()

        def _wait_and_deliver() -> None:
            finished = done_event.wait(timeout=_TEST_CONNECTION_TIMEOUT)
            if finished and result_holder:
                ok, msg = result_holder[0]
            else:
                ok, msg = False, "Plaud connection timed out."
            if self._root:
                self._root.after(0, lambda: on_done(ok, msg))

        threading.Thread(target=_wait_and_deliver, daemon=True).start()

    def _session_label(self) -> str:
        if self._session is None:
            return "Not signed in."
        days = self._manager.days_until_expiry()
        if days is None:
            return f"Signed in as {self._session.email}."
        return f"Signed in as {self._session.email}. Token valid for {days} days."

    def _sign_out(self) -> None:
        self._store.clear()
        self._session = None
        self._refresh()
        self._tk(lambda: self._login_win.show() if self._login_win else None)

    def _toggle_autostart(self) -> None:
        _set_autostart(not _autostart_enabled())

    def _quit(self) -> None:
        # Destroy the tkinter root on the tk main thread.  When mainloop()
        # returns, _run() calls icon.stop() from the main thread — safe.
        # Do NOT call icon.stop() here: if _quit() is invoked from the tk
        # main thread (e.g. via root.after()), calling icon.stop()
        # synchronously can deadlock because pystray's backend thread may be
        # waiting to schedule a callback back onto the tk thread.
        self._tk(lambda: self._root.destroy() if self._root else None)

    def _open_url(self, url: str) -> None:
        import webbrowser
        webbrowser.open(url)

    def _open_log_folder(self) -> None:
        log_dir = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local") / "PlaudTools"
        log_dir.mkdir(parents=True, exist_ok=True)
        if sys.platform == "win32":
            os.startfile(str(log_dir))  # type: ignore[attr-defined]
        else:
            import webbrowser
            webbrowser.open(log_dir.as_uri())

    # Post-login flow

    def _on_login_success(self) -> None:
        self._load_session()
        self._refresh()
        # Auto-heal any stale paths now that we have a session; the startup
        # _auto_heal returned early when no session was loaded yet.
        threading.Thread(target=self._auto_heal, daemon=True).start()
        if self._home_win:
            self._home_win.show()

    # Background tasks — see plaud_tools.tray.background._BackgroundMixin.

    # Entry

    def run(self) -> None:
        _setup_logging()
        logging.info("Plaud Tools %s starting", APP_VERSION)
        try:
            self._run()
        except Exception:
            logging.exception("fatal error in TrayApp.run")
            raise

    def _run(self) -> None:
        self._load_session()
        self._icons = _load_icons()
        logging.debug("icons loaded from %s", _assets_path())

        # tkinter root (hidden — only Toplevels are shown)
        root = tk.Tk()
        root.withdraw()
        _apply_theme(root)
        root.after(0, lambda: _set_app_icon(root))
        self._root = root

        self._login_win = LoginWindow(root, self._store, self._on_login_success)
        self._wizard_win = WizardWindow(root, on_done=lambda: None)
        self._update_win = UpdateDialog(root, self)
        self._uninstall_win = UninstallDialog(root)
        self._home_win = HomeWindow(
            root,
            on_test_connection=self._test_connection,
            on_check_for_update=self._check_for_update_action,
            on_open_update=self._open_update,
            on_open_wizard=self._open_wizard,
            on_sign_out=self._sign_out,
            on_open_uninstall=self._open_uninstall,
            on_repair_setup=self._repair_env,
            get_session_label=self._session_label,
            get_update_info=lambda: self._update_info,
            get_env_status=lambda: self._env_status,
            on_open_log_folder=self._open_log_folder,
        )

        # Build tray icon
        state = self._tray_state()
        self._icon = pystray.Icon(
            APP_NAME,
            self._icons[state],
            f"{APP_NAME}",
            menu=self._make_menu(),
        )
        self._icon.run_detached()

        # Background threads
        threading.Thread(target=self._update_poll_loop, daemon=True).start()
        threading.Thread(target=self._auto_heal, daemon=True).start()
        # Verify (not mutate) env — shows "Repair setup" button if anything is missing.
        # Actual setup is performed by install.ps1 at install time.
        threading.Thread(target=self._run_verify_env, daemon=True).start()
        threading.Thread(target=self._watch_activate_event, daemon=True).start()
        threading.Thread(target=self._event_poll_loop, daemon=True).start()

        # Always surface the appropriate window on launch.
        if self._session:
            root.after(200, self._home_win.show)
        else:
            root.after(200, self._login_win.show)

        # If the previous in-app update aborted, surface the reason. The
        # failure sentinel takes precedence over the success sentinel — and
        # update.ps1 deletes the success sentinel when writing the failure
        # one, so in practice they should never both exist.
        fail_sentinel = Path(tempfile.gettempdir()) / "plaud_update_failed.txt"
        if fail_sentinel.exists():
            try:
                import json as _json
                payload = _json.loads(fail_sentinel.read_text(encoding="utf-8"))
                fail_sentinel.unlink(missing_ok=True)
                reason = payload.get("reason", "Update failed for an unknown reason.")
                log_path = payload.get("log", "")
                logging.warning("Previous in-app update failed: %s (log: %s)", reason, log_path)

                def _show_update_failure(r: str = reason, lp: str = log_path) -> None:
                    from tkinter import messagebox
                    body = r
                    if lp:
                        body += f"\n\nLog file:\n{lp}"
                    messagebox.showerror(
                        f"{APP_NAME} — Update failed",
                        body,
                        parent=self._root,
                    )
                root.after(800, _show_update_failure)
            except Exception:
                logging.warning("Could not read update failure sentinel", exc_info=True)

        # If relaunched after an in-app update, open HomeWindow with a success message.
        sentinel = Path(tempfile.gettempdir()) / "plaud_just_updated.txt"
        if sentinel.exists():
            try:
                updated_to = sentinel.read_text(encoding="utf-8").strip()
                sentinel.unlink(missing_ok=True)
                if self._session and self._home_win:
                    def _show_update_success(v: str = updated_to) -> None:
                        self._home_win.show()
                        self._home_win._set_status(f"Updated to v{v} successfully.", ok=True)
                    root.after(500, _show_update_success)
            except Exception:
                logging.warning("Could not read update sentinel", exc_info=True)

        # If launched by install.ps1, show a Windows toast notification and
        # wire the HomeWindow welcome banner.  The sentinel is consumed here
        # (before the mainloop) so it is only shown once regardless of which
        # surface is seen first.
        install_sentinel = Path(tempfile.gettempdir()) / "plaud_just_installed.txt"
        if install_sentinel.exists():
            try:
                install_sentinel.unlink(missing_ok=True)
            except Exception:
                logging.warning("Could not delete install sentinel", exc_info=True)
            _show_install_toast()
            if self._session and self._home_win:
                self._home_win.arm_welcome_banner()
                root.after(500, self._home_win.show)

        root.mainloop()

        # Cleanup after mainloop exits
        if self._icon:
            self._icon.stop()


def main() -> None:
    if not _acquire_instance_lock():
        return
    if sys.platform == "win32":
        try:
            import ctypes
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("PlaudTools.TrayApp")
        except Exception:
            pass
    TrayApp().run()


__all__ = [
    "_TEST_CONNECTION_TIMEOUT", "_load_icon", "_load_icons",
    "_show_session_expired_toast", "_show_install_toast", "TrayApp", "main",
]
