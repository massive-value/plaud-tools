"""Plaud Tools system tray application.

Requires the [tray] optional dependencies:
    pip install plaud-tools[tray]

Entry point: plaud-tray (see pyproject.toml)
"""
from __future__ import annotations

import json
import logging
import os
import random
import subprocess
import sys
import tempfile
import threading
import time
import tkinter as tk
import urllib.request
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
# Logging (writes to %LOCALAPPDATA%\PlaudTools\tray.log in frozen builds)
# ---------------------------------------------------------------------------

def _setup_logging() -> None:
    log_dir = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local") / "PlaudTools"
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
# First-run environment setup (PATH + PowerShell completions)
# ---------------------------------------------------------------------------

def _cli_dir() -> Path | None:
    """Return the CLI directory inside the frozen bundle, or None in dev mode."""
    if not getattr(sys, "frozen", False) or sys.platform != "win32":
        return None
    return Path(sys.executable).parent / "cli"


def _completions_dir() -> Path | None:
    """Return the bundled completions directory, or None in dev mode."""
    if not getattr(sys, "frozen", False):
        return None
    meipass = getattr(sys, "_MEIPASS", None)
    candidates: list[Path] = []
    if meipass:
        candidates.append(Path(meipass) / "completions")
    candidates.append(Path(sys.executable).parent / "completions")
    candidates.append(Path(sys.executable).parent / "_internal" / "completions")
    for c in candidates:
        if (c / "plaud-tools.ps1").exists():
            return c
    return None


def _setup_cli_path() -> None:
    """Add PlaudTools/cli/ to the user PATH via HKCU\\Environment (idempotent)."""
    if sys.platform != "win32":
        return
    cli = _cli_dir()
    if cli is None or not cli.exists():
        return
    import ctypes
    import winreg
    cli_str = str(cli)
    try:
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER, "Environment", 0,
            winreg.KEY_QUERY_VALUE | winreg.KEY_SET_VALUE,
        ) as key:
            try:
                current, reg_type = winreg.QueryValueEx(key, "Path")
            except FileNotFoundError:
                current, reg_type = "", winreg.REG_EXPAND_SZ
            parts = [p.strip() for p in current.split(";") if p.strip()]
            if any(Path(p) == cli for p in parts):
                return
            parts.append(cli_str)
            winreg.SetValueEx(key, "Path", 0, reg_type, ";".join(parts))
        # Notify open shells that the user environment changed.
        HWND_BROADCAST = 0xFFFF
        WM_SETTINGCHANGE = 0x001A
        ctypes.windll.user32.SendMessageTimeoutW(
            HWND_BROADCAST, WM_SETTINGCHANGE, 0, "Environment",
            0x0002, 5000, None,
        )
        logging.info("Added %s to user PATH", cli_str)
    except OSError:
        logging.warning("Could not update user PATH", exc_info=True)


def _setup_ps_completions() -> None:
    """Source plaud-tools.ps1 from the user's PowerShell profiles (idempotent).

    Also removes any stale sourcing lines left by older builds that used plaud.ps1.
    """
    import re
    completions = _completions_dir()
    if completions is None:
        return
    ps1 = completions / "plaud-tools.ps1"
    if not ps1.exists():
        return
    source_line = f'. "{ps1}"'
    # Pattern matches any previous ". <...completions\plaud*.ps1>" sourcing lines
    stale_re = re.compile(r'^\. ".*[/\\]completions[/\\]plaud[^"]*\.ps1"', re.IGNORECASE)
    user_docs = Path.home() / "Documents"
    profiles = [
        user_docs / "PowerShell" / "Microsoft.PowerShell_profile.ps1",
        user_docs / "WindowsPowerShell" / "Microsoft.PowerShell_profile.ps1",
    ]
    for profile in profiles:
        try:
            if profile.exists():
                content = profile.read_text(encoding="utf-8-sig")
                lines = [l for l in content.splitlines(keepends=True) if not stale_re.match(l.strip())]
                content = "".join(lines)
                if source_line in content:
                    profile.write_text(content, encoding="utf-8")
                    continue
            else:
                profile.parent.mkdir(parents=True, exist_ok=True)
                content = ""
            profile.write_text(
                (content.rstrip("\n") + "\n" + source_line + "\n") if content else (source_line + "\n"),
                encoding="utf-8",
            )
            logging.info("Added plaud-tools completions to %s", profile)
        except OSError:
            logging.warning("Could not update PowerShell profile %s", profile, exc_info=True)


