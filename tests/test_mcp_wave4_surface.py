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

from plaud_tools.core.errors import PlaudApiError
from plaud_tools.core.models import RecordingDetail
from plaud_tools.mcp_pt.mcp import _WAIT_TIMEOUT_S, build_handlers

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


class TestGetRecordingTranscriptPagination:
    """Transcripts page on utterance boundaries, never mid-word.

    Replaced the old character-offset slicing in the v0.8.0 breaking batch: a
    character window tore utterances in half and made the caller do offset
    arithmetic to continue.
    """

    def _client_with_utterances(self, count: int) -> MagicMock:
        mock_client = MagicMock()
        mock_client.get_recording.return_value = RecordingDetail(
            id="r1",
            filename="Meeting",
            is_trans=True,
            transcript_segments=[{"speaker": f"S{i}", "content": f"line {i}"} for i in range(count)],
            transcript_blocks_available=["transaction"],
        )
        return mock_client

    def test_short_transcript_returns_whole_thing_untruncated(self):
        handlers = build_handlers(lambda: self._client_with_utterances(3))
        result = handlers["get_recording"]("r1", include=["transcript"])
        payload = json.loads(result["content"][0]["text"])
        assert payload["transcript"] == "S0: line 0\n\nS1: line 1\n\nS2: line 2"
        assert payload["transcript_truncated"] is False
        assert payload["transcript_utterance_count"] == 3
        assert "transcript_next_after" not in payload

    def test_limit_pages_on_utterance_boundaries(self):
        handlers = build_handlers(lambda: self._client_with_utterances(5))
        result = handlers["get_recording"]("r1", include=["transcript"], transcript_limit=2)
        payload = json.loads(result["content"][0]["text"])
        # Whole utterances only — no partial "line 1" fragment.
        assert payload["transcript"] == "S0: line 0\n\nS1: line 1"
        assert payload["transcript_truncated"] is True
        assert payload["transcript_next_after"] == 2

    def test_next_after_resumes_exactly_where_the_page_ended(self):
        handlers = build_handlers(lambda: self._client_with_utterances(5))
        result = handlers["get_recording"](
            "r1", include=["transcript"], transcript_after=2, transcript_limit=2
        )
        payload = json.loads(result["content"][0]["text"])
        assert payload["transcript"] == "S2: line 2\n\nS3: line 3"
        assert payload["transcript_next_after"] == 4

    def test_final_page_reports_no_next_cursor(self):
        handlers = build_handlers(lambda: self._client_with_utterances(5))
        result = handlers["get_recording"](
            "r1", include=["transcript"], transcript_after=4, transcript_limit=2
        )
        payload = json.loads(result["content"][0]["text"])
        assert payload["transcript"] == "S4: line 4"
        assert "transcript_next_after" not in payload
        # Still flagged truncated: this page is not the start of the transcript.
        assert payload["transcript_truncated"] is True

    def test_after_past_the_end_is_empty_not_an_error(self):
        """A stale cursor should mean "nothing more", not a failure."""
        handlers = build_handlers(lambda: self._client_with_utterances(3))
        result = handlers["get_recording"]("r1", include=["transcript"], transcript_after=99)
        payload = json.loads(result["content"][0]["text"])
        assert payload["transcript"] == ""
        assert "transcript_next_after" not in payload

    def test_default_limit_caps_a_long_transcript(self):
        handlers = build_handlers(lambda: self._client_with_utterances(500))
        result = handlers["get_recording"]("r1", include=["transcript"])
        payload = json.loads(result["content"][0]["text"])
        assert payload["transcript_next_after"] == 200
        assert payload["transcript_truncated"] is True
        assert payload["transcript_utterance_count"] == 500

    def test_negative_after_returns_validation_error(self):
        handlers = build_handlers(lambda: MagicMock())
        result = handlers["get_recording"]("r1", transcript_after=-1)
        payload = json.loads(result["content"][0]["text"])
        assert payload["error_code"] == "validation"

    def test_zero_limit_returns_validation_error(self):
        handlers = build_handlers(lambda: MagicMock())
        result = handlers["get_recording"]("r1", transcript_limit=0)
        payload = json.loads(result["content"][0]["text"])
        assert payload["error_code"] == "validation"

    def test_limit_over_the_cap_returns_validation_error(self):
        handlers = build_handlers(lambda: MagicMock())
        result = handlers["get_recording"]("r1", transcript_limit=5000)
        payload = json.loads(result["content"][0]["text"])
        assert payload["error_code"] == "validation"


# ---------------------------------------------------------------------------
# get_recording — include=["audio_url"]
# ---------------------------------------------------------------------------


class TestGetRecordingAudioUrl:
    def test_audio_url_included_with_expiry(self):
        mock_client = MagicMock()
        mock_client.get_recording.return_value = RecordingDetail(id="r1", filename="Meeting")
        mock_client.get_audio_url.return_value = "https://s3.fake/audiofiles/r1.mp3?sig=x"
        handlers = build_handlers(lambda: mock_client)

        result = handlers["get_recording"]("r1", include=["audio_url"])

        payload = json.loads(result["content"][0]["text"])
        assert payload["audio_url"] == "https://s3.fake/audiofiles/r1.mp3?sig=x"
        # The URL expires in an hour; the caller needs to know not to store it.
        assert payload["audio_url_expires_in_s"] == 3600

    def test_missing_audio_explains_itself(self):
        mock_client = MagicMock()
        mock_client.get_recording.return_value = RecordingDetail(id="r1", filename="Meeting")
        mock_client.get_audio_url.return_value = None
        handlers = build_handlers(lambda: mock_client)

        result = handlers["get_recording"]("r1", include=["audio_url"])

        payload = json.loads(result["content"][0]["text"])
        assert payload["audio_url"] is None
        assert "syncing" in payload["note"]

    def test_audio_url_not_fetched_unless_requested(self):
        mock_client = MagicMock()
        mock_client.get_recording.return_value = RecordingDetail(id="r1", filename="Meeting")
        handlers = build_handlers(lambda: mock_client)

        handlers["get_recording"]("r1")

        mock_client.get_audio_url.assert_not_called()


