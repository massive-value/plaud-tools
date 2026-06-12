"""Uninstall helpers and the :class:`UninstallDialog` window."""

from __future__ import annotations

import logging
import os
import subprocess
import sys
import tempfile
import tkinter as tk
from pathlib import Path
from tkinter import ttk

from ..appdata import data_dir as _data_dir
from ..layout import InstallLayout
from ..ps1_templates import render_uninstall_ps1
from ..session import SessionStore
from .setup import (
    APP_NAME,
    _cli_dir,
    _set_app_icon,
    _set_autostart,
    _stale_sourcing_re,
    _unregister_com_activator,
)

# Absolute path to PowerShell to prevent PATH-hijacking attacks.
# %SystemRoot% is typically C:\Windows; fall back to the hard-coded canonical
# path if the env var is absent (should never happen on a standard Windows
# install, but defensive is better).
_POWERSHELL_EXE: str = os.path.join(
    os.environ.get("SystemRoot", r"C:\Windows"),
    r"System32\WindowsPowerShell\v1.0\powershell.exe",
)


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
            winreg.HKEY_CURRENT_USER,
            "Environment",
            0,
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
            HWND_BROADCAST,
            WM_SETTINGCHANGE,
            0,
            "Environment",
            0x0002,
            5000,
            None,
        )
        logging.info("Removed %s from user PATH", cli)
    except OSError:
        logging.warning("Could not update user PATH during uninstall", exc_info=True)


def _remove_ps_completions() -> None:
    """Remove plaud-tools sourcing lines from the user's PowerShell profiles.

    Only lines that point at the running PlaudTools install directory are removed;
    unrelated user scripts in other completions folders are not touched.
    When _stale_sourcing_re() returns None (pip/dev channel with no install_root),
    no removal is performed.
    """
    stale_re = _stale_sourcing_re()
    if stale_re is None:
        return
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
            lines = [line for line in content.splitlines(keepends=True) if not stale_re.match(line.strip())]
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
    """Delete tray log files from the current data directory (and the legacy Plaud dir)."""
    current_data_dir = _data_dir()
    # Check the current data dir and the legacy path (Plaud) for any stragglers
    candidate_dirs = [current_data_dir]
    legacy = current_data_dir.parent / "Plaud"
    if legacy != current_data_dir:
        candidate_dirs.append(legacy)
    for log_dir in candidate_dirs:
        if not log_dir.exists():
            continue
        for log_file in log_dir.glob("tray.log*"):
            try:
                log_file.unlink(missing_ok=True)
                logging.info("Deleted log file %s", log_file)
            except OSError:
                logging.warning("Could not delete log file %s", log_file, exc_info=True)


def _launch_uninstall_helper(install_dir: Path, delete_logs: bool = False) -> None:
    """Write a hidden PS1 dispatcher to %TEMP% that invokes the bundled uninstall.ps1."""
    tray_pid = os.getpid()
    log_dirs: list[str] = []
    if delete_logs:
        current_data_dir = _data_dir()
        log_dirs.append(str(current_data_dir))
        legacy = current_data_dir.parent / "Plaud"
        if legacy != current_data_dir:
            log_dirs.append(str(legacy))
    ps_content = render_uninstall_ps1(
        tray_pid=tray_pid,
        install_dir=str(install_dir),
        log_dirs=log_dirs if log_dirs else None,
    )
    ps_path = Path(tempfile.gettempdir()) / f"plaud_uninstall_{tray_pid}.ps1"
    ps_path.write_text(ps_content, encoding="utf-8")
    logging.info("Launching uninstall helper: %s (will delete %s)", ps_path, install_dir)
    subprocess.Popen(
        [_POWERSHELL_EXE, "-NoProfile", "-WindowStyle", "Hidden", "-File", str(ps_path)],
        creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP,
        cwd=tempfile.gettempdir(),
    )


# ---------------------------------------------------------------------------
# Uninstall dialog
# ---------------------------------------------------------------------------