# ---------------------------------------------------------------------------
# Uninstall helpers (inverses of the setup helpers above)
# ---------------------------------------------------------------------------

def _remove_cli_path() -> None:
    """Remove PlaudTools/cli/ from the user PATH in HKCU\\Environment."""
    if sys.platform != "win32":
        return
    cli = _cli_dir()
    if cli is None:
        return
    import ctypes
    import winreg
    try:
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER, "Environment", 0,
            winreg.KEY_QUERY_VALUE | winreg.KEY_SET_VALUE,
        ) as key:
            try:
                current, reg_type = winreg.QueryValueEx(key, "Path")
            except FileNotFoundError:
                return
            parts = [p.strip() for p in current.split(";") if p.strip()]
            new_parts = [p for p in parts if Path(p) != cli]
            if len(new_parts) == len(parts):
                return  # nothing to remove
            winreg.SetValueEx(key, "Path", 0, reg_type, ";".join(new_parts))
        HWND_BROADCAST = 0xFFFF
        WM_SETTINGCHANGE = 0x001A
        ctypes.windll.user32.SendMessageTimeoutW(
            HWND_BROADCAST, WM_SETTINGCHANGE, 0, "Environment",
            0x0002, 5000, None,
        )
        logging.info("Removed %s from user PATH", cli)
    except OSError:
        logging.warning("Could not update user PATH during uninstall", exc_info=True)


def _remove_ps_completions() -> None:
    """Remove plaud-tools sourcing lines from the user's PowerShell profiles."""
    import re
    stale_re = re.compile(r'^\. ".*[/\\]completions[/\\]plaud[^"]*\.ps1"', re.IGNORECASE)
    user_docs = Path.home() / "Documents"
    profiles = [
        user_docs / "PowerShell" / "Microsoft.PowerShell_profile.ps1",
        user_docs / "WindowsPowerShell" / "Microsoft.PowerShell_profile.ps1",
    ]
    for profile in profiles:
        if not profile.exists():
            continue
        try:
            content = profile.read_text(encoding="utf-8-sig")
            lines = [l for l in content.splitlines(keepends=True) if not stale_re.match(l.strip())]
            new_content = "".join(lines)
            if new_content != content:
                profile.write_text(new_content, encoding="utf-8")
                logging.info("Removed plaud-tools completions from %s", profile)
        except OSError:
            logging.warning("Could not update PowerShell profile %s during uninstall", profile, exc_info=True)


def _delete_session_files() -> None:
    """Delete stored session/credentials via SessionStore and the fallback file."""
    SessionStore().clear()
    fallback = Path.home() / ".config" / "plaud-tools" / "session.json"
    try:
        fallback.unlink(missing_ok=True)
    except OSError:
        logging.warning("Could not delete session file %s", fallback, exc_info=True)
    logging.info("Deleted session/credentials")


def _delete_log_files() -> None:
    """Delete tray log files from both the legacy and current log directories."""
    localappdata = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
    # Check both legacy path (Plaud) and potential future path (PlaudTools)
    for log_dir_name in ("Plaud", "PlaudTools"):
        log_dir = localappdata / log_dir_name
        if not log_dir.exists():
            continue
        for log_file in log_dir.glob("tray.log*"):
            try:
                log_file.unlink(missing_ok=True)
                logging.info("Deleted log file %s", log_file)
            except OSError:
                logging.warning("Could not delete log file %s", log_file, exc_info=True)


