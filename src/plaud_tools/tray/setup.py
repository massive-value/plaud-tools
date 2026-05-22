"""Tray setup helpers: logging, paths, install lock, autostart, PATH/completions.

These helpers were previously module-level functions in ``plaud_tools.tray_app``.
They live here now but are re-exported from the shim so existing tests and the
PyInstaller entry script keep working.
"""
from __future__ import annotations

import logging
import logging.handlers
import os
import subprocess
import sys
import tempfile
import tkinter as tk
from pathlib import Path

from ..ps1_templates import render_uninstall_ps1
from ..session import SessionStore

APP_NAME = "Plaud Tools"


# Logging (writes to %LOCALAPPDATA%\PlaudTools\tray.log in frozen builds)


def _setup_logging() -> None:
    log_dir = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local") / "PlaudTools"
    log_dir.mkdir(parents=True, exist_ok=True)
    handler = logging.handlers.RotatingFileHandler(
        str(log_dir / "tray.log"),
        maxBytes=1_000_000,
        backupCount=3,
        encoding="utf-8",
    )
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logging.basicConfig(
        level=logging.DEBUG,
        handlers=[handler],
    )
    # Also capture unhandled tkinter callback exceptions
    def _tk_error(exc, val, tb):  # type: ignore[override]
        logging.exception("tkinter callback error", exc_info=(exc, val, tb))
    tk.Tk.report_callback_exception = _tk_error  # type: ignore[assignment]


# Paths


def _mcp_exe() -> str:
    if getattr(sys, "frozen", False):
        return str(Path(sys.executable).parent / "mcp" / "plaud-mcp.exe")
    # Dev fallback: PyInstaller onedir output next to repo root
    return str(Path(__file__).parent.parent.parent.parent / "out" / "plaud-mcp" / "plaud-mcp" / "plaud-mcp.exe")


# Assets / theme


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
    return Path(__file__).parent.parent / "assets"


def _set_app_icon(win: tk.Wm) -> None:
    ico = _assets_path() / "icon.ico"
    if ico.exists():
        try:
            win.iconbitmap(str(ico))
        except Exception:
            pass


def _apply_theme(root: tk.Tk) -> None:
    from tkinter import ttk
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


# Single-instance lock (Windows only)

_MUTEX_HANDLE = None
_ACTIVATE_EVENT = "Global\\PlaudToolsActivate"


def _acquire_instance_lock() -> bool:
    global _MUTEX_HANDLE
    if sys.platform != "win32":
        return True
    import ctypes
    # "Global\" prefix is required so the named mutex is visible across user sessions (e.g. UAC elevation).
    _MUTEX_HANDLE = ctypes.windll.kernel32.CreateMutexW(None, False, f"Global\\{APP_NAME.replace(' ', '')}Instance")
    if ctypes.windll.kernel32.GetLastError() == 183:  # ERROR_ALREADY_EXISTS
        # Signal the running instance to surface its window, then exit.
        h = ctypes.windll.kernel32.OpenEventW(0x0002, False, _ACTIVATE_EVENT)  # EVENT_MODIFY_STATE
        if h:
            ctypes.windll.kernel32.SetEvent(h)
            ctypes.windll.kernel32.CloseHandle(h)
        return False
    return True


# Toast AUMID registration (Windows only)
#
# Windows silently drops toasts from an AUMID that isn't registered under
# HKCU\Software\Classes\AppUserModelId\{AUMID}.  This key must exist before
# the first call to CreateToastNotifier — we write it once at startup.
#
# CustomActivator wires up COM click-activation so that clicking any toast
# shown under this AUMID invokes _COM_ACTIVATOR_CLSID (issue #83).

_TOAST_AUMID = "PlaudTools.TrayApp"
_TOAST_AUMID_KEY = rf"Software\Classes\AppUserModelId\{_TOAST_AUMID}"

