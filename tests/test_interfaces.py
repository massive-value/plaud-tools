from __future__ import annotations

import json
from pathlib import Path

from plaud_tools.cli import main, run_cli
from plaud_tools.mcp import build_handlers
from plaud_tools.models import FileTag, Recording, RecordingDetail
from plaud_tools.session import SessionStore


class StubClient:
    def list_recordings(self, query=None):
        if query is not None:
            assert query.limit == 2
        return [
            Recording(
                id="r1",
                filename="meeting",
                start_time=1_746_000_000_000,
                duration=600_000,
                is_trans=True,
                filetag_id_list=["tag1"],
            )
        ] if query is not None else [
            Recording(
                id="r1",
                filename="Q4 Review",
                start_time=1_746_000_000_000,
                duration=600_000,
                is_trans=True,
                filetag_id_list=["tag1"],
            ),
            Recording(
                id="r2",
                filename="Lunch Chat",
                start_time=1_745_000_000_000,
                duration=300_000,
                is_trans=False,
                filetag_id_list=[],
            ),
        ]

    def merge_recordings(self, ids: list[str], filename: str):
        from plaud_tools.models import RecordingDetail
        return RecordingDetail(
            id="merged1",
            filename=filename,
            start_time=1_745_000_000_000,
            duration=900_000,
            folder_id=None,
            is_trash=False,
            is_trans=False,
            is_summary=False,
            scene=None,
            transcript="",
            ai_content=None,
            extra_data={},
            raw={},
        )

    def upload_recording(self, data, filename, file_type, *, start_time=None, timezone_offset=None):
        from plaud_tools.models import Recording
        return Recording(id="uploaded1", filename=filename, start_time=start_time or 0, duration=0, is_trash=False, is_trans=False, is_summary=False, filetag_id_list=[], raw={})

    def get_recording(self, recording_id, include_transcript=False, include_summary=False):
        return RecordingDetail(
            id=recording_id,
            filename="meeting",
            is_trans=True,
            is_summary=True,
            transcript="hello world" if include_transcript else "",
            speakers=["Speaker 1", "Alex"] if include_transcript else [],
            ai_content="# Summary",
            extra_data={
                "aiContentHeader": {"headline": "Q4 review"},
                "tranConfig": {"language": "en"},
            },
        )

    def fetch_transcript(self, recording_id):
        return "full transcript text"

    def rename_recording(self, recording_id, new_name):
        self.rename_call = (recording_id, new_name)

    def list_file_tags(self):
        return [FileTag(id="tag1", name="Work", color="#191919", icon="e627")]

    def set_recording_folder(self, recording_id, folder_id):
        self.move_call = (recording_id, folder_id)

    def list_trash(self):
        return [
            Recording(
                id="t1",
                filename="old meeting",
                start_time=1_746_000_000_000,
                duration=600_000,
                is_trans=False,
            )
        ]

    def move_to_trash(self, recording_ids):
        self.trash_move_call = list(recording_ids)

    def restore_from_trash(self, recording_ids):
        self.trash_restore_call = list(recording_ids)

    def delete_recordings(self, recording_ids):
        self.delete_call = list(recording_ids)

    def rename_speaker(self, recording_id, original_label, new_name):
        self.rename_speaker_call = (recording_id, original_label, new_name)
        return {"segments_updated": 7}

    def wait_for_transcription(self, recording_id, **kwargs):
        pass

    def wait_for_summary(self, recording_id, **kwargs):
        pass

    def dump_raw_detail(self, recording_id):
        return {"file_id": recording_id, "file_name": "meeting", "content_list": [], "extra_data": {}}

    def transcribe_and_summarize(self, recording_id, template_type=None, language=None, diarization=None, llm=None):
        self.transcribe_call = (recording_id, template_type, language, diarization, llm)

    def get_task_status(self, recording_id=None):
        self.status_call = recording_id
        return [
            type(
                "Task",
                (),
                {
                    "file_id": "rec1",
                    "task_id": "t1",
                    "task_type": "transcript",
                    "task_status": 1,
                    "is_complete": True,
                    "sum_type": "",
                    "sum_type_type": "",
                },
            )()
        ]


# --- CLI tests ---

def test_cli_list_shapes_output():
    from datetime import datetime
    expected_date = datetime.fromtimestamp(1_746_000_000_000 / 1000).isoformat()[:16]
    output = run_cli(["list", "--limit", "2"], StubClient())
    payload = json.loads(output)
    assert payload == [
        {
            "id": "r1",
            "title": "meeting",
            "date": expected_date,
            "duration_minutes": 10,
            "has_transcript": True,
            "folder_id": "tag1",
        }
    ]


