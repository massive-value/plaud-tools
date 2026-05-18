"""Plaud Tools system tray application.

Requires the [tray] optional dependencies:
    pip install plaud-tools[tray]

Entry point: plaud-tray (see pyproject.toml)
"""
from __future__ import annotations

import json
import logging
import os
import sys
import threading
import tkinter as tk
from tkinter import ttk
from pathlib import Path
from typing import Callable

import pystray
from PIL import Image, ImageDraw

from .ai_clients import CLIENTS, connect, connect_all, disconnect, status_all
from .auth import PlaudAuth
from .client import PlaudClient
from .errors import PlaudApiError, PlaudSessionExpiredError
from .session import PlaudSession, SessionManager, SessionStore

APP_NAME = "Plaud Tools"
APP_VERSION = "0.1.2"
GITHUB_REPO = "massive-value/plaud-tools"


# ---------------------------------------------------------------------------
# Logging (writes to %LOCALAPPDATA%\Plaud\tray.log in frozen builds)
# ---------------------------------------------------------------------------

def _setup_logging() -> None:
    log_dir = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local") / "Plaud"
    log_dir.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        filename=str(log_dir / "tray.log"),
        level=logging.DEBUG,
        format="%(asctime)s %(levelname)s %(message)s",
        encoding="utf-8",
    )
    # Also capture unhandled tkinter callback exceptions
    def _tk_error(exc, val, tb):  # type: ignore[override]
        logging.exception("tkinter callback error", exc_info=(exc, val, tb))
    tk.Tk.report_callback_exception = _tk_error  # type: ignore[assignment]

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

def _mcp_exe() -> str:
    if getattr(sys, "frozen", False):
        return str(Path(sys.executable).parent / "mcp" / "plaud-mcp.exe")
    # Dev fallback: PyInstaller onedir output next to repo root
    return str(Path(__file__).parent.parent.parent / "out" / "plaud-mcp" / "plaud-mcp" / "plaud-mcp.exe")


# ---------------------------------------------------------------------------
# Icons — loaded from bundled assets, Pillow circle as fallback
# ---------------------------------------------------------------------------

def _assets_path() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent / "assets"
    return Path(__file__).parent / "assets"


def _load_icon(filename: str, fallback_color: str) -> Image.Image:
    path = _assets_path() / filename
    if path.exists():
        return Image.open(path).convert("RGBA")
    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    ImageDraw.Draw(img).ellipse([4, 4, 60, 60], fill=fallback_color)
    return img


def _load_icons() -> dict[str, Image.Image]:
    return {
        "signed-out": _load_icon("tray-signed-out.png", "#888888"),
        "signed-in":  _load_icon("tray-signed-in.png",  "#27ae60"),
        "expiring":   _load_icon("tray-expiring.png",   "#f39c12"),
        "expired":    _load_icon("tray-expired.png",    "#e74c3c"),
    }


# ---------------------------------------------------------------------------
# Single-instance lock (Windows only)
# ---------------------------------------------------------------------------

_MUTEX_HANDLE = None

def _acquire_instance_lock() -> bool:
    global _MUTEX_HANDLE
    if sys.platform != "win32":
        return True
    import ctypes
    _MUTEX_HANDLE = ctypes.windll.kernel32.CreateMutexW(None, False, f"Global\\{APP_NAME.replace(' ', '')}Instance")
    return ctypes.windll.kernel32.GetLastError() != 183  # 183 = ERROR_ALREADY_EXISTS


# ---------------------------------------------------------------------------
# Autostart (Windows registry)
# ---------------------------------------------------------------------------

_AUTOSTART_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
_AUTOSTART_NAME = APP_NAME


def _autostart_enabled() -> bool:
    if sys.platform != "win32":
        return False
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _AUTOSTART_KEY) as key:
            val, _ = winreg.QueryValueEx(key, _AUTOSTART_NAME)
            return Path(val).resolve() == Path(sys.executable).resolve()
    except OSError:
        return False


