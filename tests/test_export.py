"""export_transcript: whole-transcript export to a file (MCP tool + CLI `export`).

Synthetic fixtures only; no real client transcript content.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from plaud_tools.cli.cli import run_cli
from plaud_tools.core import appdata
from plaud_tools.core.models import RecordingDetail
from plaud_tools.core.query import transcript_fingerprint
from plaud_tools.mcp_pt.mcp import build_handlers


class ExportStub:
    """Serves one mutable transcript block and fakes Plaud's document renderer."""

    def __init__(self, segments, blocks=("transaction",), rendered=b"%PDF-1.4 synthetic"):
        self.segments = segments
        self.blocks = list(blocks)
        self.rendered = rendered
        self.render_calls: list[dict] = []

    def get_recording(self, recording_id, include_transcript=False, transcript_block="transaction", **kwargs):
        return RecordingDetail(
            id=recording_id,
            filename="Synthetic review",
            start_time=1_790_780_400_000,  # 2026-09-30T15:00:00Z
            transcript_segments=self.segments if transcript_block in self.blocks else [],
            transcript_blocks_available=self.blocks,
        )

    def render_transcript_document(self, detail, segments, to_format, *, with_speaker, with_timestamp):
        self.render_calls.append(
            {
                "segments": segments,
                "to_format": to_format,
                "speaker": with_speaker,
                "timestamp": with_timestamp,
            }
        )
        return self.rendered


def _segments(count: int) -> list[dict]:
    return [
        {
            "speaker": f"Speaker {i % 2 + 1}",
            "content": f"line {i}",
            "start_time": i * 1000,
            "end_time": i * 1000 + 900,
        }
        for i in range(count)
    ]


def _export(client, **kwargs) -> tuple[dict, dict]:
    result = build_handlers(lambda: client)["export_transcript"]("rec1", **kwargs)
    return result, json.loads(result["content"][0]["text"])


def _exported_files() -> list[Path]:
    root = appdata.exports_dir()
    return sorted(root.iterdir()) if root.exists() else []


def test_every_utterance_is_exported_once_in_order_including_real_repeats():
    segments = _segments(450)
    # Genuine repeats in the source ("Yes." twice in a row) must survive.
    segments[10]["content"] = segments[11]["content"] = "Yes."
    _, meta = _export(ExportStub(segments))

    doc = json.loads(Path(meta["file"]["path"]).read_text(encoding="utf-8"))
    assert [s["index"] for s in doc["segments"]] == list(range(450))
    assert [s["text"] for s in doc["segments"]] == [s["content"] for s in segments]
    assert meta["segment_count"] == 450
    assert doc["schema_version"] == 1 and doc["source"] == "plaud" and doc["portion_id"] == "full"
    assert doc["recording_id"] == "rec1"
    assert doc["recorded_at"] == "2026-09-30T15:00:00Z"
    assert doc["transcript_fingerprint"] == meta["transcript_fingerprint"]


def test_text_round_trips_exactly_and_missing_speaker_or_timing_is_null():
    awkward = '  Café — "quoted" \\back\\slash\nsecond line\t日本語 🎉  '
    segments = [
        {"speaker": "", "content": awkward},
        {
            "speaker": "Jane Doe",
            "original_speaker": "Speaker 2",
            "content": "ok",
            "start_time": 5,
            "end_time": 5,
        },
    ]
    _, meta = _export(ExportStub(segments))

    first, second = json.loads(Path(meta["file"]["path"]).read_bytes().decode("utf-8"))["segments"]
    assert first == {
        "index": 0,
        "speaker": None,
        "speaker_id": None,
        "text": awkward,
        "start_ms": None,
        "end_ms": None,
    }
    assert second["speaker"] == "Jane Doe" and second["speaker_id"] == "Speaker 2"
    assert (second["start_ms"], second["end_ms"]) == (5, 5)


def test_reported_size_and_sha256_match_the_file_and_reexport_is_byte_identical():
    client = ExportStub(_segments(3))
    _, first = _export(client)
    _, again = _export(client)

    data = Path(first["file"]["path"]).read_bytes()
    assert first["file"]["byte_size"] == len(data)
    assert first["file"]["sha256"] == hashlib.sha256(data).hexdigest()
    assert first["file"]["mime_type"] == "application/json"
    assert again["file"]["sha256"] == first["file"]["sha256"]
    assert again["transcript_fingerprint"] == first["transcript_fingerprint"]


def test_an_edited_source_keeps_its_id_and_gets_a_new_fingerprint():
    client = ExportStub(_segments(3))
    _, before = _export(client)
    client.segments[1]["speaker"] = "Jane Doe"
    _, after = _export(client)
    assert after["recording_id"] == before["recording_id"]
    assert after["transcript_fingerprint"] != before["transcript_fingerprint"]