def test_cli_search_filters_by_query():
    output = run_cli(["search", "lunch"], StubClient())
    payload = json.loads(output)
    assert [item["id"] for item in payload] == ["r2"]


def test_cli_search_returns_all_when_no_match():
    output = run_cli(["search", "zzznomatch"], StubClient())
    payload = json.loads(output)
    assert payload == []


def test_cli_show_returns_metadata_speakers_headline():
    output = run_cli(["show", "rec1"], StubClient())
    payload = json.loads(output)
    assert payload["id"] == "rec1"
    assert payload["title"] == "meeting"
    assert payload["speakers"] == ["Speaker 1", "Alex"]
    assert payload["headline"] == "Q4 review"
    assert "transcript" not in payload


def test_cli_summary_returns_ai_content():
    output = run_cli(["summary", "rec1"], StubClient())
    payload = json.loads(output)
    assert payload["recording_id"] == "rec1"
    assert payload["summary"] == "# Summary"


def test_cli_list_filters_query_and_unfiled():
    output = run_cli(["list", "--limit", "5", "--query", "lunch", "--unfiled"], StubClient())
    payload = json.loads(output)
    assert [item["id"] for item in payload] == ["r2"]


def test_cli_trash_no_arg_lists_trash():
    from datetime import datetime
    expected_date = datetime.fromtimestamp(1_746_000_000_000 / 1000).isoformat()[:16]
    output = run_cli(["trash"], StubClient())
    payload = json.loads(output)
    assert payload == [
        {
            "id": "t1",
            "title": "old meeting",
            "date": expected_date,
            "duration_minutes": 10,
            "has_transcript": False,
            "folder_id": None,
        }
    ]


def test_cli_trash_with_id_moves_to_trash():
    client = StubClient()
    output = run_cli(["trash", "rec1"], client)
    payload = json.loads(output)
    assert client.trash_move_call == ["rec1"]
    assert payload == {"ok": True, "recording_id": "rec1", "mutation": "trash"}


def test_cli_restore_single_recording():
    client = StubClient()
    output = run_cli(["restore", "rec1"], client)
    payload = json.loads(output)
    assert client.trash_restore_call == ["rec1"]
    assert payload == {"ok": True, "recording_id": "rec1", "mutation": "restore"}


def test_cli_delete_requires_yes_flag():
    import pytest
    with pytest.raises(ValueError, match="--yes"):
        run_cli(["delete", "rec1"], StubClient())


def test_cli_delete_with_yes_deletes():
    client = StubClient()
    output = run_cli(["delete", "rec1", "--yes"], client)
    payload = json.loads(output)
    assert client.delete_call == ["rec1"]
    assert payload == {"ok": True, "recording_id": "rec1", "mutation": "delete"}


def test_cli_move_assigns_folder():
    client = StubClient()
    output = run_cli(["move", "rec1", "tag1"], client)
    payload = json.loads(output)
    assert client.move_call == ("rec1", "tag1")
    assert payload == {"ok": True, "recording_id": "rec1", "folder_id": "tag1"}


def test_cli_move_clears_folder_with_dash():
    client = StubClient()
    output = run_cli(["move", "rec1", "-"], client)
    payload = json.loads(output)
    assert client.move_call == ("rec1", None)
    assert payload == {"ok": True, "recording_id": "rec1", "folder_id": None}


def test_cli_rename_shapes_success_response():
    client = StubClient()
    output = run_cli(["rename", "rec1", "New Name"], client)
    payload = json.loads(output)
    assert client.rename_call == ("rec1", "New Name")
    assert payload == {"ok": True, "recording_id": "rec1", "new_name": "New Name"}


def test_cli_folders_returns_curated_list():
    output = run_cli(["folders"], StubClient())
    payload = json.loads(output)
    assert payload == [{"id": "tag1", "name": "Work", "color": "#191919", "icon": "e627"}]


def test_cli_move_to_folder_supports_clear():
    client = StubClient()
    output = run_cli(["move-to-folder", "rec1", "-"], client)
    payload = json.loads(output)
    assert client.move_call == ("rec1", None)
    assert payload == {"ok": True, "recording_id": "rec1", "folder_id": None}


