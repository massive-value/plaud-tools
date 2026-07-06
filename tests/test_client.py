from __future__ import annotations

import base64
import gzip
import io
import json
from pathlib import Path
from urllib.error import HTTPError

import pytest

import plaud_tools.client as client_mod
from plaud_tools.client import PlaudClient, PlaudRecordingQuery
from plaud_tools.errors import PlaudApiError, PlaudSessionExpiredError
from plaud_tools.session import FileSessionStore, PlaudSession, SessionManager, SessionStore
from plaud_tools.transport import HttpResponse


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


def make_jwt(days=300):
    payload = {"exp": 2_000_000_000 + days * 86400}
    encoded = base64.urlsafe_b64encode(json.dumps(payload).encode("utf-8")).decode("ascii").rstrip("=")
    return f"header.{encoded}.sig"


def make_manager(tmp_path: Path, region="eu"):
    path = tmp_path / "session.json"
    store = FileSessionStore(path)
    store.save(PlaudSession(access_token=make_jwt(), region=region, email="test@example.com"))
    return SessionManager(store), store


def test_list_recordings_filters_trash_by_default(tmp_path):
    manager, _ = make_manager(tmp_path)
    transport = StubTransport(
        [
            HttpResponse(
                200,
                json.dumps(
                    {
                        "status": 0,
                        "data_file_list": [
                            {"id": "a", "filename": "keep", "is_trash": False},
                            {"id": "b", "filename": "drop", "is_trash": True},
                        ],
                    }
                ).encode(),
                {},
            )
        ]
    )
    client = PlaudClient(manager, transport=transport)
    items = client.list_recordings()
    assert [item.id for item in items] == ["a"]
    headers = transport.calls[0]["headers"]
    assert headers["app-platform"] == "web"
    assert headers["edit-from"] == "web"
    assert headers["User-Agent"].startswith("Mozilla/5.0")


def test_list_recordings_honors_explicit_query_and_region(tmp_path):
    manager, _ = make_manager(tmp_path)
    transport = StubTransport(
        [HttpResponse(200, json.dumps({"status": 0, "data_file_list": []}).encode(), {})]
    )
    client = PlaudClient(manager, transport=transport)
    client.list_recordings(PlaudRecordingQuery(limit=5, is_trash=0, sort_by="start_time", is_desc=True))
    assert (
        transport.calls[0]["url"]
        == "https://api-euc1.plaud.ai/file/simple/web?limit=5&is_trash=0&sort_by=start_time&is_desc=true"
    )


def test_get_recording_fetches_transcript_from_data_link(tmp_path):
    manager, _ = make_manager(tmp_path)
    segments = [
        {"speaker": "Alex", "original_speaker": "Speaker 1", "content": "Hi"},
        {"speaker": "", "original_speaker": "Speaker 2", "content": "Hello"},
    ]
    transport = StubTransport(
        [
            HttpResponse(
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
                                    "task_status": 1,
                                    "data_link": "https://s3.fake/transcript.json",
                                },
                                {
                                    "data_type": "auto_sum_note",
                                    "task_status": 1,
                                    "data_id": "sum1",
                                },
                            ],
                            "pre_download_content_list": [
                                {"data_id": "sum1", "data_content": json.dumps({"ai_content": "# Summary"})}
                            ],
                        },
                    }
                ).encode(),
                {},
            ),
            HttpResponse(200, json.dumps(segments).encode(), {}),
        ]
    )
    client = PlaudClient(manager, transport=transport)
    detail = client.get_recording("rec1", include_transcript=True)
    assert detail.transcript == "Alex: Hi\n\nSpeaker 2: Hello"
    assert detail.ai_content == "# Summary"
    assert transport.calls[1]["url"] == "https://s3.fake/transcript.json"
    assert detail.speakers == ["Alex", "Speaker 2"]


def test_get_recording_inline_summary_does_not_fetch(tmp_path):
    manager, _ = make_manager(tmp_path)
    transport = StubTransport(
        [
            HttpResponse(
                200,
                json.dumps(
                    {
                        "status": 0,
                        "data": {
                            "file_id": "rec1",
                            "file_name": "Meeting",
                            "content_list": [
                                {
                                    "data_type": "auto_sum_note",
                                    "task_status": 1,
                                    "data_id": "sum1",
                                    "data_link": "https://s3.fake/summary.json",
                                },
                            ],
                            "pre_download_content_list": [
                                {"data_id": "sum1", "data_content": json.dumps({"ai_content": "# Inline"})}
                            ],
                        },
                    }
                ).encode(),
                {},
            ),
        ]
    )
    client = PlaudClient(manager, transport=transport)
    detail = client.get_recording("rec1", include_summary=True)
    assert detail.is_summary is True
    assert detail.ai_content == "# Inline"
    assert len(transport.calls) == 1
    assert "s3.fake" not in transport.calls[0]["url"]


def test_get_recording_fetches_summary_from_data_link(tmp_path):
    manager, _ = make_manager(tmp_path)
    summary_text = "# Meeting Summary\n\n- Point A\n- Point B"
    transport = StubTransport(
        [
            HttpResponse(
                200,
                json.dumps(
                    {
                        "status": 0,
                        "data": {
                            "file_id": "rec1",
                            "file_name": "Meeting",
                            "content_list": [
                                {
                                    "data_type": "auto_sum_note",
                                    "task_status": 1,
                                    "data_id": "sum1",
                                    "data_link": "https://s3.fake/summary.json",
                                },
                            ],
                        },
                    }
                ).encode(),
                {},
            ),
            HttpResponse(200, json.dumps({"ai_content": summary_text}).encode(), {}),
        ]
    )
    client = PlaudClient(manager, transport=transport)
    detail = client.get_recording("rec1", include_summary=True)
    assert detail.is_summary is True
    assert detail.ai_content == summary_text
    assert transport.calls[1]["url"] == "https://s3.fake/summary.json"


def test_get_recording_skips_summary_fetch_when_not_requested(tmp_path):
    manager, _ = make_manager(tmp_path)
    transport = StubTransport(
        [
            HttpResponse(
                200,
                json.dumps(
                    {
                        "status": 0,
                        "data": {
                            "file_id": "rec1",
                            "file_name": "Meeting",
                            "content_list": [
                                {
                                    "data_type": "auto_sum_note",
                                    "task_status": 1,
                                    "data_id": "sum1",
                                    "data_link": "https://s3.fake/summary.json",
                                },
                            ],
                        },
                    }
                ).encode(),
                {},
            ),
        ]
    )
    client = PlaudClient(manager, transport=transport)
    detail = client.get_recording("rec1")
    assert detail.is_summary is True
    assert detail.ai_content is None
    assert len(transport.calls) == 1


def test_get_recording_speakers_empty_when_no_transcript(tmp_path):
    manager, _ = make_manager(tmp_path)
    transport = StubTransport(
        [
            HttpResponse(
                200,
                json.dumps({"status": 0, "data": {"file_id": "rec1", "file_name": "Meeting"}}).encode(),
                {},
            )
        ]
    )
    client = PlaudClient(manager, transport=transport)
    detail = client.get_recording("rec1", include_transcript=False)
    assert detail.speakers == []


def test_region_failover_updates_persisted_region(tmp_path):
    manager, store = make_manager(tmp_path, region="us")
    transport = StubTransport(
        [
            HttpResponse(
                200,
                json.dumps({"status": -302, "data": {"domains": {"api": "api-euc1.plaud.ai"}}}).encode(),
                {},
            ),
            HttpResponse(200, json.dumps({"status": 0, "data_file_list": []}).encode(), {}),
        ]
    )
    client = PlaudClient(manager, transport=transport)
    client.list_recordings()
    assert store.load().region == "eu"
    assert transport.calls[1]["url"].startswith("https://api-euc1.plaud.ai/")


