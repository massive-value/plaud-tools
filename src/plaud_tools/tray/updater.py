"""Update check, in-app update dialog, and the download/install worker."""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import tempfile
import threading
import tkinter as tk
import urllib.request
from pathlib import Path
from tkinter import ttk
from typing import TYPE_CHECKING

from .. import __version__ as APP_VERSION
from ..layout import InstallLayout
from ..ps1_templates import render_update_ps1
from .setup import APP_NAME, _set_app_icon

if TYPE_CHECKING:  # pragma: no cover
    from .app import TrayApp


GITHUB_REPO = "massive-value/plaud-tools"


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
# Update dialog
# ---------------------------------------------------------------------------


class UpdateDialog:
    """Dialog that shows an available update and allows in-app install (frozen only)."""

    def __init__(self, root: tk.Tk, app: TrayApp) -> None:
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
        _set_app_icon(win)
        win.title(f"{APP_NAME} — Update available")
        win.resizable(False, False)
        win.geometry("400x240")
        self._win = win

        frame = ttk.Frame(win, padding=20)
        frame.pack(fill="both", expand=True)

        ttk.Label(
            frame, text="A new version of Plaud Tools is available.", font=("Segoe UI", 10, "bold")
        ).pack(anchor="w", pady=(0, 8))

        ttk.Label(frame, text=f"Current version:    {APP_VERSION}").pack(anchor="w")
        ttk.Label(frame, text=f"Available version:  {latest}").pack(anchor="w", pady=(0, 12))

        status_var = tk.StringVar()
        status_label = ttk.Label(frame, textvariable=status_var, foreground="#1d4ed8", wraplength=360)
        status_label.pack(anchor="w", pady=(0, 8))

        frozen = getattr(sys, "frozen", False)

        btn_frame = ttk.Frame(frame)
        btn_frame.pack(fill="x", pady=(4, 0))

        if not frozen:
            ttk.Label(
                frame,
                text="In-app install is only available in the bundled tray.",
                foreground="#6b7280",
                font=("Segoe UI", 9),
            ).pack(anchor="w", pady=(0, 8))
        else:
            install_btn = ttk.Button(btn_frame, text="Install update and restart")
            install_btn.pack(side="left")

            def _start_install(zu: str) -> None:
                install_btn.config(state="disabled")
                status_var.set("Downloading…")
                threading.Thread(
                    target=self._install_worker,
                    args=(zu, status_var, install_btn),
                    daemon=True,
                ).start()

            if zip_url:
                install_btn.config(command=lambda zu=zip_url: _start_install(zu))  # type: ignore[misc]  # default-arg lambda; tkinter stubs cannot infer type
            else:
                # zip_url was cached as None (poller ran before CI finished uploading).
                # Re-fetch once; enable the button if the asset is now available.
                install_btn.config(state="disabled", text="Checking…")

                def _refetch() -> None:
                    fresh = _check_for_update()
                    fresh_zip = fresh[2] if fresh else None
                    if self._root:

                        def _apply(zu: str | None = fresh_zip) -> None:
                            if not win.winfo_exists():
                                return
                            if zu:
                                self._app._update_info = (fresh[0], fresh[1], zu)  # type: ignore[index]  # fresh is non-None when zu is truthy (zu = fresh[2] if fresh else None)
                                install_btn.config(
                                    state="normal",
                                    text="Install update and restart",
                                    command=lambda: _start_install(zu),
                                )
                            else:
                                install_btn.config(
                                    text="Open release page",
                                    state="normal",
                                    command=lambda: self._app._open_url(url),
                                )

                        self._root.after(0, _apply)

                threading.Thread(target=_refetch, daemon=True).start()

        def _close() -> None:
            if win.winfo_exists():
                win.destroy()

        close_text = "Cancel" if frozen else "Close"
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
        import time as _time

        def _set_status(text: str) -> None:
            if self._root:
                self._root.after(0, lambda: status_var.set(text))

        def _on_error(err: Exception) -> None:
            logging.exception("in-app update download failed")
            if self._root:
                self._root.after(
                    0,
                    lambda: (
                        status_var.set(f"Download failed: {err}"),  # type: ignore[func-returns-value]  # StringVar.set() returns None; tuple used as side-effect expression in lambda body
                        install_btn.config(state="normal"),
                    ),
                )

        try:
            req = urllib.request.Request(
                zip_url,
                headers={"User-Agent": f"{APP_NAME}/{APP_VERSION}"},
            )
            with urllib.request.urlopen(req, timeout=60) as resp:
                content_length = resp.headers.get("Content-Length")
                total_mb: float | None = int(content_length) / (1024 * 1024) if content_length else None
                tmp = tempfile.NamedTemporaryFile(suffix=".zip", delete=False, prefix="plaud_update_")
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

            install_dir = InstallLayout.detect().install_root or Path(sys.executable).parent
            tray_pid = os.getpid()
            sentinel = Path(tempfile.gettempdir()) / "plaud_just_updated.txt"
            fail_sentinel = Path(tempfile.gettempdir()) / "plaud_update_failed.txt"
            ps_path = Path(tempfile.gettempdir()) / f"plaud_update_{tray_pid}.ps1"

            update_info = self._app._update_info
            new_version = update_info[0] if update_info else "unknown"

            ps_content = render_update_ps1(
                tray_pid=tray_pid,
                install_dir=str(install_dir),
                zip_path=str(zip_path),
                extract_dir=str(install_dir.parent),
                dispatcher_path=str(ps_path),
            )
            ps_path.write_text(ps_content, encoding="utf-8")
            sentinel.write_text(new_version, encoding="utf-8")

            logging.info(
                "in-app update: launching updater for v%s (tray_pid=%s zip=%s dispatcher=%s)",
                new_version,
                tray_pid,
                zip_path,
                ps_path,
            )

            # CREATE_NO_WINDOW (not DETACHED_PROCESS) + explicit DEVNULL handles:
            # DETACHED_PROCESS from a no-console frozen app passes NULL stdio
            # handles to the child, which causes PowerShell to crash before any
            # script code runs. CREATE_NO_WINDOW suppresses the window without
            # detaching, and DEVNULL handles are always valid.
            proc = subprocess.Popen(
                [
                    "powershell",
                    "-NoProfile",
                    "-NonInteractive",
                    "-ExecutionPolicy",
                    "Bypass",
                    "-WindowStyle",
                    "Hidden",
                    "-File",
                    str(ps_path),
                ],
                creationflags=subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                cwd=tempfile.gettempdir(),
            )
            logging.info("in-app update: PowerShell updater launched (pid=%s)", proc.pid)

            # Sanity-check: if PowerShell exits within 0.5 s the script almost
            # certainly never ran (invalid handles, policy block, etc.).
            _time.sleep(0.5)
            rc = proc.poll()
            if rc is not None:
                import json as _json

                fail_msg = (
                    f"PowerShell exited immediately with code {rc} — "
                    "the update script may have been blocked by an enterprise "
                    f"policy (AppLocker / WDAC). Dispatcher: {ps_path}"
                )
                logging.error("in-app update: %s", fail_msg)
                fail_sentinel.write_text(
                    _json.dumps(
                        {
                            "reason": fail_msg,
                            "log": str(ps_path),
                            "time": "",
                            "tray_pid": tray_pid,
                        }
                    ),
                    encoding="utf-8",
                )
                sentinel.unlink(missing_ok=True)

            if self._root:
                self._root.after(0, self._app._quit)

        except Exception as exc:
            _on_error(exc)


__all__ = [
    "GITHUB_REPO",
    "_version_gt",
    "_check_for_update",
    "UpdateDialog",
]
