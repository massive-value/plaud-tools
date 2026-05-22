from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path

# Formats the Plaud API accepts natively (no transcode needed).
NATIVE_EXTS: dict[str, str] = {
    ".mp3": "MP3",
    ".opus": "OPUS",
    ".ogg": "OGG",
    ".oga": "OGG",
}

# Formats that must be transcoded to MP3 before upload.
TRANSCODE_EXTS: frozenset[str] = frozenset(
    {".m4a", ".mp4", ".wav", ".aac", ".flac", ".wma", ".amr"}
)


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
    raise ValueError(
        f"Unsupported audio format {ext!r}. "
        f"Supported: {', '.join(supported)}."
    )


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


def transcode_to_mp3(source_bytes: bytes, source_ext: str, *, quality: int = 4) -> bytes:
    """Transcode audio to MP3 using ffmpeg.

    source_ext is the source file extension (with or without a leading dot).
    It is used as the temp-file suffix so ffmpeg picks the right demuxer.
    Seekable containers like m4a/mp4 need a real file on disk, not stdin.

    quality is the VBR -qscale:a value: 0 = best (~245 kbps), 4 = speech
    default (~165 kbps), 9 = worst (~65 kbps).
    """
    ff = _find_ffmpeg()
    ext = source_ext if source_ext.startswith(".") else f".{source_ext}"
    tmp_dir = tempfile.gettempdir()
    in_path = os.path.join(tmp_dir, f"plaud-transcode-{uuid.uuid4()}{ext}")
    out_path = os.path.join(tmp_dir, f"plaud-transcode-{uuid.uuid4()}.mp3")
    try:
        Path(in_path).write_bytes(source_bytes)
        result = subprocess.run(
            [
                ff, "-y",
                "-i", in_path,
                "-codec:a", "libmp3lame",
                "-qscale:a", str(quality),
                "-map_metadata", "-1",
                out_path,
            ],
            capture_output=True,
        )
        if result.returncode != 0:
            tail = result.stderr.decode("utf-8", errors="replace").strip().splitlines()
            msg = " ".join(tail[-3:])[:500]
            raise RuntimeError(f"ffmpeg exited {result.returncode}: {msg}")
        return Path(out_path).read_bytes()
    finally:
        for p in (in_path, out_path):
            try:
                os.unlink(p)
            except OSError:
                pass
