"""Tests for the Wave 4 / v0.7.0 MCP surface changes (§2 of the audit plan).

Covers behaviors that don't fit test_mcp_error_codes.py's error-code focus:
- edit_transcript (merged rename_speaker + correct_transcript): both actions,
  dry_run preview, dropped find/replace echo, unknown action
- edit_summary dry_run preview
- mutate_recording batch (recording_ids) — see also test_mcp_error_codes.py
- bounded process_recording wait -> still_processing on soft-deadline timeout
- get_recording transcript_offset/transcript_max_chars slicing
- browse_recordings trash=True
- compact JSON output (no indentation/whitespace tax)
- merge_recordings slim response shape
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock

from plaud_tools.errors import PlaudApiError
from plaud_tools.mcp import _WAIT_TIMEOUT_S, build_handlers
from plaud_tools.models import RecordingDetail

# ---------------------------------------------------------------------------
# edit_transcript — merged rename_speaker + correct_transcript
# ---------------------------------------------------------------------------


class TestEditTranscript:
    def test_rename_speaker_success(self):
        mock_client = MagicMock()
        mock_client.rename_speaker.return_value = {"segments_updated": 5}
        handlers = build_handlers(lambda: mock_client)

        result = handlers["edit_transcript"](
            recording_id="r1", action="rename_speaker", original_label="Speaker 1", new_name="Alex"
        )

        payload = json.loads(result["content"][0]["text"])
        assert payload == {
            "ok": True,
            "recording_id": "r1",
            "action": "rename_speaker",
            "segments_updated": 5,
        }
        mock_client.rename_speaker.assert_called_once_with("r1", "Speaker 1", "Alex")

    def test_rename_speaker_missing_params_returns_validation(self):
        handlers = build_handlers(lambda: MagicMock())
        result = handlers["edit_transcript"](recording_id="r1", action="rename_speaker")
        payload = json.loads(result["content"][0]["text"])
        assert payload["error_code"] == "validation"

    def test_correct_success_drops_find_replace_echo(self):
        mock_client = MagicMock()
        mock_client.correct_transcript.return_value = {"replacements": 3, "segments_changed": 2}
        handlers = build_handlers(lambda: mock_client)

        result = handlers["edit_transcript"](recording_id="r1", action="correct", find="teh", replace="the")

        payload = json.loads(result["content"][0]["text"])
        assert payload == {
            "ok": True,
            "recording_id": "r1",
            "action": "correct",
            "replacements": 3,
            "segments_changed": 2,
        }
        assert "find" not in payload
        assert "replace" not in payload

    def test_correct_missing_find_returns_validation(self):
        handlers = build_handlers(lambda: MagicMock())
        result = handlers["edit_transcript"](recording_id="r1", action="correct", find="x")
        payload = json.loads(result["content"][0]["text"])
        assert payload["error_code"] == "validation"

    def test_correct_dry_run_returns_match_count_without_mutating(self):
        mock_client = MagicMock()
        mock_client.get_recording.return_value = RecordingDetail(
            id="r1", filename="Meeting", transcript="hello teh world, teh end"
        )
        handlers = build_handlers(lambda: mock_client)

        result = handlers["edit_transcript"](
            recording_id="r1", action="correct", find="teh", replace="the", dry_run=True
        )

        payload = json.loads(result["content"][0]["text"])
        assert payload == {
            "ok": True,
            "recording_id": "r1",
            "action": "correct",
            "dry_run": True,
            "matches": 2,
        }
        mock_client.correct_transcript.assert_not_called()
        mock_client.get_recording.assert_called_once_with("r1", include_transcript=True)

    def test_correct_dry_run_zero_matches_is_not_an_error(self):
        mock_client = MagicMock()
        mock_client.get_recording.return_value = RecordingDetail(
            id="r1", filename="Meeting", transcript="nothing matches here"
        )
        handlers = build_handlers(lambda: mock_client)

        result = handlers["edit_transcript"](
            recording_id="r1", action="correct", find="teh", replace="the", dry_run=True
        )

        payload = json.loads(result["content"][0]["text"])
        assert payload["matches"] == 0
        assert "isError" not in result

    def test_correct_dry_run_no_transcript_returns_validation(self):
        mock_client = MagicMock()
        mock_client.get_recording.return_value = RecordingDetail(id="r1", filename="Meeting", transcript="")
        handlers = build_handlers(lambda: mock_client)

        result = handlers["edit_transcript"](
            recording_id="r1", action="correct", find="x", replace="y", dry_run=True
        )

        payload = json.loads(result["content"][0]["text"])
        assert payload["error_code"] == "validation"

    def test_unknown_action_returns_validation(self):
        handlers = build_handlers(lambda: MagicMock())
        result = handlers["edit_transcript"](recording_id="r1", action="explode")
        payload = json.loads(result["content"][0]["text"])
        assert payload["error_code"] == "validation"
        assert "unknown action" in payload["error"]


# ---------------------------------------------------------------------------
# edit_summary — dry_run preview
# ---------------------------------------------------------------------------


class TestEditSummaryDryRun:
    def test_correct_dry_run_returns_match_count_without_mutating(self):
        mock_client = MagicMock()
        mock_client.get_recording.return_value = RecordingDetail(
            id="r1", filename="Meeting", ai_content="Suzan said hi. Suzan left."
        )
        handlers = build_handlers(lambda: mock_client)

        result = handlers["edit_summary"](
            recording_id="r1", action="correct", find="Suzan", replace="Susan", dry_run=True
        )

        payload = json.loads(result["content"][0]["text"])
        assert payload == {
            "ok": True,
            "recording_id": "r1",
            "action": "correct",
            "dry_run": True,
            "matches": 2,
        }
        mock_client.correct_summary.assert_not_called()
        mock_client.get_recording.assert_called_once_with("r1", include_summary=True)

    def test_correct_dry_run_no_summary_returns_validation(self):
        mock_client = MagicMock()
        mock_client.get_recording.return_value = RecordingDetail(id="r1", filename="Meeting", ai_content=None)
        handlers = build_handlers(lambda: mock_client)

        result = handlers["edit_summary"](
            recording_id="r1", action="correct", find="x", replace="y", dry_run=True
        )

        payload = json.loads(result["content"][0]["text"])
        assert payload["error_code"] == "validation"


# ---------------------------------------------------------------------------
# process_recording — bounded wait (#151)
# ---------------------------------------------------------------------------


class TestProcessRecordingBoundedWait:
    def test_transcript_wait_timeout_returns_still_processing(self):
        mock_client = MagicMock()
        mock_client.wait_for_transcription.side_effect = PlaudApiError("transcription timed out after 90s")
        handlers = build_handlers(lambda: mock_client)

        result = handlers["process_recording"]("r1", wait="transcript")

        payload = json.loads(result["content"][0]["text"])
        assert payload == {"recording_id": "r1", "status": "still_processing"}
        assert "isError" not in result
        mock_client.wait_for_transcription.assert_called_once_with("r1", timeout_s=_WAIT_TIMEOUT_S)
        mock_client.get_recording.assert_not_called()

    def test_summary_wait_timeout_returns_still_processing_with_is_trans(self):
        mock_client = MagicMock()
        mock_client.wait_for_transcription.return_value = None
        mock_client.wait_for_summary.side_effect = PlaudApiError("summary timed out after 90s")
        handlers = build_handlers(lambda: mock_client)

        result = handlers["process_recording"]("r1", wait="summary")

        payload = json.loads(result["content"][0]["text"])
        assert payload == {"recording_id": "r1", "status": "still_processing", "is_trans": True}
        assert "isError" not in result

    def test_non_timeout_api_error_during_wait_still_propagates(self):
        mock_client = MagicMock()
        mock_client.wait_for_transcription.side_effect = PlaudApiError("server error", http_status=503)
        handlers = build_handlers(lambda: mock_client)

        result = handlers["process_recording"]("r1", wait="transcript")

        payload = json.loads(result["content"][0]["text"])
        assert payload["error_code"] == "transient"
        assert payload["retryable"] is True


# ---------------------------------------------------------------------------
# get_recording — transcript_offset / transcript_max_chars slicing
# ---------------------------------------------------------------------------


class TestGetRecordingTranscriptTruncation:
    def _client_with_transcript(self, transcript: str) -> MagicMock:
        mock_client = MagicMock()
        mock_client.get_recording.return_value = RecordingDetail(
            id="r1", filename="Meeting", is_trans=True, transcript=transcript
        )
        return mock_client

    def test_offset_and_max_chars_slice_and_flag_truncated(self):
        handlers = build_handlers(lambda: self._client_with_transcript("0123456789"))
        result = handlers["get_recording"](
            "r1", include=["transcript"], transcript_offset=2, transcript_max_chars=3
        )
        payload = json.loads(result["content"][0]["text"])
        assert payload["transcript"] == "234"
        assert payload["transcript_truncated"] is True

    def test_no_slicing_params_returns_full_untruncated_transcript(self):
        handlers = build_handlers(lambda: self._client_with_transcript("0123456789"))
        result = handlers["get_recording"]("r1", include=["transcript"])
        payload = json.loads(result["content"][0]["text"])
        assert payload["transcript"] == "0123456789"
        assert payload["transcript_truncated"] is False

    def test_max_chars_exactly_covering_full_text_is_not_truncated(self):
        handlers = build_handlers(lambda: self._client_with_transcript("0123456789"))
        result = handlers["get_recording"]("r1", include=["transcript"], transcript_max_chars=10)
        payload = json.loads(result["content"][0]["text"])
        assert payload["transcript"] == "0123456789"
        assert payload["transcript_truncated"] is False

    def test_negative_offset_returns_validation_error(self):
        handlers = build_handlers(lambda: MagicMock())
        result = handlers["get_recording"]("r1", transcript_offset=-1)
        payload = json.loads(result["content"][0]["text"])
        assert payload["error_code"] == "validation"

    def test_zero_max_chars_returns_validation_error(self):
        handlers = build_handlers(lambda: MagicMock())
        result = handlers["get_recording"]("r1", transcript_max_chars=0)
        payload = json.loads(result["content"][0]["text"])
        assert payload["error_code"] == "validation"


# ---------------------------------------------------------------------------
# browse_recordings — trash=True
# ---------------------------------------------------------------------------


class TestBrowseRecordingsTrash:
    def test_trash_true_queries_with_is_trash_one(self):
        mock_client = MagicMock()
        mock_client.list_recordings.return_value = []
        handlers = build_handlers(lambda: mock_client)

        handlers["browse_recordings"](limit=5, trash=True)

        call_query = mock_client.list_recordings.call_args[0][0]
        assert call_query.is_trash == 1

    def test_trash_false_default_queries_with_is_trash_zero(self):
        mock_client = MagicMock()
        mock_client.list_recordings.return_value = []
        handlers = build_handlers(lambda: mock_client)

        handlers["browse_recordings"](limit=5)

        call_query = mock_client.list_recordings.call_args[0][0]
        assert call_query.is_trash == 0


# ---------------------------------------------------------------------------
# merge_recordings — slim response
# ---------------------------------------------------------------------------


class TestMergeRecordingsSlimResponse:
    def test_response_is_slim_ok_recording_id_title(self):
        mock_client = MagicMock()
        mock_client.merge_recordings.return_value = RecordingDetail(id="merged1", filename="Combined")
        handlers = build_handlers(lambda: mock_client)

        result = handlers["merge_recordings"](recording_ids=["r1", "r2"], title="Combined")

        payload = json.loads(result["content"][0]["text"])
        assert payload == {"ok": True, "recording_id": "merged1", "title": "Combined"}


# ---------------------------------------------------------------------------
# Compact JSON — no indentation/space tax on every response
# ---------------------------------------------------------------------------


class TestCompactJson:
    def test_json_result_has_no_extra_whitespace(self):
        from plaud_tools.mcp import _json_result

        result = _json_result({"a": 1, "b": [1, 2, 3]})
        text = result["content"][0]["text"]
        assert text == '{"a":1,"b":[1,2,3]}'