class UninstallDialog:
    """Checklist dialog that removes selected Plaud Tools components."""

    def __init__(self, root: tk.Tk) -> None:
        self._root = root
        self._win: tk.Toplevel | None = None

    # ------------------------------------------------------------------
    # Public helper — pure predicate, easy to unit-test
    # ------------------------------------------------------------------

    @staticmethod
    def ai_client_warning_visible(delete_installdir: bool, disconnect_clients: bool) -> bool:
        """Return True when the AI-client warning should be shown.

        The dangerous combination is: install dir will be deleted but AI client
        configs will NOT be cleaned up — clients will have dangling paths.
        """
        return delete_installdir and not disconnect_clients

    def show(self) -> None:
        if self._win and self._win.winfo_exists():
            self._win.lift()
            self._win.focus_force()
            return

        win = tk.Toplevel(self._root)
        _set_app_icon(win)
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
        var_clients = tk.BooleanVar(value=True)
        var_path = tk.BooleanVar(value=True)
        var_autostart = tk.BooleanVar(value=True)
        var_ps = tk.BooleanVar(value=True)
        var_installdir = tk.BooleanVar(value=True)
        var_session = tk.BooleanVar(value=False)
        var_logs = tk.BooleanVar(value=False)

        checks_frame = ttk.Frame(frame)
        checks_frame.pack(fill="x")

        items = [
            (var_clients, "Disconnect AI clients (Claude Desktop, Claude Code, Codex)"),
            (var_path, "Remove from user PATH"),
            (var_autostart, "Remove autostart registry key"),
            (var_ps, "Remove PowerShell profile sourcing lines"),
            (var_installdir, "Delete install directory"),
            (var_session, "Delete session / credentials"),
            (var_logs, "Delete log files"),
        ]
        for var, label in items:
            ttk.Checkbutton(checks_frame, text=label, variable=var).pack(anchor="w", pady=2)

        # --- AI-client warning label (orange, hidden until needed) ---
        warning_var = tk.StringVar()
        warning_label = ttk.Label(
            frame,
            textvariable=warning_var,
            foreground="#c05000",
            wraplength=380,
            justify="left",
        )
        warning_label.pack(anchor="w", pady=(6, 0))

        def _update_warning(*_args: object) -> None:
            if self.ai_client_warning_visible(var_installdir.get(), var_clients.get()):
                warning_var.set(
                    "⚠ AI clients will still point at the deleted install directory. "
                    "Restart Claude Desktop / Claude Code / Codex after uninstalling to clear the error."
                )
            else:
                warning_var.set("")

        var_installdir.trace_add("write", _update_warning)
        var_clients.trace_add("write", _update_warning)
        # Seed the initial state (install dir checked, clients checked → no warning).
        _update_warning()

        ttk.Separator(frame, orient="horizontal").pack(fill="x", pady=12)

        btn_row = ttk.Frame(frame)
        btn_row.pack(fill="x")

        cancel_btn = ttk.Button(btn_row, text="Cancel", command=win.destroy)
        cancel_btn.pack(side="left")

        def _set_buttons_in_flight(in_flight: bool) -> None:
            """Disable/re-enable both action buttons while uninstall runs."""
            state = "disabled" if in_flight else "normal"
            cancel_btn.config(state=state)
            uninstall_btn.config(
                state=state,
                text="Uninstalling…" if in_flight else "Uninstall",
            )

        def do_uninstall() -> None:
            from tkinter import messagebox

            # Disable buttons immediately so a second click cannot spawn a
            # second helper process racing to delete the install directory.
            _set_buttons_in_flight(True)
            win.update()

            # Execute simple removals first.
            if var_clients.get():
                try:
                    from ..ai_clients import disconnect_all

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
            try:
                _unregister_com_activator()
            except Exception:
                logging.warning("Could not remove COM activator registry keys", exc_info=True)
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
                    install_dir = InstallLayout.detect().install_root or Path(sys.executable).parent
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

        uninstall_btn = ttk.Button(btn_row, text="Uninstall", command=do_uninstall)
        uninstall_btn.pack(side="right")

        win.lift()
        win.focus_force()
        win.after(50, lambda: win.grab_set() if win.winfo_exists() else None)


__all__ = [
    "_remove_cli_path",
    "_remove_ps_completions",
    "_delete_session_files",
    "_delete_log_files",
    "_launch_uninstall_helper",
    "UninstallDialog",
]
