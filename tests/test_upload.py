from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest

from plaud_tools.core.client import _CHUNK_SIZE, PlaudClient
from plaud_tools.core.errors import PlaudApiError
from plaud_tools.core.session import FileSessionStore, PlaudSession, SessionManager
from plaud_tools.core.transcode import get_file_type, transcode_to_mp3_path
from plaud_tools.core.transport import HttpResponse

# ---------------------------------------------------------------------------
# Helpers shared with test_client.py
# ---------------------------------------------------------------------------


class StubTransport:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def request(self, method, url, headers, body=None, *, timeout=None):
        self.calls.append({"method": method, "url": url, "headers": headers, "body": body})
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def _make_manager(tmp_path: Path, region: str = "eu") -> SessionManager:
    import base64

    payload = {"exp": 2_000_000_000 + 300 * 86400}
    encoded = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
    token = f"header.{encoded}.sig"
    store = FileSessionStore(tmp_path / "session.json")
    store.save(PlaudSession(access_token=token, region=region, email="test@example.com"))
    return SessionManager(store)


def _ok(body: dict) -> HttpResponse:
    return HttpResponse(200, json.dumps({"status": 0, **body}).encode(), {})


def _s3_ok(etag: str = "abc123") -> HttpResponse:
    return HttpResponse(200, b"", {"etag": f'"{etag}"'})


# ---------------------------------------------------------------------------
# Unit tests: file-type detection and transcode decision logic
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "ext,expected_type",
    [
        (".mp3", "MP3"),
        (".MP3", "MP3"),
        (".opus", "OPUS"),
        (".ogg", "OGG"),
        (".oga", "OGG"),
    ],
)
def test_get_file_type_native_formats(ext, expected_type, tmp_path):
    p = tmp_path / f"audio{ext}"
    p.write_bytes(b"")
    file_type, needs_transcode = get_file_type(p)
    assert file_type == expected_type
    assert needs_transcode is False


@pytest.mark.parametrize("ext", [".m4a", ".mp4", ".wav", ".aac", ".flac", ".wma", ".amr"])
def test_get_file_type_transcode_formats(ext, tmp_path):
    p = tmp_path / f"audio{ext}"
    p.write_bytes(b"")
    file_type, needs_transcode = get_file_type(p)
    assert file_type == "MP3"
    assert needs_transcode is True


def test_get_file_type_unsupported_raises(tmp_path):
    p = tmp_path / "audio.txt"
    p.write_bytes(b"")
    with pytest.raises(ValueError, match="Unsupported audio format"):
        get_file_type(p)


# ---------------------------------------------------------------------------
# Unit tests: multipart chunk assembly
# ---------------------------------------------------------------------------


def test_chunk_assembly_three_parts(tmp_path):
    """12 MiB of audio → 3 presigned URLs → 3 chunks of 5+5+2 MiB."""
    manager = _make_manager(tmp_path)
    audio_data = b"x" * (12 * 1024 * 1024)

    transport = StubTransport(
        [
            # Step 1: presign — return 3 part URLs
            HttpResponse(
                200,
                json.dumps(
                    {
                        "status": 0,
                        "data": {
                            "part_urls": [
                                "https://s3.fake/part1",
                                "https://s3.fake/part2",
                                "https://s3.fake/part3",
                            ],
                            "upload_id": "uid123",
                            "object_name": "test.mp3",
                        },
                    }
                ).encode(),
                {},
            ),
            # Steps 2a-2c: S3 PUTs
            _s3_ok("etag1"),
            _s3_ok("etag2"),
            _s3_ok("etag3"),
            # Step 3: merge_multipart
            _ok({}),
            # Step 4: confirm_upload
            _ok({"data": {"id": "rec1", "filename": "test"}}),
        ]
    )
    client = PlaudClient(manager, transport=transport)
    client.upload_recording(audio_data, "test", "MP3")

    # Verify chunk sizes sent in the S3 PUTs
    s3_calls = [c for c in transport.calls if "s3.fake" in c["url"]]
    assert len(s3_calls) == 3
    assert len(s3_calls[0]["body"]) == _CHUNK_SIZE  # 5 MiB
    assert len(s3_calls[1]["body"]) == _CHUNK_SIZE  # 5 MiB
    assert len(s3_calls[2]["body"]) == 2 * 1024 * 1024  # 2 MiB remainder