def _launch_uninstall_helper(install_dir: Path, delete_logs: bool = False) -> None:
    """Write a .bat helper to %TEMP% that deletes the install dir after the tray exits."""
    import subprocess
    import tempfile
    tray_pid = os.getpid()
    localappdata = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    log_lines = ""
    if delete_logs:
        for name in ("PlaudTools", "Plaud"):
            log_dir = Path(localappdata) / name
            log_lines += f'rmdir /S /Q "{log_dir}" >NUL 2>&1\n'
    bat_content = (
        "@echo off\n"
        ":wait\n"
        f'tasklist /FI "PID eq {tray_pid}" 2>NUL | find /I "{tray_pid}" >NUL\n'
        "if %ERRORLEVEL%==0 (\n"
        "    timeout /t 1 /nobreak >NUL\n"
        "    goto wait\n"
        ")\n"
        'taskkill /F /IM plaud-mcp.exe >NUL 2>&1\n'
        'timeout /t 1 /nobreak >NUL\n'
        f'rmdir /S /Q "{install_dir}"\n'
        + log_lines +
        '(goto) 2>nul & del "%~f0"\n'
    )
    tmp = Path(tempfile.gettempdir()) / f"plaud_uninstall_{tray_pid}.bat"
    tmp.write_text(bat_content, encoding="utf-8")
    logging.info("Launching uninstall helper bat: %s (will delete %s)", tmp, install_dir)
    DETACHED_PROCESS = 0x00000008
    CREATE_NEW_PROCESS_GROUP = 0x00000200
    subprocess.Popen(
        ["cmd", "/c", "start", "", str(tmp)],
        creationflags=DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP,
        cwd=tempfile.gettempdir(),
    )


# ---------------------------------------------------------------------------
# Update check
# ---------------------------------------------------------------------------

def _version_gt(a: str, b: str) -> bool:
    try:
        return tuple(int(x) for x in a.split(".")) > tuple(int(x) for x in b.split("."))
    except ValueError:
        return False


def _check_for_update() -> tuple[str, str, str | None] | None:
    """Return (latest_version, release_url, zip_asset_url) if an update is available, else None.

    zip_asset_url is the browser_download_url of the PlaudTools.zip asset, or None if not found.
    """
    try:
        url = f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest"
        req = urllib.request.Request(url, headers={"User-Agent": f"{APP_NAME}/{APP_VERSION}"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode())
        latest = data["tag_name"].lstrip("v")
        if _version_gt(latest, APP_VERSION):
            zip_url: str | None = None
            for asset in data.get("assets", []):
                if asset.get("name") == "PlaudTools.zip":
                    zip_url = asset.get("browser_download_url")
                    break
            return latest, data["html_url"], zip_url
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
    ) -> None:
        self._root = root
        self._on_done = on_done
        self._win: tk.Toplevel | None = None
        self._row_widgets: dict[str, dict[str, object]] = {}
        self._help_var: tk.StringVar | None = None

    def show(self) -> None:
        if self._win and self._win.winfo_exists():
            self._win.lift()
            self._win.focus_force()
            self._render()
            return

        win = tk.Toplevel(self._root)
        win.title(f"{APP_NAME} — Configure AI Agents")
        win.resizable(False, False)
        win.geometry("460x340")
        self._win = win

        frame = ttk.Frame(win, padding=16)
        frame.pack(fill="both", expand=True)

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

        action_row = ttk.Frame(frame)
        action_row.pack(fill="x")
        ttk.Button(action_row, text="Close",
                   command=win.destroy).pack(side="right")

        win.lift()
        win.focus_force()
        self._render()

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
# Update dialog
# ---------------------------------------------------------------------------