def test_cli_trash_list_returns_curated_items():
    from datetime import datetime
    expected_date = datetime.fromtimestamp(1_746_000_000_000 / 1000).isoformat()[:16]
    output = run_cli(["trash"], StubClient())
    payload = json.loads(output)
    assert payload == [
        {
            "id": "t1",
            "title": "old meeting",
            "date": expected_date,
            "duration_minutes": 10,
            "has_transcript": False,
            "folder_id": None,
        }
    ]


def test_cli_trash_move_shapes_success_response():
    client = StubClient()
    output = run_cli(["trash-move", "rec1", "rec2"], client)
    payload = json.loads(output)
    assert client.trash_move_call == ["rec1", "rec2"]
    assert payload == {"ok": True, "count": 2, "recording_ids": ["rec1", "rec2"]}


def test_cli_trash_restore_shapes_success_response():
    client = StubClient()
    output = run_cli(["trash-restore", "rec1"], client)
    payload = json.loads(output)
    assert client.trash_restore_call == ["rec1"]
    assert payload == {"ok": True, "count": 1, "recording_ids": ["rec1"]}


def test_cli_rename_speaker_shapes_success_response():
    client = StubClient()
    output = run_cli(["rename-speaker", "rec1", "Speaker 1", "Alex Riley"], client)
    payload = json.loads(output)
    assert client.rename_speaker_call == ("rec1", "Speaker 1", "Alex Riley")
    assert payload == {
        "ok": True,
        "recording_id": "rec1",
        "original_label": "Speaker 1",
        "new_name": "Alex Riley",
        "segments_updated": 7,
    }


def test_cli_transcribe_shapes_accept_response():
    client = StubClient()
    output = run_cli(["transcribe", "rec1", "--template", "MEETING-CONSULT"], client)
    payload = json.loads(output)
    assert client.transcribe_call == ("rec1", "MEETING-CONSULT", None, None, None)
    assert payload == {
        "accepted": True,
        "recording_id": "rec1",
        "template_type": "MEETING-CONSULT",
    }


def test_cli_status_returns_task_list():
    client = StubClient()
    output = run_cli(["status", "rec1"], client)
    payload = json.loads(output)
    assert client.status_call == "rec1"
    assert payload == [
        {
            "file_id": "rec1",
            "task_id": "t1",
            "task_type": "transcript",
            "task_status": 1,
            "is_complete": True,
            "sum_type": "",
            "sum_type_type": "",
        }
    ]


def test_cli_session_set_and_show(tmp_path: Path):
    store = SessionStore(tmp_path / "session.json", service_name="plaud-tools-test-cli", account_name="session")
    set_output = run_cli(
        ["session", "set", "--token", "header.payload.signature", "--region", "eu", "--email", "test@example.com"],
        session_store=store,
    )
    set_payload = json.loads(set_output)
    assert set_payload["ok"] is True
    assert store.load().region == "eu"

    show_output = run_cli(["session", "show"], session_store=store)
    show_payload = json.loads(show_output)
    assert show_payload["region"] == "eu"
    assert show_payload["email"] == "test@example.com"
    assert show_payload["token"] == "header...nature"


def test_cli_session_show_returns_none_when_missing(tmp_path: Path):
    store = SessionStore(tmp_path / "missing.json", service_name="plaud-tools-test-cli-missing", account_name="session")
    output = run_cli(["session", "show"], session_store=store)
    payload = json.loads(output)
    assert payload["session"] is None


class StubAuth:
    def __init__(self):
        self.calls = []

    def login(self, email, password, region):
        self.calls.append((email, password, region))
        return type("Session", (), {"email": email, "region": region})()


def test_cli_login_uses_auth_and_returns_stored_shape(tmp_path: Path):
    auth = StubAuth()
    store = SessionStore(tmp_path / "session.json", service_name="plaud-tools-test-login", account_name="session")
    output = run_cli(
        ["login", "--email", "user@example.com", "--password", "pw", "--region", "eu"],
        session_store=store,
        auth=auth,
    )
    payload = json.loads(output)
    assert auth.calls == [("user@example.com", "pw", "eu")]
    assert payload == {
        "ok": True,
        "email": "user@example.com",
        "region": "eu",
        "status": "stored",
    }