# Imported here to avoid circular imports when com_activation.py uses setup.py.
# The actual GUID lives in com_activation.py; we duplicate only the string so
# setup.py has no dependency on comtypes at module load time.
_COM_ACTIVATOR_CLSID = "{DC6F6422-E7ED-4F4E-BBDE-8332A399DBD5}"
_COM_ACTIVATOR_KEY = rf"Software\Classes\CLSID\{_COM_ACTIVATOR_CLSID}"


def _register_aumid() -> None:
    """Register the toast AUMID so Windows will display notifications (idempotent)."""
    if sys.platform != "win32":
        return
    try:
        import winreg
        with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, _TOAST_AUMID_KEY, 0, winreg.KEY_SET_VALUE) as key:
            winreg.SetValueEx(key, "DisplayName", 0, winreg.REG_SZ, APP_NAME)
            winreg.SetValueEx(key, "CustomActivator", 0, winreg.REG_SZ, _COM_ACTIVATOR_CLSID)
        ico = _assets_path() / "icon.ico"
        if ico.exists():
            with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, _TOAST_AUMID_KEY, 0, winreg.KEY_SET_VALUE) as key:
                winreg.SetValueEx(key, "IconUri", 0, winreg.REG_SZ, str(ico))
    except OSError:
        logging.warning("Could not register toast AUMID", exc_info=True)


def _register_com_activator() -> None:
    """Register the COM LocalServer32 activator CLSID (idempotent).

    Writes:
      HKCU\\Software\\Classes\\CLSID\\{CLSID}          (default = friendly name)
      HKCU\\Software\\Classes\\CLSID\\{CLSID}\\LocalServer32  (default = exe --com-activate)
    """
    if sys.platform != "win32":
        return
    try:
        import winreg
        exe = str(Path(sys.executable))
        cmd = f'"{exe}" --com-activate'
        with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, _COM_ACTIVATOR_KEY, 0, winreg.KEY_SET_VALUE) as key:
            winreg.SetValueEx(key, "", 0, winreg.REG_SZ, "Plaud Tools Notification Activator")
        localserver_key = _COM_ACTIVATOR_KEY + r"\LocalServer32"
        with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, localserver_key, 0, winreg.KEY_SET_VALUE) as key:
            winreg.SetValueEx(key, "", 0, winreg.REG_SZ, cmd)
        logging.info("Registered COM activator %s → %s", _COM_ACTIVATOR_CLSID, cmd)
    except OSError:
        logging.warning("Could not register COM activator", exc_info=True)


def _unregister_com_activator() -> None:
    """Remove the COM activator CLSID registry keys (inverse of _register_com_activator)."""
    if sys.platform != "win32":
        return
    import winreg
    for subkey in (
        _COM_ACTIVATOR_KEY + r"\LocalServer32",
        _COM_ACTIVATOR_KEY,
    ):
        try:
            winreg.DeleteKey(winreg.HKEY_CURRENT_USER, subkey)
        except FileNotFoundError:
            pass
        except OSError:
            logging.warning("Could not remove COM activator key %s", subkey, exc_info=True)


# Autostart (Windows registry)

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


# First-run environment setup (PATH + PowerShell completions)


def _install_dir() -> Path:
    """Return the canonical install directory (%LOCALAPPDATA%\\Programs\\PlaudTools)."""
    localappdata = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    return Path(localappdata) / "Programs" / "PlaudTools"


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


def _install_completions_dir() -> Path:
    """Return the expected completions path inside the canonical install dir."""
    return _install_dir() / "completions"