def test_region_redirect_preserves_request_body(tmp_path):
    """Regression: -302 redirect must re-send the original POST body, not drop it."""
    manager, store = make_manager(tmp_path, region="us")
    transport = StubTransport(
        [
            # First call returns -302 region mismatch
            HttpResponse(
                200,
                json.dumps({"status": -302, "data": {"domains": {"api": "api-euc1.plaud.ai"}}}).encode(),
                {},
            ),
            # Retry after region update should succeed
            HttpResponse(200, json.dumps({"status": 0}).encode(), {}),
        ]
    )
    client = PlaudClient(manager, transport=transport)
    # move_to_trash issues POST /file/trash/ with body ["rec1"]
    client.move_to_trash(["rec1"])

    assert len(transport.calls) == 2
    # First call went to the original (us) region
    assert transport.calls[0]["url"].startswith("https://api.plaud.ai/")
    # Second call went to the eu region
    assert transport.calls[1]["url"].startswith("https://api-euc1.plaud.ai/")
    # Body must be preserved in the retried request
    retry_body = json.loads(transport.calls[1]["body"].decode("utf-8"))
    assert retry_body == ["rec1"], "retry must carry original body, not None"
    # Region was persisted
    assert store.load().region == "eu"


def test_missing_session_raises(tmp_path):
    manager = SessionManager(FileSessionStore(tmp_path / "missing.json"))
    client = PlaudClient(manager, transport=StubTransport([]))
    with pytest.raises(PlaudSessionExpiredError):
        client.list_recordings()


def test_nonzero_status_raises_api_error(tmp_path):
    manager, _ = make_manager(tmp_path)
    transport = StubTransport(
        [HttpResponse(200, json.dumps({"status": -1, "msg": "recording not found"}).encode(), {})]
    )
    client = PlaudClient(manager, transport=transport)
    with pytest.raises(PlaudApiError, match="recording not found"):
        client.get_recording("bogus")


def test_rename_recording_uses_patch_payload(tmp_path):
    manager, _ = make_manager(tmp_path)
    transport = StubTransport([HttpResponse(200, json.dumps({"status": 0}).encode(), {})])
    client = PlaudClient(manager, transport=transport)
    client.rename_recording("rec1", "New Name")
    call = transport.calls[0]
    assert call["method"] == "PATCH"
    assert call["url"] == "https://api-euc1.plaud.ai/file/rec1"
    assert json.loads(call["body"].decode("utf-8")) == {"filename": "New Name"}


def test_rename_recording_rejects_blank_name(tmp_path):
    manager, _ = make_manager(tmp_path)
    client = PlaudClient(manager, transport=StubTransport([]))
    with pytest.raises(ValueError, match="filename cannot be empty"):
        client.rename_recording("rec1", "   ")


def test_list_file_tags_handles_live_shape(tmp_path):
    manager, _ = make_manager(tmp_path)
    transport = StubTransport(
        [
            HttpResponse(
                200,
                json.dumps(
                    {
                        "status": 0,
                        "data_filetag_list": [
                            {"id": "tag1", "name": "Work", "color": "#191919", "icon": "e627"}
                        ],
                    }
                ).encode(),
                {},
            )
        ]
    )
    client = PlaudClient(manager, transport=transport)
    tags = client.list_file_tags()
    assert len(tags) == 1
    assert tags[0].id == "tag1"
    assert tags[0].name == "Work"


def test_set_recording_folder_uses_update_tags_shape(tmp_path):
    manager, _ = make_manager(tmp_path)
    transport = StubTransport([HttpResponse(200, json.dumps({"status": 0}).encode(), {})])
    client = PlaudClient(manager, transport=transport)
    client.set_recording_folder("rec1", "tag1")
    call = transport.calls[0]
    assert call["method"] == "POST"
    assert call["url"] == "https://api-euc1.plaud.ai/file/update-tags"
    assert json.loads(call["body"].decode("utf-8")) == {"file_id_list": ["rec1"], "filetag_id": "tag1"}


def test_list_trash_uses_is_trash_query(tmp_path):
    manager, _ = make_manager(tmp_path)
    transport = StubTransport(
        [
            HttpResponse(
                200,
                json.dumps(
                    {"status": 0, "data_file_list": [{"id": "rec1", "filename": "Old", "is_trash": True}]}
                ).encode(),
                {},
            )
        ]
    )
    client = PlaudClient(manager, transport=transport)
    items = client.list_trash()
    assert [item.id for item in items] == ["rec1"]
    assert transport.calls[0]["url"] == "https://api-euc1.plaud.ai/file/simple/web?is_trash=1"


def test_move_to_trash_uses_array_body(tmp_path):
    manager, _ = make_manager(tmp_path)
    transport = StubTransport([HttpResponse(200, json.dumps({"status": 0}).encode(), {})])
    client = PlaudClient(manager, transport=transport)
    client.move_to_trash(["rec1", "rec2"])
    call = transport.calls[0]
    assert call["method"] == "POST"
    assert call["url"] == "https://api-euc1.plaud.ai/file/trash/"
    assert json.loads(call["body"].decode("utf-8")) == ["rec1", "rec2"]


def test_restore_from_trash_uses_array_body(tmp_path):
    manager, _ = make_manager(tmp_path)
    transport = StubTransport([HttpResponse(200, json.dumps({"status": 0}).encode(), {})])
    client = PlaudClient(manager, transport=transport)
    client.restore_from_trash("rec1")
    call = transport.calls[0]
    assert call["method"] == "POST"
    assert call["url"] == "https://api-euc1.plaud.ai/file/untrash/"
    assert json.loads(call["body"].decode("utf-8")) == ["rec1"]


def test_trash_methods_reject_empty_ids(tmp_path):
    manager, _ = make_manager(tmp_path)
    client = PlaudClient(manager, transport=StubTransport([]))
    with pytest.raises(ValueError, match="recording_ids cannot be empty"):
        client.move_to_trash([])
    with pytest.raises(ValueError, match="recording_ids cannot be empty"):
        client.restore_from_trash([])


def test_delete_recordings_uses_delete_method(tmp_path):
    manager, _ = make_manager(tmp_path)
    transport = StubTransport([HttpResponse(200, json.dumps({"status": 0}).encode(), {})])
    client = PlaudClient(manager, transport=transport)
    client.delete_recordings(["rec1", "rec2"])
    call = transport.calls[0]
    assert call["method"] == "DELETE"
    assert call["url"] == "https://api-euc1.plaud.ai/file/"
    assert json.loads(call["body"].decode("utf-8")) == ["rec1", "rec2"]


def test_delete_recordings_accepts_single_id(tmp_path):
    manager, _ = make_manager(tmp_path)
    transport = StubTransport([HttpResponse(200, json.dumps({"status": 0}).encode(), {})])
    client = PlaudClient(manager, transport=transport)
    client.delete_recordings("rec1")
    assert json.loads(transport.calls[0]["body"].decode("utf-8")) == ["rec1"]


def test_delete_recordings_rejects_empty_ids(tmp_path):
    manager, _ = make_manager(tmp_path)
    client = PlaudClient(manager, transport=StubTransport([]))
    with pytest.raises(ValueError, match="recording_ids cannot be empty"):
        client.delete_recordings([])


def test_edit_transcript_uses_full_trans_result_payload(tmp_path):
    manager, _ = make_manager(tmp_path)
    transport = StubTransport([HttpResponse(200, json.dumps({"status": 0}).encode(), {})])
    client = PlaudClient(manager, transport=transport)
    segments = [
        {
            "start_time": 0,
            "end_time": 5000,
            "content": "hello",
            "speaker": "Alex",
            "original_speaker": "Speaker 1",
        },
        {
            "start_time": 5000,
            "end_time": 10000,
            "content": "world",
            "speaker": "Bee",
            "original_speaker": "Speaker 2",
        },
    ]
    client.edit_transcript("rec1", segments)
    call = transport.calls[0]
    assert call["method"] == "PATCH"
    assert call["url"] == "https://api-euc1.plaud.ai/file/rec1"
    assert json.loads(call["body"].decode("utf-8")) == {
        "trans_result": segments,
        "support_mul_summ": True,
    }


