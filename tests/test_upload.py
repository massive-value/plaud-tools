from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest

from plaud_tools.core.client import _CHUNK_SIZE, PlaudClient
from plaud_tools.core.errors import PlaudApiError, PlaudWaitTimeoutError
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


def _audio(tmp_path: Path, data: bytes) -> Path:
    """Write *data* to an audio file under tmp_path; upload_recording reads from disk."""
    path = tmp_path / "audio.mp3"
    path.write_bytes(data)
    return path


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
    client.upload_recording(_audio(tmp_path, audio_data), "test", "MP3")

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
    client.upload_recording(_audio(tmp_path, audio_data), "small", "MP3")

    s3_calls = [c for c in transport.calls if "s3.fake" in c["url"]]
    assert len(s3_calls) == 1
    assert len(s3_calls[0]["body"]) == 2 * 1024 * 1024


# ---------------------------------------------------------------------------
# Fixture-based tests: upload request shapes
# ---------------------------------------------------------------------------


def _created(*part_urls: str, upload_id: str = "uid") -> HttpResponse:
    return _ok({"data": {"part_urls": list(part_urls) or ["https://s3.fake/p1"], "upload_id": upload_id}})


def _confirmed(file_id: str = "f_new") -> HttpResponse:
    return _ok(
        {"data": {"file_id": file_id, "node_id": "n_file", "folder_node_id": "n_wrap", "file_name": "rec"}}
    )


def test_upload_create_request_shape(tmp_path):
    transport = StubTransport([_created(), _s3_ok(), _ok({}), _confirmed()])
    client = PlaudClient(_make_manager(tmp_path), transport=transport)
    client.upload_recording(_audio(tmp_path, b"a" * 1024), "my recording", "MP3")

    create_call = transport.calls[0]
    assert create_call["method"] == "POST"
    assert create_call["url"].endswith("/file-app/v4/upload/create")
    assert json.loads(create_call["body"]) == {
        "file_name": "my recording",
        "file_size": 1024,
        "file_type": "audio/mpeg",
        "file_extension": "mp3",
    }
    assert create_call["headers"]["Authorization"].startswith("Bearer ")


def test_upload_s3_put_carries_no_headers(tmp_path):
    """The presigned URL is signed without a Content-Type, so the PUT must send none."""
    transport = StubTransport([_created("https://s3.aws.fake/presigned"), _s3_ok(), _ok({}), _confirmed()])
    client = PlaudClient(_make_manager(tmp_path), transport=transport)
    client.upload_recording(_audio(tmp_path, b"b" * 512), "rec", "MP3")

    s3_call = transport.calls[1]
    assert s3_call["method"] == "PUT"
    assert s3_call["url"] == "https://s3.aws.fake/presigned"
    assert s3_call["headers"] == {}
    assert s3_call["body"] == b"b" * 512


def test_upload_merge_part_sends_unquoted_etags(tmp_path):
    transport = StubTransport(
        [
            _created("https://s3.fake/p1", "https://s3.fake/p2", upload_id="uid-x"),
            _s3_ok("etag-1"),
            _s3_ok("etag-2"),
            _ok({}),
            _confirmed(),
        ]
    )
    client = PlaudClient(_make_manager(tmp_path), transport=transport)
    client.upload_recording(_audio(tmp_path, b"c" * (7 * 1024 * 1024)), "rec", "MP3")

    merge_call = transport.calls[3]
    assert merge_call["url"].endswith("/file-app/v4/upload/merge-part")
    assert json.loads(merge_call["body"]) == {
        "upload_id": "uid-x",
        "parts": [{"etag": "etag-1", "part_number": 1}, {"etag": "etag-2", "part_number": 2}],
    }


def test_upload_confirm_request_shape(tmp_path):
    transport = StubTransport([_created(upload_id="uid-y"), _s3_ok(), _ok({}), _confirmed("f_abc")])
    client = PlaudClient(_make_manager(tmp_path), transport=transport)
    recording = client.upload_recording(
        _audio(tmp_path, b"d" * 512),
        "clip",
        "OGG",
        start_time=1_700_000_000_000,
        timezone_offset=-7.0,
        folder_id="n_folder",
    )

    confirm_call = transport.calls[3]
    assert confirm_call["url"].endswith("/file-app/v4/upload/confirm-audio-web")
    assert confirm_call["headers"]["x-timezone-iana"] == "Etc/GMT+7"
    body = json.loads(confirm_call["body"])
    assert body.pop("file_id").startswith("f_")
    assert body == {
        "upload_id": "uid-y",
        "file_name": "clip",
        "start_time": 1_700_000_000_000,
        "scene_source": 101,
        "conflict_strategy": "keep_both",
        "target_node_id": "n_folder",
    }
    assert (recording.id, recording.node_id, recording.wrapper_id, recording.folder_id) == (
        "f_abc",
        "n_file",
        "n_wrap",
        "n_folder",
    )


def test_upload_rejects_blank_filename(tmp_path):
    manager = _make_manager(tmp_path)
    client = PlaudClient(manager, transport=StubTransport([]))
    with pytest.raises(ValueError, match="filename cannot be empty"):
        client.upload_recording(_audio(tmp_path, b"x"), "  ", "MP3")