def test_cli_main_prints_output_for_session_command(tmp_path: Path, capsys):
    from plaud_tools import cli as cli_module

    original_store = cli_module.SessionStore
    cli_module.SessionStore = lambda: SessionStore(
        tmp_path / "session.json",
        service_name="plaud-tools-test-main-set",
        account_name="session",
    )
    try:
        code = main(
            ["session", "set", "--token", "header.payload.signature", "--region", "eu"],
        )
    finally:
        cli_module.SessionStore = original_store
    assert code == 0
    captured = capsys.readouterr()
    assert '"ok": true' in captured.out.lower()


def test_cli_main_returns_nonzero_on_missing_session(capsys, monkeypatch, tmp_path: Path):
    monkeypatch.delenv("PLAUD_ACCESS_TOKEN", raising=False)
    monkeypatch.delenv("PLAUD_REGION", raising=False)
    monkeypatch.delenv("PLAUD_EMAIL", raising=False)
    monkeypatch.setattr(
        "plaud_tools.cli.SessionStore",
        lambda: SessionStore(tmp_path / "missing.json", service_name="plaud-tools-test-main", account_name="session"),
    )
    code = main(["list"])
    assert code == 1
    captured = capsys.readouterr()
    assert "No Plaud session available." in captured.err


# --- MCP tests ---

def test_mcp_browse_recordings_returns_curated_list():
    handlers = build_handlers(lambda: StubClient())
    result = handlers["browse_recordings"](limit=2)
    payload = json.loads(result["content"][0]["text"])
    assert "items" in payload
    assert "next_after" in payload
    assert len(payload["items"]) == 1
    item = payload["items"][0]
    assert item["id"] == "r1"
    assert item["title"] == "meeting"
    assert item["duration_minutes"] == 10
    assert item["has_transcript"] is True
    assert item["folder_id"] == "tag1"
    # date is local time — just check shape, not exact value
    assert len(item["date"]) == 16
    assert item["date"][4] == "-" and item["date"][7] == "-" and item["date"][10] == "T"


def test_mcp_browse_recordings_filters_by_since_and_folder():
    handlers = build_handlers(lambda: StubClient())
    result = handlers["browse_recordings"](since="2025-04-01T00:00:00Z", folder="tag1")
    payload = json.loads(result["content"][0]["text"])
    assert [item["id"] for item in payload["items"]] == ["r1"]


def test_mcp_browse_recordings_date_only_until_includes_full_day():
    # until="2025-04-30" must include recordings that fall anywhere on that day,
    # not just at midnight (regression: was parsed as start-of-day, excluding all non-midnight times)
    handlers = build_handlers(lambda: StubClient())
    result = handlers["browse_recordings"](until="2025-04-30")
    payload = json.loads(result["content"][0]["text"])
    ids = [item["id"] for item in payload["items"]]
    assert "r1" in ids


def test_mcp_browse_recordings_reports_invalid_dates():
    handlers = build_handlers(lambda: StubClient())
    result = handlers["browse_recordings"](since="not-a-date")
    assert result["isError"] is True
    payload = json.loads(result["content"][0]["text"])
    assert "Invalid since value" in payload["error"]


def test_mcp_browse_recordings_returns_session_error_when_client_missing():
    handlers = build_handlers(lambda: None)
    result = handlers["browse_recordings"]()
    assert result["isError"] is True


def test_mcp_browse_recordings_next_after_null_when_short_page():
    """next_after is null when the page is shorter than limit (no more results)."""
    handlers = build_handlers(lambda: StubClient())
    # StubClient with filters returns 1 item; limit=10 means short page → next_after=null
    result = handlers["browse_recordings"](limit=10, folder="tag1")
    payload = json.loads(result["content"][0]["text"])
    assert payload["next_after"] is None


def test_mcp_browse_recordings_next_after_set_when_full_page():
    """next_after equals after + len(items) when a full page is returned."""

    class ManyRecordingsClient(StubClient):
        def list_recordings(self, query=None):
            # Return enough items to fill a limit=1 page when filtered by folder
            from plaud_tools.models import Recording
            return [
                Recording(
                    id=f"r{i}",
                    filename=f"rec {i}",
                    start_time=1_746_000_000_000 - i * 1000,
                    duration=600_000,
                    is_trans=True,
                    filetag_id_list=["tag1"],
                )
                for i in range(5)
            ]

    handlers = build_handlers(lambda: ManyRecordingsClient())
    result = handlers["browse_recordings"](limit=2, folder="tag1")
    payload = json.loads(result["content"][0]["text"])
    assert len(payload["items"]) == 2
    assert payload["next_after"] == 2  # after=0 + len=2


