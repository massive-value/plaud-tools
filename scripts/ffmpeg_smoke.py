"""Real-ffmpeg transcode smoke test.

This script is invoked by the CI bundle-smoke job only.
It is NOT collected by pytest (no test_ prefix, and it lives under scripts/).

Usage:
    python scripts/ffmpeg_smoke.py

Requires ffmpeg on PATH (or FFMPEG_BIN set).  The CI job downloads a
pinned ffmpeg build and puts it on PATH before calling this script.

Exit code 0 = pass, non-zero = fail.
"""

from __future__ import annotations

import sys
import tempfile
import wave
from pathlib import Path


def _generate_silence_wav() -> bytes:
    """Generate a minimal valid WAV: 0.1 s of 16-bit mono silence at 8 kHz."""
    import io

    sample_rate = 8000
    n_channels = 1
    sampwidth = 2  # 16-bit
    n_frames = int(sample_rate * 0.1)  # 0.1 second

    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(n_channels)
        wf.setsampwidth(sampwidth)
        wf.setframerate(sample_rate)
        wf.writeframes(b"\x00" * n_frames * n_channels * sampwidth)
    return buf.getvalue()


def _is_mp3(data: bytes) -> bool:
    """Return True when data starts with a recognised MP3 magic sequence.

    Accepts:
      - ID3 tag header:   0x49 0x44 0x33  ('ID3')
      - MPEG frame sync with MPEG-1/2, Layer II/III bits set:
          first byte 0xFF, second byte high nibble 0xE or 0xF,
          and layer bits (bits 1-2) indicating Layer II (10) or Layer III (11).
    """
    if len(data) < 3:
        return False
    # ID3 tag
    if data[:3] == b"ID3":
        return True
    # MPEG frame sync: 0xFF + 0xEx or 0xFF + 0xFx (any MPEG layer/bitrate)
    if data[0] == 0xFF and (data[1] & 0xE0) == 0xE0:
        return True
    return False


def main() -> int:
    print("=== ffmpeg transcode smoke ===")

    # 1. Generate a tiny WAV in memory.
    wav_bytes = _generate_silence_wav()
    print(f"Generated WAV: {len(wav_bytes)} bytes")

    # 2. Transcode to MP3 via the real ffmpeg subprocess (path-in/path-out —
    #    the same function production upload flows use).
    from plaud_tools.transcode import transcode_to_mp3_path

    tmp_dir = Path(tempfile.mkdtemp(prefix="plaud-ffmpeg-smoke-"))
    src_path = tmp_dir / "silence.wav"
    dest_path = tmp_dir / "silence.mp3"
    src_path.write_bytes(wav_bytes)
    try:
        transcode_to_mp3_path(src_path, dest_path)
    except RuntimeError as exc:
        print(f"FAIL: transcode_to_mp3_path raised RuntimeError: {exc}", file=sys.stderr)
        return 1

    # 3. Assert non-empty output.
    mp3_bytes = dest_path.read_bytes()
    if not mp3_bytes:
        print("FAIL: transcode_to_mp3_path produced an empty file", file=sys.stderr)
        return 1
    print(f"MP3 output: {len(mp3_bytes)} bytes")

    # 4. Assert MP3 magic bytes.
    if not _is_mp3(mp3_bytes):
        header_hex = mp3_bytes[:16].hex(" ")
        print(
            f"FAIL: output does not begin with MP3 magic bytes. First 16 bytes: {header_hex}",
            file=sys.stderr,
        )
        return 1

    header_hex = mp3_bytes[:4].hex(" ")
    print(f"MP3 magic OK: first 4 bytes = {header_hex}")
    print("PASS: transcode smoke succeeded.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