def test_rename_speaker_reads_segments_and_patches_full_transcript(tmp_path):
    manager, _ = make_manager(tmp_path)
    transport = StubTransport(
        [
            HttpResponse(
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
                                    "task_status": 1,
                                    "data_link": "https://s3.fake/rec1-transcript.json",
                                }
                            ],
                        },
                    }
                ).encode(),
                {},
            ),
            HttpResponse(
                200,
                json.dumps(
                    [
                        {
                            "start_time": 0,
                            "end_time": 5000,
                            "content": "one",
                            "speaker": "Speaker 1",
                            "original_speaker": "Speaker 1",
                        },
                        {
                            "start_time": 5000,
                            "end_time": 10000,
                            "content": "two",
                            "speaker": "Speaker 2",
                            "original_speaker": "Speaker 2",
                        },
                        {
                            "start_time": 10000,
                            "end_time": 15000,
                            "content": "three",
                            "speaker": "Speaker 1",
                            "original_speaker": "Speaker 1",
                        },
                    ]
                ).encode(),
                {},
            ),
            HttpResponse(200, json.dumps({"status": 0}).encode(), {}),
        ]
    )
    client = PlaudClient(manager, transport=transport)
    result = client.rename_speaker("rec1", "Speaker 1", "Alex Riley")
    assert result == {"segments_updated": 2}
    write_body = json.loads(transport.calls[2]["body"].decode("utf-8"))
    assert write_body["support_mul_summ"] is True
    assert write_body["trans_result"][0]["speaker"] == "Alex Riley"
    assert write_body["trans_result"][2]["speaker"] == "Alex Riley"
    assert write_body["trans_result"][0]["original_speaker"] == "Speaker 1"
    assert write_body["trans_result"][1]["speaker"] == "Speaker 2"


def test_rename_speaker_rejects_missing_transcript(tmp_path):
    manager, _ = make_manager(tmp_path)
    transport = StubTransport(
        [
            HttpResponse(
                200,
                json.dumps({"status": 0, "data": {"file_id": "rec1", "file_name": "Meeting"}}).encode(),
                {},
            )
        ]
    )
    client = PlaudClient(manager, transport=transport)
    with pytest.raises(ValueError, match="has no transcript yet"):
        client.rename_speaker("rec1", "Speaker 1", "Alex")


def test_rename_speaker_rejects_missing_label_match(tmp_path):
    manager, _ = make_manager(tmp_path)
    transport = StubTransport(
        [
            HttpResponse(
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
                                    "task_status": 1,
                                    "data_link": "https://s3.fake/rec1-transcript.json",
                                }
                            ],
                        },
                    }
                ).encode(),
                {},
            ),
            HttpResponse(
                200,
                json.dumps(
                    [
                        {
                            "start_time": 0,
                            "end_time": 5000,
                            "content": "one",
                            "speaker": "Speaker 2",
                            "original_speaker": "Speaker 2",
                        },
                    ]
                ).encode(),
                {},
            ),
        ]
    )
    client = PlaudClient(manager, transport=transport)
    with pytest.raises(ValueError, match='no segments found for speaker "Speaker 1"'):
        client.rename_speaker("rec1", "Speaker 1", "Alex")


def test_rename_speaker_rejects_blank_labels(tmp_path):
    manager, _ = make_manager(tmp_path)
    client = PlaudClient(manager, transport=StubTransport([]))
    with pytest.raises(ValueError, match="original_label cannot be empty"):
        client.rename_speaker("rec1", "", "Alex")
    with pytest.raises(ValueError, match="new_name cannot be empty"):
        client.rename_speaker("rec1", "Speaker 1", "   ")


def _detail_and_transcript_transport(segments):
    """Build a StubTransport for the GET /file/detail → S3 transcript → PATCH flow."""
    return StubTransport(
        [
            HttpResponse(
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
                                    "task_status": 1,
                                    "data_link": "https://s3.fake/rec1-transcript.json",
                                }
                            ],
                        },
                    }
                ).encode(),
                {},
            ),
            HttpResponse(200, json.dumps(segments).encode(), {}),
            HttpResponse(200, json.dumps({"status": 0}).encode(), {}),
        ]
    )


def test_rename_speaker_matches_display_name(tmp_path):
    """Plaud auto-resolves enrolled voices, so the displayed `speaker` can differ
    from `original_speaker`. Renaming by the displayed name must work (issue: agent
    reported rename_speaker fails for named/bracketed labels)."""
    manager, _ = make_manager(tmp_path)
    transport = _detail_and_transcript_transport(
        [
            {
                "start_time": 0,
                "end_time": 5,
                "content": "hi",
                "speaker": "Kadin Bullock",
                "original_speaker": "Speaker 1",
            },
            {
                "start_time": 5,
                "end_time": 9,
                "content": "yo",
                "speaker": "Speaker 2",
                "original_speaker": "Speaker 2",
            },
            {
                "start_time": 9,
                "end_time": 12,
                "content": "ok",
                "speaker": "Kadin Bullock",
                "original_speaker": "Speaker 1",
            },
        ]
    )
    client = PlaudClient(manager, transport=transport)
    result = client.rename_speaker("rec1", "Kadin Bullock", "Advisor")
    assert result == {"segments_updated": 2}
    write_body = json.loads(transport.calls[2]["body"].decode("utf-8"))
    assert write_body["trans_result"][0]["speaker"] == "Advisor"
    assert write_body["trans_result"][2]["speaker"] == "Advisor"
    # original_speaker is preserved; the untouched speaker is left alone.
    assert write_body["trans_result"][0]["original_speaker"] == "Speaker 1"
    assert write_body["trans_result"][1]["speaker"] == "Speaker 2"


def test_rename_speaker_matches_original_speaker_label(tmp_path):
    """Renaming by the generic 'Speaker N' label still works when the display
    name has been auto-resolved to something else."""
    manager, _ = make_manager(tmp_path)
    transport = _detail_and_transcript_transport(
        [
            {
                "start_time": 0,
                "end_time": 5,
                "content": "hi",
                "speaker": "Kadin Bullock",
                "original_speaker": "Speaker 1",
            },
        ]
    )
    client = PlaudClient(manager, transport=transport)
    result = client.rename_speaker("rec1", "Speaker 1", "Advisor")
    assert result == {"segments_updated": 1}
    write_body = json.loads(transport.calls[2]["body"].decode("utf-8"))
    assert write_body["trans_result"][0]["speaker"] == "Advisor"


def test_correct_transcript_replaces_text_and_patches(tmp_path):
    manager, _ = make_manager(tmp_path)
    transport = _detail_and_transcript_transport(
        [
            {
                "start_time": 0,
                "end_time": 5,
                "content": "Flourish Cache account",
                "speaker": "A",
                "original_speaker": "Speaker 1",
            },
            {
                "start_time": 5,
                "end_time": 9,
                "content": "no match here",
                "speaker": "B",
                "original_speaker": "Speaker 2",
            },
            {
                "start_time": 9,
                "end_time": 12,
                "content": "another Cache and Cache",
                "speaker": "A",
                "original_speaker": "Speaker 1",
            },
        ]
    )
    client = PlaudClient(manager, transport=transport)
    result = client.correct_transcript("rec1", "Cache", "Cash")
    assert result == {"replacements": 3, "segments_changed": 2}
    write_body = json.loads(transport.calls[2]["body"].decode("utf-8"))
    assert write_body["support_mul_summ"] is True
    assert write_body["trans_result"][0]["content"] == "Flourish Cash account"
    assert write_body["trans_result"][1]["content"] == "no match here"
    assert write_body["trans_result"][2]["content"] == "another Cash and Cash"
    # Speaker labels are untouched by a text correction.
    assert write_body["trans_result"][0]["speaker"] == "A"


def test_correct_transcript_rejects_no_match(tmp_path):
    manager, _ = make_manager(tmp_path)
    transport = _detail_and_transcript_transport(
        [
            {
                "start_time": 0,
                "end_time": 5,
                "content": "hello",
                "speaker": "A",
                "original_speaker": "Speaker 1",
            }
        ]
    )
    client = PlaudClient(manager, transport=transport)
    with pytest.raises(ValueError, match='no occurrences of "zzz" found'):
        client.correct_transcript("rec1", "zzz", "x")


def test_correct_transcript_rejects_empty_find(tmp_path):
    manager, _ = make_manager(tmp_path)
    client = PlaudClient(manager, transport=StubTransport([]))
    with pytest.raises(ValueError, match="find text cannot be empty"):
        client.correct_transcript("rec1", "", "x")


def test_correct_transcript_rejects_missing_transcript(tmp_path):
    manager, _ = make_manager(tmp_path)
    transport = StubTransport(
        [
            HttpResponse(
                200,
                json.dumps({"status": 0, "data": {"file_id": "rec1", "file_name": "Meeting"}}).encode(),
                {},
            )
        ]
    )
    client = PlaudClient(manager, transport=transport)
    with pytest.raises(ValueError, match="has no transcript yet"):
        client.correct_transcript("rec1", "a", "b")


