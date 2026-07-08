"""Tray setup helpers: logging, paths, install lock, autostart, PATH/completions."""

from __future__ import annotations

import logging
import logging.handlers
import re
import sys
import tkinter as tk
from pathlib import Path
from typing import TypeGuard, TypeVar

from ..core.appdata import events_path as _events_path
from ..core.appdata import tray_log as _tray_log_path
from ..core.layout import InstallLayout

APP_NAME = "Plaud Tools"


# Logging (writes to appdata.tray_log() — platform-aware via appdata.py)


def _setup_logging() -> None:
    log_path = _tray_log_path()
    log_path.parent.mkdir(parents=True, exist_ok=True)
    handler = logging.handlers.RotatingFileHandler(
        str(log_path),
        maxBytes=1_000_000,
        backupCount=3,
        encoding="utf-8",
    )
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logging.basicConfig(
        level=logging.DEBUG,
        handlers=[handler],
    )

    # Also capture unhandled tkinter callback exceptions.
    # NOTE: this is assigned to the *class* (tk.Tk), so Tk invokes it as a
    # bound method — the first positional arg is the Tk instance.  It must
    # therefore accept ``self``; a 3-arg ``(exc, val, tb)`` signature raises
    # "takes 3 positional arguments but 4 were given" *inside* the error
    # reporter, turning every recoverable callback error into a hard crash.
    def _tk_error(self, exc, val, tb):  # type: ignore[override]
        logging.exception("tkinter callback error", exc_info=(exc, val, tb))

    tk.Tk.report_callback_exception = _tk_error  # type: ignore[assignment]


# Paths


def _mcp_exe() -> str:
    """Return the absolute path to plaud-mcp as a string.

    Delegates to InstallLayout.detect() so the path is derived from the running
    install rather than a hardcoded location.
    """
    layout = InstallLayout.detect()
    if layout.mcp_exe is not None:
        return str(layout.mcp_exe)
    # Dev fallback: PyInstaller onedir output next to repo root
    return str(
        Path(__file__).parent.parent.parent.parent / "out" / "plaud-mcp" / "plaud-mcp" / "plaud-mcp.exe"
    )


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


_WidgetT = TypeVar("_WidgetT", bound=tk.Misc)


def _widget_alive(widget: _WidgetT | None) -> TypeGuard[_WidgetT]:
    """Return True if *widget* is non-None and its underlying Tk window still exists.

    Async callbacks arrive via ``root.after(0, ...)`` (or a chain of them)
    from background threads after an unpredictable delay; the window that
    owned the widget may have been closed by the user before the callback
    fires.  ``widget is None`` does not catch this -- the Python reference
    outlives Tk window destruction -- so calling ``.configure()`` on it raises
    ``TclError: invalid command name``.  See #157 (the v0.3.3 crash class).

    Typed as a ``TypeGuard`` (bound to the caller's concrete widget type, not
    widened to ``tk.Misc``) so ``if not _widget_alive(x): return`` narrows
    ``x`` for the rest of the caller the same way an ``is not None`` check
    would, without every call site needing its own ``# type: ignore``.
    """
    if widget is None:
        return False
    try:
        return bool(widget.winfo_exists())
    except tk.TclError:
        return False


def _configure_if_alive(widget: _WidgetT | None, **kwargs: object) -> bool:
    """Configure *widget* if it is still alive; return whether it happened.

    See :func:`_widget_alive` for the crash this guards against.
    """
    if not _widget_alive(widget):
        return False
    widget.configure(**kwargs)  # type: ignore[attr-defined]
    return True


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

_MUTEX_HANDLE: object = None
_ACTIVATE_EVENT = "Global\\PlaudToolsActivate"

# Win32 error code returned by GetLastError when a named mutex already exists.
_ERROR_ALREADY_EXISTS = 183