def test_upload_rejects_unknown_file_type(tmp_path):
    manager = _make_manager(tmp_path)
    client = PlaudClient(manager, transport=StubTransport([]))
    with pytest.raises(ValueError, match="file_type must be MP3"):
        client.upload_recording(_audio(tmp_path, b"x"), "test", "FLAC")


def test_upload_raises_on_missing_create_fields(tmp_path):
    transport = StubTransport([_ok({"data": {"upload_id": "uid"}})])  # no part_urls
    client = PlaudClient(_make_manager(tmp_path), transport=transport)
    with pytest.raises(PlaudApiError, match="upload/create response missing fields"):
        client.upload_recording(_audio(tmp_path, b"x"), "test", "MP3")


def test_upload_raises_when_s3_returns_no_etag(tmp_path):
    transport = StubTransport([_created(), HttpResponse(200, b"", {})])
    client = PlaudClient(_make_manager(tmp_path), transport=transport)
    with pytest.raises(PlaudApiError, match="no ETag for part 1"):
        client.upload_recording(_audio(tmp_path, b"data"), "test", "MP3")


def test_upload_refuses_to_merge_when_file_outlasts_part_urls(tmp_path):
    """Fewer part URLs than chunks must fail, not store a truncated recording."""
    transport = StubTransport([_created(), _s3_ok("etag1")])
    client = PlaudClient(_make_manager(tmp_path), transport=transport)
    with pytest.raises(PlaudApiError, match="file has more data"):
        client.upload_recording(_audio(tmp_path, b"z" * (_CHUNK_SIZE + 1)), "big", "MP3")
    assert not any("merge-part" in call["url"] for call in transport.calls)


# ---------------------------------------------------------------------------
# Fixture-based tests: merge + polling
# ---------------------------------------------------------------------------


def _detail(file_id: str = "f_1", *, duration: int = 60_000, objects: list | None = None) -> HttpResponse:
    """A minimal ``files/detail`` response: recording in folder n_fd inside container n_w."""
    return _ok(
        {
            "data": {
                "node": {
                    "node_id": "n_file",
                    "name": "Meeting",
                    "version_ms": 1,
                    "folder": {"folder_id": "n_w"},
                    "display_folder": {"folder_id": "n_fd"},
                },
                "meta": {"file_id": file_id, "duration": duration},
                "objects": objects or [],
            }
        }
    )


def _not_found() -> HttpResponse:
    return HttpResponse(200, json.dumps({"status": -1800311, "msg": "file not found"}).encode(), {})


def test_merge_request_shape(tmp_path):
    transport = StubTransport(
        [_detail("of_aa"), _ok({"data": {"task_id": "t1", "file_id": "f_m"}}), _detail("f_m")]
    )
    client = PlaudClient(_make_manager(tmp_path), transport=transport)
    merged = client.merge_recordings(
        ["d48edd585ca134b19580ce827752925b", "of_bb"], "Combined", poll_interval_s=0
    )

    merge_call = transport.calls[1]
    assert merge_call["url"].endswith("/file-app/v4/tasks/audio-merge")
    body = json.loads(merge_call["body"])
    assert body["source_file_ids"] == ["of_d48edd585ca134b19580ce827752925b", "of_bb"]
    assert body["target_node_id"] == "n_fd"
    assert body["file_name"] == "Combined"
    assert merged.id == "f_m"


def test_merge_polls_until_merged_file_has_audio(tmp_path):
    transport = StubTransport(
        [
            _detail("of_aa"),
            _ok({"data": {"task_id": "t1", "file_id": "f_m"}}),
            _not_found(),
            _detail("f_m", duration=0),
            _detail("f_m"),
        ]
    )
    client = PlaudClient(_make_manager(tmp_path), transport=transport)
    assert client.merge_recordings(["of_aa", "of_bb"], "Merged", poll_interval_s=0).id == "f_m"
    assert all(call["url"].endswith("/files/detail/f_m") for call in transport.calls[2:])


def test_merge_timeout_carries_task_id(tmp_path):
    """A merge that outlives its budget reports Plaud's task id so nobody re-runs it."""
    transport = StubTransport(
        [_detail("of_aa"), _ok({"data": {"task_id": "task-slow", "file_id": "f_m"}}), _not_found()]
    )
    client = PlaudClient(_make_manager(tmp_path), transport=transport)
    with pytest.raises(PlaudWaitTimeoutError, match="timed out") as info:
        client.merge_recordings(["of_aa", "of_bb"], "slow", poll_interval_s=0, timeout_s=0)
    assert info.value.task_id == "task-slow"


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


# ---------------------------------------------------------------------------
# Fixture-based tests: wait_for_transcription
# ---------------------------------------------------------------------------