# ---------------------------------------------------------------------------
# Summary editing (set_summary / correct_summary) — POST /ai/update_note_info
# ---------------------------------------------------------------------------


def _detail_with_inline_summary(summary_text, *, note_id="auto_sum:hash:rec1"):
    """GET /file/detail response carrying an inline auto_sum_note summary."""
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
                            "data_type": "auto_sum_note",
                            "task_status": 1,
                            "data_id": note_id,
                            "data_link": "https://s3.fake/summary.json",
                        }
                    ],
                    "pre_download_content_list": [
                        {"data_id": note_id, "data_content": json.dumps({"ai_content": summary_text})}
                    ],
                },
            }
        ).encode(),
        {},
    )


def test_correct_summary_replaces_text_and_posts_note_update(tmp_path):
    manager, _ = make_manager(tmp_path)
    transport = StubTransport(
        [
            _detail_with_inline_summary("Customer: Suzan met with Suzan again."),
            HttpResponse(200, json.dumps({"status": 0, "data": {}}).encode(), {}),
        ]
    )
    client = PlaudClient(manager, transport=transport)
    result = client.correct_summary("rec1", "Suzan", "Susan")
    assert result == {"replacements": 2}
    # No S3 fetch was needed (inline summary); detail + note-update = 2 calls.
    assert len(transport.calls) == 2
    post = transport.calls[1]
    assert post["method"] == "POST"
    assert post["url"] == "https://api-euc1.plaud.ai/ai/update_note_info"
    body = json.loads(post["body"].decode("utf-8"))
    assert body == {
        "file_id": "rec1",
        "note_id": "auto_sum:hash:rec1",
        "note_type": "auto_sum_note",
        "note_content": "Customer: Susan met with Susan again.",
    }


def test_correct_summary_rejects_no_match(tmp_path):
    manager, _ = make_manager(tmp_path)
    transport = StubTransport([_detail_with_inline_summary("nothing to change here")])
    client = PlaudClient(manager, transport=transport)
    with pytest.raises(ValueError, match='no occurrences of "zzz" found in summary'):
        client.correct_summary("rec1", "zzz", "x")


def test_correct_summary_rejects_empty_find(tmp_path):
    manager, _ = make_manager(tmp_path)
    client = PlaudClient(manager, transport=StubTransport([]))
    with pytest.raises(ValueError, match="find text cannot be empty"):
        client.correct_summary("rec1", "", "x")


def test_correct_summary_rejects_missing_summary(tmp_path):
    manager, _ = make_manager(tmp_path)
    # content_list has a transcript but no completed auto_sum_note.
    transport = StubTransport(
        [
            HttpResponse(
                200,
                json.dumps(
                    {
                        "status": 0,
                        "data": {
                            "file_id": "rec1",
                            "file_name": "Meeting",
                            "content_list": [{"data_type": "transaction", "task_status": 1}],
                        },
                    }
                ).encode(),
                {},
            )
        ]
    )
    client = PlaudClient(manager, transport=transport)
    with pytest.raises(ValueError, match="has no summary yet"):
        client.correct_summary("rec1", "a", "b")


def test_correct_summary_fetches_content_from_data_link(tmp_path):
    """When the summary is not inlined, correct_summary fetches it from data_link."""
    manager, _ = make_manager(tmp_path)
    transport = StubTransport(
        [
            HttpResponse(
                200,
                json.dumps(
                    {
                        "status": 0,
                        "data": {
                            "file_id": "rec1",
                            "file_name": "Meeting",
                            "content_list": [
                                {
                                    "data_type": "auto_sum_note",
                                    "task_status": 1,
                                    "data_id": "auto_sum:hash:rec1",
                                    "data_link": "https://s3.fake/summary.json",
                                }
                            ],
                        },
                    }
                ).encode(),
                {},
            ),
            HttpResponse(200, json.dumps({"ai_content": "Meeting with Bxb"}).encode(), {}),
            HttpResponse(200, json.dumps({"status": 0, "data": {}}).encode(), {}),
        ]
    )
    client = PlaudClient(manager, transport=transport)
    result = client.correct_summary("rec1", "Bxb", "Bob")
    assert result == {"replacements": 1}
    assert transport.calls[1]["url"] == "https://s3.fake/summary.json"
    body = json.loads(transport.calls[2]["body"].decode("utf-8"))
    assert body["note_content"] == "Meeting with Bob"


def test_set_summary_overwrites_full_content(tmp_path):
    manager, _ = make_manager(tmp_path)
    transport = StubTransport(
        [
            _detail_with_inline_summary("old summary"),
            HttpResponse(200, json.dumps({"status": 0, "data": {}}).encode(), {}),
        ]
    )
    client = PlaudClient(manager, transport=transport)
    client.set_summary("rec1", "# Brand New Summary\n\n- point")
    post = transport.calls[1]
    body = json.loads(post["body"].decode("utf-8"))
    assert body["note_content"] == "# Brand New Summary\n\n- point"
    assert body["note_id"] == "auto_sum:hash:rec1"


def test_set_summary_rejects_empty_content(tmp_path):
    manager, _ = make_manager(tmp_path)
    client = PlaudClient(manager, transport=StubTransport([]))
    with pytest.raises(ValueError, match="summary content cannot be empty"):
        client.set_summary("rec1", "   ")


# ---------------------------------------------------------------------------
# Folder CRUD (create_folder / update_folder / delete_folder) — /filetag/
# ---------------------------------------------------------------------------


def test_create_folder_posts_name_icon_color_and_returns_tag(tmp_path):
    manager, _ = make_manager(tmp_path)
    transport = StubTransport(
        [
            HttpResponse(
                200,
                json.dumps(
                    {
                        "status": 0,
                        "data_filetag": {
                            "id": "new1",
                            "name": "Clients",
                            "icon": "e708",
                            "color": "#f9a251",
                        },
                    }
                ).encode(),
                {},
            )
        ]
    )
    client = PlaudClient(manager, transport=transport)
    tag = client.create_folder("Clients", color="#f9a251", icon="e708")
    assert tag.id == "new1"
    assert tag.name == "Clients"
    call = transport.calls[0]
    assert call["method"] == "POST"
    assert call["url"] == "https://api-euc1.plaud.ai/filetag/"
    assert json.loads(call["body"].decode("utf-8")) == {
        "name": "Clients",
        "icon": "e708",
        "color": "#f9a251",
    }


def test_create_folder_defaults_icon_and_color(tmp_path):
    manager, _ = make_manager(tmp_path)
    transport = StubTransport(
        [HttpResponse(200, json.dumps({"status": 0, "data_filetag": {"id": "x", "name": "N"}}).encode(), {})]
    )
    client = PlaudClient(manager, transport=transport)
    client.create_folder("N")
    body = json.loads(transport.calls[0]["body"].decode("utf-8"))
    assert body["icon"] == client_mod._DEFAULT_FOLDER_ICON
    assert body["color"] == client_mod._DEFAULT_FOLDER_COLOR


def test_create_folder_rejects_blank_name(tmp_path):
    manager, _ = make_manager(tmp_path)
    client = PlaudClient(manager, transport=StubTransport([]))
    with pytest.raises(ValueError, match="folder name cannot be empty"):
        client.create_folder("   ")


def test_create_folder_duplicate_name_raises_api_error(tmp_path):
    manager, _ = make_manager(tmp_path)
    transport = StubTransport(
        [
            HttpResponse(
                200,
                json.dumps({"status": -2, "msg": "filetag name existed", "data_filetag": None}).encode(),
                {},
            )
        ]
    )
    client = PlaudClient(manager, transport=transport)
    with pytest.raises(PlaudApiError, match="filetag name existed"):
        client.create_folder("Existing")


def test_update_folder_patches_only_supplied_fields(tmp_path):
    manager, _ = make_manager(tmp_path)
    transport = StubTransport(
        [
            HttpResponse(
                200,
                json.dumps(
                    {
                        "status": 0,
                        "data_filetag": {"id": "f1", "name": "Renamed", "icon": "e604", "color": "#fb5c5c"},
                    }
                ).encode(),
                {},
            )
        ]
    )
    client = PlaudClient(manager, transport=transport)
    tag = client.update_folder("f1", name="Renamed", color="#fb5c5c")
    assert tag.name == "Renamed"
    call = transport.calls[0]
    assert call["method"] == "PATCH"
    assert call["url"] == "https://api-euc1.plaud.ai/filetag/f1"
    # icon was not supplied, so it must not be in the body.
    assert json.loads(call["body"].decode("utf-8")) == {"name": "Renamed", "color": "#fb5c5c"}