def test_chunk_assembly_single_part(tmp_path):
    """File under 5 MiB → 1 presigned URL → 1 chunk."""
    manager = _make_manager(tmp_path)
    audio_data = b"y" * (2 * 1024 * 1024)

    transport = StubTransport(
        [
            HttpResponse(
                200,
                json.dumps(
                    {
                        "status": 0,
                        "data": {
                            "part_urls": ["https://s3.fake/part1"],
                            "upload_id": "uid1",
                            "object_name": "small.mp3",
                        },
                    }
                ).encode(),
                {},
            ),
            _s3_ok("etag_only"),
            _ok({}),
            _ok({"data": {"id": "rec2", "filename": "small"}}),
        ]
    )
    client = PlaudClient(manager, transport=transport)
    client.upload_recording(audio_data, "small", "MP3")

    s3_calls = [c for c in transport.calls if "s3.fake" in c["url"]]
    assert len(s3_calls) == 1
    assert len(s3_calls[0]["body"]) == 2 * 1024 * 1024


# ---------------------------------------------------------------------------
# Fixture-based tests: upload request shapes
# ---------------------------------------------------------------------------


def test_upload_presign_request_shape(tmp_path):
    manager = _make_manager(tmp_path)
    audio_data = b"a" * 1024

    transport = StubTransport(
        [
            HttpResponse(
                200,
                json.dumps(
                    {
                        "status": 0,
                        "data": {
                            "part_urls": ["https://s3.fake/p1"],
                            "upload_id": "uid",
                            "object_name": "obj.mp3",
                        },
                    }
                ).encode(),
                {},
            ),
            _s3_ok(),
            _ok({}),
            _ok({"data": {"id": "r1", "filename": "rec"}}),
        ]
    )
    client = PlaudClient(manager, transport=transport)
    client.upload_recording(audio_data, "my recording", "MP3")

    presign_call = transport.calls[0]
    assert presign_call["method"] == "POST"
    assert presign_call["url"].endswith("/file/get_upload_presigned_url")
    body = json.loads(presign_call["body"])
    assert body == {"filesize": 1024, "file_type": "MP3"}
    assert presign_call["headers"]["Authorization"].startswith("Bearer ")


def test_upload_s3_put_shape(tmp_path):
    """S3 PUT must use correct Content-Type and carry no Plaud auth header."""
    manager = _make_manager(tmp_path)
    audio_data = b"b" * 512

    transport = StubTransport(
        [
            HttpResponse(
                200,
                json.dumps(
                    {
                        "status": 0,
                        "data": {
                            "part_urls": ["https://s3.aws.fake/presigned"],
                            "upload_id": "uid",
                            "object_name": "obj.mp3",
                        },
                    }
                ).encode(),
                {},
            ),
            _s3_ok("deadbeef"),
            _ok({}),
            _ok({"data": {"id": "r1", "filename": "rec"}}),
        ]
    )
    client = PlaudClient(manager, transport=transport)
    client.upload_recording(audio_data, "rec", "MP3")

    s3_call = transport.calls[1]
    assert s3_call["method"] == "PUT"
    assert s3_call["url"] == "https://s3.aws.fake/presigned"
    assert s3_call["headers"] == {"Content-Type": "application/x-www-form-urlencoded"}
    assert "Authorization" not in s3_call["headers"]
    assert s3_call["body"] == audio_data


def test_upload_merge_multipart_request_shape(tmp_path):
    """merge_multipart body must use Plaud's unusual casing: {Etag, PartNumber}."""
    manager = _make_manager(tmp_path)
    audio_data = b"c" * (7 * 1024 * 1024)

    transport = StubTransport(
        [
            HttpResponse(
                200,
                json.dumps(
                    {
                        "status": 0,
                        "data": {
                            "part_urls": ["https://s3.fake/p1", "https://s3.fake/p2"],
                            "upload_id": "uid-x",
                            "object_name": "obj.mp3",
                        },
                    }
                ).encode(),
                {},
            ),
            _s3_ok("etag-1"),
            _s3_ok("etag-2"),
            _ok({}),
            _ok({"data": {"id": "r1", "filename": "rec"}}),
        ]
    )
    client = PlaudClient(manager, transport=transport)
    client.upload_recording(audio_data, "rec", "MP3")

    merge_call = transport.calls[3]
    assert merge_call["url"].endswith("/file/merge_multipart")
    body = json.loads(merge_call["body"])
    assert body["upload_id"] == "uid-x"
    assert body["object_name"] == "obj.mp3"
    assert body["parts"] == [
        {"Etag": "etag-1", "PartNumber": 1},
        {"Etag": "etag-2", "PartNumber": 2},
    ]