def _set_autostart(enable: bool) -> None:
    if sys.platform != "win32":
        return
    import winreg
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _AUTOSTART_KEY, 0, winreg.KEY_SET_VALUE) as key:
        if enable:
            winreg.SetValueEx(key, _AUTOSTART_NAME, 0, winreg.REG_SZ, str(sys.executable))
        else:
            try:
                winreg.DeleteValue(key, _AUTOSTART_NAME)
            except FileNotFoundError:
                pass


# ---------------------------------------------------------------------------
# Update check
# ---------------------------------------------------------------------------

def _version_gt(a: str, b: str) -> bool:
    try:
        return tuple(int(x) for x in a.split(".")) > tuple(int(x) for x in b.split("."))
    except ValueError:
        return False


def _check_for_update() -> tuple[str, str] | None:
    """Return (latest_version, release_url) if an update is available, else None."""
    import urllib.request
    try:
        url = f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest"
        req = urllib.request.Request(url, headers={"User-Agent": f"{APP_NAME}/{APP_VERSION}"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode())
        latest = data["tag_name"].lstrip("v")
        if _version_gt(latest, APP_VERSION):
            return latest, data["html_url"]
    except Exception:
        pass
    return None


# ---------------------------------------------------------------------------
# Login window
# ---------------------------------------------------------------------------

class LoginWindow:
    def __init__(self, root: tk.Tk, store: SessionStore, on_success: Callable) -> None:
        self._root = root
        self._store = store
        self._on_success = on_success
        self._win: tk.Toplevel | None = None

    def show(self) -> None:
        if self._win and self._win.winfo_exists():
            self._win.lift()
            self._win.focus_force()
            return
        win = tk.Toplevel(self._root)
        win.title(f"{APP_NAME} — Sign in")
        win.resizable(False, False)
        win.geometry("340x220")
        self._win = win  # set before widget build so partial state is always visible

        frame = ttk.Frame(win, padding=16)
        frame.pack(fill="both", expand=True)

        ttk.Label(frame, text="Email").grid(row=0, column=0, sticky="w", pady=4)
        email_var = tk.StringVar()
        ttk.Entry(frame, textvariable=email_var, width=32).grid(row=0, column=1, padx=8, pady=4)

        ttk.Label(frame, text="Password").grid(row=1, column=0, sticky="w", pady=4)
        password_var = tk.StringVar()
        ttk.Entry(frame, textvariable=password_var, show="•", width=32).grid(row=1, column=1, padx=8, pady=4)

        ttk.Label(frame, text="Region").grid(row=2, column=0, sticky="w", pady=4)
        region_var = tk.StringVar(value="us")
        ttk.Combobox(frame, textvariable=region_var, values=["us", "eu"], state="readonly", width=10).grid(
            row=2, column=1, sticky="w", padx=8, pady=4
        )

        error_var = tk.StringVar()
        error_label = ttk.Label(frame, textvariable=error_var, foreground="#c0392b", wraplength=280)
        error_label.grid(row=3, column=0, columnspan=2, pady=4)

        btn = ttk.Button(frame, text="Sign in")
        btn.grid(row=4, column=0, columnspan=2, pady=8)

        def do_login() -> None:
            btn.config(state="disabled", text="Signing in…")
            error_var.set("")
            win.update()
            try:
                auth = PlaudAuth(self._store)
                auth.login(email_var.get().strip(), password_var.get(), region_var.get())
                win.destroy()
                self._on_success()
            except (PlaudApiError, PlaudSessionExpiredError) as exc:
                error_var.set(str(exc))
                btn.config(state="normal", text="Sign in")

        btn.config(command=do_login)
        win.bind("<Return>", lambda _: do_login())

        # Bring to front after the event loop has drawn the window, then grab.
        # grab_set() requires the window to be "viewable"; deferring 50 ms
        # ensures it is mapped before we attempt to grab.
        win.lift()
        win.focus_force()
        win.after(50, lambda: win.grab_set() if win.winfo_exists() else None)


# ---------------------------------------------------------------------------
# Wizard window (AI client wiring)
# ---------------------------------------------------------------------------

class WizardWindow:
    def __init__(self, root: tk.Tk, on_done: Callable) -> None:
        self._root = root
        self._on_done = on_done
        self._win: tk.Toplevel | None = None

    def show(self) -> None:
        if self._win and self._win.winfo_exists():
            self._win.lift()
            self._win.focus_force()
            return
        mcp = _mcp_exe()
        statuses = status_all(mcp)

        win = tk.Toplevel(self._root)
        win.title(f"{APP_NAME} — AI client setup")
        win.resizable(False, False)
        win.geometry("380x260")
        self._win = win

        frame = ttk.Frame(win, padding=16)
        frame.pack(fill="both", expand=True)

        ttk.Label(frame, text="Connect Plaud to your AI clients:", font=("", 10, "bold")).pack(anchor="w", pady=(0, 8))

        vars_: dict[str, tk.BooleanVar] = {}
        status_labels: dict[str, tk.StringVar] = {}

        for cid, label in CLIENTS.items():
            s = statuses[cid]
            row = ttk.Frame(frame)
            row.pack(fill="x", pady=2)
            var = tk.BooleanVar(value=s in ("connected", "stale"))
            vars_[cid] = var
            sl = tk.StringVar(value=_status_text(s))
            status_labels[cid] = sl
            ttk.Checkbutton(row, text=label, variable=var, width=20).pack(side="left")
            ttk.Label(row, textvariable=sl, foreground=_status_color(s)).pack(side="left")

        msg_var = tk.StringVar()
        ttk.Label(frame, textvariable=msg_var, foreground="#27ae60").pack(pady=8)

        def apply() -> None:
            for cid, var in vars_.items():
                try:
                    if var.get():
                        connect(cid, mcp)
                    else:
                        disconnect(cid)
                    new_status = status_all(mcp)[cid]
                    status_labels[cid].set(_status_text(new_status))
                except Exception:
                    status_labels[cid].set("error")
            msg_var.set("Changes applied. Restart your AI client to take effect.")
            self._on_done()

        def close() -> None:
            win.destroy()

        btn_frame = ttk.Frame(frame)
        btn_frame.pack(anchor="e", pady=(4, 0))
        ttk.Button(btn_frame, text="Apply", command=apply).pack(side="left", padx=4)
        ttk.Button(btn_frame, text="Close", command=close).pack(side="left")

        win.lift()
        win.focus_force()


def _status_text(s: str) -> str:
    return {"not-detected": "not installed", "not-connected": "not connected",
            "connected": "connected", "stale": "stale path"}.get(s, s)


def _status_color(s: str) -> str:
    return {"connected": "#27ae60", "stale": "#f39c12"}.get(s, "#888888")


# ---------------------------------------------------------------------------
# Main tray application
# ---------------------------------------------------------------------------

class TrayApp:
    def __init__(self) -> None:
        self._store = SessionStore()
        self._manager = SessionManager(self._store)
        self._session: PlaudSession | None = None
        self._icon: pystray.Icon | None = None
        self._root: tk.Tk | None = None
        self._update_info: tuple[str, str] | None = None
        self._login_win: LoginWindow | None = None
        self._wizard_win: WizardWindow | None = None
        self._icons: dict[str, Image.Image] = {}

    # ------------------------------------------------------------------
    # Session helpers
    # ------------------------------------------------------------------

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

    # ------------------------------------------------------------------
    # Tray icon / menu
    # ------------------------------------------------------------------

    def _make_menu(self) -> pystray.Menu:
        state = self._tray_state()
        items: list = []

        if self._update_info:
            version, url = self._update_info
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
            items.append(pystray.MenuItem("Manage AI clients…", self._open_wizard, default=True))
            items.append(pystray.Menu.SEPARATOR)
            items.append(pystray.MenuItem(
                "Start with Windows",
                self._toggle_autostart,
                checked=lambda _: _autostart_enabled(),
            ))
            items.append(pystray.MenuItem("Sign out", self._sign_out))
        else:
            items.append(pystray.MenuItem("Sign in…", self._open_login, default=True))

        items.append(pystray.Menu.SEPARATOR)
        items.append(pystray.MenuItem("Quit", self._quit))
        return pystray.Menu(*items)

    def _refresh(self) -> None:
        if self._icon is None:
            return
        state = self._tray_state()
        self._icon.icon = self._icons[state]
        self._icon.title = f"{APP_NAME} — {state.replace('-', ' ')}"
        self._icon.menu = self._make_menu()

    # ------------------------------------------------------------------
    # Menu actions (called from pystray thread — schedule on tkinter)
    # ------------------------------------------------------------------

    def _tk(self, fn: Callable) -> None:
        if self._root:
            self._root.after(0, fn)

    def _open_login(self) -> None:
        self._tk(lambda: self._login_win.show() if self._login_win else None)

    def _open_wizard(self) -> None:
        self._tk(lambda: self._wizard_win.show() if self._wizard_win else None)

    def _open_test_connection(self) -> None:
        self._tk(self._test_connection)

    def _test_connection(self) -> None:
        def _worker() -> None:
            try:
                PlaudClient(self._manager).get_user_info()
                ok, msg = True, "Successfully connected to Plaud."
            except Exception as exc:
                ok, msg = False, str(exc)
            def _show() -> None:
                from tkinter import messagebox
                if ok:
                    messagebox.showinfo(APP_NAME, msg, parent=self._root)
                else:
                    messagebox.showerror(APP_NAME, f"Connection failed:\n{msg}", parent=self._root)
            if self._root:
                self._root.after(0, _show)
        threading.Thread(target=_worker, daemon=True).start()

    def _sign_out(self) -> None:
        self._store.clear()
        self._session = None
        self._refresh()
        self._tk(lambda: self._login_win.show() if self._login_win else None)

    def _toggle_autostart(self) -> None:
        _set_autostart(not _autostart_enabled())

    def _quit(self) -> None:
        self._tk(lambda: self._root.destroy() if self._root else None)
        if self._icon:
            self._icon.stop()

    def _open_url(self, url: str) -> None:
        import webbrowser
        webbrowser.open(url)

    # ------------------------------------------------------------------
    # Post-login flow
    # ------------------------------------------------------------------

    def _on_login_success(self) -> None:
        self._load_session()
        self._refresh()
        if self._wizard_win:
            self._wizard_win.show()

    # ------------------------------------------------------------------
    # Background tasks
    # ------------------------------------------------------------------

    def _poll_update(self) -> None:
        result = _check_for_update()
        if result:
            self._update_info = result
            self._refresh()

    def _auto_heal(self) -> None:
        if not self._session:
            return
        mcp = _mcp_exe()
        statuses = status_all(mcp)
        if any(s == "stale" for s in statuses.values()):
            connect_all(mcp)

    # ------------------------------------------------------------------
    # Entry
    # ------------------------------------------------------------------

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
        self._root = root

        self._login_win = LoginWindow(root, self._store, self._on_login_success)
        self._wizard_win = WizardWindow(root, lambda: None)

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
        threading.Thread(target=self._poll_update, daemon=True).start()
        threading.Thread(target=self._auto_heal, daemon=True).start()

        # Ensure autostart on first run when a session already exists
        if self._session and not _autostart_enabled():
            _set_autostart(True)

        # Show login window on first launch if not signed in
        if not self._session:
            root.after(200, self._login_win.show)

        root.mainloop()

        # Cleanup after mainloop exits
        if self._icon:
            self._icon.stop()


def main() -> None:
    if not _acquire_instance_lock():
        return
    TrayApp().run()