def test_update_folder_requires_at_least_one_field(tmp_path):
    manager, _ = make_manager(tmp_path)
    client = PlaudClient(manager, transport=StubTransport([]))
    with pytest.raises(ValueError, match="at least one of name, color, icon"):
        client.update_folder("f1")


def test_update_folder_rejects_blank_id(tmp_path):
    manager, _ = make_manager(tmp_path)
    client = PlaudClient(manager, transport=StubTransport([]))
    with pytest.raises(ValueError, match="folder_id cannot be empty"):
        client.update_folder("", name="X")


def test_delete_folder_uses_delete_method(tmp_path):
    manager, _ = make_manager(tmp_path)
    transport = StubTransport(
        [HttpResponse(200, json.dumps({"status": 0, "msg": "delete success"}).encode(), {})]
    )
    client = PlaudClient(manager, transport=transport)
    client.delete_folder("f1")
    call = transport.calls[0]
    assert call["method"] == "DELETE"
    assert call["url"] == "https://api-euc1.plaud.ai/filetag/f1"
    assert call["body"] is None


def test_delete_folder_rejects_blank_id(tmp_path):
    manager, _ = make_manager(tmp_path)
    client = PlaudClient(manager, transport=StubTransport([]))
    with pytest.raises(ValueError, match="folder_id cannot be empty"):
        client.delete_folder("")


def test_transcribe_and_summarize_uses_expected_payload(tmp_path):
    manager, _ = make_manager(tmp_path)
    transport = StubTransport(
        [HttpResponse(200, json.dumps({"status": 0, "msg": "task processing"}).encode(), {})]
    )
    client = PlaudClient(manager, transport=transport)
    client.transcribe_and_summarize("rec1")
    call = transport.calls[0]
    assert call["method"] == "POST"
    assert call["url"] == "https://api-euc1.plaud.ai/ai/transsumm/rec1"
    body = json.loads(call["body"].decode("utf-8"))
    assert body["is_reload"] == 0
    assert body["summ_type"] == "AUTO-SELECT"
    assert body["summ_type_type"] == "system"
    assert body["support_mul_summ"] is True
    info = json.loads(body["info"])
    assert info["language"] == "auto"
    assert info["diarization"] == 1
    assert info["llm"] == "auto"


def test_transcribe_and_summarize_honors_overrides(tmp_path):
    manager, _ = make_manager(tmp_path)
    transport = StubTransport([HttpResponse(200, json.dumps({"status": 0}).encode(), {})])
    client = PlaudClient(manager, transport=transport)
    client.transcribe_and_summarize(
        "rec1", template_type="MEETING-CONSULT", language="en", diarization=False, llm="gpt-5"
    )
    body = json.loads(transport.calls[0]["body"].decode("utf-8"))
    assert body["summ_type"] == "MEETING-CONSULT"
    info = json.loads(body["info"])
    assert info == {
        "language": "en",
        "timezone": info["timezone"],
        "diarization": 0,
        "llm": "gpt-5",
    }


def test_get_task_status_shapes_and_filters(tmp_path):
    manager, _ = make_manager(tmp_path)
    transport = StubTransport(
        [
            HttpResponse(
                200,
                json.dumps(
                    {
                        "status": 0,
                        "data": {
                            "file_status_list": [
                                {
                                    "file_id": "rec1",
                                    "task_id": "t1",
                                    "task_type": "transcript",
                                    "task_status": 1,
                                    "sum_type": "",
                                    "sum_type_type": "",
                                    "post_id": 0,
                                    "ppc_status": 0,
                                    "is_chatllm": False,
                                    "auto_save": False,
                                },
                                {
                                    "file_id": "rec2",
                                    "task_id": "t2",
                                    "task_type": "summary",
                                    "task_status": 0,
                                },
                            ]
                        },
                    }
                ).encode(),
                {},
            )
        ]
    )
    client = PlaudClient(manager, transport=transport)
    tasks = client.get_task_status("rec1")
    assert len(tasks) == 1
    assert tasks[0].file_id == "rec1"
    assert tasks[0].is_complete is True


def test_get_task_status_handles_missing_list(tmp_path):
    manager, _ = make_manager(tmp_path)
    transport = StubTransport([HttpResponse(200, json.dumps({"status": 0, "data": {}}).encode(), {})])
    client = PlaudClient(manager, transport=transport)
    assert client.get_task_status() == []


def test_get_recording_fetches_gzip_transcript_from_data_link(tmp_path):
    manager, _ = make_manager(tmp_path)
    transport = StubTransport(
        [
            HttpResponse(
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
                                    "task_status": 1,
                                    "data_link": "https://s3.fake/transcript.json.gz",
                                }
                            ],
                        },
                    }
                ).encode(),
                {},
            ),
            HttpResponse(
                200,
                gzip.compress(
                    json.dumps(
                        [
                            {"speaker": "Alex", "original_speaker": "Speaker 1", "content": "Hi"},
                        ]
                    ).encode("utf-8")
                ),
                {"content-encoding": "gzip"},
            ),
        ]
    )
    client = PlaudClient(manager, transport=transport)
    detail = client.get_recording("rec1", include_transcript=True)
    assert detail.transcript == "Alex: Hi"


def test_session_store_prefers_keyring_when_available(tmp_path, monkeypatch):
    calls = {}

    class FakeKeyring:
        value = None

        @staticmethod
        def set_password(service, account, value):
            calls["service"] = service
            calls["account"] = account
            FakeKeyring.value = value

        @staticmethod
        def get_password(service, account):
            return FakeKeyring.value

    monkeypatch.setattr("plaud_tools.session.importlib.import_module", lambda name: FakeKeyring)
    # dpapi_path MUST be pinned under tmp_path.  Leaving it unset on Windows
    # makes appdata.dpapi_shadow_path() resolve to the real
    # %LOCALAPPDATA%\PlaudTools\session.dat, so store.save() DPAPI-encrypts
    # the synthetic test session straight into the user's production shadow
    # — the v0.2.7 regression caught in v0.2.8.
    store = SessionStore(tmp_path / "session.json", dpapi_path=tmp_path / "session.dat")
    store.save(PlaudSession(access_token="jwt", region="eu", email="user@example.com"))
    session, source = store.load_with_source()
    assert calls == {"service": "plaud-tools", "account": "session"}
    assert source == "keyring"
    assert session.email == "user@example.com"
    assert not (tmp_path / "session.json").exists()


def test_session_store_falls_back_to_file_when_keyring_unavailable(tmp_path, monkeypatch):
    def raise_import_error(name):
        raise ImportError(name)

    monkeypatch.setattr("plaud_tools.session.importlib.import_module", raise_import_error)
    # Explicitly disable DPAPI for this test — we are pinning the plaintext
    # file-store fallback that fires when *every* OS-protected path is
    # unavailable.  The DPAPI path has its own dedicated tests.
    store = SessionStore(tmp_path / "session.json", dpapi_path=None)
    store.save(PlaudSession(access_token="jwt", region="eu", email="user@example.com"))
    session, source = store.load_with_source()
    assert source == "file"
    assert session.region == "eu"
    assert (tmp_path / "session.json").exists()


# ---------------------------------------------------------------------------
# Session cache tests (issue #43)
# ---------------------------------------------------------------------------


class CountingStore:
    """A SessionStoreProtocol stub that counts load() calls."""

    def __init__(self, session: PlaudSession) -> None:
        self._session = session
        self.load_count = 0
        self.save_count = 0

    def load(self) -> PlaudSession | None:
        self.load_count += 1
        return self._session

    def save(self, session: PlaudSession) -> None:
        self.save_count += 1
        self._session = session


def make_counting_manager(region="eu") -> tuple[SessionManager, CountingStore]:
    store = CountingStore(PlaudSession(access_token=make_jwt(), region=region, email="test@example.com"))
    return SessionManager(store), store