def _stale_sourcing_re() -> "re.Pattern[str]":
    """Regex that matches only sourcing lines that point at the PlaudTools install dir.

    Anchored to the canonical install path so unrelated user scripts that happen
    to live in a directory called ``completions`` are never touched.
    """
    import re
    # Escape backslashes for use inside a regex; the install dir may contain
    # only standard ASCII path characters so a simple re.escape is safe.
    install_completions = str(_install_completions_dir())
    escaped = re.escape(install_completions)
    # Allow either forward or back slashes as the trailing separator.
    return re.compile(
        r'^\. "' + escaped.replace(re.escape("\\"), r"[/\\]") + r'[/\\]plaud[^"]*\.ps1"',
        re.IGNORECASE,
    )


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

    Also removes any stale sourcing lines left by older builds that pointed at
    the same install directory (e.g. plaud.ps1 renamed to plaud-tools.ps1).
    Only lines pointing at the canonical PlaudTools install directory are removed;
    unrelated user scripts in other completions folders are not touched.
    """
    completions = _completions_dir()
    if completions is None:
        return
    ps1 = completions / "plaud-tools.ps1"
    if not ps1.exists():
        return
    source_line = f'. "{ps1}"'
    stale_re = _stale_sourcing_re()
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


# Environment verification (read-only; used by the tray at startup)

class EnvStatus:
    """Result of a read-only environment check performed at tray startup."""

    def __init__(
        self,
        path_ok: bool,
        completions_ok: bool,
        autostart_ok: bool,
    ) -> None:
        self.path_ok = path_ok
        self.completions_ok = completions_ok
        self.autostart_ok = autostart_ok

    @property
    def all_ok(self) -> bool:
        return self.path_ok and self.completions_ok and self.autostart_ok

    def missing_labels(self) -> list[str]:
        out: list[str] = []
        if not self.path_ok:
            out.append("PATH")
        if not self.completions_ok:
            out.append("shell completions")
        if not self.autostart_ok:
            out.append("autostart")
        return out


def _check_cli_path() -> bool:
    """Return True if the bundled cli/ directory is already on the user PATH."""
    if sys.platform != "win32":
        return True
    cli = _cli_dir()
    if cli is None:
        return True  # not a frozen bundle — nothing to verify
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as key:
            current, _ = winreg.QueryValueEx(key, "Path")
        parts = [p.strip() for p in current.split(";") if p.strip()]
        return any(Path(p) == cli for p in parts)
    except OSError:
        return False


def _check_ps_completions() -> bool:
    """Return True if a plaud-tools.ps1 sourcing line is present in at least one profile."""
    completions = _completions_dir()
    if completions is None:
        return True  # not a frozen bundle — nothing to verify
    ps1 = completions / "plaud-tools.ps1"
    if not ps1.exists():
        return True  # completions script not present; nothing to verify
    source_line = f'. "{ps1}"'
    user_docs = Path.home() / "Documents"
    profiles = [
        user_docs / "PowerShell" / "Microsoft.PowerShell_profile.ps1",
        user_docs / "WindowsPowerShell" / "Microsoft.PowerShell_profile.ps1",
    ]
    for profile in profiles:
        try:
            if profile.exists() and source_line in profile.read_text(encoding="utf-8-sig"):
                return True
        except OSError:
            pass
    return False


def _verify_env() -> "EnvStatus":
    """Read-only environment check — does not modify PATH, profiles, or registry."""
    return EnvStatus(
        path_ok=_check_cli_path(),
        completions_ok=_check_ps_completions(),
        autostart_ok=_autostart_enabled(),
    )


# Events file path (shared with mcp.py via same convention)


def _events_path() -> Path:
    localappdata = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
    return localappdata / "PlaudTools" / "events.jsonl"


__all__ = [
    "_setup_logging", "_mcp_exe", "_assets_path", "_set_app_icon", "_apply_theme",
    "_acquire_instance_lock", "_ACTIVATE_EVENT", "_AUTOSTART_KEY", "_AUTOSTART_NAME",
    "_register_aumid", "_TOAST_AUMID",
    "_COM_ACTIVATOR_CLSID", "_register_com_activator", "_unregister_com_activator",
    "_autostart_enabled", "_set_autostart", "_install_dir", "_cli_dir",
    "_completions_dir", "_install_completions_dir", "_stale_sourcing_re",
    "_setup_cli_path", "_setup_ps_completions", "EnvStatus", "_check_cli_path",
    "_check_ps_completions", "_verify_env", "_events_path", "APP_NAME", "Path",
]