def test_upload_confirm_request_shape(tmp_path):
    manager = _make_manager(tmp_path)
    audio_data = b"d" * 512
    fixed_start = 1_700_000_000_000

    transport = StubTransport(
        [
            HttpResponse(
                200,
                json.dumps(
                    {
                        "status": 0,
                        "data": {
                            "part_urls": ["https://s3.fake/p1"],
                            "upload_id": "uid-y",
                            "object_name": "obj.ogg",
                        },
                    }
                ).encode(),
                {},
            ),
            _s3_ok("etag-only"),
            _ok({}),
            _ok({"data": {"id": "r2", "filename": "clip"}}),
        ]
    )
    client = PlaudClient(manager, transport=transport)
    client.upload_recording(audio_data, "clip", "OGG", start_time=fixed_start, timezone_offset=-7.0)

    confirm_call = transport.calls[3]
    assert confirm_call["url"].endswith("/file/confirm_upload")
    body = json.loads(confirm_call["body"])
    assert body["upload_id"] == "uid-y"
    assert body["object_name"] == "obj.ogg"
    assert body["file_type"] == "OGG"
    assert body["filename"] == "clip"
    assert body["start_time"] == fixed_start
    assert body["session_id"] == fixed_start // 1000
    assert body["timezone"] == -7.0
    assert body["scene"] == 101
    assert body["is_tmp"] == 0
    assert body["support_mul_summ"] is True
    assert "serial_number" in body


def test_upload_strips_etag_quotes(tmp_path):
    """S3 may return ETag with surrounding quotes — strip them."""
    manager = _make_manager(tmp_path)
    audio_data = b"e" * 100

    transport = StubTransport(
        [
            HttpResponse(
                200,
                json.dumps(
                    {
                        "status": 0,
                        "data": {
                            "part_urls": ["https://s3.fake/p1"],
                            "upload_id": "uid",
                            "object_name": "obj.mp3",
                        },
                    }
                ).encode(),
                {},
            ),
            HttpResponse(200, b"", {"etag": '"quoted-etag"'}),
            _ok({}),
            _ok({"data": {"id": "r1", "filename": "f"}}),
        ]
    )
    client = PlaudClient(manager, transport=transport)
    client.upload_recording(audio_data, "f", "MP3")

    merge_body = json.loads(transport.calls[2]["body"])
    assert merge_body["parts"][0]["Etag"] == "quoted-etag"


def test_upload_rejects_empty_data(tmp_path):
    manager = _make_manager(tmp_path)
    client = PlaudClient(manager, transport=StubTransport([]))
    with pytest.raises(ValueError, match="data cannot be empty"):
        client.upload_recording(b"", "test", "MP3")


def test_upload_rejects_blank_filename(tmp_path):
    manager = _make_manager(tmp_path)
    client = PlaudClient(manager, transport=StubTransport([]))
    with pytest.raises(ValueError, match="filename cannot be empty"):
        client.upload_recording(b"x", "  ", "MP3")


def test_upload_rejects_unknown_file_type(tmp_path):
    manager = _make_manager(tmp_path)
    client = PlaudClient(manager, transport=StubTransport([]))
    with pytest.raises(ValueError, match="file_type must be MP3"):
        client.upload_recording(b"x", "test", "FLAC")


def test_upload_raises_on_missing_presign_fields(tmp_path):
    manager = _make_manager(tmp_path)
    transport = StubTransport(
        [
            _ok({"data": {"upload_id": "uid"}}),  # missing part_urls and object_name
        ]
    )
    client = PlaudClient(manager, transport=transport)
    with pytest.raises(PlaudApiError, match="presign response missing fields"):
        client.upload_recording(b"x", "test", "MP3")


def test_upload_raises_when_s3_returns_no_etag(tmp_path):
    manager = _make_manager(tmp_path)
    transport = StubTransport(
        [
            HttpResponse(
                200,
                json.dumps(
                    {
                        "status": 0,
                        "data": {
                            "part_urls": ["https://s3.fake/p1"],
                            "upload_id": "uid",
                            "object_name": "obj.mp3",
                        },
                    }
                ).encode(),
                {},
            ),
            HttpResponse(200, b"", {}),  # no etag header
        ]
    )
    client = PlaudClient(manager, transport=transport)
    with pytest.raises(PlaudApiError, match="no ETag for part 1"):
        client.upload_recording(b"data", "test", "MP3")


# ---------------------------------------------------------------------------
# Fixture-based tests: merge + polling
# ---------------------------------------------------------------------------