# ---------------------------------------------------------------------------
# get_recording — transcript_block selection + degraded-result notes
# ---------------------------------------------------------------------------


class TestGetRecordingTranscriptBlock:
    def test_defaults_to_raw_transaction_block(self):
        mock_client = MagicMock()
        mock_client.get_recording.return_value = RecordingDetail(
            id="r1",
            filename="Meeting",
            is_trans=True,
            transcript_segments=[{"speaker": "S1", "content": "raw text"}],
            transcript_blocks_available=["transaction"],
        )
        handlers = build_handlers(lambda: mock_client)

        result = handlers["get_recording"]("r1", include=["transcript"])

        payload = json.loads(result["content"][0]["text"])
        assert payload["transcript_block"] == "transaction"
        assert "note" not in payload
        assert mock_client.get_recording.call_args.kwargs["transcript_block"] == "transaction"

    def test_polish_block_is_forwarded_to_the_client(self):
        mock_client = MagicMock()
        mock_client.get_recording.return_value = RecordingDetail(
            id="r1",
            filename="Meeting",
            is_trans=True,
            transcript_segments=[{"speaker": "S1", "content": "cleaned text"}],
            transcript_blocks_available=["transaction", "transaction_polish"],
        )
        handlers = build_handlers(lambda: mock_client)

        result = handlers["get_recording"](
            "r1", include=["transcript"], transcript_block="transaction_polish"
        )

        payload = json.loads(result["content"][0]["text"])
        assert payload["transcript"] == "S1: cleaned text"
        assert payload["transcript_block"] == "transaction_polish"
        assert mock_client.get_recording.call_args.kwargs["transcript_block"] == "transaction_polish"

    def test_unknown_block_returns_validation_error(self):
        handlers = build_handlers(lambda: MagicMock())
        result = handlers["get_recording"]("r1", transcript_block="outline")
        payload = json.loads(result["content"][0]["text"])
        assert payload["error_code"] == "validation"

    def test_missing_requested_block_names_the_available_ones(self):
        """An empty transcript must say *why* and what to ask for instead."""
        mock_client = MagicMock()
        mock_client.get_recording.return_value = RecordingDetail(
            id="r1",
            filename="Meeting",
            is_trans=True,
            transcript="",
            transcript_blocks_available=["transaction"],
        )
        handlers = build_handlers(lambda: mock_client)

        result = handlers["get_recording"](
            "r1", include=["transcript"], transcript_block="transaction_polish"
        )

        payload = json.loads(result["content"][0]["text"])
        assert "transaction_polish" in payload["note"]
        assert "Available blocks: transaction" in payload["note"]

    def test_untranscribed_recording_points_at_process_recording(self):
        mock_client = MagicMock()
        mock_client.get_recording.return_value = RecordingDetail(
            id="r1", filename="Meeting", transcript="", transcript_blocks_available=[]
        )
        handlers = build_handlers(lambda: mock_client)

        result = handlers["get_recording"]("r1", include=["transcript"])

        payload = json.loads(result["content"][0]["text"])
        assert "process_recording" in payload["note"]

    def test_unfetchable_summary_reports_null_plus_retry_hint(self):
        """is_summary=True but no content is transient — say so instead of a bare placeholder."""
        mock_client = MagicMock()
        mock_client.get_recording.return_value = RecordingDetail(
            id="r1", filename="Meeting", is_summary=True, ai_content=None
        )
        handlers = build_handlers(lambda: mock_client)

        result = handlers["get_recording"]("r1", include=["summary"])

        payload = json.loads(result["content"][0]["text"])
        assert payload["summary"] is None
        assert "retry" in payload["note"].lower()


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

    def test_wait_timeout_returns_still_processing(self):
        # (#151) merge_recordings' own poll loop (up to 300s by default) is
        # now bounded the same way process_recording's waits are — a soft
        # deadline that reports still_processing instead of blocking the
        # handler thread for the full window.
        mock_client = MagicMock()
        mock_client.merge_recordings.side_effect = PlaudApiError("merge timed out after 90s")
        handlers = build_handlers(lambda: mock_client)

        result = handlers["merge_recordings"](recording_ids=["r1", "r2"], title="Combined")

        payload = json.loads(result["content"][0]["text"])
        assert payload == {"recording_ids": ["r1", "r2"], "title": "Combined", "status": "still_processing"}
        assert "isError" not in result
        mock_client.merge_recordings.assert_called_once_with(
            ["r1", "r2"], "Combined", timeout_s=_WAIT_TIMEOUT_S
        )


# ---------------------------------------------------------------------------
# Compact JSON — no indentation/space tax on every response
# ---------------------------------------------------------------------------


class TestCompactJson:
    def test_json_result_has_no_extra_whitespace(self):
        from plaud_tools.mcp_pt.mcp import _json_result

        result = _json_result({"a": 1, "b": [1, 2, 3]})
        text = result["content"][0]["text"]
        assert text == '{"a":1,"b":[1,2,3]}'