def test_session_cache_require_hits_store_only_once_for_repeated_calls(tmp_path):
    """Multiple require() calls within the same SessionManager should load from
    the store exactly once — subsequent calls use the in-memory cache."""
    manager, counting_store = make_counting_manager()
    manager.require()
    manager.require()
    manager.require()
    assert counting_store.load_count == 1


def test_session_cache_multiple_client_requests_hit_store_once(tmp_path):
    """A PlaudClient that issues multiple HTTP requests should cause store.load()
    to be called at most once per client lifetime (acceptance criterion)."""
    manager, counting_store = make_counting_manager()
    ok_response = HttpResponse(200, json.dumps({"status": 0, "data_file_list": []}).encode(), {})
    transport = StubTransport([ok_response, ok_response, ok_response])
    client = PlaudClient(manager, transport=transport)
    client.list_recordings()
    client.list_recordings()
    client.list_recordings()
    assert counting_store.load_count == 1


def test_session_cache_invalidate_causes_reload(tmp_path):
    """invalidate_cache() must discard the cached session so the next require()
    triggers a fresh store.load()."""
    manager, counting_store = make_counting_manager()
    manager.require()
    assert counting_store.load_count == 1
    manager.invalidate_cache()
    manager.require()
    assert counting_store.load_count == 2


def test_session_cache_update_region_updates_cache_and_store(tmp_path):
    """update_region() should update both the persisted store and the in-memory
    cache so the next require() returns the new region without a store read."""
    manager, counting_store = make_counting_manager(region="us")
    manager.require()
    initial_load_count = counting_store.load_count

    updated = manager.update_region("eu")
    assert updated.region == "eu"
    # Cache now holds the updated session; require() must NOT call store.load() again.
    session = manager.require()
    assert session.region == "eu"
    # store.load() should have been called at most once more during update_region
    # (to read current token), but NOT again after cache is repopulated.
    assert counting_store.load_count <= initial_load_count + 1


def test_session_cache_region_failover_loads_store_at_most_twice(tmp_path):
    """-302 region failover triggers update_region which reloads the store once,
    then the retry uses the cache.  Total store.load() calls: ≤ 2."""
    manager, counting_store = make_counting_manager(region="us")
    transport = StubTransport(
        [
            HttpResponse(
                200,
                json.dumps({"status": -302, "data": {"domains": {"api": "api-euc1.plaud.ai"}}}).encode(),
                {},
            ),
            HttpResponse(200, json.dumps({"status": 0, "data_file_list": []}).encode(), {}),
        ]
    )
    client = PlaudClient(manager, transport=transport)
    client.list_recordings()
    assert counting_store.load_count <= 2


# ---------------------------------------------------------------------------
# Region-redirect recursion-bound tests (Wave 0 / A2)
# ---------------------------------------------------------------------------


def test_region_redirect_loop_raises_after_two_requests(tmp_path):
    """A server that returns -302 on every call must not recurse unboundedly.
    Exactly 2 transport requests should be made (original + one retry), then
    PlaudApiError('region redirect loop') must be raised."""
    manager, _ = make_manager(tmp_path, region="us")
    redirect_response = HttpResponse(
        200,
        json.dumps({"status": -302, "data": {"domains": {"api": "api-euc1.plaud.ai"}}}).encode(),
        {},
    )
    # Provide more than 2 responses to prove we stop early, not because the
    # stub runs out.
    transport = StubTransport([redirect_response, redirect_response, redirect_response])
    client = PlaudClient(manager, transport=transport)

    with pytest.raises(PlaudApiError, match="region redirect loop"):
        client.list_recordings()

    # Exactly 2 requests: the original call and the single allowed retry.
    assert len(transport.calls) == 2


def test_region_redirect_once_then_success(tmp_path):
    """A single -302 followed by a normal status-0 payload must succeed and
    persist the new region — the existing happy-path contract must hold."""
    manager, store = make_manager(tmp_path, region="us")
    transport = StubTransport(
        [
            HttpResponse(
                200,
                json.dumps({"status": -302, "data": {"domains": {"api": "api-euc1.plaud.ai"}}}).encode(),
                {},
            ),
            HttpResponse(200, json.dumps({"status": 0, "data_file_list": []}).encode(), {}),
        ]
    )
    client = PlaudClient(manager, transport=transport)
    result = client.list_recordings()

    assert result == []
    # Region must have been persisted to "eu".
    assert store.load().region == "eu"
    # Second request must have gone to the EU base URL.
    assert transport.calls[1]["url"].startswith("https://api-euc1.plaud.ai/")
    assert len(transport.calls) == 2


def test_session_cache_expired_session_invalidates_cache(tmp_path):
    """When require() raises PlaudSessionExpiredError, the cache must be cleared
    so the caller could theoretically re-authenticate and retry."""
    import base64
    import json as _json

    # Build a JWT that is already past the buffer window (expired).
    expired_payload = {"exp": 1}  # epoch 1 second — definitely expired
    encoded = base64.urlsafe_b64encode(_json.dumps(expired_payload).encode()).decode().rstrip("=")
    expired_jwt = f"header.{encoded}.sig"

    counting_store = CountingStore(PlaudSession(access_token=expired_jwt, region="us", email="x@x.com"))
    manager = SessionManager(counting_store)

    with pytest.raises(PlaudSessionExpiredError):
        manager.require()

    # Cache must be None — a subsequent require() after re-auth would reload.
    assert manager._cache is None


# ---------------------------------------------------------------------------
# Wave 2 / C5 — 429 backoff & polite polling
# ---------------------------------------------------------------------------


def _make_http_error(status: int, body: bytes, headers: dict[str, str] | None = None) -> HTTPError:
    """Build a urllib.error.HTTPError with a readable body and optional headers."""
    from email.message import Message

    msg = Message()
    for k, v in (headers or {}).items():
        msg[k] = v
    fp = io.BytesIO(body)
    return HTTPError(url="https://example.com/api", code=status, msg="Error", hdrs=msg, fp=fp)


def _transient_error(status: int = 429, retry_after: float | None = None) -> PlaudApiError:
    """Build a PlaudApiError that classify() deems retryable."""
    headers: dict[str, str] = {}
    if retry_after is not None:
        headers["Retry-After"] = str(int(retry_after))
    exc = _make_http_error(status, b"rate limited", headers=headers)
    return PlaudApiError.from_http_error(exc)


class TestRetryBackoff:
    """_request_json must retry 429 / 5xx up to _MAX_ATTEMPTS total attempts."""

    def test_succeeds_after_one_transient_failure(self, tmp_path, monkeypatch):
        """Transport raises 429 once, then succeeds — caller gets the result."""
        manager, _ = make_manager(tmp_path)
        ok = HttpResponse(200, json.dumps({"status": 0, "data_file_list": []}).encode(), {})
        transport = StubTransport([_transient_error(429), ok])

        sleep_calls: list[float] = []
        monkeypatch.setattr(client_mod, "_sleep", lambda s: sleep_calls.append(s))
        monkeypatch.setattr(client_mod, "_jitter", lambda lo, hi: lo)  # deterministic

        client = PlaudClient(manager, transport=transport)
        result = client.list_recordings()

        assert result == []
        # 1 original attempt + 1 retry = 2 transport calls total.
        assert len(transport.calls) == 2
        # One sleep call for the retry.
        assert len(sleep_calls) == 1

    def test_succeeds_after_two_transient_failures(self, tmp_path, monkeypatch):
        """Transport raises 503 twice, then succeeds — caller gets the result."""
        manager, _ = make_manager(tmp_path)
        ok = HttpResponse(200, json.dumps({"status": 0, "data_file_list": []}).encode(), {})
        transport = StubTransport([_transient_error(503), _transient_error(503), ok])

        sleep_calls: list[float] = []
        monkeypatch.setattr(client_mod, "_sleep", lambda s: sleep_calls.append(s))
        monkeypatch.setattr(client_mod, "_jitter", lambda lo, hi: lo)

        client = PlaudClient(manager, transport=transport)
        result = client.list_recordings()

        assert result == []
        # 1 original + 2 retries = 3 transport calls total.
        assert len(transport.calls) == 3
        assert len(sleep_calls) == 2

    def test_raises_after_exhausting_all_attempts(self, tmp_path, monkeypatch):
        """Transport raises 429 every time — raises after _MAX_ATTEMPTS calls."""
        manager, _ = make_manager(tmp_path)
        # Provide more responses than _MAX_ATTEMPTS to prove we stop early.
        transport = StubTransport(
            [_transient_error(429), _transient_error(429), _transient_error(429), _transient_error(429)]
        )

        monkeypatch.setattr(client_mod, "_sleep", lambda s: None)
        monkeypatch.setattr(client_mod, "_jitter", lambda lo, hi: lo)

        client = PlaudClient(manager, transport=transport)
        with pytest.raises(PlaudApiError):
            client.list_recordings()

        # Must stop at exactly _MAX_ATTEMPTS calls.
        assert len(transport.calls) == client_mod._MAX_ATTEMPTS

    def test_non_retryable_error_not_retried(self, tmp_path, monkeypatch):
        """A 404 must not be retried — exactly 1 transport call."""
        manager, _ = make_manager(tmp_path)
        err_404 = PlaudApiError.from_http_error(_make_http_error(404, b"not found"))
        transport = StubTransport([err_404])

        sleep_calls: list[float] = []
        monkeypatch.setattr(client_mod, "_sleep", lambda s: sleep_calls.append(s))

        client = PlaudClient(manager, transport=transport)
        with pytest.raises(PlaudApiError):
            client.list_recordings()

        assert len(transport.calls) == 1
        assert sleep_calls == []


