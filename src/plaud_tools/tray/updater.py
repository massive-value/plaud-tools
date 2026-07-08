"""Update check, in-app update dialog, and the download/install worker."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import subprocess
import sys
import tempfile
import threading
import tkinter as tk
import urllib.parse
import urllib.request
from pathlib import Path
from tkinter import ttk
from typing import TYPE_CHECKING

from .. import __version__ as APP_VERSION
from ..core.layout import InstallLayout
from .process_launch import _CREATE_BREAKAWAY_FROM_JOB, launch_hidden_powershell
from .process_launch import POWERSHELL_EXE as _POWERSHELL_EXE
from .ps1_templates import render_update_ps1
from .setup import APP_NAME, _configure_if_alive, _set_app_icon

if TYPE_CHECKING:  # pragma: no cover
    from .app import TrayApp


GITHUB_REPO = "massive-value/plaud-tools"

# Hosts from which update downloads are permitted.  Any other host is refused
# before a network connection is made.
_ALLOWED_UPDATE_HOSTS: frozenset[str] = frozenset(
    {
        "github.com",
        "objects.githubusercontent.com",
    }
)

# How long the tray waits for update.ps1's heartbeat file before giving up and
# reporting failure (instead of quitting into a half-applied update). The
# updater writes the heartbeat as its very first action, so this only needs to
# cover PowerShell cold-start (slow under Defender/enterprise scanning).
_UPDATER_HEARTBEAT_TIMEOUT_S: float = 20.0


def _launch_updater(ps_path: Path) -> subprocess.Popen[bytes]:
    """Launch the bundled update dispatcher as a detached PowerShell process.

    The child MUST outlive the tray: the tray quits moments after this returns,
    and update.ps1 then waits for the tray to exit before replacing its files.
    Delegates to :func:`process_launch.launch_hidden_powershell` (#142) for the
    safe-stdio + job-breakaway launch semantics shared with the uninstaller.
    ``-NonInteractive -ExecutionPolicy Bypass`` are injected by the shared
    helper itself, so they aren't repeated here.
    """
    args = [
        _POWERSHELL_EXE,
        "-NoProfile",
        "-WindowStyle",
        "Hidden",
        "-File",
        str(ps_path),
    ]
    return launch_hidden_powershell(args, cwd=tempfile.gettempdir(), breakaway=True)


def _check_download_host(url: str) -> None:
    """Raise :exc:`ValueError` if *url* does not parse to an allowed update host.

    The check is exact: the parsed ``netloc`` (host[:port]) must equal one of
    the entries in :data:`_ALLOWED_UPDATE_HOSTS`.  A host that merely *contains*
    ``github.com`` as a substring (e.g. ``github.com.evil.com``) is refused.

    Parameters
    ----------
    url:
        The download URL to validate before any network connection is made.

    Raises
    ------
    ValueError
        When the host is not in :data:`_ALLOWED_UPDATE_HOSTS`.
    """
    parsed = urllib.parse.urlparse(url)
    # netloc includes an optional port (e.g. "github.com:443"); strip the port
    # for the host comparison so "github.com:443" is still accepted.
    host = parsed.hostname or ""
    if host not in _ALLOWED_UPDATE_HOSTS:
        raise ValueError(
            f"Refusing to download update from untrusted host {host!r}. "
            f"Allowed hosts: {sorted(_ALLOWED_UPDATE_HOSTS)}"
        )


# ---------------------------------------------------------------------------
# Update check
# ---------------------------------------------------------------------------


def _version_gt(a: str, b: str) -> bool:
    try:
        return tuple(int(x) for x in a.split(".")) > tuple(int(x) for x in b.split("."))
    except ValueError:
        return False


def _check_for_update() -> tuple[str, str, str | None, str | None] | None:
    """Return (latest_version, release_url, zip_url, sums_url) if an update is available, else None.

    zip_url is the browser_download_url of the PlaudTools.zip asset, or None if not found.
    sums_url is the browser_download_url of the SHA256SUMS asset, or None if not published
    (older releases pre-dating task A3 have no SHA256SUMS asset).
    """
    try:
        url = f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest"
        req = urllib.request.Request(url, headers={"User-Agent": f"{APP_NAME}/{APP_VERSION}"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode())
        latest = data["tag_name"].lstrip("v")
        if _version_gt(latest, APP_VERSION):
            zip_url: str | None = None
            sums_url: str | None = None
            for asset in data.get("assets", []):
                name = asset.get("name", "")
                if name == "PlaudTools.zip":
                    zip_url = asset.get("browser_download_url")
                elif name == "SHA256SUMS":
                    sums_url = asset.get("browser_download_url")
            return latest, data["html_url"], zip_url, sums_url
    except Exception:
        pass
    return None


# ---------------------------------------------------------------------------
# Hash verification
# ---------------------------------------------------------------------------


class ChecksumMismatch(ValueError):
    """Raised when the downloaded zip's SHA-256 does not match SHA256SUMS."""


def verify_zip_checksum(zip_path: Path, sums_url: str | None) -> None:
    """Verify *zip_path* against the SHA256SUMS asset at *sums_url*.

    Verification is unconditionally FAIL CLOSED (#113): the caller must not
    install a zip unless this returns normally.
    - sums_url is not None  →  download the asset, parse the expected hash,
      compute the actual hash, and raise :exc:`ChecksumMismatch` on mismatch.
    - sums_url is None      →  raise :exc:`ChecksumMismatch`. The integrity of
      the download cannot be established, so refuse to install. (Every release
      from v0.3.0 onward publishes SHA256SUMS; an absent asset now means a
      malformed/incomplete release or a tampered asset list.)

    The SHA256SUMS format is the standard sha256sum two-space format::

        <lowercase-hex>  PlaudTools.zip

    Only the first whitespace-separated token on the first non-empty line is
    used, so the filename column is ignored.

    Parameters
    ----------
    zip_path:
        Local path to the downloaded zip to verify.
    sums_url:
        ``browser_download_url`` of the SHA256SUMS release asset, or ``None``
        when the asset is absent (pre-A3 release).

    Raises
    ------
    ChecksumMismatch
        When *sums_url* is None (no SHA256SUMS asset to verify against), or when
        it is present but the computed hash does not match.
    """
    if sums_url is None:
        # No SHA256SUMS asset → integrity cannot be verified → refuse to install.
        raise ChecksumMismatch(
            "SHA256SUMS asset is missing from this release; the download's "
            "integrity cannot be verified. Refusing to install. If this "
            "persists, report it at "
            "https://github.com/massive-value/plaud-tools/issues"
        )

    # Download the SHA256SUMS file.
    req = urllib.request.Request(sums_url, headers={"User-Agent": f"{APP_NAME}/{APP_VERSION}"})
    with urllib.request.urlopen(req, timeout=10) as resp:
        sums_text = resp.read().decode("utf-8")

    # Parse the expected hash: first whitespace-delimited token.
    expected = sums_text.strip().split()[0].lower()

    # Compute SHA-256 of the local zip.
    sha256 = hashlib.sha256()
    with zip_path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            sha256.update(chunk)
    actual = sha256.hexdigest().lower()

    if actual != expected:
        raise ChecksumMismatch(
            f"SHA256 mismatch — the downloaded zip may be corrupt or tampered.\n"
            f"  Expected: {expected}\n"
            f"  Actual:   {actual}\n"
            "Refusing to install. Please retry; if the mismatch persists, "
            "report it at https://github.com/massive-value/plaud-tools/issues"
        )


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
        latest, url, zip_url, sums_url = update_info

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

            def _start_install(zu: str, su: str | None) -> None:
                install_btn.config(state="disabled")
                status_var.set("Downloading…")
                threading.Thread(
                    target=self._install_worker,
                    args=(zu, su, status_var, install_btn),
                    daemon=True,
                ).start()

            if zip_url:
                _cmd = lambda zu=zip_url, su=sums_url: _start_install(zu, su)  # noqa: E731  # default-arg lambda; tkinter stubs cannot infer type  # type: ignore[misc]
                install_btn.config(command=_cmd)
            else:
                # zip_url was cached as None (poller ran before CI finished uploading).
                # Re-fetch once; enable the button if the asset is now available.
                install_btn.config(state="disabled", text="Checking…")

                def _refetch() -> None:
                    fresh = _check_for_update()
                    fresh_zip = fresh[2] if fresh else None
                    fresh_sums = fresh[3] if fresh else None
                    if self._root:

                        def _apply(zu: str | None = fresh_zip, su: str | None = fresh_sums) -> None:
                            if not win.winfo_exists():
                                return
                            if zu:
                                self._app._update_info = (fresh[0], fresh[1], zu, su)  # type: ignore[index]  # fresh is non-None when zu is truthy (zu = fresh[2] if fresh else None)
                                install_btn.config(
                                    state="normal",
                                    text="Install update and restart",
                                    command=lambda: _start_install(zu, su),
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
        sums_url: str | None,
        status_var: tk.StringVar,
        install_btn: ttk.Button,
    ) -> None:
        """Download the zip, verify its checksum, write the PS1 helper, launch it, then quit the tray."""
        import time as _time

        def _set_status(text: str) -> None:
            if self._root:
                self._root.after(0, lambda: status_var.set(text))

        def _on_error(err: Exception) -> None:
            logging.exception("in-app update download failed")

            def _apply() -> None:
                status_var.set(f"Download failed: {err}")
                # Delivered via root.after() from this worker thread -- the
                # UpdateDialog window (and install_btn with it) may have been
                # closed in the meantime (#157).
                _configure_if_alive(install_btn, state="normal")

            if self._root:
                self._root.after(0, _apply)

        try:
            # Allowlist check — must happen before any network connection.
            _check_download_host(zip_url)

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

        # --- Hash verification (MUST happen before writing dispatcher/sentinel) ---
        # verify_zip_checksum is unconditionally fail-closed (#113): it raises on
        # a hash mismatch AND when the SHA256SUMS asset is absent.
        try:
            _set_status("Verifying…")
            verify_zip_checksum(zip_path, sums_url)
        except Exception as exc:
            # ChecksumMismatch or network error fetching SHA256SUMS — refuse to proceed.
            zip_path.unlink(missing_ok=True)
            _on_error(exc)
            return

        try:
            _set_status("Installing…")

            install_dir = InstallLayout.detect().install_root or Path(sys.executable).parent
            tray_pid = os.getpid()
            fail_sentinel = Path(tempfile.gettempdir()) / "plaud_update_failed.txt"
            ps_path = Path(tempfile.gettempdir()) / f"plaud_update_{tray_pid}.ps1"
            # Heartbeat update.ps1 writes the instant it starts running. We gate
            # the tray's exit on this file's appearance (see below).
            alive_path = Path(tempfile.gettempdir()) / f"plaud_update_{tray_pid}.alive.txt"
            alive_path.unlink(missing_ok=True)  # clear any stale heartbeat from a prior run

            update_info = self._app._update_info
            new_version = update_info[0] if update_info else "unknown"

            # NOTE: the success sentinel (plaud_just_updated.txt) is intentionally
            # NOT written here. update.ps1 writes it only AFTER a successful
            # extraction. Pre-writing it meant a silently-failed update (e.g. the
            # updater process being killed before it ran) still left the sentinel
            # behind, and the restarted old tray falsely announced success.
            ps_content = render_update_ps1(
                tray_pid=tray_pid,
                install_dir=str(install_dir),
                zip_path=str(zip_path),
                extract_dir=str(install_dir.parent),
                dispatcher_path=str(ps_path),
                new_version=new_version,
            )
            # utf-8-sig (BOM) so Windows PowerShell 5.1 -- which treats a
            # BOM-less file as the system ANSI codepage, not UTF-8 -- reliably
            # reinterprets the dispatcher even when a path embedded in it
            # (e.g. %TEMP% under a non-ASCII Windows username) is non-ASCII.
            # See #153, same family as the v0.3.4 update.ps1/tray.log fix.
            ps_path.write_text(ps_content, encoding="utf-8-sig")

            logging.info(
                "in-app update: launching updater for v%s (tray_pid=%s zip=%s dispatcher=%s)",
                new_version,
                tray_pid,
                zip_path,
                ps_path,
            )

            proc = _launch_updater(ps_path)
            logging.info("in-app update: PowerShell updater launched (pid=%s)", proc.pid)

            # Gate the tray exit on the updater actually RUNNING. The tray is in
            # a Windows Job Object; if it quits before the child PowerShell is
            # established, a kill-on-close job tears the still-cold-starting
            # child down before update.ps1 runs a single line (the root cause of
            # "Updated successfully" while staying on the old version). We launch
            # with CREATE_BREAKAWAY_FROM_JOB (see _launch_updater) so the child
            # escapes the job when permitted, AND we wait here for update.ps1's
            # heartbeat file before quitting — so we only exit once the updater
            # is confirmed alive, and we report honest failure otherwise.
            deadline = _time.monotonic() + _UPDATER_HEARTBEAT_TIMEOUT_S
            while _time.monotonic() < deadline:
                if alive_path.exists():
                    logging.info("in-app update: updater heartbeat seen; quitting tray to hand off")
                    if self._root:
                        self._root.after(0, self._app._quit)
                    return
                rc = proc.poll()
                if rc is not None:
                    # Updater exited before writing a heartbeat → it never ran.
                    self._record_launch_failure(fail_sentinel, ps_path, tray_pid, rc)
                    _on_error(
                        RuntimeError(
                            f"The updater exited (code {rc}) before it could start. "
                            "Please try again, or download the update from the website."
                        )
                    )
                    return
                _time.sleep(0.2)

            # Timed out waiting for the heartbeat while the process is still
            # alive — PowerShell is wedged or blocked. Do NOT quit the tray
            # (the user would be stranded mid-update); surface a failure.
            logging.error("in-app update: no updater heartbeat after %ss", _UPDATER_HEARTBEAT_TIMEOUT_S)
            self._record_launch_failure(fail_sentinel, ps_path, tray_pid, None)
            _on_error(
                RuntimeError(
                    "The updater did not start within the expected time. "
                    "Please try again, or download the update from the website."
                )
            )

        except Exception as exc:
            _on_error(exc)

    @staticmethod
    def _record_launch_failure(fail_sentinel: Path, ps_path: Path, tray_pid: int, rc: int | None) -> None:
        """Write the failure sentinel so the next tray launch can surface the cause."""
        import json as _json

        if rc is not None:
            reason = (
                f"The updater exited with code {rc} before it could run — it may "
                "have been blocked by an enterprise policy (AppLocker / WDAC)."
            )
        else:
            reason = "The updater did not start within the expected time."
        try:
            fail_sentinel.write_text(
                _json.dumps({"reason": reason, "log": str(ps_path), "time": "", "tray_pid": tray_pid}),
                encoding="utf-8",
            )
        except Exception:
            logging.warning("in-app update: could not write failure sentinel", exc_info=True)


__all__ = [
    "GITHUB_REPO",
    "_ALLOWED_UPDATE_HOSTS",
    "_POWERSHELL_EXE",
    "_CREATE_BREAKAWAY_FROM_JOB",  # re-exported for test_updater_launch.py
    "_check_download_host",
    "_version_gt",
    "_check_for_update",
    "ChecksumMismatch",
    "verify_zip_checksum",
    "UpdateDialog",
]