def test_merge_combine_request_shape(tmp_path):
    manager = _make_manager(tmp_path)
    transport = StubTransport(
        [
            _ok({"task_id": "task-abc"}),
            HttpResponse(
                200,
                json.dumps(
                    {
                        "status": 0,
                        "data": {
                            "status": "success",
                            "file": {"file_id": "merged1", "filename": "Combined"},
                        },
                    }
                ).encode(),
                {},
            ),
        ]
    )
    client = PlaudClient(manager, transport=transport)
    client.merge_recordings(["r1", "r2"], "Combined", poll_interval_s=0)

    combine_call = transport.calls[0]
    assert combine_call["method"] == "POST"
    assert combine_call["url"].endswith("/file/combine")
    body = json.loads(combine_call["body"])
    assert body == {"file_ids": ["r1", "r2"], "filename": "Combined"}


def test_merge_polls_combine_tasks(tmp_path):
    """Polling should hit /file/combine-tasks/{task_id} and return on success."""
    manager = _make_manager(tmp_path)
    transport = StubTransport(
        [
            _ok({"task_id": "task-xyz"}),
            # First poll: still pending
            HttpResponse(
                200,
                json.dumps({"status": 0, "data": {"status": "pending"}}).encode(),
                {},
            ),
            # Second poll: success
            HttpResponse(
                200,
                json.dumps(
                    {
                        "status": 0,
                        "data": {
                            "status": "success",
                            "file": {"file_id": "merged2", "file_name": "Merged"},
                        },
                    }
                ).encode(),
                {},
            ),
        ]
    )
    client = PlaudClient(manager, transport=transport)
    detail = client.merge_recordings(["r1", "r2"], "Merged", poll_interval_s=0)

    assert detail.id == "merged2"
    poll_urls = [c["url"] for c in transport.calls[1:]]
    assert all("combine-tasks/task-xyz" in u for u in poll_urls)


def test_merge_raises_on_error_status(tmp_path):
    manager = _make_manager(tmp_path)
    transport = StubTransport(
        [
            _ok({"task_id": "task-err"}),
            HttpResponse(
                200,
                json.dumps(
                    {
                        "status": 0,
                        "data": {"status": "error", "error_message": "source file deleted"},
                    }
                ).encode(),
                {},
            ),
        ]
    )
    client = PlaudClient(manager, transport=transport)
    with pytest.raises(PlaudApiError, match="source file deleted"):
        client.merge_recordings(["r1", "r2"], "X", poll_interval_s=0)


def test_merge_raises_on_timeout(tmp_path):
    manager = _make_manager(tmp_path)
    # timeout_s=-1 guarantees deadline is already in the past: no poll responses needed.
    transport = StubTransport([_ok({"task_id": "task-slow"})])
    client = PlaudClient(manager, transport=transport)
    with pytest.raises(PlaudApiError, match="timed out"):
        client.merge_recordings(["r1", "r2"], "slow", poll_interval_s=0, timeout_s=-1)


def test_merge_rejects_fewer_than_two_ids(tmp_path):
    manager = _make_manager(tmp_path)
    client = PlaudClient(manager, transport=StubTransport([]))
    with pytest.raises(ValueError, match="at least 2"):
        client.merge_recordings(["r1"], "title")


def test_merge_rejects_blank_filename(tmp_path):
    manager = _make_manager(tmp_path)
    client = PlaudClient(manager, transport=StubTransport([]))
    with pytest.raises(ValueError, match="filename cannot be empty"):
        client.merge_recordings(["r1", "r2"], "  ")


def test_merge_raises_on_missing_task_id(tmp_path):
    manager = _make_manager(tmp_path)
    transport = StubTransport([_ok({})])  # no task_id in response
    client = PlaudClient(manager, transport=transport)
    with pytest.raises(PlaudApiError, match="missing task_id"):
        client.merge_recordings(["r1", "r2"], "title")


# ---------------------------------------------------------------------------
# Fixture-based tests: wait_for_transcription
# ---------------------------------------------------------------------------


def _make_detail_response(*, is_trans: bool) -> HttpResponse:
    return HttpResponse(
        200,
        json.dumps(
            {
                "status": 0,
                "data": {
                    "file_id": "rec1",
                    "file_name": "Meeting",
                    "content_list": [
                        {
                            "data_type": "transaction",
                            "task_status": 1 if is_trans else 0,
                        }
                    ],
                },
            }
        ).encode(),
        {},
    )


def test_wait_for_transcription_returns_when_done(tmp_path):
    manager = _make_manager(tmp_path)
    transport = StubTransport(
        [
            _make_detail_response(is_trans=False),
            _make_detail_response(is_trans=True),
        ]
    )
    client = PlaudClient(manager, transport=transport)
    client.wait_for_transcription("rec1", poll_interval_s=0)
    assert len(transport.calls) == 2


