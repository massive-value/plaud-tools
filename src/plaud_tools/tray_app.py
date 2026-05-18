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
from . import __version__ as APP_VERSION
from .client import PlaudClient
from .errors import PlaudApiError, PlaudSessionExpiredError
from .session import PlaudSession, SessionManager, SessionStore

APP_NAME = "Plaud Tools"
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
    # PyInstaller (onefile + onedir 6+) exposes data files via sys._MEIPASS,
    # which points at _internal/ for onedir. The exe-parent fallback covers
    # older PyInstaller layouts where data files sat next to the exe.
    if getattr(sys, "frozen", False):
        meipass = getattr(sys, "_MEIPASS", None)
        candidates = []
        if meipass:
            candidates.append(Path(meipass) / "assets")
        candidates.append(Path(sys.executable).parent / "assets")
        candidates.append(Path(sys.executable).parent / "_internal" / "assets")
        for c in candidates:
            if c.exists():
                return c
        return candidates[0]
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
# Theme (Sun Valley via sv_ttk) — falls back silently to default ttk if the
# dependency is missing so dev installs without the [tray] extras still run.
# ---------------------------------------------------------------------------

def _apply_theme(root: tk.Tk) -> None:
    try:
        import sv_ttk
        sv_ttk.set_theme("light")
    except Exception:
        logging.warning("sv_ttk theme unavailable; falling back to default ttk", exc_info=True)
        return
    style = ttk.Style(root)
    # Slightly larger default font and roomier button padding to match the
    # Sun Valley look-and-feel.
    style.configure(".", font=("Segoe UI", 10))
    style.configure("TButton", padding=(12, 4))


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

_STATUS_BADGE: dict[str, tuple[str, str]] = {
    "not-detected": ("Not installed", "#6b7280"),
    "not-connected": ("Not connected", "#1d4ed8"),
    "connected": ("✓ Connected", "#15803d"),
    "stale": ("⚠ Path outdated", "#b45309"),
}


