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
# before a network connection is made, including on every redirect hop.
# GitHub release assets redirect github.com -> release-assets.githubusercontent.com
# (objects.githubusercontent.com on older releases).
_ALLOWED_UPDATE_HOSTS: frozenset[str] = frozenset(
    {
        "github.com",
        "objects.githubusercontent.com",
        "release-assets.githubusercontent.com",
    }
)

# Name of the release asset whose line we read from SHA256SUMS.
_ZIP_ASSET_NAME = "PlaudTools.zip"

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


class _AllowlistRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Refuse any redirect hop whose target host is not on the allowlist.

    Runs before the redirected request is sent, so an off-allowlist host is
    never contacted.
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[no-untyped-def]
        _check_download_host(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


_UPDATE_OPENER = urllib.request.build_opener(_AllowlistRedirectHandler())


def _open_update_url(url: str, timeout: float):  # type: ignore[no-untyped-def]
    """Open an update download URL with the host allowlist enforced end to end.

    Checks the starting URL, every redirect hop (via the opener), and the
    final URL the response actually came from.  Returns the open response,
    to be used as a context manager.
    """
    _check_download_host(url)
    req = urllib.request.Request(url, headers={"User-Agent": f"{APP_NAME}/{APP_VERSION}"})
    resp = _UPDATE_OPENER.open(req, timeout=timeout)
    try:
        _check_download_host(resp.geturl())
    except ValueError:
        resp.close()
        raise
    return resp


def _expected_hash_from_sums(sums_text: str, file_name: str = _ZIP_ASSET_NAME) -> str | None:
    """Return the lower-case hash listed for *file_name* in SHA256SUMS text, or None.

    Standard ``sha256sum`` format: one ``<hex>  <name>`` (or ``<hex> *<name>``
    for binary mode) per line.  Lines for other files are ignored, so the
    order of entries in the file does not matter.
    """
    for line in sums_text.splitlines():
        parts = line.strip().split(None, 1)
        if len(parts) != 2:
            continue
        digest, name = parts
        if name.strip().lstrip("*") == file_name and len(digest) == 64:
            return digest.lower()
    return None


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

    The hash on the ``PlaudTools.zip`` line is used; a file that does not
    list ``PlaudTools.zip`` is treated as a failure.  The SHA256SUMS URL is
    held to the same host allowlist as the zip, redirects included.

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

    # Download the SHA256SUMS file (allowlisted host, every hop).
    with _open_update_url(sums_url, timeout=10) as resp:
        sums_text = resp.read().decode("utf-8")

    expected = _expected_hash_from_sums(sums_text)
    if expected is None:
        raise ChecksumMismatch(
            f"SHA256SUMS does not list {_ZIP_ASSET_NAME}; the download's integrity "
            "cannot be verified. Refusing to install."
        )

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


class UpdateCancelled(Exception):
    """Raised inside the install worker when the user cancels before hand-off."""


def _discard_download(zip_path: Path | None) -> None:
    """Delete a partial or rejected ``plaud_update_*.zip`` from %TEMP% (best effort)."""
    if zip_path is None:
        return
    try:
        zip_path.unlink(missing_ok=True)
    except OSError:
        logging.warning("in-app update: could not delete %s", zip_path, exc_info=True)


class UpdateDialog:
    """Dialog that shows an available update and allows in-app install (frozen only)."""

    def __init__(self, root: tk.Tk, app: TrayApp) -> None:
        self._root = root
        self._app = app
        self._win: tk.Toplevel | None = None
        # Set from the moment an install starts until it fails, is cancelled,
        # or hands off to update.ps1.  The tray owns exactly one UpdateDialog,
        # so this is the app-wide guard against starting a second updater
        # (e.g. by closing and reopening the dialog mid-download).
        self._in_progress = threading.Event()
        # Set by Cancel / closing the window; checked by the download loop.
        # Ignored once the updater process has been launched.
        self._cancel = threading.Event()

    def _try_begin_install(self) -> bool:
        """Claim the app-wide install slot; False if an install is already running.

        Called on the Tk thread, so check-then-set cannot race another click.
        """
        if self._in_progress.is_set():
            return False
        self._in_progress.set()
        self._cancel.clear()
        return True

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

        close_text = "Cancel" if frozen else "Close"
        close_btn = ttk.Button(btn_frame, text=close_text)

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
                if not self._try_begin_install():
                    status_var.set("An update is already in progress.")
                    return
                install_btn.config(state="disabled")
                status_var.set("Downloading…")
                threading.Thread(
                    target=self._install_worker,
                    args=(zu, su, status_var, install_btn, close_btn),
                    daemon=True,
                ).start()

            if self._in_progress.is_set():
                # Reopened while an install from an earlier window is running.
                install_btn.config(state="disabled")
                status_var.set("An update is already in progress.")
            elif zip_url:
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
            # Cancels a download in progress; a no-op once update.ps1 has
            # been launched (the worker stops checking after hand-off).
            self._cancel.set()
            if win.winfo_exists():
                win.destroy()

        close_btn.config(command=_close)
        close_btn.pack(side="left", padx=8)
        win.protocol("WM_DELETE_WINDOW", _close)

        win.lift()
        win.focus_force()
        win.after(50, lambda: win.grab_set() if win.winfo_exists() else None)

    def _install_worker(
        self,
        zip_url: str,
        sums_url: str | None,
        status_var: tk.StringVar,
        install_btn: ttk.Button,
        close_btn: ttk.Button | None = None,
    ) -> None:
        """Download the zip, verify its checksum, write the PS1 helper, launch it, then quit the tray."""
        import time as _time

        def _set_status(text: str) -> None:
            if self._root:
                self._root.after(0, lambda: status_var.set(text))

        def _on_error(err: Exception) -> None:
            logging.exception("in-app update download failed")
            self._in_progress.clear()

            def _apply() -> None:
                status_var.set(f"Download failed: {err}")
                # Delivered via root.after() from this worker thread -- the
                # UpdateDialog window (and install_btn with it) may have been
                # closed in the meantime (#157).
                _configure_if_alive(install_btn, state="normal")
                _configure_if_alive(close_btn, state="normal")

            if self._root:
                self._root.after(0, _apply)

        def _check_cancelled() -> None:
            if self._cancel.is_set():
                raise UpdateCancelled()

        # --- Download + hash verification (cancellable) ---
        # verify_zip_checksum is unconditionally fail-closed (#113): it raises
        # on a hash mismatch AND when the SHA256SUMS asset is absent.  Any
        # failure or cancel deletes the partial/rejected zip from %TEMP%.
        zip_path: Path | None = None
        try:
            with _open_update_url(zip_url, timeout=60) as resp:
                content_length = resp.headers.get("Content-Length")
                total_mb: float | None = int(content_length) / (1024 * 1024) if content_length else None
                tmp = tempfile.NamedTemporaryFile(suffix=".zip", delete=False, prefix="plaud_update_")
                zip_path = Path(tmp.name)
                try:
                    downloaded = 0
                    chunk_size = 65536
                    while True:
                        _check_cancelled()
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

            _check_cancelled()
            _set_status("Verifying…")
            verify_zip_checksum(zip_path, sums_url)
            _check_cancelled()
        except UpdateCancelled:
            logging.info("in-app update: cancelled by user")
            _discard_download(zip_path)
            self._in_progress.clear()
            return
        except Exception as exc:
            _discard_download(zip_path)
            _on_error(exc)
            return

        # --- Hand-off: from here on Cancel can no longer stop the update ---
        if self._root:
            self._root.after(0, lambda: _configure_if_alive(close_btn, state="disabled"))

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
            # swap. Pre-writing it meant a silently-failed update (e.g. the
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

            # Last chance to honour a Cancel clicked while we were writing the
            # dispatcher; after _launch_updater the update can't be stopped.
            if self._cancel.is_set():
                logging.info("in-app update: cancelled by user before launch")
                ps_path.unlink(missing_ok=True)
                _discard_download(zip_path)
                self._in_progress.clear()
                return

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
                    _discard_download(zip_path)
                    _on_error(
                        RuntimeError(
                            f"The updater exited (code {rc}) before it could start. "
                            "Please try again, or download the update from the website."
                        )
                    )
                    return
                _time.sleep(0.2)

            # Timed out waiting for the heartbeat while the process is still
            # alive — PowerShell is wedged or blocked. Kill it so it cannot
            # wake up later and install behind the user's back after we have
            # reported failure (and possibly after they retried), then report.
            logging.error("in-app update: no updater heartbeat after %ss", _UPDATER_HEARTBEAT_TIMEOUT_S)
            try:
                proc.kill()
                proc.wait(timeout=5)
            except Exception:
                logging.warning(
                    "in-app update: could not kill wedged updater pid=%s", proc.pid, exc_info=True
                )
            self._record_launch_failure(fail_sentinel, ps_path, tray_pid, None)
            _discard_download(zip_path)
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
    "UpdateCancelled",
    "UpdateDialog",
]
