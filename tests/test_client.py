from __future__ import annotations

import base64
import gzip
import json
import tempfile
from pathlib import Path

import pytest

from plaud_tools.client import PlaudClient, PlaudRecordingQuery
from plaud_tools.errors import PlaudApiError, PlaudSessionExpiredError
from plaud_tools.session import FileSessionStore, PlaudSession, SessionManager, SessionStore
from plaud_tools.transport import HttpResponse


class StubTransport:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def request(self, method, url, headers, body=None):
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
        [HttpResponse(200, json.dumps({"status": 0, "data": {"file_id": "rec1", "file_name": "Meeting"}}).encode(), {})]
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
                json.dumps({"status": 0, "data_file_list": [{"id": "rec1", "filename": "Old", "is_trash": True}]}).encode(),
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
        {"start_time": 0, "end_time": 5000, "content": "hello", "speaker": "Alex", "original_speaker": "Speaker 1"},
        {"start_time": 5000, "end_time": 10000, "content": "world", "speaker": "Bee", "original_speaker": "Speaker 2"},
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
                        {"start_time": 0, "end_time": 5000, "content": "one", "speaker": "Speaker 1", "original_speaker": "Speaker 1"},
                        {"start_time": 5000, "end_time": 10000, "content": "two", "speaker": "Speaker 2", "original_speaker": "Speaker 2"},
                        {"start_time": 10000, "end_time": 15000, "content": "three", "speaker": "Speaker 1", "original_speaker": "Speaker 1"},
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
        [HttpResponse(200, json.dumps({"status": 0, "data": {"file_id": "rec1", "file_name": "Meeting"}}).encode(), {})]
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
                        {"start_time": 0, "end_time": 5000, "content": "one", "speaker": "Speaker 2", "original_speaker": "Speaker 2"},
                    ]
                ).encode(),
                {},
            ),
        ]
    )
    client = PlaudClient(manager, transport=transport)
    with pytest.raises(ValueError, match='no segments found with original_speaker "Speaker 1"'):
        client.rename_speaker("rec1", "Speaker 1", "Alex")


def test_rename_speaker_rejects_blank_labels(tmp_path):
    manager, _ = make_manager(tmp_path)
    client = PlaudClient(manager, transport=StubTransport([]))
    with pytest.raises(ValueError, match="original_label cannot be empty"):
        client.rename_speaker("rec1", "", "Alex")
    with pytest.raises(ValueError, match="new_name cannot be empty"):
        client.rename_speaker("rec1", "Speaker 1", "   ")


def test_transcribe_and_summarize_uses_expected_payload(tmp_path):
    manager, _ = make_manager(tmp_path)
    transport = StubTransport([HttpResponse(200, json.dumps({"status": 0, "msg": "task processing"}).encode(), {})])
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
    client.transcribe_and_summarize("rec1", template_type="MEETING-CONSULT", language="en", diarization=False, llm="gpt-5")
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
    from plaud_tools.session import TOKEN_REFRESH_BUFFER_SECONDS
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
    assert manager._cached_session is None