class WizardWindow:
    def __init__(
        self,
        root: tk.Tk,
        on_done: Callable,
        on_test_connection: Callable[[Callable[[bool, str], None]], None],
        on_sign_out: Callable[[], None],
        get_session_label: Callable[[], str],
    ) -> None:
        self._root = root
        self._on_done = on_done
        self._on_test_connection = on_test_connection
        self._on_sign_out = on_sign_out
        self._get_session_label = get_session_label
        self._win: tk.Toplevel | None = None
        self._row_widgets: dict[str, dict[str, object]] = {}
        self._help_var: tk.StringVar | None = None
        self._session_var: tk.StringVar | None = None
        self._test_btn: ttk.Button | None = None

    def show(self) -> None:
        if self._win and self._win.winfo_exists():
            self._win.lift()
            self._win.focus_force()
            self._refresh_session_label()
            self._render()
            return

        win = tk.Toplevel(self._root)
        win.title(f"{APP_NAME} — Status & AI clients")
        win.resizable(False, False)
        win.geometry("460x430")
        self._win = win

        frame = ttk.Frame(win, padding=16)
        frame.pack(fill="both", expand=True)

        # Signed-in header
        self._session_var = tk.StringVar()
        ttk.Label(frame, textvariable=self._session_var,
                  font=("", 10, "bold")).pack(anchor="w")
        self._refresh_session_label()

        ttk.Separator(frame, orient="horizontal").pack(fill="x", pady=8)

        ttk.Label(frame, text="Connect Plaud to your AI clients:",
                  font=("", 9, "bold")).pack(anchor="w", pady=(0, 4))

        rows_frame = ttk.Frame(frame)
        rows_frame.pack(fill="x")
        self._row_widgets.clear()
        for cid, label in CLIENTS.items():
            row = ttk.Frame(rows_frame)
            row.pack(fill="x", pady=4)
            name = ttk.Label(row, text=label, width=18)
            name.pack(side="left")
            badge_var = tk.StringVar()
            badge = ttk.Label(row, textvariable=badge_var)
            badge.pack(side="left", padx=(0, 8))
            btn = ttk.Button(row, text="…", width=12)
            btn.pack(side="right")
            self._row_widgets[cid] = {"badge": badge, "badge_var": badge_var, "btn": btn}

        self._help_var = tk.StringVar()
        help_label = ttk.Label(frame, textvariable=self._help_var,
                               foreground="#15803d", wraplength=420, justify="left")
        help_label.pack(anchor="w", pady=(8, 0))

        ttk.Separator(frame, orient="horizontal").pack(fill="x", pady=10)

        # Action row: Test connection + Sign out on the left, Close on the right.
        action_row = ttk.Frame(frame)
        action_row.pack(fill="x")

        self._test_btn = ttk.Button(action_row, text="Test connection",
                                    command=self._handle_test)
        self._test_btn.pack(side="left")

        ttk.Button(action_row, text="Sign out",
                   command=self._handle_sign_out).pack(side="left", padx=8)

        ttk.Button(action_row, text="Close",
                   command=win.destroy).pack(side="right")

        # Version footer — muted, bottom-right.
        footer = ttk.Frame(frame)
        footer.pack(fill="x", pady=(8, 0))
        ttk.Label(footer, text=f"v{APP_VERSION}",
                  foreground="#6b7280",
                  font=("Segoe UI", 8)).pack(side="right")

        win.lift()
        win.focus_force()
        self._render()

    # --- session header ---

    def _refresh_session_label(self) -> None:
        if self._session_var is not None:
            self._session_var.set(self._get_session_label())

    # --- action handlers ---

    def _handle_test(self) -> None:
        if self._test_btn is None:
            return
        self._test_btn.configure(state="disabled", text="Testing…")

        def _done(ok: bool, msg: str) -> None:
            if self._test_btn is None:
                return
            self._test_btn.configure(
                state="normal",
                text="✓ OK" if ok else "Failed",
            )
            if self._help_var is not None:
                self._help_var.set(
                    "Successfully connected to Plaud." if ok
                    else f"Connection failed: {msg}"
                )
            # Reset label after a few seconds so the button is reusable.
            def _reset() -> None:
                if self._win and self._win.winfo_exists() and self._test_btn is not None:
                    try:
                        self._test_btn.configure(text="Test connection")
                    except tk.TclError:
                        pass
            if self._win and self._win.winfo_exists():
                self._win.after(3500, _reset)

        self._on_test_connection(_done)

    def _handle_sign_out(self) -> None:
        self._on_sign_out()
        if self._win and self._win.winfo_exists():
            self._win.destroy()

    # --- helpers ---

    def _render(self) -> None:
        mcp = _mcp_exe()
        statuses = status_all(mcp)
        for cid, status in statuses.items():
            self._apply_status(cid, status, mcp)

    def _apply_status(self, cid: str, status: str, mcp: str) -> None:
        widgets = self._row_widgets.get(cid)
        if not widgets:
            return
        text, color = _STATUS_BADGE.get(status, ("unknown", "#6b7280"))
        widgets["badge_var"].set(text)        # type: ignore[union-attr]
        widgets["badge"].configure(foreground=color)  # type: ignore[union-attr]

        btn: ttk.Button = widgets["btn"]      # type: ignore[assignment]
        if status == "not-detected":
            btn.configure(text="—", state="disabled", command=lambda: None)
        elif status == "connected":
            btn.configure(text="Disconnect", state="normal",
                          command=lambda c=cid: self._do(c, "disconnect", mcp))
        elif status == "stale":
            btn.configure(text="Reconnect", state="normal",
                          command=lambda c=cid: self._do(c, "connect", mcp))
        else:  # not-connected
            btn.configure(text="Connect", state="normal",
                          command=lambda c=cid: self._do(c, "connect", mcp))

    def _do(self, cid: str, action: str, mcp: str) -> None:
        widgets = self._row_widgets[cid]
        btn: ttk.Button = widgets["btn"]      # type: ignore[assignment]
        btn.configure(state="disabled",
                      text="Connecting…" if action == "connect" else "Disconnecting…")
        try:
            if action == "connect":
                connect(cid, mcp)
            else:
                disconnect(cid)
        except Exception as exc:
            btn.configure(text=f"Failed: {exc}", state="normal")
            return
        self._render()
        if self._help_var is not None:
            label = CLIENTS[cid]
            if action == "connect":
                self._help_var.set(
                    f"✓ Connected {label}. Restart {label} to load the new MCP server, "
                    "then ask it about your Plaud notes to confirm."
                )
            else:
                self._help_var.set("")
        self._on_done()


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
            items.append(pystray.MenuItem("Open log folder", self._open_log_folder))
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
        from tkinter import messagebox

        def _show(ok: bool, msg: str) -> None:
            if ok:
                messagebox.showinfo(APP_NAME, msg, parent=self._root)
            else:
                messagebox.showerror(APP_NAME, f"Connection failed:\n{msg}", parent=self._root)

        self._tk(lambda: self._test_connection(_show))

    def _test_connection(self, on_done: Callable[[bool, str], None]) -> None:
        """Run get_user_info on a worker thread and deliver result on the tkinter thread."""
        def _worker() -> None:
            try:
                PlaudClient(self._manager).get_user_info()
                ok, msg = True, "Successfully connected to Plaud."
            except Exception as exc:
                ok, msg = False, str(exc)
            if self._root:
                self._root.after(0, lambda: on_done(ok, msg))
        threading.Thread(target=_worker, daemon=True).start()

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
        self._tk(lambda: self._root.destroy() if self._root else None)
        if self._icon:
            self._icon.stop()

    def _open_url(self, url: str) -> None:
        import webbrowser
        webbrowser.open(url)

    def _open_log_folder(self) -> None:
        log_dir = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local") / "Plaud"
        log_dir.mkdir(parents=True, exist_ok=True)
        if sys.platform == "win32":
            os.startfile(str(log_dir))  # type: ignore[attr-defined]
        else:
            import webbrowser
            webbrowser.open(log_dir.as_uri())

    # ------------------------------------------------------------------
    # Post-login flow
    # ------------------------------------------------------------------

    def _on_login_success(self) -> None:
        self._load_session()
        self._refresh()
        # Auto-heal any stale paths now that we have a session; the startup
        # _auto_heal returned early when no session was loaded yet.
        threading.Thread(target=self._auto_heal, daemon=True).start()
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
        _apply_theme(root)
        self._root = root

        self._login_win = LoginWindow(root, self._store, self._on_login_success)
        self._wizard_win = WizardWindow(
            root,
            on_done=lambda: None,
            on_test_connection=self._test_connection,
            on_sign_out=self._sign_out,
            get_session_label=self._session_label,
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