def _acquire_instance_lock(
    _kernel32: object = None,
) -> bool:
    """Acquire the single-instance named mutex and return True if the lock was obtained.

    Returns False when another instance already holds the mutex (ERROR_ALREADY_EXISTS),
    which also triggers a focus signal to the running instance.

    If ``CreateMutexW`` returns a NULL handle (API failure) the function logs a
    warning and returns True — fail-open — so the tray still starts rather than
    silently blocking the user due to an unrelated OS error.

    Parameters
    ----------
    _kernel32:
        Override for the kernel32 WinDLL object.  Accepted so tests can inject a
        fake without touching ``ctypes`` globally.  Pass ``None`` (default) in
        production.
    """
    global _MUTEX_HANDLE
    if sys.platform != "win32":
        return True
    import ctypes

    if _kernel32 is None:
        # use_last_error=True keeps the Win32 error code thread-local and readable
        # via ctypes.get_last_error() rather than re-querying GetLastError() after
        # a Python-level call that might clobber the value.
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    else:
        k32 = _kernel32  # type: ignore[assignment]

    # "Global\" prefix is required so the named mutex is visible across user
    # sessions (e.g. UAC elevation).
    handle = k32.CreateMutexW(None, False, f"Global\\{APP_NAME.replace(' ', '')}Instance")
    last_err = ctypes.get_last_error()

    if not handle:
        # CreateMutexW returned NULL — OS-level failure unrelated to mutex
        # ownership.  Fail-open: allow the tray to start so the user is not
        # silently blocked.
        logging.warning(
            "CreateMutexW returned NULL (error=%d); proceeding without single-instance lock",
            last_err,
        )
        return True

    _MUTEX_HANDLE = handle

    if last_err == _ERROR_ALREADY_EXISTS:
        # Signal the running instance to surface its window, then exit.
        h = k32.OpenEventW(0x0002, False, _ACTIVATE_EVENT)  # EVENT_MODIFY_STATE
        if h:
            k32.SetEvent(h)
            k32.CloseHandle(h)
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
            with winreg.CreateKeyEx(
                winreg.HKEY_CURRENT_USER, _TOAST_AUMID_KEY, 0, winreg.KEY_SET_VALUE
            ) as key:
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
# Marker file written into the install dir when the user explicitly disables
# autostart via the "Start with Windows" menu toggle.  Read by _verify_env so
# the tray's auto-repair pass does NOT silently re-enable autostart for a user
# who deliberately turned it off.  Lives in the install dir so it survives
# in-app upgrades (Expand-Archive doesn't delete files absent from the zip)
# but is wiped by uninstall (the install dir is deleted wholesale).
_AUTOSTART_OPT_OUT_MARKER = ".autostart_disabled"


def _autostart_enabled() -> bool:
    if sys.platform != "win32":
        return False
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _AUTOSTART_KEY) as key:
            val, _ = winreg.QueryValueEx(key, _AUTOSTART_NAME)
            # _set_autostart writes the value double-quoted (#160); strip a
            # matching pair of surrounding quotes before comparing so an
            # already-quoted registry entry still compares equal.
            return Path(val.strip('"')).resolve() == Path(sys.executable).resolve()
    except OSError:
        return False


def _autostart_opt_out_marker_path() -> Path | None:
    """Return the autostart opt-out marker path, or None outside a frozen bundle."""
    if not getattr(sys, "frozen", False) or sys.platform != "win32":
        return None
    return Path(sys.executable).parent / _AUTOSTART_OPT_OUT_MARKER


def _autostart_opted_out() -> bool:
    """True if the user has explicitly turned off autostart via the tray toggle."""
    marker = _autostart_opt_out_marker_path()
    return marker is not None and marker.exists()


def _set_autostart(enable: bool) -> None:
    if sys.platform != "win32":
        return
    import winreg

    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _AUTOSTART_KEY, 0, winreg.KEY_SET_VALUE) as key:
        if enable:
            # Quoted so a spaced install path (e.g. a Windows username with a
            # space, under %LOCALAPPDATA%) is not split into multiple
            # arguments by the Run-key launcher -- also closes a PATH-
            # hijacking window an unquoted spaced path opens (#160).
            winreg.SetValueEx(key, _AUTOSTART_NAME, 0, winreg.REG_SZ, f'"{sys.executable}"')
        else:
            try:
                winreg.DeleteValue(key, _AUTOSTART_NAME)
            except FileNotFoundError:
                pass
    # Sync the opt-out marker so subsequent tray launches respect the user's
    # explicit choice (or forget the previous opt-out, for enable=True).
    marker = _autostart_opt_out_marker_path()
    if marker is not None:
        try:
            if enable:
                marker.unlink(missing_ok=True)
            else:
                marker.parent.mkdir(parents=True, exist_ok=True)
                marker.touch(exist_ok=True)
        except OSError:
            logging.warning("Could not sync autostart opt-out marker", exc_info=True)


# First-run environment setup (PATH + PowerShell completions)


def _install_dir() -> Path | None:
    """Return the root directory of the running install, or None for pip/dev.

    Derived from sys.executable via InstallLayout.detect(), NOT from a
    hardcoded canonical path.  This closes the latent autostart bug: a bundle
    extracted outside the canonical path now correctly points the autostart
    registry entry at the *running* install rather than at the empty canonical
    location.
    """
    return InstallLayout.detect().install_root


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


def _install_completions_dir() -> Path | None:
    """Return the expected completions path inside the running install dir, or None.

    Returns None for pip/dev channels where install_root is None (no bundle
    install directory to anchor the stale-sourcing regex to).
    """
    install_root = _install_dir()
    if install_root is None:
        return None
    return install_root / "completions"