def test_wait_for_transcription_times_out(tmp_path):
    manager = _make_manager(tmp_path)
    # timeout_s=-1: deadline already in the past, raises before any poll.
    transport = StubTransport([])
    client = PlaudClient(manager, transport=transport)
    with pytest.raises(PlaudApiError, match="timed out"):
        client.wait_for_transcription("rec1", timeout_s=-1, poll_interval_s=0)


# ---------------------------------------------------------------------------
# Transcode unit tests
# ---------------------------------------------------------------------------


def test_transcode_raises_when_ffmpeg_not_found(tmp_path, monkeypatch):
    monkeypatch.delenv("FFMPEG_BIN", raising=False)
    monkeypatch.setattr("plaud_tools.core.transcode.shutil.which", lambda _: None)
    src = tmp_path / "audio.wav"
    src.write_bytes(b"x")
    with pytest.raises(RuntimeError, match="Could not locate ffmpeg"):
        transcode_to_mp3_path(src, tmp_path / "out.mp3")


def test_find_ffmpeg_frozen_sibling(tmp_path, monkeypatch):
    """Frozen mode: ffmpeg.exe beside the exe is found first (MCP path)."""
    from plaud_tools.core.transcode import _find_ffmpeg

    exe_dir = tmp_path / "mcp"
    exe_dir.mkdir()
    ffmpeg_exe = exe_dir / "ffmpeg.exe"
    ffmpeg_exe.write_bytes(b"")
    fake_exe = exe_dir / "plaud-mcp.exe"

    monkeypatch.delenv("FFMPEG_BIN", raising=False)
    monkeypatch.setattr(
        "plaud_tools.core.transcode.sys", type("S", (), {"frozen": True, "executable": str(fake_exe)})()
    )
    monkeypatch.setattr("plaud_tools.core.transcode.shutil.which", lambda _: None)

    assert _find_ffmpeg() == str(ffmpeg_exe)


def test_find_ffmpeg_frozen_cli_falls_back_to_mcp_sibling(tmp_path, monkeypatch):
    """Frozen CLI mode: no ffmpeg beside the CLI exe, resolves ../mcp/ffmpeg.exe."""
    from plaud_tools.core.transcode import _find_ffmpeg

    # Lay out PlaudTools/cli/ and PlaudTools/mcp/ under tmp_path
    cli_dir = tmp_path / "cli"
    cli_dir.mkdir()
    mcp_dir = tmp_path / "mcp"
    mcp_dir.mkdir()
    ffmpeg_exe = mcp_dir / "ffmpeg.exe"
    ffmpeg_exe.write_bytes(b"")
    fake_cli_exe = cli_dir / "plaud-tools.exe"

    monkeypatch.delenv("FFMPEG_BIN", raising=False)
    monkeypatch.setattr(
        "plaud_tools.core.transcode.sys", type("S", (), {"frozen": True, "executable": str(fake_cli_exe)})()
    )
    monkeypatch.setattr("plaud_tools.core.transcode.shutil.which", lambda _: None)

    assert _find_ffmpeg() == str(ffmpeg_exe)


def test_find_ffmpeg_frozen_no_bundle_falls_back_to_path(tmp_path, monkeypatch):
    """Frozen mode: no bundled ffmpeg at all → falls back to shutil.which."""
    from plaud_tools.core.transcode import _find_ffmpeg

    cli_dir = tmp_path / "cli"
    cli_dir.mkdir()
    fake_cli_exe = cli_dir / "plaud-tools.exe"

    monkeypatch.delenv("FFMPEG_BIN", raising=False)
    monkeypatch.setattr(
        "plaud_tools.core.transcode.sys", type("S", (), {"frozen": True, "executable": str(fake_cli_exe)})()
    )
    monkeypatch.setattr("plaud_tools.core.transcode.shutil.which", lambda _: "/usr/bin/ffmpeg")

    assert _find_ffmpeg() == "/usr/bin/ffmpeg"


# ---------------------------------------------------------------------------
# Path-based (streaming) upload tests
# ---------------------------------------------------------------------------


def _make_presign_response(
    n_parts: int, upload_id: str = "uid", object_name: str = "obj.mp3"
) -> HttpResponse:
    """Return a presign HttpResponse for *n_parts* part URLs."""
    return HttpResponse(
        200,
        json.dumps(
            {
                "status": 0,
                "data": {
                    "part_urls": [f"https://s3.fake/part{i + 1}" for i in range(n_parts)],
                    "upload_id": upload_id,
                    "object_name": object_name,
                },
            }
        ).encode(),
        {},
    )