class TestMutationRetryPolicy:
    """#147: non-idempotent methods must not be auto-retried on 429/5xx.

    Design decision (issue #147, needs-triage): retry the idempotent GET path
    on transient errors as before, but give POST/PATCH/DELETE exactly one
    attempt regardless of ``classify()``'s retryable flag. Blindly retrying a
    transient failure on a mutation risks duplicating a side effect that may
    have already landed server-side before the "failure" was observed (a
    second /file/combine merge, a folder create that raises a false
    "already exists" after the first attempt actually succeeded). GET has no
    such risk, so it keeps the full retry budget.
    """

    def test_post_with_transient_error_is_not_retried(self, tmp_path, monkeypatch):
        """A 429 on a POST (e.g. folder create) must raise after exactly one
        transport call — not silently retried into a duplicate mutation."""
        manager, _ = make_manager(tmp_path)
        transport = StubTransport([_transient_error(429)])

        sleep_calls: list[float] = []
        monkeypatch.setattr(client_mod, "_sleep", lambda s: sleep_calls.append(s))

        client = PlaudClient(manager, transport=transport)
        with pytest.raises(PlaudApiError):
            client.create_folder("Clients")

        assert len(transport.calls) == 1
        assert sleep_calls == []

    def test_post_with_5xx_is_not_retried(self, tmp_path, monkeypatch):
        """A 503 on a POST must also not be retried."""
        manager, _ = make_manager(tmp_path)
        transport = StubTransport([_transient_error(503)])
        monkeypatch.setattr(client_mod, "_sleep", lambda s: None)

        client = PlaudClient(manager, transport=transport)
        with pytest.raises(PlaudApiError):
            client.create_folder("Clients")

        assert len(transport.calls) == 1

    def test_get_with_transient_error_still_retries(self, tmp_path, monkeypatch):
        """Control: GET keeps the existing retry-on-transient behaviour."""
        manager, _ = make_manager(tmp_path)
        ok = HttpResponse(200, json.dumps({"status": 0, "data_file_list": []}).encode(), {})
        transport = StubTransport([_transient_error(429), ok])
        monkeypatch.setattr(client_mod, "_sleep", lambda s: None)
        monkeypatch.setattr(client_mod, "_jitter", lambda lo, hi: lo)

        client = PlaudClient(manager, transport=transport)
        result = client.list_recordings()

        assert result == []
        assert len(transport.calls) == 2


class TestRetryAfterHeader:
    """Retry-After header value must be honoured: sleep >= Retry-After."""

    def test_retry_after_larger_than_backoff_is_used(self, tmp_path, monkeypatch):
        """When Retry-After > computed backoff, we sleep Retry-After."""
        manager, _ = make_manager(tmp_path)
        ok = HttpResponse(200, json.dumps({"status": 0, "data_file_list": []}).encode(), {})
        # Retry-After = 30s, which is much larger than the ~1 s back-off.
        transport = StubTransport([_transient_error(429, retry_after=30.0), ok])

        sleep_calls: list[float] = []
        monkeypatch.setattr(client_mod, "_sleep", lambda s: sleep_calls.append(s))
        # Force jitter to minimum so computed delay is clearly < 30.
        monkeypatch.setattr(client_mod, "_jitter", lambda lo, hi: lo)

        client = PlaudClient(manager, transport=transport)
        client.list_recordings()

        assert len(sleep_calls) == 1
        # The sleep value must be at least the Retry-After value.
        assert sleep_calls[0] >= 30.0

    def test_retry_after_parsed_from_header(self, tmp_path, monkeypatch):
        """from_http_error must populate retry_after from the Retry-After header."""
        exc = _make_http_error(429, b"rate limited", headers={"Retry-After": "45"})
        err = PlaudApiError.from_http_error(exc)
        assert err.retry_after == 45.0

    def test_retry_after_absent_gives_none(self, tmp_path, monkeypatch):
        """Missing Retry-After header → retry_after is None."""
        exc = _make_http_error(429, b"rate limited")
        err = PlaudApiError.from_http_error(exc)
        assert err.retry_after is None

    def test_retry_after_http_date_ignored(self, tmp_path, monkeypatch):
        """HTTP-date form of Retry-After must be ignored (set to None), not crash."""
        exc = _make_http_error(429, b"rate limited", headers={"Retry-After": "Wed, 21 Oct 2015 07:28:00 GMT"})
        err = PlaudApiError.from_http_error(exc)
        assert err.retry_after is None

    def test_backoff_larger_than_retry_after_is_used(self, tmp_path, monkeypatch):
        """When computed backoff > Retry-After, we sleep the backoff (never under-sleep)."""
        manager, _ = make_manager(tmp_path)
        ok = HttpResponse(200, json.dumps({"status": 0, "data_file_list": []}).encode(), {})
        # Retry-After = 0 s — server says "retry now", but we still back off.
        transport = StubTransport([_transient_error(429, retry_after=0.0), ok])

        sleep_calls: list[float] = []
        monkeypatch.setattr(client_mod, "_sleep", lambda s: sleep_calls.append(s))
        # Force jitter to upper bound so computed delay > 0.
        monkeypatch.setattr(client_mod, "_jitter", lambda lo, hi: hi)

        client = PlaudClient(manager, transport=transport)
        client.list_recordings()

        assert len(sleep_calls) == 1
        # Sleep must be at least the computed backoff, not the zero Retry-After.
        assert sleep_calls[0] > 0.0


class TestNonJsonBodyOnSuccess:
    """#146: a 2xx response with a non-JSON body must surface as a
    PlaudApiError (server/upstream error shape), not an unguarded
    json.JSONDecodeError — the latter is a ValueError subclass that the MCP
    facade's `except ValueError` branch reports as error_code="validation",
    misleadingly blaming the caller for what is actually a server outage.
    """

    def test_main_api_non_json_2xx_body_raises_plaud_api_error(self, tmp_path):
        manager, _ = make_manager(tmp_path)
        # A 200 whose body is not JSON at all (e.g. an HTML error page served
        # through a CDN/proxy in front of the real API).
        transport = StubTransport([HttpResponse(200, b"<html>upstream hiccup</html>", {})])

        client = PlaudClient(manager, transport=transport)
        with pytest.raises(PlaudApiError) as exc_info:
            client.list_recordings()

        # Must NOT be a bare json.JSONDecodeError (a ValueError subclass) —
        # it must be wrapped as a PlaudApiError carrying the HTTP status.
        assert not isinstance(exc_info.value, json.JSONDecodeError)
        assert exc_info.value.http_status == 200

    def test_transcript_data_link_non_json_2xx_body_raises_plaud_api_error(self, tmp_path):
        """Same guarantee for the transcript S3 data_link fetch path
        (client.py _fetch_transcript_segments), a separate code path from the
        main /file/... API request loop."""
        manager, _ = make_manager(tmp_path)
        detail_response = HttpResponse(
            200,
            json.dumps(
                {
                    "status": 0,
                    "data": {
                        "file_id": "rec1",
                        "content_list": [
                            {
                                "data_type": "transaction",
                                "task_status": 1,
                                "data_link": "https://s3.fake/transcript.json",
                            }
                        ],
                    },
                }
            ).encode(),
            {},
        )
        transport = StubTransport([detail_response, HttpResponse(200, b"not json at all", {})])

        client = PlaudClient(manager, transport=transport)
        with pytest.raises(PlaudApiError) as exc_info:
            client.get_recording("rec1", include_transcript=True)

        assert not isinstance(exc_info.value, json.JSONDecodeError)
        assert exc_info.value.http_status == 200