def _stale_sourcing_re() -> re.Pattern[str] | None:
    """Regex that matches only sourcing lines that point at the PlaudTools install dir.

    Anchored to the running install path (derived from sys.executable) so
    unrelated user scripts that happen to live in a directory called
    ``completions`` are never touched.

    Returns None for pip/dev channels where there is no install directory to
    anchor the pattern to.  Callers must handle the None case.
    """
    completions_dir = _install_completions_dir()
    if completions_dir is None:
        return None
    # Escape backslashes for use inside a regex; the install dir may contain
    # only standard ASCII path characters so a simple re.escape is safe.
    install_completions = str(completions_dir)
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
            winreg.HKEY_CURRENT_USER,
            "Environment",
            0,
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
            HWND_BROADCAST,
            WM_SETTINGCHANGE,
            0,
            "Environment",
            0x0002,
            5000,
            None,
        )
        logging.info("Added %s to user PATH", cli_str)
    except OSError:
        logging.warning("Could not update user PATH", exc_info=True)


# ---------------------------------------------------------------------------
# Shared PowerShell-profile IO (#154)
#
# Every profile read/write in the tray (completions setup, completions
# removal, the completions presence check) previously hand-rolled
# ``.read_text(encoding="utf-8-sig")`` / ``.write_text(encoding="utf-8")``.
# Two bugs came from that duplication: ``UnicodeDecodeError`` (raised by a
# profile that isn't valid UTF-8, e.g. a legacy ANSI file with non-ASCII
# bytes) is a ``ValueError`` subclass, NOT an ``OSError`` -- it escaped the
# ``except OSError`` guards at every call site.  And rewriting a BOM'd profile
# with plain ``encoding="utf-8"`` silently drops the user's own BOM, which
# PowerShell 5.1 needs to reliably reinterpret a UTF-8 file with any
# non-ASCII content the user later adds.  Route every profile touch through
# these two helpers so both fixes land once.
# ---------------------------------------------------------------------------


def _read_profile_text(path: Path) -> tuple[str | None, bool]:
    """Read a PowerShell profile file tolerantly.

    Returns ``(content, had_bom)``:

    - path missing             -> ``("", False)`` -- nothing to preserve, safe to create.
    - path exists, decodes OK  -> ``(text, had_bom)``.
    - path exists, undecodable -> ``(None, False)`` -- caller must leave the file untouched.
    """
    if not path.exists():
        return "", False
    try:
        raw = path.read_bytes()
    except OSError:
        return "", False
    had_bom = raw.startswith(b"\xef\xbb\xbf")
    try:
        return raw.decode("utf-8-sig"), had_bom
    except UnicodeDecodeError:
        return None, False


def _write_profile_text(path: Path, content: str, had_bom: bool) -> None:
    """Write *content* back to *path*, preserving the original BOM presence.

    A brand-new file (``had_bom=False``, since there was nothing to sniff)
    is written without a BOM, matching prior behaviour; an existing BOM'd
    profile keeps its BOM on rewrite instead of silently losing it.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8-sig" if had_bom else "utf-8")


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
            content, had_bom = _read_profile_text(profile)
            if content is None:
                logging.warning(
                    "Could not decode PowerShell profile %s as UTF-8; leaving it untouched", profile
                )
                continue
            if profile.exists():
                lines = [
                    line
                    for line in content.splitlines(keepends=True)
                    if stale_re is None or not stale_re.match(line.strip())
                ]
                content = "".join(lines)
                if source_line in content:
                    _write_profile_text(profile, content, had_bom)
                    continue
            _write_profile_text(
                profile,
                (content.rstrip("\n") + "\n" + source_line + "\n") if content else (source_line + "\n"),
                had_bom,
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
        content, _had_bom = _read_profile_text(profile)
        if content and source_line in content:
            return True
    return False


def _verify_env() -> EnvStatus:
    """Read-only environment check — does not modify PATH, profiles, or registry.

    The autostart slot is considered OK both when the registry entry is present
    AND when the user has explicitly opted out via the "Start with Windows"
    menu toggle (tracked by ``_autostart_opted_out``).  That way the auto-heal
    pass in ``_run_verify_env`` doesn't fight the user's deliberate choice.
    """
    return EnvStatus(
        path_ok=_check_cli_path(),
        completions_ok=_check_ps_completions(),
        autostart_ok=_autostart_enabled() or _autostart_opted_out(),
    )


__all__ = [
    "_setup_logging",
    "_mcp_exe",
    "_assets_path",
    "_set_app_icon",
    "_apply_theme",
    "_acquire_instance_lock",
    "_ACTIVATE_EVENT",
    "_AUTOSTART_KEY",
    "_AUTOSTART_NAME",
    "_register_aumid",
    "_TOAST_AUMID",
    "_COM_ACTIVATOR_CLSID",
    "_register_com_activator",
    "_unregister_com_activator",
    "_autostart_enabled",
    "_autostart_opted_out",
    "_autostart_opt_out_marker_path",
    "_set_autostart",
    "_install_dir",
    "_cli_dir",
    "_completions_dir",
    "_install_completions_dir",
    "_stale_sourcing_re",
    "_setup_cli_path",
    "_setup_ps_completions",
    "_read_profile_text",
    "_write_profile_text",
    "EnvStatus",
    "_check_cli_path",
    "_check_ps_completions",
    "_verify_env",
    "_events_path",
    "_widget_alive",
    "_configure_if_alive",
    "APP_NAME",
    "Path",
]