def test_upload_path_reads_from_disk(tmp_path):
    """Path variant: bytes are never loaded into Python memory — chunks come from disk."""
    manager = _make_manager(tmp_path)
    # Write a 7 MiB file to disk; a bytes overload would load all 7 MiB into Python.
    # The Path variant should open-and-read in two chunks (5 MiB + 2 MiB).
    audio_file = tmp_path / "recording.mp3"
    audio_file.write_bytes(b"A" * (7 * 1024 * 1024))

    transport = StubTransport(
        [
            _make_presign_response(2),
            _s3_ok("etag1"),
            _s3_ok("etag2"),
            _ok({}),
            _ok({"data": {"id": "rec1", "filename": "recording"}}),
        ]
    )
    client = PlaudClient(manager, transport=transport)
    recording = client.upload_recording(audio_file, "recording", "MP3")

    assert recording.id == "rec1"
    s3_calls = [c for c in transport.calls if "s3.fake" in c["url"]]
    assert len(s3_calls) == 2
    assert len(s3_calls[0]["body"]) == _CHUNK_SIZE  # 5 MiB
    assert len(s3_calls[1]["body"]) == 2 * 1024 * 1024  # 2 MiB remainder


def test_upload_path_presign_uses_stat_filesize(tmp_path):
    """Path variant: presign body must report the on-disk file size via stat()."""
    manager = _make_manager(tmp_path)
    audio_file = tmp_path / "clip.mp3"
    file_content = b"Z" * 4321
    audio_file.write_bytes(file_content)

    transport = StubTransport(
        [
            _make_presign_response(1, object_name="clip.mp3"),
            _s3_ok(),
            _ok({}),
            _ok({"data": {"id": "r1", "filename": "clip"}}),
        ]
    )
    client = PlaudClient(manager, transport=transport)
    client.upload_recording(audio_file, "clip", "MP3")

    presign_call = transport.calls[0]
    body = json.loads(presign_call["body"])
    assert body["filesize"] == len(file_content)


def test_upload_path_rejects_empty_file(tmp_path):
    """Path variant: zero-byte file must raise ValueError('data cannot be empty')."""
    manager = _make_manager(tmp_path)
    empty = tmp_path / "empty.mp3"
    empty.write_bytes(b"")
    client = PlaudClient(manager, transport=StubTransport([]))
    with pytest.raises(ValueError, match="data cannot be empty"):
        client.upload_recording(empty, "empty", "MP3")