def test_expected_fingerprint_refuses_a_transcript_edited_since_review():
    client = ExportStub(_segments(3))
    reviewed = transcript_fingerprint(client.segments, "transaction")
    ok, _ = _export(client, expected_transcript_fingerprint=reviewed)
    assert not ok.get("isError")

    client.segments[0]["content"] = "line 0, corrected"
    for path in _exported_files():
        path.unlink()
    result, payload = _export(client, expected_transcript_fingerprint=reviewed)
    assert result["isError"] and payload["error_code"] == "revision_conflict"
    assert _exported_files() == []


@pytest.mark.parametrize(
    ("client", "kwargs", "code"),
    [
        (ExportStub([], blocks=()), {}, "transcript_unavailable"),
        # Asked for polished, only raw exists: refuse, never fall back.
        (ExportStub(_segments(2)), {"transcript_block": "transaction_polish"}, "transcript_unavailable"),
        (ExportStub([]), {}, "transcript_unavailable"),
        (ExportStub([{"content": "x", "start_time": -1, "end_time": 5}]), {}, "invalid_source"),
        (ExportStub([{"content": "x", "start_time": 10, "end_time": None}]), {}, "invalid_source"),
        (ExportStub([{"content": "x", "start_time": 10, "end_time": 9}]), {}, "invalid_source"),
        # Plaud handed back something that is not a PDF (an error page, say).
        (ExportStub(_segments(2), rendered=b"<html>"), {"format": "pdf"}, "invalid_source"),
    ],
)
def test_failures_leave_no_file(client, kwargs, code):
    result, payload = _export(client, **kwargs)
    assert result["isError"] and payload["error_code"] == code
    assert _exported_files() == []


def test_plaud_formats_send_the_fetched_segments_and_options():
    client = ExportStub(_segments(3), rendered=b"PK\x03\x04 synthetic docx")
    _, meta = _export(client, format="docx", with_speakers=False, with_timestamps=False)
    assert client.render_calls[0] == {
        "segments": client.segments,
        "to_format": "DOCX",
        "speaker": False,
        "timestamp": False,
    }
    assert Path(meta["file"]["path"]).name == "rec1.transaction.docx"
    assert Path(meta["file"]["path"]).read_bytes() == b"PK\x03\x04 synthetic docx"
    assert meta["transcript_fingerprint"] == transcript_fingerprint(client.segments, "transaction")


def test_srt_always_gets_timestamps():
    # Plaud writes every cue as 00:00:00,000 --> 00:00:00,000 without them.
    client = ExportStub(_segments(2), rendered=b"1\n00:00:00,000 --> 00:00:00,900\n")
    _export(client, format="srt", with_timestamps=False)
    assert client.render_calls[0]["timestamp"] is True


def test_output_path_accepts_a_folder_or_a_file_and_never_replaces_without_overwrite(tmp_path):
    client = ExportStub(_segments(2))
    _, into_folder = _export(client, output_path=str(tmp_path / "client") + "/")
    assert into_folder["file"]["path"] == str((tmp_path / "client" / "rec1.transaction.json").resolve())

    target = tmp_path / "2026-09-30 Meeting Transcript.json"
    target.write_text("already filed", encoding="utf-8")
    result, payload = _export(client, output_path=str(target))
    assert result["isError"] and payload["error_code"] == "file_exists"
    assert target.read_text(encoding="utf-8") == "already filed"

    _, replaced = _export(client, output_path=str(target), overwrite=True)
    assert replaced["file"]["path"] == str(target.resolve())
    assert json.loads(target.read_text(encoding="utf-8"))["recording_id"] == "rec1"


def test_unsafe_recording_id_is_rejected_before_any_fetch():
    result = build_handlers(lambda: ExportStub(_segments(1)))["export_transcript"]("../../evil")
    assert json.loads(result["content"][0]["text"])["error_code"] == "validation"
    assert _exported_files() == []


def test_response_carries_metadata_not_transcript_text():
    segments = _segments(3)
    segments[0]["content"] = "a sentence that must stay out of the response"
    result, _ = _export(ExportStub(segments))
    assert "must stay out" not in json.dumps(result)


def test_cli_export_writes_the_file_and_prints_metadata(tmp_path):
    client = ExportStub(_segments(2))
    out = json.loads(
        run_cli(["export", "rec1", "-f", "pdf", "--no-speakers", "-o", str(tmp_path) + "/"], client)
    )
    assert Path(out["file"]["path"]).read_bytes() == client.rendered
    assert client.render_calls[0]["speaker"] is False and client.render_calls[0]["timestamp"] is True


def test_fingerprint_distinguishes_raw_from_polished_with_identical_segments():
    segments = _segments(2)
    assert transcript_fingerprint(segments, "transaction") != transcript_fingerprint(
        segments, "transaction_polish"
    )