def test_mcp_browse_recordings_pagination_cursor_advances():
    """Passing next_after as after returns the next page."""

    class ManyRecordingsClient(StubClient):
        def list_recordings(self, query=None):
            from plaud_tools.models import Recording
            return [
                Recording(
                    id=f"r{i}",
                    filename=f"rec {i}",
                    start_time=1_746_000_000_000 - i * 1000,
                    duration=600_000,
                    is_trans=True,
                    filetag_id_list=["tag1"],
                )
                for i in range(5)
            ]

    handlers = build_handlers(lambda: ManyRecordingsClient())
    # Page 1
    result1 = handlers["browse_recordings"](limit=2, folder="tag1")
    page1 = json.loads(result1["content"][0]["text"])
    assert page1["next_after"] == 2

    # Page 2
    result2 = handlers["browse_recordings"](limit=2, folder="tag1", after=page1["next_after"])
    page2 = json.loads(result2["content"][0]["text"])
    assert len(page2["items"]) == 2
    assert page2["next_after"] == 4

    # Page 3 (last, short page)
    result3 = handlers["browse_recordings"](limit=2, folder="tag1", after=page2["next_after"])
    page3 = json.loads(result3["content"][0]["text"])
    assert len(page3["items"]) == 1
    assert page3["next_after"] is None


def test_mcp_get_recording_default_excludes_transcript_and_summary():
    handlers = build_handlers(lambda: StubClient())
    result = handlers["get_recording"]("rec1")
    payload = json.loads(result["content"][0]["text"])
    assert payload["id"] == "rec1"
    assert payload["title"] == "meeting"
    assert payload["headline"] == "Q4 review"
    assert payload["language"] == "en"
    assert "transcript" not in payload
    assert "summary" not in payload
    assert "speakers" not in payload


def test_mcp_get_recording_include_transcript():
    handlers = build_handlers(lambda: StubClient())
    result = handlers["get_recording"]("rec1", include=["transcript"])
    payload = json.loads(result["content"][0]["text"])
    assert payload["transcript"] == "hello world"
    assert "summary" not in payload


def test_mcp_get_recording_include_speakers():
    handlers = build_handlers(lambda: StubClient())
    result = handlers["get_recording"]("rec1", include=["speakers"])
    payload = json.loads(result["content"][0]["text"])
    assert payload["speakers"] == ["Speaker 1", "Alex"]
    assert "transcript" not in payload


def test_mcp_get_recording_include_summary():
    handlers = build_handlers(lambda: StubClient())
    result = handlers["get_recording"]("rec1", include=["summary"])
    payload = json.loads(result["content"][0]["text"])
    assert payload["summary"] == "# Summary"
    assert "transcript" not in payload


def test_mcp_list_folders_returns_tags():
    handlers = build_handlers(lambda: StubClient())
    result = handlers["list_folders"]()
    payload = json.loads(result["content"][0]["text"])
    assert payload == [{"id": "tag1", "name": "Work", "color": "#191919", "icon": "e627"}]


def test_mcp_list_folders_returns_empty_when_no_tags():
    class EmptyClient(StubClient):
        def list_file_tags(self):
            return []

    handlers = build_handlers(lambda: EmptyClient())
    result = handlers["list_folders"]()
    payload = json.loads(result["content"][0]["text"])
    assert payload == []


def test_mcp_list_folders_returns_session_error_when_client_missing():
    handlers = build_handlers(lambda: None)
    result = handlers["list_folders"]()
    assert result["isError"] is True


def test_mcp_mutate_recording_rename():
    client = StubClient()
    handlers = build_handlers(lambda: client)
    result = handlers["mutate_recording"]("rec1", "rename", new_name="New Title")
    payload = json.loads(result["content"][0]["text"])
    assert payload == {"ok": True, "recording_id": "rec1", "new_name": "New Title"}
    assert client.rename_call == ("rec1", "New Title")


def test_mcp_mutate_recording_trash():
    client = StubClient()
    handlers = build_handlers(lambda: client)
    result = handlers["mutate_recording"]("rec1", "trash")
    payload = json.loads(result["content"][0]["text"])
    assert payload == {"ok": True, "recording_id": "rec1", "mutation": "trash"}
    assert client.trash_move_call == ["rec1"]


def test_mcp_mutate_recording_restore():
    client = StubClient()
    handlers = build_handlers(lambda: client)
    result = handlers["mutate_recording"]("rec1", "restore")
    payload = json.loads(result["content"][0]["text"])
    assert payload == {"ok": True, "recording_id": "rec1", "mutation": "restore"}
    assert client.trash_restore_call == ["rec1"]