class UpdateDialog:
    """Dialog that shows an available update and allows in-app install (frozen only)."""

    def __init__(self, root: tk.Tk, app: "TrayApp") -> None:
        self._root = root
        self._app = app
        self._win: tk.Toplevel | None = None

    def show(self) -> None:
        if self._win and self._win.winfo_exists():
            self._win.lift()
            self._win.focus_force()
            return

        update_info = self._app._update_info
        if update_info is None:
            return
        latest, url, zip_url = update_info

        win = tk.Toplevel(self._root)
        win.title(f"{APP_NAME} — Update available")
        win.resizable(False, False)
        win.geometry("400x240")
        self._win = win

        frame = ttk.Frame(win, padding=20)
        frame.pack(fill="both", expand=True)

        ttk.Label(frame, text="A new version of Plaud Tools is available.",
                  font=("Segoe UI", 10, "bold")).pack(anchor="w", pady=(0, 8))

        ttk.Label(frame, text=f"Current version:    {APP_VERSION}").pack(anchor="w")
        ttk.Label(frame, text=f"Available version:  {latest}").pack(anchor="w", pady=(0, 12))

        status_var = tk.StringVar()
        status_label = ttk.Label(frame, textvariable=status_var, foreground="#1d4ed8", wraplength=360)
        status_label.pack(anchor="w", pady=(0, 8))

        frozen = getattr(sys, "frozen", False)

        btn_frame = ttk.Frame(frame)
        btn_frame.pack(fill="x", pady=(4, 0))

        if frozen and zip_url:
            install_btn = ttk.Button(btn_frame, text="Install update and restart")
            install_btn.pack(side="left")

            def _start_install() -> None:
                install_btn.config(state="disabled")
                status_var.set("Downloading…")
                threading.Thread(
                    target=self._install_worker,
                    args=(zip_url, status_var, install_btn),
                    daemon=True,
                ).start()

            install_btn.config(command=_start_install)
        elif not frozen:
            ttk.Label(
                frame,
                text="In-app install is only available in the bundled tray.",
                foreground="#6b7280",
                font=("Segoe UI", 9),
            ).pack(anchor="w", pady=(0, 8))
        else:
            # Frozen but no zip asset found — fall back to browser
            ttk.Button(
                btn_frame,
                text="Open release page",
                command=lambda: self._app._open_url(url),
            ).pack(side="left")

        def _close() -> None:
            if win.winfo_exists():
                win.destroy()

        close_text = "Cancel" if (frozen and zip_url) else "Close"
        ttk.Button(btn_frame, text=close_text, command=_close).pack(side="left", padx=8)

        win.lift()
        win.focus_force()
        win.after(50, lambda: win.grab_set() if win.winfo_exists() else None)

    def _install_worker(
        self,
        zip_url: str,
        status_var: tk.StringVar,
        install_btn: ttk.Button,
    ) -> None:
        """Download the zip, write the .bat helper, launch it, then quit the tray."""

        def _set_status(text: str) -> None:
            if self._root:
                self._root.after(0, lambda: status_var.set(text))

        def _on_error(err: Exception) -> None:
            logging.exception("in-app update download failed")
            if self._root:
                self._root.after(0, lambda: (
                    status_var.set(f"Download failed: {err}"),
                    install_btn.config(state="normal"),
                ))

        try:
            req = urllib.request.Request(
                zip_url,
                headers={"User-Agent": f"{APP_NAME}/{APP_VERSION}"},
            )
            with urllib.request.urlopen(req, timeout=60) as resp:
                content_length = resp.headers.get("Content-Length")
                total_mb: float | None = (
                    int(content_length) / (1024 * 1024) if content_length else None
                )
                tmp = tempfile.NamedTemporaryFile(
                    suffix=".zip", delete=False, prefix="plaud_update_"
                )
                try:
                    downloaded = 0
                    chunk_size = 65536
                    while True:
                        chunk = resp.read(chunk_size)
                        if not chunk:
                            break
                        tmp.write(chunk)
                        downloaded += len(chunk)
                        downloaded_mb = downloaded / (1024 * 1024)
                        if total_mb is not None:
                            label = f"Downloading… ({downloaded_mb:.1f} MB / {total_mb:.1f} MB)"
                        else:
                            label = f"Downloading… ({downloaded_mb:.1f} MB)"
                        _set_status(label)
                finally:
                    tmp.close()

            zip_path = Path(tmp.name)
        except Exception as exc:
            _on_error(exc)
            return

        try:
            _set_status("Installing…")

            install_dir = Path(sys.executable).parent
            # The zip contains a top-level PlaudTools\ folder, so extract to
            # the parent so files land at Programs\PlaudTools\ not Programs\PlaudTools\PlaudTools\.
            extract_dir = install_dir.parent
            tray_pid = os.getpid()
            bat_path = Path(tempfile.gettempdir()) / f"plaud_update_{tray_pid}.bat"

            bat_content = (
                "@echo off\n"
                ":wait\n"
                f'tasklist /FI "PID eq {tray_pid}" 2>NUL | find /I "{tray_pid}" >NUL\n'
                "if %ERRORLEVEL%==0 (\n"
                "    timeout /t 1 /nobreak >NUL\n"
                "    goto wait\n"
                ")\n"
                # timeout /t is unreliable in detached/minimised consoles — use PowerShell
                'powershell -NoProfile -Command "Start-Sleep -Seconds 2"\n'
                f'powershell -NoProfile -Command "$ProgressPreference=\'SilentlyContinue\'; Expand-Archive -Path \'{zip_path}\' -DestinationPath \'{extract_dir}\' -Force"\n'
                f'powershell -NoProfile -Command "Start-Process \'{install_dir}\\PlaudTools.exe\'"\n'
                '(goto) 2>nul & del "%~f0"\n'
            )
            bat_path.write_text(bat_content, encoding="utf-8")

            subprocess.Popen(
                ["cmd", "/c", "start", "/min", "", str(bat_path)],
                creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP,
                cwd=tempfile.gettempdir(),
            )

            if self._root:
                self._root.after(0, self._app._quit)

        except Exception as exc:
            _on_error(exc)