def test_upload_large_sparse_file_chunk_count(tmp_path):
    """20 MiB sparse file uploads in ceil(20 MiB / 5 MiB) = 4 chunks, each ≤ 5 MiB.

    This is the key D2 acceptance test: a large file must be sent in the
    correct number of parts without holding the full file in memory.  We verify
    the chunk count and that no individual part body exceeds _CHUNK_SIZE.
    """
    import math

    manager = _make_manager(tmp_path)
    total_size = 20 * 1024 * 1024  # 20 MiB exactly
    expected_parts = math.ceil(total_size / _CHUNK_SIZE)  # 4

    audio_file = tmp_path / "large.mp3"
    # Write a sparse-ish file: alternating 1 MiB slabs so it isn't fully zero.
    with audio_file.open("wb") as fh:
        for i in range(total_size // (1024 * 1024)):
            fh.write(bytes([i % 256]) * (1024 * 1024))

    responses: list[HttpResponse] = [
        _make_presign_response(expected_parts),
        *[_s3_ok(f"etag{i}") for i in range(expected_parts)],
        _ok({}),
        _ok({"data": {"id": "big1", "filename": "large"}}),
    ]
    transport = StubTransport(responses)
    client = PlaudClient(manager, transport=transport)
    client.upload_recording(audio_file, "large", "MP3")

    s3_calls = [c for c in transport.calls if "s3.fake" in c["url"]]
    assert len(s3_calls) == expected_parts, (
        f"Expected {expected_parts} S3 PUTs for {total_size} bytes at "
        f"{_CHUNK_SIZE}-byte chunks, got {len(s3_calls)}"
    )
    for idx, call in enumerate(s3_calls):
        part_size = len(call["body"])
        assert part_size <= _CHUNK_SIZE, (
            f"Part {idx + 1} body size {part_size} exceeds _CHUNK_SIZE {_CHUNK_SIZE}"
        )
    # All 4 parts fill the chunk size exactly (20 MiB / 5 MiB = 4 with no remainder)
    for idx, call in enumerate(s3_calls):
        assert len(call["body"]) == _CHUNK_SIZE, (
            f"Part {idx + 1} should be exactly {_CHUNK_SIZE} bytes, got {len(call['body'])}"
        )


def test_transcode_to_mp3_path_writes_output(tmp_path, monkeypatch):
    """transcode_to_mp3_path writes ffmpeg output directly to dest_path."""
    fake_ff = tmp_path / "ffmpeg"
    fake_ff.write_bytes(b"")
    monkeypatch.setenv("FFMPEG_BIN", str(fake_ff))

    expected_output = b"streamed mp3 bytes"

    def fake_run(cmd, capture_output):
        # cmd[-1] is the dest_path argument
        Path(cmd[-1]).write_bytes(expected_output)
        return type("R", (), {"returncode": 0, "stderr": b""})()

    monkeypatch.setattr("plaud_tools.core.transcode.subprocess.run", fake_run)

    src = tmp_path / "audio.m4a"
    src.write_bytes(b"source audio")
    dest = tmp_path / "out.mp3"

    transcode_to_mp3_path(src, dest)

    assert dest.read_bytes() == expected_output


def test_transcode_to_mp3_path_raises_on_ffmpeg_failure(tmp_path, monkeypatch):
    """transcode_to_mp3_path raises RuntimeError when ffmpeg exits non-zero."""
    fake_ff = tmp_path / "ffmpeg"
    fake_ff.write_bytes(b"")
    monkeypatch.setenv("FFMPEG_BIN", str(fake_ff))

    def fake_run(cmd, capture_output):
        return type("R", (), {"returncode": 1, "stderr": b"bad input"})()

    monkeypatch.setattr("plaud_tools.core.transcode.subprocess.run", fake_run)

    src = tmp_path / "audio.m4a"
    src.write_bytes(b"bad audio")
    dest = tmp_path / "out.mp3"

    with pytest.raises(RuntimeError, match="ffmpeg exited 1"):
        transcode_to_mp3_path(src, dest)


# ---------------------------------------------------------------------------
# upload_with_transcode — shared CLI/MCP upload orchestration (#149 / Simp 7.3)
# ---------------------------------------------------------------------------


class _FakeUploadClient:
    """Minimal stand-in for PlaudClient's upload/folder-move surface."""

    def __init__(self, *, folder_move_error: Exception | None = None):
        self.upload_calls: list[tuple] = []
        self.folder_calls: list[tuple] = []
        self._folder_move_error = folder_move_error
        self._next_id = 0

    def upload_recording(
        self, data, filename, file_type, *, start_time=None, timezone_offset=None, timeout_s=None
    ):
        from plaud_tools.core.models import Recording

        self._next_id += 1
        # Record whether *data* was a real on-disk path so tests can assert
        # the transcode branch swapped in the temp mp3 path.
        self.upload_calls.append((data, filename, file_type, start_time, timezone_offset))
        return Recording(id=f"rec{self._next_id}", filename=filename)

    def set_recording_folder(self, recording_id, folder_id):
        self.folder_calls.append((recording_id, folder_id))
        if self._folder_move_error is not None:
            raise self._folder_move_error


def test_upload_with_transcode_native_format_skips_ffmpeg(tmp_path):
    from plaud_tools.core.transcode import upload_with_transcode

    audio = tmp_path / "audio.mp3"
    audio.write_bytes(b"fake mp3")
    client = _FakeUploadClient()

    outcome = upload_with_transcode(client, audio, "My Recording")

    assert outcome.recording.id == "rec1"
    assert outcome.transcoded is False
    assert outcome.folder_error is None
    assert outcome.folder_id is None
    assert client.upload_calls[0][0] == audio  # uploaded straight from the source path
    assert client.folder_calls == []


def test_upload_with_transcode_transcodes_non_native_format(tmp_path, monkeypatch):
    from plaud_tools.core.transcode import upload_with_transcode

    fake_ff = tmp_path / "ffmpeg"
    fake_ff.write_bytes(b"")
    monkeypatch.setenv("FFMPEG_BIN", str(fake_ff))

    written_mp3_paths: list[Path] = []

    def fake_run(cmd, capture_output):
        out_path = Path(cmd[-1])
        out_path.write_bytes(b"transcoded")
        written_mp3_paths.append(out_path)
        return type("R", (), {"returncode": 0, "stderr": b""})()

    monkeypatch.setattr("plaud_tools.core.transcode.subprocess.run", fake_run)

    audio = tmp_path / "audio.wav"
    audio.write_bytes(b"raw wav bytes")
    client = _FakeUploadClient()

    outcome = upload_with_transcode(client, audio, "Wav Recording")

    assert outcome.transcoded is True
    uploaded_path = client.upload_calls[0][0]
    assert uploaded_path != audio  # uploaded from the transcoded temp file, not the source
    assert client.upload_calls[0][2] == "MP3"
    # The temp mp3 must be cleaned up after upload.
    assert not written_mp3_paths[0].exists()


def test_upload_with_transcode_missing_file_raises_value_error(tmp_path):
    from plaud_tools.core.transcode import upload_with_transcode

    client = _FakeUploadClient()
    with pytest.raises(ValueError, match="file not found"):
        upload_with_transcode(client, tmp_path / "missing.mp3", "title")
    assert client.upload_calls == []


def test_upload_with_transcode_ffmpeg_failure_propagates_and_skips_upload(tmp_path, monkeypatch):
    from plaud_tools.core.transcode import upload_with_transcode

    fake_ff = tmp_path / "ffmpeg"
    fake_ff.write_bytes(b"")
    monkeypatch.setenv("FFMPEG_BIN", str(fake_ff))

    def fake_run(cmd, capture_output):
        return type("R", (), {"returncode": 1, "stderr": b"bad input"})()

    monkeypatch.setattr("plaud_tools.core.transcode.subprocess.run", fake_run)

    audio = tmp_path / "audio.wav"
    audio.write_bytes(b"raw wav bytes")
    client = _FakeUploadClient()

    with pytest.raises(RuntimeError, match="ffmpeg exited 1"):
        upload_with_transcode(client, audio, "title")
    assert client.upload_calls == []


def test_upload_with_transcode_folder_move_success(tmp_path):
    from plaud_tools.core.transcode import upload_with_transcode

    audio = tmp_path / "audio.mp3"
    audio.write_bytes(b"fake mp3")
    client = _FakeUploadClient()

    outcome = upload_with_transcode(client, audio, "title", folder_id="tag1")

    assert outcome.folder_id == "tag1"
    assert outcome.folder_error is None
    assert client.folder_calls == [("rec1", "tag1")]


def test_upload_with_transcode_folder_move_failure_is_partial_success(tmp_path):
    """#149: a post-upload folder-move failure must not lose the recording —
    the upload already succeeded, so the caller needs the id back to avoid
    re-uploading a duplicate."""
    from plaud_tools.core.errors import PlaudApiError
    from plaud_tools.core.transcode import upload_with_transcode

    audio = tmp_path / "audio.mp3"
    audio.write_bytes(b"fake mp3")
    client = _FakeUploadClient(folder_move_error=PlaudApiError("folder not found", http_status=404))

    outcome = upload_with_transcode(client, audio, "title", folder_id="missing-folder")

    # The recording must still be surfaced — this is the whole point of #149.
    assert outcome.recording.id == "rec1"
    assert outcome.folder_id is None
    assert outcome.folder_error is not None
    assert "folder not found" in outcome.folder_error


# ---------------------------------------------------------------------------
# Opt-in live integration test
# ---------------------------------------------------------------------------


@pytest.mark.live
@pytest.mark.skipif(
    os.getenv("PLAUD_LIVE_UPLOADS") != "1",
    reason="Set PLAUD_LIVE_UPLOADS=1 to run live upload tests against sacrificial Plaud data.",
)
def test_live_upload_small_mp3():
    """Upload a small MP3 and confirm a transcript is produced."""
    import plaud_tools.core.session as session_mod

    session_path = os.getenv("PLAUD_SESSION_PATH")
    audio_path = os.getenv("PLAUD_TEST_AUDIO_PATH")
    if not session_path or not audio_path:
        pytest.skip("Set PLAUD_SESSION_PATH and PLAUD_TEST_AUDIO_PATH to run this test.")

    from plaud_tools.core.transcode import get_file_type

    store = session_mod.FileSessionStore(session_path)
    client = PlaudClient(SessionManager(store))

    audio = Path(audio_path)
    file_type, needs_transcode = get_file_type(audio)
    # Use path-based upload to exercise the streaming code path in the live test.
    recording = client.upload_recording(audio, f"plaud-tools-live-test-{int(time.time())}", file_type)
    assert recording.id, "Upload should return a recording ID"

    client.transcribe_and_summarize(recording.id)
    client.wait_for_transcription(recording.id, timeout_s=300)

    detail = client.get_recording(recording.id, include_transcript=True)
    assert detail.is_trans, "Recording should have a transcript after processing"
    assert detail.transcript, "Transcript should be non-empty"