def test_mcp_mutate_recording_delete():
    client = StubClient()
    handlers = build_handlers(lambda: client)
    result = handlers["mutate_recording"]("rec1", "delete")
    payload = json.loads(result["content"][0]["text"])
    assert payload == {"ok": True, "recording_id": "rec1", "mutation": "delete"}
    assert client.delete_call == ["rec1"]


def test_mcp_mutate_recording_move():
    client = StubClient()
    handlers = build_handlers(lambda: client)
    result = handlers["mutate_recording"]("rec1", "move", folder_id="tag1")
    payload = json.loads(result["content"][0]["text"])
    assert payload == {"ok": True, "recording_id": "rec1", "folder_id": "tag1"}
    assert client.move_call == ("rec1", "tag1")


def test_mcp_mutate_recording_move_clears_folder():
    client = StubClient()
    handlers = build_handlers(lambda: client)
    result = handlers["mutate_recording"]("rec1", "move", folder_id="")
    payload = json.loads(result["content"][0]["text"])
    assert payload["folder_id"] is None
    assert client.move_call == ("rec1", None)


def test_mcp_mutate_recording_rename_speaker():
    client = StubClient()
    handlers = build_handlers(lambda: client)
    result = handlers["mutate_recording"]("rec1", "rename_speaker", original_label="Speaker 1", new_name="Alex")
    payload = json.loads(result["content"][0]["text"])
    assert payload["segments_updated"] == 7
    assert payload["original_label"] == "Speaker 1"
    assert client.rename_speaker_call == ("rec1", "Speaker 1", "Alex")


def test_mcp_mutate_recording_unknown_mutation():
    handlers = build_handlers(lambda: StubClient())
    result = handlers["mutate_recording"]("rec1", "fly_away")
    assert result["isError"] is True
    payload = json.loads(result["content"][0]["text"])
    assert "unknown mutation" in payload["error"]


def test_mcp_mutate_recording_rename_missing_new_name():
    handlers = build_handlers(lambda: StubClient())
    result = handlers["mutate_recording"]("rec1", "rename")
    assert result["isError"] is True


# --- CLI upload / merge tests ---

class UploadStubClient(StubClient):
    def __init__(self):
        self.upload_call = None
        self.transcribe_call = None
        self.wait_call = None
        self.summary_wait_call = None
        self.merge_call = None

    def upload_recording(self, data, filename, file_type, **kwargs):
        self.upload_call = (len(data), filename, file_type)
        return Recording(id="new-rec", filename=filename)

    def transcribe_and_summarize(self, recording_id, **kwargs):
        self.transcribe_call = recording_id

    def wait_for_transcription(self, recording_id, **kwargs):
        self.wait_call = recording_id

    def wait_for_summary(self, recording_id, **kwargs):
        self.summary_wait_call = recording_id

    def merge_recordings(self, ids, filename, **kwargs):
        self.merge_call = (ids, filename)
        return RecordingDetail(id="merged-rec", filename=filename)


def test_cli_upload_mp3_triggers_transcription(tmp_path):
    mp3_file = tmp_path / "test.mp3"
    mp3_file.write_bytes(b"fake mp3 data")
    client = UploadStubClient()
    output = run_cli(["upload", str(mp3_file)], client)
    payload = json.loads(output)
    assert payload["ok"] is True
    assert payload["recording_id"] == "new-rec"
    assert payload["transcoded"] is False
    assert payload["transcribed"] is True
    assert client.upload_call[1] == "test"  # filename stem as default title
    assert client.upload_call[2] == "MP3"
    assert client.transcribe_call == "new-rec"
    assert client.wait_call == "new-rec"
    assert client.summary_wait_call == "new-rec"


def test_cli_upload_with_title_and_detach(tmp_path):
    mp3_file = tmp_path / "audio.mp3"
    mp3_file.write_bytes(b"data")
    client = UploadStubClient()
    output = run_cli(["upload", str(mp3_file), "--title", "My Meeting", "--detach"], client)
    payload = json.loads(output)
    assert payload["ok"] is True
    assert payload["detached"] is True
    assert "transcribed" not in payload
    assert client.upload_call[1] == "My Meeting"
    assert client.transcribe_call is None
    assert client.wait_call is None


