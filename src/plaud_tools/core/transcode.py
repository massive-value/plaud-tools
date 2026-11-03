from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .client import PlaudClient, ProgressCallback
    from .models import Recording

# Formats the Plaud API accepts natively (no transcode needed).
NATIVE_EXTS: dict[str, str] = {
    ".mp3": "MP3",
    ".opus": "OPUS",
    ".ogg": "OGG",
    ".oga": "OGG",
}

# Formats that must be transcoded to MP3 before upload.
TRANSCODE_EXTS: frozenset[str] = frozenset({".m4a", ".mp4", ".wav", ".aac", ".flac", ".wma", ".amr"})

# Upper bound on one ffmpeg run.  Speech-quality MP3 encodes run far faster
# than real time, so even a multi-hour recording finishes well inside this;
# it only exists so a wedged ffmpeg can never hang the CLI or MCP forever.
_FFMPEG_TIMEOUT_S = 30 * 60


def get_file_type(path: str | Path) -> tuple[str, bool]:
    """Return (plaud_file_type, needs_transcode) for a given file path.

    plaud_file_type is one of "MP3", "OPUS", "OGG" — the values the presign
    endpoint accepts. needs_transcode is True when the source must be
    converted before upload.

    Raises ValueError for unsupported extensions.
    """
    ext = Path(path).suffix.lower()
    if ext in NATIVE_EXTS:
        return NATIVE_EXTS[ext], False
    if ext in TRANSCODE_EXTS:
        return "MP3", True
    supported = sorted(NATIVE_EXTS) + sorted(TRANSCODE_EXTS)
    raise ValueError(f"Unsupported audio format {ext!r}. Supported: {', '.join(supported)}.")


def _find_ffmpeg() -> str:
    env_path = os.environ.get("FFMPEG_BIN")
    if env_path and Path(env_path).exists():
        return env_path
    # When frozen by PyInstaller, look for ffmpeg.exe next to the executable
    # before falling back to PATH. The Electron tray build places ffmpeg.exe
    # alongside plaud-mcp.exe in PlaudTools\mcp\. The CLI exe lives in the
    # sibling PlaudTools\cli\ directory. When the sibling-lookup fails (CLI
    # caller), also check ../mcp/ffmpeg.exe so both exe surfaces share the
    # single bundled ffmpeg without duplicating the ~70 MB binary.
    if getattr(sys, "frozen", False):
        sibling = Path(sys.executable).parent / "ffmpeg.exe"
        if sibling.exists():
            return str(sibling)
        mcp_sibling = Path(sys.executable).parent.parent / "mcp" / "ffmpeg.exe"
        if mcp_sibling.exists():
            return str(mcp_sibling)
    found = shutil.which("ffmpeg")
    if found:
        return found
    raise RuntimeError(
        "Could not locate ffmpeg. Install ffmpeg and ensure it is on PATH, "
        "or set the FFMPEG_BIN environment variable."
    )


def transcode_to_mp3_path(source_path: Path, dest_path: Path, *, quality: int = 4) -> None:
    """Transcode audio from *source_path* to *dest_path* (MP3) using ffmpeg.

    This is the path-in / path-out variant.  The transcoded bytes are written
    directly to *dest_path* and are never loaded into Python memory, which
    keeps peak RSS proportional to ffmpeg's own pipeline rather than the full
    file size.

    *source_path* must already exist.  *dest_path* will be created (or
    overwritten) by ffmpeg; the caller is responsible for cleaning it up.

    quality is the VBR -qscale:a value: 0 = best (~245 kbps), 4 = speech
    default (~165 kbps), 9 = worst (~65 kbps).
    """
    ff = _find_ffmpeg()
    # -nostdin + stdin=DEVNULL: ffmpeg otherwise reads the parent's stdin for
    # interactive keys, which under the MCP server is the JSON-RPC stream.
    try:
        result = subprocess.run(
            [
                ff,
                "-nostdin",
                "-y",
                "-i",
                str(source_path),
                "-codec:a",
                "libmp3lame",
                "-qscale:a",
                str(quality),
                "-map_metadata",
                "-1",
                str(dest_path),
            ],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=_FFMPEG_TIMEOUT_S,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"ffmpeg did not finish within {_FFMPEG_TIMEOUT_S // 60} minutes") from exc
    if result.returncode != 0:
        tail = result.stderr.decode("utf-8", errors="replace").strip().splitlines()
        msg = " ".join(tail[-3:])[:500]
        raise RuntimeError(f"ffmpeg exited {result.returncode}: {msg}")


# ---------------------------------------------------------------------------
# Shared upload+transcode orchestration, used by cli.py's ``_handle_upload``
# and mcp.py's ``upload_recording``: check the file exists, decide whether to
# transcode, upload (filing it in a folder in the same request), and clean up
# the temp file.
# ---------------------------------------------------------------------------


@dataclass
class UploadOutcome:
    """Result of :func:`upload_with_transcode`."""

    recording: Recording
    transcoded: bool


def upload_with_transcode(
    client: PlaudClient,
    path: Path,
    title: str,
    *,
    start_time: int | None = None,
    timezone_offset: float | None = None,
    folder_id: str | None = None,
    on_progress: ProgressCallback | None = None,
) -> UploadOutcome:
    """Upload *path* to Plaud, transcoding first if the format requires it.

    Raises ``ValueError`` if *path* does not exist or has an unsupported
    extension, and ``RuntimeError`` if ffmpeg fails — callers already map
    both into their respective error-reporting conventions.  *folder_id*
    files the recording as part of the upload itself.  *on_progress* hears
    about each part sent (see ``PlaudClient.upload_recording``).
    """
    if not path.exists():
        raise ValueError(f"file not found: {path}")
    file_type, needs_transcode = get_file_type(path)

    if needs_transcode:
        # Transcode to a temp MP3 on disk, then upload from that path so the
        # transcoded bytes never round-trip through Python memory.
        tmp_fd, tmp_mp3 = tempfile.mkstemp(suffix=".mp3", prefix="plaud-upload-")
        os.close(tmp_fd)
        tmp_mp3_path = Path(tmp_mp3)
        try:
            transcode_to_mp3_path(path, tmp_mp3_path)
            recording = client.upload_recording(
                tmp_mp3_path,
                title,
                file_type,
                start_time=start_time,
                timezone_offset=timezone_offset,
                folder_id=folder_id,
                on_progress=on_progress,
            )
        finally:
            try:
                tmp_mp3_path.unlink()
            except OSError:
                pass
    else:
        recording = client.upload_recording(
            path,
            title,
            file_type,
            start_time=start_time,
            timezone_offset=timezone_offset,
            folder_id=folder_id,
            on_progress=on_progress,
        )

    return UploadOutcome(recording=recording, transcoded=needs_transcode)