# ---------------------------------------------------------------------------
# Uninstall dialog
# ---------------------------------------------------------------------------

class UninstallDialog:
    """Checklist dialog that removes selected Plaud Tools components."""

    def __init__(self, root: tk.Tk) -> None:
        self._root = root
        self._win: tk.Toplevel | None = None

    def show(self) -> None:
        if self._win and self._win.winfo_exists():
            self._win.lift()
            self._win.focus_force()
            return

        win = tk.Toplevel(self._root)
        win.title(f"Uninstall {APP_NAME}")
        win.resizable(False, False)
        self._win = win

        frame = ttk.Frame(win, padding=16)
        frame.pack(fill="both", expand=True)

        ttk.Label(
            frame,
            text="Select items to remove:",
            font=("", 10, "bold"),
        ).pack(anchor="w", pady=(0, 8))

        # --- checkboxes ---
        var_clients   = tk.BooleanVar(value=True)
        var_path      = tk.BooleanVar(value=True)
        var_autostart = tk.BooleanVar(value=True)
        var_ps        = tk.BooleanVar(value=True)
        var_installdir = tk.BooleanVar(value=True)
        var_session   = tk.BooleanVar(value=False)
        var_logs      = tk.BooleanVar(value=False)

        checks_frame = ttk.Frame(frame)
        checks_frame.pack(fill="x")

        items = [
            (var_clients,    "Disconnect AI clients (Claude Desktop, Claude Code, Codex)"),
            (var_path,       "Remove from user PATH"),
            (var_autostart,  "Remove autostart registry key"),
            (var_ps,         "Remove PowerShell profile sourcing lines"),
            (var_installdir, "Delete install directory"),
            (var_session,    "Delete session / credentials"),
            (var_logs,       "Delete log files"),
        ]
        for var, label in items:
            ttk.Checkbutton(checks_frame, text=label, variable=var).pack(anchor="w", pady=2)

        ttk.Separator(frame, orient="horizontal").pack(fill="x", pady=12)

        btn_row = ttk.Frame(frame)
        btn_row.pack(fill="x")

        ttk.Button(btn_row, text="Cancel", command=win.destroy).pack(side="left")

        def do_uninstall() -> None:
            from tkinter import messagebox

            # Execute simple removals first.
            if var_clients.get():
                try:
                    from .ai_clients import disconnect_all
                    disconnect_all()
                    logging.info("Disconnected AI clients")
                except Exception:
                    logging.warning("Could not disconnect AI clients", exc_info=True)
            if var_path.get():
                _remove_cli_path()
            if var_autostart.get():
                try:
                    _set_autostart(False)
                    logging.info("Removed autostart registry key")
                except Exception:
                    logging.warning("Could not remove autostart key", exc_info=True)
            if var_ps.get():
                _remove_ps_completions()
            if var_session.get():
                _delete_session_files()
            if var_logs.get():
                _delete_log_files()

            # Install directory deletion: requires .bat helper in frozen mode.
            if var_installdir.get():
                if not getattr(sys, "frozen", False):
                    logging.warning("dev mode: skipping install dir deletion")
                    win.destroy()
                    messagebox.showinfo(
                        APP_NAME,
                        "Uninstall complete. The selected items were removed.\n\n"
                        "(Install directory deletion skipped in dev mode.)",
                        parent=self._root,
                    )
                else:
                    install_dir = Path(sys.executable).parent
                    win.destroy()
                    _launch_uninstall_helper(install_dir, delete_logs=var_logs.get())
                    # Quit the tray so the helper can delete the directory.
                    if self._root:
                        self._root.after(0, lambda: self._root.destroy() if self._root else None)  # type: ignore[union-attr]
                    # (icon.stop() will be called by TrayApp._quit path via mainloop exit)
            else:
                win.destroy()
                messagebox.showinfo(
                    APP_NAME,
                    "Uninstall complete. The selected items were removed.",
                    parent=self._root,
                )

        ttk.Button(btn_row, text="Uninstall", command=do_uninstall).pack(side="right")

        win.lift()
        win.focus_force()
        win.after(50, lambda: win.grab_set() if win.winfo_exists() else None)