class TestPollLoopSurvival:
    """Poll loops must survive transient errors and abort on non-transient ones."""

    def _recording_response(self, is_trans: bool = True, is_summary: bool = True) -> HttpResponse:
        return HttpResponse(
            200,
            json.dumps(
                {
                    "status": 0,
                    "data": {
                        "file_id": "rec1",
                        "file_name": "Test",
                        "content_list": [
                            {"data_type": "transaction", "task_status": 1 if is_trans else 0},
                            {"data_type": "auto_sum_note", "task_status": 1 if is_summary else 0},
                        ],
                    },
                }
            ).encode(),
            {},
        )

    def test_wait_for_transcription_survives_one_transient_error(self, tmp_path, monkeypatch):
        """A single 429 during the poll must be skipped, not abort the wait."""
        manager, _ = make_manager(tmp_path)
        # First poll: 429 (transient). Second poll: recording ready.
        transport = StubTransport([_transient_error(429), self._recording_response(is_trans=True)])

        monkeypatch.setattr(client_mod, "_sleep", lambda s: None)

        client = PlaudClient(manager, transport=transport)
        # Must complete without raising.
        client.wait_for_transcription("rec1", timeout_s=60.0, poll_interval_s=0.0)
        # Transport was called twice: once for the transient, once for success.
        assert len(transport.calls) == 2

    def test_wait_for_transcription_survives_one_network_blip(self, tmp_path, monkeypatch):
        """#143: a raw transport-level failure (e.g. a socket timeout, no HTTP
        status at all) during the poll must also be skipped, not abort a
        multi-minute transcription wait that is still succeeding server-side.
        Before the fix, network_error-flagged errors classified as
        ("api_error", False) and aborted the wait on the very first blip."""
        manager, _ = make_manager(tmp_path)
        network_blip = PlaudApiError("Plaud API request timed out after 30.0s", network_error=True)
        transport = StubTransport([network_blip, self._recording_response(is_trans=True)])

        monkeypatch.setattr(client_mod, "_sleep", lambda s: None)

        client = PlaudClient(manager, transport=transport)
        client.wait_for_transcription("rec1", timeout_s=60.0, poll_interval_s=0.0)
        assert len(transport.calls) == 2

    def test_wait_for_transcription_propagates_non_transient_error(self, tmp_path, monkeypatch):
        """A non-transient error (e.g. 404) during the poll must propagate."""
        manager, _ = make_manager(tmp_path)
        err_404 = PlaudApiError.from_http_error(_make_http_error(404, b"not found"))
        transport = StubTransport([err_404])

        monkeypatch.setattr(client_mod, "_sleep", lambda s: None)

        client = PlaudClient(manager, transport=transport)
        with pytest.raises(PlaudApiError):
            client.wait_for_transcription("rec1", timeout_s=60.0, poll_interval_s=0.0)

        assert len(transport.calls) == 1

    def test_wait_for_summary_survives_one_transient_error(self, tmp_path, monkeypatch):
        """A single 503 during the summary poll must be skipped."""
        manager, _ = make_manager(tmp_path)
        transport = StubTransport([_transient_error(503), self._recording_response(is_summary=True)])

        monkeypatch.setattr(client_mod, "_sleep", lambda s: None)

        client = PlaudClient(manager, transport=transport)
        client.wait_for_summary("rec1", timeout_s=60.0, poll_interval_s=0.0)
        assert len(transport.calls) == 2

    def test_wait_for_summary_propagates_non_transient_error(self, tmp_path, monkeypatch):
        """A non-transient error (e.g. 401) during the summary poll must propagate."""
        manager, _ = make_manager(tmp_path)
        err_401 = PlaudApiError.from_http_error(_make_http_error(401, b"unauthorized"))
        transport = StubTransport([err_401])

        monkeypatch.setattr(client_mod, "_sleep", lambda s: None)

        client = PlaudClient(manager, transport=transport)
        with pytest.raises(PlaudApiError):
            client.wait_for_summary("rec1", timeout_s=60.0, poll_interval_s=0.0)

        assert len(transport.calls) == 1

    def test_merge_poll_survives_one_transient_error(self, tmp_path, monkeypatch):
        """A transient error during a merge poll must be skipped, not abort."""
        manager, _ = make_manager(tmp_path)

        combine_response = HttpResponse(
            200,
            json.dumps({"status": 0, "task_id": "task123"}).encode(),
            {},
        )
        poll_transient = _transient_error(429)
        poll_success = HttpResponse(
            200,
            json.dumps(
                {
                    "status": 0,
                    "data": {
                        "status": "success",
                        "file": {"file_id": "merged1", "file_name": "merged", "start_time": 0},
                    },
                }
            ).encode(),
            {},
        )

        transport = StubTransport([combine_response, poll_transient, poll_success])
        monkeypatch.setattr(client_mod, "_sleep", lambda s: None)

        client = PlaudClient(manager, transport=transport)
        result = client.merge_recordings(["id1", "id2"], "merged")
        # Should return the merged recording detail without aborting on the 429.
        assert result.id == "merged1"
        assert len(transport.calls) == 3

    def test_merge_poll_propagates_non_transient_error(self, tmp_path, monkeypatch):
        """A non-transient error during a merge poll must propagate immediately."""
        manager, _ = make_manager(tmp_path)

        combine_response = HttpResponse(
            200,
            json.dumps({"status": 0, "task_id": "task123"}).encode(),
            {},
        )
        err_404 = PlaudApiError.from_http_error(_make_http_error(404, b"task not found"))

        transport = StubTransport([combine_response, err_404])
        monkeypatch.setattr(client_mod, "_sleep", lambda s: None)

        client = PlaudClient(manager, transport=transport)
        with pytest.raises(PlaudApiError):
            client.merge_recordings(["id1", "id2"], "merged")

        assert len(transport.calls) == 2


class TestRedirectAndRetryComposition:
    """Region redirect and retry/backoff must not interfere with each other."""

    def test_redirect_then_success_still_works_with_retry_present(self, tmp_path, monkeypatch):
        """The existing -302 redirect contract is preserved after adding retry logic."""
        manager, store = make_manager(tmp_path, region="us")
        redirect_response = HttpResponse(
            200,
            json.dumps({"status": -302, "data": {"domains": {"api": "api-euc1.plaud.ai"}}}).encode(),
            {},
        )
        ok_response = HttpResponse(200, json.dumps({"status": 0, "data_file_list": []}).encode(), {})
        transport = StubTransport([redirect_response, ok_response])

        monkeypatch.setattr(client_mod, "_sleep", lambda s: None)

        client = PlaudClient(manager, transport=transport)
        result = client.list_recordings()

        assert result == []
        assert store.load().region == "eu"
        assert len(transport.calls) == 2
        assert transport.calls[1]["url"].startswith("https://api-euc1.plaud.ai/")

    def test_redirect_loop_still_raises_with_retry_present(self, tmp_path, monkeypatch):
        """-302 on every call must still raise 'region redirect loop', not retry infinitely."""
        manager, _ = make_manager(tmp_path, region="us")
        redirect_response = HttpResponse(
            200,
            json.dumps({"status": -302, "data": {"domains": {"api": "api-euc1.plaud.ai"}}}).encode(),
            {},
        )
        transport = StubTransport(
            [redirect_response, redirect_response, redirect_response, redirect_response]
        )
        monkeypatch.setattr(client_mod, "_sleep", lambda s: None)

        client = PlaudClient(manager, transport=transport)
        with pytest.raises(PlaudApiError, match="region redirect loop"):
            client.list_recordings()

        # Still exactly 2 requests (original + 1 redirect retry).
        assert len(transport.calls) == 2