def test_cli_upload_missing_file_raises(tmp_path):
    import pytest
    client = UploadStubClient()
    with pytest.raises(ValueError, match="file not found"):
        run_cli(["upload", str(tmp_path / "missing.mp3")], client)


def test_cli_merge_calls_merge_and_returns_result(tmp_path):
    client = UploadStubClient()
    output = run_cli(["merge", "r1", "r2", "r3", "--title", "Combined"], client)
    payload = json.loads(output)
    assert payload["ok"] is True
    assert payload["recording_id"] == "merged-rec"
    assert payload["source_ids"] == ["r1", "r2", "r3"]
    assert client.merge_call == (["r1", "r2", "r3"], "Combined")


# --- MCP upload_recording / process_recording tests ---

class MutateStub(StubClient):
    def __init__(self):
        self.upload_call = None
        self.transcribe_call = None
        self.wait_call = None
        self.summary_wait_call = None

    def upload_recording(self, data, filename, file_type, **kwargs):
        self.upload_call = (filename, file_type)
        return Recording(id="mcp-rec", filename=filename)

    def transcribe_and_summarize(self, recording_id, **kwargs):
        self.transcribe_call = recording_id

    def wait_for_transcription(self, recording_id, **kwargs):
        self.wait_call = recording_id

    def wait_for_summary(self, recording_id, **kwargs):
        self.summary_wait_call = recording_id


def test_mcp_upload_recording_returns_ok(tmp_path):
    mp3_file = tmp_path / "audio.mp3"
    mp3_file.write_bytes(b"fake mp3")
    client = MutateStub()
    handlers = build_handlers(lambda: client)
    result = handlers["upload_recording"](str(mp3_file))
    payload = json.loads(result["content"][0]["text"])
    assert payload["ok"] is True
    assert payload["recording_id"] == "mcp-rec"
    assert payload["transcoded"] is False


def test_mcp_upload_recording_with_title(tmp_path):
    mp3_file = tmp_path / "audio.mp3"
    mp3_file.write_bytes(b"data")
    client = MutateStub()
    handlers = build_handlers(lambda: client)
    result = handlers["upload_recording"](str(mp3_file), title="Custom Title")
    payload = json.loads(result["content"][0]["text"])
    assert payload["ok"] is True
    assert client.upload_call[0] == "Custom Title"


def test_mcp_upload_recording_missing_file_returns_error(tmp_path):
    handlers = build_handlers(lambda: MutateStub())
    result = handlers["upload_recording"](str(tmp_path / "missing.mp3"))
    assert result["isError"] is True
    payload = json.loads(result["content"][0]["text"])
    assert "file not found" in payload["error"]


def test_mcp_process_recording_triggers_and_polls():
    client = MutateStub()
    handlers = build_handlers(lambda: client)
    result = handlers["process_recording"]("rec1")
    payload = json.loads(result["content"][0]["text"])
    assert payload["ok"] is True
    assert payload["recording_id"] == "rec1"
    assert client.transcribe_call == "rec1"
    assert client.wait_call == "rec1"
    assert client.summary_wait_call == "rec1"


def test_mcp_merge_recordings_returns_summary():
    handlers = build_handlers(lambda: StubClient())
    result = handlers["merge_recordings"](recording_ids=["r1", "r2"], title="Combined")
    payload = json.loads(result["content"][0]["text"])
    assert payload["id"] == "merged1"
    assert payload["title"] == "Combined"
    assert payload["is_trans"] is False


def test_mcp_upload_recording_passes_timestamp(tmp_path):
    captured = {}

    class TimestampCapturingClient(StubClient):
        def upload_recording(self, data, filename, file_type, *, start_time=None, timezone_offset=None):
            captured["start_time"] = start_time
            captured["timezone_offset"] = timezone_offset
            return super().upload_recording(data, filename, file_type)

    fake_mp3 = tmp_path / "test.mp3"
    fake_mp3.write_bytes(b"\xff\xfb" + b"\x00" * 100)
    handlers = build_handlers(lambda: TimestampCapturingClient())
    handlers["upload_recording"](file_path=str(fake_mp3), start_time=1735732800000, timezone_offset=-7.0)
    assert captured["start_time"] == 1735732800000
    assert captured["timezone_offset"] == -7.0


# --- new tests for items added this session ---