# ---------------------------------------------------------------------------
# Home window (tray left-click target)
# ---------------------------------------------------------------------------

class HomeWindow:
    def __init__(
        self,
        root: tk.Tk,
        on_test_connection: Callable[[Callable[[bool, str], None]], None],
        on_check_for_update: Callable[[Callable[[bool, str], None]], None],
        on_open_update: Callable[[], None],
        on_open_wizard: Callable[[], None],
        on_sign_out: Callable[[], None],
        on_open_uninstall: Callable[[], None],
        get_session_label: Callable[[], str],
        get_update_info: Callable[[], "tuple[str, str, str | None] | None"],
    ) -> None:
        self._root = root
        self._on_test_connection = on_test_connection
        self._on_check_for_update = on_check_for_update
        self._on_open_update = on_open_update
        self._on_open_wizard = on_open_wizard
        self._on_sign_out = on_sign_out
        self._on_open_uninstall = on_open_uninstall
        self._get_session_label = get_session_label
        self._get_update_info = get_update_info
        self._win: tk.Toplevel | None = None
        self._session_var: tk.StringVar | None = None
        self._status_var: tk.StringVar | None = None
        self._status_label: ttk.Label | None = None
        self._test_btn: ttk.Button | None = None
        self._update_btn: ttk.Button | None = None

    def show(self) -> None:
        if self._win and self._win.winfo_exists():
            self._win.lift()
            self._win.focus_force()
            self._refresh_session()
            self._refresh_update_btn()
            return

        win = tk.Toplevel(self._root)
        win.title(APP_NAME)
        win.resizable(False, False)
        win.geometry("400x420")
        self._win = win

        frame = ttk.Frame(win, padding=20)
        frame.pack(fill="both", expand=True)

        self._session_var = tk.StringVar()
        ttk.Label(frame, textvariable=self._session_var,
                  font=("", 10, "bold"), wraplength=360).pack(anchor="w")
        self._refresh_session()

        ttk.Separator(frame, orient="horizontal").pack(fill="x", pady=10)

        btn_frame = ttk.Frame(frame)
        btn_frame.pack(fill="x")

        ttk.Button(btn_frame, text="Configure AI Agents…",
                   command=self._on_open_wizard).pack(fill="x", pady=(0, 6))

        self._test_btn = ttk.Button(btn_frame, text="Test Connection",
                                     command=self._handle_test)
        self._test_btn.pack(fill="x", pady=(0, 6))

        self._update_btn = ttk.Button(btn_frame, text="Check for Updates",
                                       command=self._handle_check_update)
        self._update_btn.pack(fill="x")
        self._refresh_update_btn()

        self._status_var = tk.StringVar()
        self._status_label = ttk.Label(frame, textvariable=self._status_var,
                                        foreground="#15803d",
                                        font=("Segoe UI", 9), wraplength=360)
        self._status_label.pack(anchor="w", pady=(6, 0))

        ttk.Separator(frame, orient="horizontal").pack(fill="x", pady=10)

        ttk.Button(frame, text="Sign out",
                   command=self._handle_sign_out).pack(fill="x", pady=(0, 6))

        ttk.Separator(frame, orient="horizontal").pack(fill="x", pady=(0, 6))

        ttk.Button(frame, text="Uninstall…",
                   command=self._handle_uninstall).pack(fill="x")

        footer = ttk.Frame(frame)
        footer.pack(fill="x", pady=(10, 0))
        ttk.Label(footer, text=f"v{APP_VERSION}",
                  foreground="#6b7280",
                  font=("Segoe UI", 8)).pack(side="right")

        win.lift()
        win.focus_force()

    def _refresh_session(self) -> None:
        if self._session_var is not None:
            self._session_var.set(self._get_session_label())

    def _refresh_update_btn(self) -> None:
        if self._update_btn is None:
            return
        if self._get_update_info() is not None:
            self._update_btn.configure(state="disabled")
        else:
            self._update_btn.configure(state="normal")

    def _set_status(self, msg: str, ok: bool = True) -> None:
        if self._status_var is None or self._status_label is None:
            return
        self._status_label.configure(foreground="#15803d" if ok else "#c0392b")
        self._status_var.set(msg)

    def _handle_test(self) -> None:
        if self._test_btn is None:
            return
        self._test_btn.configure(state="disabled", text="Testing…")
        if self._status_var is not None:
            self._status_var.set("")

        def _done(ok: bool, msg: str) -> None:
            if self._test_btn is None:
                return
            self._test_btn.configure(state="normal", text="Test connection")
            self._set_status(msg, ok)
            if self._win and self._win.winfo_exists():
                self._win.after(4000, lambda: self._status_var.set("") if self._status_var else None)

        self._on_test_connection(_done)

    def _handle_check_update(self) -> None:
        if self._update_btn is None:
            return
        self._update_btn.configure(state="disabled", text="Checking…")
        if self._status_var is not None:
            self._status_var.set("")

        def _done(found: bool, msg: str) -> None:
            if self._update_btn is None:
                return
            self._update_btn.configure(text="Check for updates")
            if found:
                self._set_status(msg, ok=True)
                self._on_open_update()
            else:
                self._update_btn.configure(state="normal")
                self._set_status(msg, ok=True)
                if self._win and self._win.winfo_exists():
                    self._win.after(4000, lambda: self._status_var.set("") if self._status_var else None)

        self._on_check_for_update(_done)

    def _handle_sign_out(self) -> None:
        self._on_sign_out()
        if self._win and self._win.winfo_exists():
            self._win.destroy()

    def _handle_uninstall(self) -> None:
        self._on_open_uninstall()


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
        self._update_info: tuple[str, str, str | None] | None = None
        self._login_win: LoginWindow | None = None
        self._wizard_win: WizardWindow | None = None
        self._home_win: HomeWindow | None = None
        self._update_win: UpdateDialog | None = None
        self._uninstall_win: UninstallDialog | None = None
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

    # ------------------------------------------------------------------
    # Menu actions (called from pystray thread — schedule on tkinter)
    # ------------------------------------------------------------------

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
        log_dir = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local") / "PlaudTools"
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
        if self._home_win:
            self._home_win.show()

    # ------------------------------------------------------------------
    # Background tasks
    # ------------------------------------------------------------------

    def _update_poll_loop(self) -> None:
        interval_seconds = random.uniform(20 * 3600, 28 * 3600)
        # Run the first check immediately (preserves current startup behaviour).
        try:
            result = _check_for_update()
            if result:
                self._update_info = result
                self._refresh()
        except Exception:
            logging.warning("update check failed", exc_info=True)
        last_check_wall_time = time.time()
        while True:
            time.sleep(300)
            if time.time() - last_check_wall_time >= interval_seconds:
                try:
                    result = _check_for_update()
                    if result:
                        self._update_info = result
                        self._refresh()
                except Exception:
                    logging.warning("update check failed", exc_info=True)
                last_check_wall_time = time.time()
                interval_seconds = random.uniform(20 * 3600, 28 * 3600)

    def _auto_heal(self) -> None:
        if not self._session:
            return
        mcp = _mcp_exe()
        statuses = status_all(mcp)
        if any(s == "stale" for s in statuses.values()):
            connect_all(mcp)

    def _setup_env(self) -> None:
        _setup_cli_path()
        _setup_ps_completions()

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
            get_session_label=self._session_label,
            get_update_info=lambda: self._update_info,
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
        threading.Thread(target=self._setup_env, daemon=True).start()

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