def test_wait_for_transcription_returns_when_done(tmp_path):
    pending = {"object_type": "TRANSCRIPT", "task_status": 0}
    done = {"object_type": "TRANSCRIPT", "task_status": 1, "content_url": "https://s3.fake/t"}
    transport = StubTransport([_detail(objects=[pending]), _detail(objects=[done])])
    client = PlaudClient(_make_manager(tmp_path), transport=transport)
    client.wait_for_transcription("f_1", poll_interval_s=0)
    assert len(transport.calls) == 2


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
            _confirmed("f_rec1"),
        ]
    )
    client = PlaudClient(manager, transport=transport)
    recording = client.upload_recording(audio_file, "recording", "MP3")

    assert recording.id == "f_rec1"
    s3_calls = [c for c in transport.calls if "s3.fake" in c["url"]]
    assert len(s3_calls) == 2
    assert len(s3_calls[0]["body"]) == _CHUNK_SIZE  # 5 MiB
    assert len(s3_calls[1]["body"]) == 2 * 1024 * 1024  # 2 MiB remainder


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

    def fake_run(cmd, **kwargs):
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

    def fake_run(cmd, **kwargs):
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
    """Minimal stand-in for PlaudClient's upload surface."""

    def __init__(self):
        self.upload_calls: list[tuple] = []
        self._next_id = 0

    def upload_recording(
        self,
        data,
        filename,
        file_type,
        *,
        start_time=None,
        timezone_offset=None,
        folder_id=None,
        on_progress=None,
    ):
        from plaud_tools.core.models import Recording

        self._next_id += 1
        # Record whether *data* was a real on-disk path so tests can assert
        # the transcode branch swapped in the temp mp3 path.
        self.upload_calls.append((data, filename, file_type, start_time, timezone_offset, folder_id))
        return Recording(id=f"rec{self._next_id}", filename=filename, folder_id=folder_id)


def test_upload_with_transcode_native_format_skips_ffmpeg(tmp_path):
    from plaud_tools.core.transcode import upload_with_transcode

    audio = tmp_path / "audio.mp3"
    audio.write_bytes(b"fake mp3")
    client = _FakeUploadClient()

    outcome = upload_with_transcode(client, audio, "My Recording")

    assert outcome.recording.id == "rec1"
    assert outcome.transcoded is False
    assert client.upload_calls[0][0] == audio  # uploaded straight from the source path


def test_upload_with_transcode_transcodes_non_native_format(tmp_path, monkeypatch):
    from plaud_tools.core.transcode import upload_with_transcode

    fake_ff = tmp_path / "ffmpeg"
    fake_ff.write_bytes(b"")
    monkeypatch.setenv("FFMPEG_BIN", str(fake_ff))

    written_mp3_paths: list[Path] = []

    def fake_run(cmd, **kwargs):
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

    def fake_run(cmd, **kwargs):
        return type("R", (), {"returncode": 1, "stderr": b"bad input"})()

    monkeypatch.setattr("plaud_tools.core.transcode.subprocess.run", fake_run)

    audio = tmp_path / "audio.wav"
    audio.write_bytes(b"raw wav bytes")
    client = _FakeUploadClient()

    with pytest.raises(RuntimeError, match="ffmpeg exited 1"):
        upload_with_transcode(client, audio, "title")
    assert client.upload_calls == []


def test_upload_with_transcode_passes_folder_to_upload(tmp_path):
    from plaud_tools.core.transcode import upload_with_transcode

    audio = tmp_path / "audio.mp3"
    audio.write_bytes(b"fake mp3")
    client = _FakeUploadClient()

    outcome = upload_with_transcode(client, audio, "title", folder_id="n_folder")

    assert client.upload_calls[0][5] == "n_folder"
    assert outcome.recording.folder_id == "n_folder"


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


# ---------------------------------------------------------------------------
# Progress callbacks (#221)
# ---------------------------------------------------------------------------


def test_upload_reports_each_part_sent(tmp_path):
    transport = StubTransport(
        [
            _created("https://s3.fake/p1", "https://s3.fake/p2"),
            _s3_ok("e1"),
            _s3_ok("e2"),
            _ok({}),
            _confirmed(),
        ]
    )
    events: list[tuple] = []
    client = PlaudClient(_make_manager(tmp_path), transport=transport)
    client.upload_recording(
        _audio(tmp_path, b"x" * (_CHUNK_SIZE + 1)), "t", "MP3", on_progress=lambda *args: events.append(args)
    )
    assert events == [(1, 2, "Uploaded part 1 of 2"), (2, 2, "Uploaded part 2 of 2")]


def test_wait_reports_elapsed_out_of_budget_after_each_unfinished_poll(tmp_path):
    transport = StubTransport(
        [
            _detail("of_aa"),
            _ok({"data": {"task_id": "t1", "file_id": "f_m"}}),
            _not_found(),
            _detail("f_m", duration=0),
            _detail("f_m"),
        ]
    )
    events: list[tuple] = []
    client = PlaudClient(_make_manager(tmp_path), transport=transport)
    client.merge_recordings(
        ["of_aa", "of_bb"],
        "Merged",
        poll_interval_s=0,
        timeout_s=300,
        on_progress=lambda *args: events.append(args),
    )
    # Two polls found the merge unfinished; the third saw it done, so no third report.
    assert [(total, message.split(":")[0]) for _progress, total, message in events] == [
        (300, "Waiting for merge"),
        (300, "Waiting for merge"),
    ]