def test_cli_upload_skip_summary_does_not_call_wait_for_summary(tmp_path):
    mp3_file = tmp_path / "test.mp3"
    mp3_file.write_bytes(b"fake mp3 data")
    client = UploadStubClient()
    output = run_cli(["upload", str(mp3_file), "--skip-summary"], client)
    payload = json.loads(output)
    assert payload["transcribed"] is True
    assert client.wait_call == "new-rec"
    assert client.summary_wait_call is None


def test_cli_upload_start_time_as_iso_string(tmp_path):
    captured = {}

    class IsoCapturingClient(UploadStubClient):
        def upload_recording(self, data, filename, file_type, *, start_time=None, **kwargs):
            captured["start_time"] = start_time
            return super().upload_recording(data, filename, file_type)

    mp3_file = tmp_path / "test.mp3"
    mp3_file.write_bytes(b"fake mp3 data")
    run_cli(["upload", str(mp3_file), "--start-time", "2026-01-01T10:00:00"], IsoCapturingClient())
    assert isinstance(captured["start_time"], int)
    assert captured["start_time"] > 0


def test_cli_upload_start_time_as_epoch_int(tmp_path):
    captured = {}

    class EpochCapturingClient(UploadStubClient):
        def upload_recording(self, data, filename, file_type, *, start_time=None, **kwargs):
            captured["start_time"] = start_time
            return super().upload_recording(data, filename, file_type)

    mp3_file = tmp_path / "test.mp3"
    mp3_file.write_bytes(b"fake mp3 data")
    run_cli(["upload", str(mp3_file), "--start-time", "1735732800000"], EpochCapturingClient())
    assert captured["start_time"] == 1735732800000


def test_mcp_upload_recording_start_time_as_iso_string(tmp_path):
    captured = {}

    class IsoCapturingClient(StubClient):
        def upload_recording(self, data, filename, file_type, *, start_time=None, timezone_offset=None):
            captured["start_time"] = start_time
            return super().upload_recording(data, filename, file_type)

    fake_mp3 = tmp_path / "test.mp3"
    fake_mp3.write_bytes(b"\xff\xfb" + b"\x00" * 100)
    handlers = build_handlers(lambda: IsoCapturingClient())
    handlers["upload_recording"](file_path=str(fake_mp3), start_time="2026-01-01T10:00:00")
    assert isinstance(captured["start_time"], int)
    assert captured["start_time"] > 0


def test_cli_dump_returns_raw_json():
    client = StubClient()
    output = run_cli(["dump", "rec1"], client)
    payload = json.loads(output)
    assert payload["file_id"] == "rec1"


def test_extract_inline_summary_handles_dict_data_content():
    from plaud_tools.client import PlaudClient
    from plaud_tools.session import SessionStore, SessionManager

    client = PlaudClient(SessionManager(SessionStore()))
    raw = {
        "content_list": [{"data_type": "auto_sum_note", "task_status": 1, "data_id": "d1"}],
        "pre_download_content_list": [
            {"data_id": "d1", "data_type": "auto_sum_note", "data_content": {"ai_content": "summary text"}}
        ],
    }
    result = client._extract_inline_summary(raw, "d1")
    assert result == "summary text"


def test_extract_inline_summary_fallback_by_data_type():
    from plaud_tools.client import PlaudClient
    from plaud_tools.session import SessionStore, SessionManager

    client = PlaudClient(SessionManager(SessionStore()))
    raw = {
        "content_list": [{"data_type": "auto_sum_note", "task_status": 1, "data_id": "d1"}],
        "pre_download_content_list": [
            # data_id doesn't match "d1" — should fall back to data_type match
            {"data_id": "d99", "data_type": "auto_sum_note", "data_content": '{"ai_content": "fallback summary"}'}
        ],
    }
    result = client._extract_inline_summary(raw, "d1")
    assert result == "fallback summary"


def test_fetch_summary_from_data_link_handles_plain_text():
    from plaud_tools.client import PlaudClient
    from plaud_tools.session import SessionStore, SessionManager
    from plaud_tools.transport import HttpResponse

    class PlainTextTransport:
        def request(self, method, url, headers, body=None):
            return HttpResponse(status_code=200, body=b"# My Summary\n\nContent here.", headers={})

    client = PlaudClient(SessionManager(SessionStore()), transport=PlainTextTransport())
    raw = {
        "content_list": [{"data_type": "auto_sum_note", "task_status": 1, "data_link": "https://cdn.example.com/summary.txt"}]
    }
    result = client._fetch_summary_from_data_link(raw)
    assert result == "# My Summary\n\nContent here."
