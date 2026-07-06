from __future__ import annotations

import inspect
import json
from pathlib import Path

import pytest

from plaud_tools.cli import main, run_cli
from plaud_tools.client import PlaudClient
from plaud_tools.mcp import build_handlers
from plaud_tools.models import FileTag, Recording, RecordingDetail
from plaud_tools.session import SessionStore


class StubClient:
    # Full fixture set — returned for both the unfiltered path (no query) and
    # the incremental filtered path (query with limit=BROWSE_PAGE_SIZE).  The
    # old assertion `query.limit == 2` is removed: the direct unfiltered path
    # passes `limit=args.limit`, while the incremental path passes
    # `limit=BROWSE_PAGE_SIZE`.  Client-side filtering via collect_filtered_paged
    # is responsible for narrowing results.
    _ALL_RECORDINGS = [
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

    def list_recordings(self, query=None):
        if query is not None and query.limit is not None and query.limit <= 5:
            # Direct unfiltered path (no filter flags): passes the exact CLI limit.
            # Return only the first record to satisfy the shape test.
            return [
                Recording(
                    id="r1",
                    filename="meeting",
                    start_time=1_746_000_000_000,
                    duration=600_000,
                    is_trans=True,
                    filetag_id_list=["tag1"],
                )
            ]
        # Incremental filtered path (limit=BROWSE_PAGE_SIZE) or no-query path:
        # return all records so client-side filtering can narrow them.
        return list(self._ALL_RECORDINGS)

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

        return Recording(
            id="uploaded1",
            filename=filename,
            start_time=start_time or 0,
            duration=0,
            is_trash=False,
            is_trans=False,
            is_summary=False,
            filetag_id_list=[],
            raw={},
        )

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

    def rename_recording(self, recording_id, filename):
        # Parameter named to match PlaudClient.rename_recording's real
        # signature (Wave 5, 2026-07-06 audit, S5.4) -- it used to be
        # `new_name`, a harmless-in-practice drift (both call sites pass it
        # positionally) that the signature-sync test below now catches.
        self.rename_call = (recording_id, filename)

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

    def correct_transcript(self, recording_id, find, replace):
        self.correct_transcript_call = (recording_id, find, replace)
        return {"replacements": 3, "segments_changed": 2}

    def wait_for_transcription(self, recording_id, **kwargs):
        pass

    def wait_for_summary(self, recording_id, **kwargs):
        pass

    def dump_raw_detail(self, recording_id):
        return {"file_id": recording_id, "file_name": "meeting", "content_list": [], "extra_data": {}}

    def transcribe_and_summarize(
        self, recording_id, template_type=None, language=None, diarization=None, llm=None
    ):
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
            "has_summary": False,
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


def test_cli_search_supports_unfiled():
    # search shares _list_recordings_filtered with list — it must accept the
    # same --unfiled flag list does, not just --since/--until/--folder-id.
    output = run_cli(["search", "lunch", "--unfiled"], StubClient())
    payload = json.loads(output)
    assert [item["id"] for item in payload] == ["r2"]


def test_cli_detail_omits_transcript_by_default_and_fetches_summary():
    # #detail fix: no more always-null "transcript" key, and "summary" must
    # reflect an actual include_summary=True fetch (StubClient always returns
    # ai_content regardless, but this pins that include_summary=True is passed
    # through, matching `summary <id>`).
    output = run_cli(["detail", "rec1"], StubClient())
    payload = json.loads(output)
    assert payload["id"] == "rec1"
    assert payload["is_trans"] is True
    assert payload["is_summary"] is True
    assert payload["summary"] == "# Summary"
    assert "transcript" not in payload


def test_cli_detail_include_transcript_populates_transcript():
    output = run_cli(["detail", "rec1", "--include-transcript"], StubClient())
    payload = json.loads(output)
    assert payload["transcript"] == "hello world"
    assert payload["summary"] == "# Summary"


def test_cli_detail_passes_include_summary_to_client():
    calls = {}

    class DetailStub(StubClient):
        def get_recording(self, recording_id, include_transcript=False, include_summary=False):
            calls["include_summary"] = include_summary
            return super().get_recording(
                recording_id, include_transcript=include_transcript, include_summary=include_summary
            )

    run_cli(["detail", "rec1"], DetailStub())
    assert calls["include_summary"] is True


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


def test_cli_trash_list_flag_lists_trash():
    from datetime import datetime

    expected_date = datetime.fromtimestamp(1_746_000_000_000 / 1000).isoformat()[:16]
    output = run_cli(["trash", "--list"], StubClient())
    payload = json.loads(output)
    assert payload == [
        {
            "id": "t1",
            "title": "old meeting",
            "date": expected_date,
            "duration_minutes": 10,
            "has_transcript": False,
            "has_summary": False,
            "folder_id": None,
        }
    ]


def test_cli_trash_bare_no_id_no_list_raises():
    # Split-trash fix: a bare `trash` with no ID and no --list used to
    # silently list trash — a dropped/mistyped ID argument turned an intended
    # mutation into a no-op listing with no error. It must now raise instead.
    import pytest

    with pytest.raises(ValueError, match="requires a recording ID"):
        run_cli(["trash"], StubClient())


def test_cli_trash_list_and_id_together_raises():
    import pytest

    with pytest.raises(ValueError, match="cannot be combined"):
        run_cli(["trash", "rec1", "--list"], StubClient())


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
    output = run_cli(["trash", "--list"], StubClient())
    payload = json.loads(output)
    assert payload == [
        {
            "id": "t1",
            "title": "old meeting",
            "date": expected_date,
            "duration_minutes": 10,
            "has_transcript": False,
            "has_summary": False,
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


def test_cli_correct_transcript_shapes_success_response():
    client = StubClient()
    output = run_cli(["correct-transcript", "rec1", "Cache", "Cash"], client)
    payload = json.loads(output)
    assert client.correct_transcript_call == ("rec1", "Cache", "Cash")
    assert payload == {
        "ok": True,
        "recording_id": "rec1",
        "find": "Cache",
        "replace": "Cash",
        "replacements": 3,
        "segments_changed": 2,
    }


def test_mcp_edit_transcript_correct_shapes_success_response():
    handlers = build_handlers(lambda: StubClient())
    result = handlers["edit_transcript"](recording_id="rec1", action="correct", find="Cache", replace="Cash")
    payload = json.loads(result["content"][0]["text"])
    assert payload == {
        "ok": True,
        "recording_id": "rec1",
        "action": "correct",
        "replacements": 3,
        "segments_changed": 2,
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


def test_cli_transcribe_passes_language_diarization_llm():
    client = StubClient()
    run_cli(
        [
            "transcribe",
            "rec1",
            "--language",
            "en-US",
            "--diarization",
            "--llm",
            "gpt-4",
        ],
        client,
    )
    assert client.transcribe_call == ("rec1", None, "en-US", True, "gpt-4")


def test_cli_transcribe_no_diarization_flag():
    client = StubClient()
    run_cli(["transcribe", "rec1", "--no-diarization"], client)
    assert client.transcribe_call == ("rec1", None, None, False, None)


def test_cli_transcribe_wait_none_is_default_and_never_polls():
    client = StubClient()
    calls = []
    client.wait_for_transcription = lambda recording_id, **kwargs: calls.append("transcript")
    client.wait_for_summary = lambda recording_id, **kwargs: calls.append("summary")
    output = run_cli(["transcribe", "rec1"], client)
    payload = json.loads(output)
    assert calls == []
    assert "is_trans" not in payload
    assert "is_summary" not in payload


def test_cli_transcribe_wait_transcript_polls_transcript_only():
    client = StubClient()
    calls = []
    client.wait_for_transcription = lambda recording_id, **kwargs: calls.append("transcript")
    client.wait_for_summary = lambda recording_id, **kwargs: calls.append("summary")
    output = run_cli(["transcribe", "rec1", "--wait", "transcript"], client)
    payload = json.loads(output)
    assert calls == ["transcript"]
    assert payload["is_trans"] is True
    assert payload["is_summary"] is True


def test_cli_transcribe_wait_summary_polls_both():
    client = StubClient()
    calls = []
    client.wait_for_transcription = lambda recording_id, **kwargs: calls.append("transcript")
    client.wait_for_summary = lambda recording_id, **kwargs: calls.append("summary")
    output = run_cli(["transcribe", "rec1", "--wait", "summary"], client)
    payload = json.loads(output)
    assert calls == ["transcript", "summary"]
    assert payload["is_trans"] is True
    assert payload["is_summary"] is True


def test_cli_transcribe_rejects_unknown_wait_mode():
    import pytest

    with pytest.raises(SystemExit):
        run_cli(["transcribe", "rec1", "--wait", "forever"], StubClient())


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
    store = SessionStore(
        tmp_path / "session.json", service_name="plaud-tools-test-cli", account_name="session"
    )
    set_output = run_cli(
        [
            "session",
            "set",
            "--token",
            "header.payload.signature",
            "--region",
            "eu",
            "--email",
            "test@example.com",
        ],
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
    store = SessionStore(
        tmp_path / "missing.json", service_name="plaud-tools-test-cli-missing", account_name="session"
    )
    output = run_cli(["session", "show"], session_store=store)
    payload = json.loads(output)
    assert payload["session"] is None


class StubAuth:
    def __init__(self):
        self.calls = []

    def login(self, email, password, region):
        self.calls.append((email, password, region))
        return type("Session", (), {"email": email, "region": region})()


def test_login_password_help_warns_about_exposure():
    """--password help text must warn about process-listing exposure and point to safer alternatives."""
    from plaud_tools.cli import build_parser

    parser = build_parser()
    # Drill into the login subparser to get its specific help
    login_parser = parser._subparsers._actions[-1].choices["login"]
    login_help = login_parser.format_help()
    assert "process listing" in login_help or "ps" in login_help or "Task Manager" in login_help
    assert "PLAUD_ACCESS_TOKEN" in login_help
    assert "session set --token" in login_help


def test_cli_login_uses_auth_and_returns_stored_shape(tmp_path: Path):
    auth = StubAuth()
    store = SessionStore(
        tmp_path / "session.json", service_name="plaud-tools-test-login", account_name="session"
    )
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
        lambda: SessionStore(
            tmp_path / "missing.json", service_name="plaud-tools-test-main", account_name="session"
        ),
    )
    code = main(["list"])
    assert code == 1
    captured = capsys.readouterr()
    assert "No Plaud session available." in captured.err


# ---------------------------------------------------------------------------
# §6.2 — session-expired CLI output must name the remedy.
# ---------------------------------------------------------------------------


def _run_main_with_client(monkeypatch, client, argv):
    """Exercise main()'s error handling with an injected client.

    main() always builds its own PlaudClient from a SessionStore, so to drive
    a specific client-raised error through main()'s except clauses, patch
    run_cli to forward the given client instead.
    """
    from plaud_tools import cli as cli_module

    real_run_cli = cli_module.run_cli
    monkeypatch.setattr(cli_module, "run_cli", lambda argv_inner: real_run_cli(argv_inner, client=client))
    return cli_module.main(argv)


def test_cli_main_session_expired_error_names_remedy(capsys, monkeypatch):
    from plaud_tools.errors import PlaudSessionExpiredError

    class ExpiredClient:
        def list_recordings(self, query=None):
            raise PlaudSessionExpiredError("Plaud session expired or expiring soon.")

    code = _run_main_with_client(monkeypatch, ExpiredClient(), ["list"])
    assert code == 1
    captured = capsys.readouterr()
    assert "Plaud session expired or expiring soon." in captured.err
    assert "plaud-tools refresh" in captured.err
    assert "PlaudTools tray" in captured.err


def test_cli_main_401_api_error_names_remedy(capsys, monkeypatch):
    from plaud_tools.errors import PlaudApiError

    class UnauthorizedClient:
        def list_recordings(self, query=None):
            raise PlaudApiError("Plaud API error: HTTP 401: unauthorized", http_status=401)

    code = _run_main_with_client(monkeypatch, UnauthorizedClient(), ["list"])
    assert code == 1
    captured = capsys.readouterr()
    assert "plaud-tools refresh" in captured.err


def test_cli_main_non_session_api_error_has_no_remedy_text(capsys, monkeypatch):
    from plaud_tools.errors import PlaudApiError

    class NotFoundClient:
        def list_recordings(self, query=None):
            raise PlaudApiError("Plaud API error: HTTP 404: not found", http_status=404)

    code = _run_main_with_client(monkeypatch, NotFoundClient(), ["list"])
    assert code == 1
    captured = capsys.readouterr()
    assert "plaud-tools refresh" not in captured.err


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
    assert payload == {"ok": True, "recording_id": "rec1", "action": "trash"}
    assert client.trash_move_call == ["rec1"]


def test_mcp_mutate_recording_restore():
    client = StubClient()
    handlers = build_handlers(lambda: client)
    result = handlers["mutate_recording"]("rec1", "restore")
    payload = json.loads(result["content"][0]["text"])
    assert payload == {"ok": True, "recording_id": "rec1", "action": "restore"}
    assert client.trash_restore_call == ["rec1"]


def test_mcp_mutate_recording_move():
    client = StubClient()
    handlers = build_handlers(lambda: client)
    result = handlers["mutate_recording"]("rec1", "move", folder_id="tag1")
    payload = json.loads(result["content"][0]["text"])
    assert payload == {"ok": True, "recording_id": "rec1", "folder_id": "tag1"}
    assert client.move_call == ("rec1", "tag1")


def test_mcp_mutate_recording_move_clears_folder_with_empty_string():
    client = StubClient()
    handlers = build_handlers(lambda: client)
    result = handlers["mutate_recording"]("rec1", "move", folder_id="")
    payload = json.loads(result["content"][0]["text"])
    assert payload["folder_id"] is None
    assert client.move_call == ("rec1", None)


def test_mcp_mutate_recording_move_clears_folder_with_clear_folder_flag():
    client = StubClient()
    handlers = build_handlers(lambda: client)
    result = handlers["mutate_recording"]("rec1", "move", folder_id="tag1", clear_folder=True)
    payload = json.loads(result["content"][0]["text"])
    assert payload["folder_id"] is None
    assert client.move_call == ("rec1", None)


def test_mcp_mutate_recording_delete_is_unknown_action():
    """delete is no longer a valid mutate_recording action — it moved to delete_recording."""
    handlers = build_handlers(lambda: StubClient())
    result = handlers["mutate_recording"]("rec1", "delete")
    assert result["isError"] is True
    payload = json.loads(result["content"][0]["text"])
    assert "unknown action" in payload["error"]


def test_mcp_delete_recording_calls_client_and_returns_ok():
    # D4: confirm=True is required; pass it explicitly to exercise the happy path.
    client = StubClient()
    handlers = build_handlers(lambda: client)
    result = handlers["delete_recording"]("rec1", confirm=True)
    payload = json.loads(result["content"][0]["text"])
    assert payload == {"ok": True, "recording_id": "rec1"}
    assert client.delete_call == ["rec1"]


def test_mcp_delete_recording_returns_session_error_when_client_missing():
    handlers = build_handlers(lambda: None)
    result = handlers["delete_recording"]("rec1")
    assert result["isError"] is True


def test_mcp_edit_transcript_rename_speaker_calls_client_and_returns_segments_updated():
    client = StubClient()
    handlers = build_handlers(lambda: client)
    result = handlers["edit_transcript"](
        recording_id="rec1", action="rename_speaker", original_label="Speaker 1", new_name="Alex"
    )
    payload = json.loads(result["content"][0]["text"])
    assert payload["ok"] is True
    assert payload["recording_id"] == "rec1"
    assert payload["action"] == "rename_speaker"
    assert payload["segments_updated"] == 7
    assert client.rename_speaker_call == ("rec1", "Speaker 1", "Alex")


def test_mcp_edit_transcript_rename_speaker_returns_session_error_when_client_missing():
    handlers = build_handlers(lambda: None)
    result = handlers["edit_transcript"](
        recording_id="rec1", action="rename_speaker", original_label="Speaker 1", new_name="Alex"
    )
    assert result["isError"] is True


def test_mcp_mutate_recording_unknown_action():
    handlers = build_handlers(lambda: StubClient())
    result = handlers["mutate_recording"]("rec1", "fly_away")
    assert result["isError"] is True
    payload = json.loads(result["content"][0]["text"])
    assert "unknown action" in payload["error"]


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
        from pathlib import Path as _Path

        size = data.stat().st_size if isinstance(data, _Path) else len(data)
        self.upload_call = (size, filename, file_type)
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
    assert client.summary_wait_call is None


def test_mcp_process_recording_wait_none_returns_accepted_without_polling():
    client = MutateStub()
    handlers = build_handlers(lambda: client)
    result = handlers["process_recording"]("rec1", wait="none")
    payload = json.loads(result["content"][0]["text"])
    assert payload == {"recording_id": "rec1", "accepted": True}
    assert client.transcribe_call == "rec1"
    assert client.wait_call is None
    assert client.summary_wait_call is None


def test_mcp_process_recording_wait_transcript_does_not_wait_for_summary():
    client = MutateStub()
    handlers = build_handlers(lambda: client)
    result = handlers["process_recording"]("rec1", wait="transcript")
    payload = json.loads(result["content"][0]["text"])
    assert payload["ok"] is True
    assert payload["recording_id"] == "rec1"
    assert client.transcribe_call == "rec1"
    assert client.wait_call == "rec1"
    assert client.summary_wait_call is None


def test_mcp_process_recording_wait_summary_preserves_old_blocking_behavior():
    client = MutateStub()
    handlers = build_handlers(lambda: client)
    result = handlers["process_recording"]("rec1", wait="summary")
    payload = json.loads(result["content"][0]["text"])
    assert payload["ok"] is True
    assert payload["recording_id"] == "rec1"
    assert client.transcribe_call == "rec1"
    assert client.wait_call == "rec1"
    assert client.summary_wait_call == "rec1"


def test_mcp_process_recording_rejects_unknown_wait_mode():
    handlers = build_handlers(lambda: MutateStub())
    result = handlers["process_recording"]("rec1", wait="forever")
    assert result["isError"] is True
    payload = json.loads(result["content"][0]["text"])
    assert "wait must be one of" in payload["error"]


def test_mcp_merge_recordings_returns_slim_summary():
    handlers = build_handlers(lambda: StubClient())
    result = handlers["merge_recordings"](recording_ids=["r1", "r2"], title="Combined")
    payload = json.loads(result["content"][0]["text"])
    assert payload == {"ok": True, "recording_id": "merged1", "title": "Combined"}


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
    from plaud_tools.session import SessionManager, SessionStore

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
    from plaud_tools.session import SessionManager, SessionStore

    client = PlaudClient(SessionManager(SessionStore()))
    raw = {
        "content_list": [{"data_type": "auto_sum_note", "task_status": 1, "data_id": "d1"}],
        "pre_download_content_list": [
            # data_id doesn't match "d1" — should fall back to data_type match
            {
                "data_id": "d99",
                "data_type": "auto_sum_note",
                "data_content": '{"ai_content": "fallback summary"}',
            }
        ],
    }
    result = client._extract_inline_summary(raw, "d1")
    assert result == "fallback summary"


def test_fetch_summary_from_data_link_handles_plain_text():
    from plaud_tools.client import PlaudClient
    from plaud_tools.session import SessionManager, SessionStore
    from plaud_tools.transport import HttpResponse

    class PlainTextTransport:
        def request(self, method, url, headers, body=None, *, timeout=None):
            return HttpResponse(status_code=200, body=b"# My Summary\n\nContent here.", headers={})

    client = PlaudClient(SessionManager(SessionStore()), transport=PlainTextTransport())
    raw = {
        "content_list": [
            {
                "data_type": "auto_sum_note",
                "task_status": 1,
                "data_link": "https://cdn.example.com/summary.txt",
            }
        ]
    }
    result = client._fetch_summary_from_data_link(raw)
    assert result == "# My Summary\n\nContent here."


# ---------------------------------------------------------------------------
# edit_summary — MCP tool + CLI (correct-summary / set-summary)
# ---------------------------------------------------------------------------


class SummaryStub(StubClient):
    def __init__(self):
        self.correct_call = None
        self.set_call = None

    def correct_summary(self, recording_id, find, replace):
        self.correct_call = (recording_id, find, replace)
        return {"replacements": 3}

    def set_summary(self, recording_id, content):
        self.set_call = (recording_id, content)


def test_mcp_edit_summary_correct_calls_correct_summary():
    client = SummaryStub()
    handlers = build_handlers(lambda: client)
    result = handlers["edit_summary"](recording_id="rec1", action="correct", find="Suzan", replace="Susan")
    payload = json.loads(result["content"][0]["text"])
    assert payload["ok"] is True
    assert payload["replacements"] == 3
    assert client.correct_call == ("rec1", "Suzan", "Susan")


def test_mcp_edit_summary_correct_requires_find_and_replace():
    handlers = build_handlers(lambda: SummaryStub())
    result = handlers["edit_summary"](recording_id="rec1", action="correct", find="x")
    assert result["isError"] is True
    payload = json.loads(result["content"][0]["text"])
    assert "find and replace" in payload["error"]


def test_mcp_edit_summary_replace_calls_set_summary():
    client = SummaryStub()
    handlers = build_handlers(lambda: client)
    result = handlers["edit_summary"](recording_id="rec1", action="replace", content="# New")
    payload = json.loads(result["content"][0]["text"])
    assert payload["ok"] is True
    assert client.set_call == ("rec1", "# New")


def test_mcp_edit_summary_replace_requires_content():
    handlers = build_handlers(lambda: SummaryStub())
    result = handlers["edit_summary"](recording_id="rec1", action="replace")
    assert result["isError"] is True
    assert "content is required" in json.loads(result["content"][0]["text"])["error"]


def test_mcp_edit_summary_rejects_unknown_action():
    handlers = build_handlers(lambda: SummaryStub())
    result = handlers["edit_summary"](recording_id="rec1", action="frobnicate")
    assert result["isError"] is True
    assert "unknown action" in json.loads(result["content"][0]["text"])["error"]


def test_cli_correct_summary_calls_client():
    client = SummaryStub()
    output = run_cli(["correct-summary", "rec1", "Suzan", "Susan"], client)
    payload = json.loads(output)
    assert payload["replacements"] == 3
    assert client.correct_call == ("rec1", "Suzan", "Susan")


def test_cli_set_summary_from_content_flag():
    client = SummaryStub()
    output = run_cli(["set-summary", "rec1", "--content", "# Fresh summary"], client)
    payload = json.loads(output)
    assert payload["ok"] is True
    assert client.set_call == ("rec1", "# Fresh summary")


def test_cli_set_summary_from_content_file(tmp_path):
    md = tmp_path / "summary.md"
    md.write_text("# From file\n\nbody", encoding="utf-8")
    client = SummaryStub()
    output = run_cli(["set-summary", "rec1", "--content-file", str(md)], client)
    payload = json.loads(output)
    assert payload["ok"] is True
    assert client.set_call == ("rec1", "# From file\n\nbody")


# ---------------------------------------------------------------------------
# mutate_folder — MCP tool + CLI (folder create/edit/delete)
# ---------------------------------------------------------------------------


class FolderStub(StubClient):
    def __init__(self):
        self.create_call = None
        self.update_call = None
        self.delete_call = None

    def create_folder(self, name, *, color=None, icon=None):
        self.create_call = (name, color, icon)
        return FileTag(id="new1", name=name, color=color or "", icon=icon or "")

    def update_folder(self, folder_id, *, name=None, color=None, icon=None):
        self.update_call = (folder_id, name, color, icon)
        return FileTag(id=folder_id, name=name or "old", color=color or "", icon=icon or "")

    def delete_folder(self, folder_id):
        self.delete_call = folder_id


def test_mcp_mutate_folder_create():
    client = FolderStub()
    handlers = build_handlers(lambda: client)
    result = handlers["mutate_folder"](action="create", name="Clients", color="#111", icon="e627")
    payload = json.loads(result["content"][0]["text"])
    assert payload["ok"] is True
    assert payload["folder"]["id"] == "new1"
    assert client.create_call == ("Clients", "#111", "e627")


def test_mcp_mutate_folder_create_requires_name():
    handlers = build_handlers(lambda: FolderStub())
    result = handlers["mutate_folder"](action="create")
    assert result["isError"] is True
    assert "name is required" in json.loads(result["content"][0]["text"])["error"]


def test_mcp_mutate_folder_edit():
    client = FolderStub()
    handlers = build_handlers(lambda: client)
    result = handlers["mutate_folder"](action="edit", folder_id="f1", name="Renamed")
    payload = json.loads(result["content"][0]["text"])
    assert payload["ok"] is True
    assert client.update_call == ("f1", "Renamed", None, None)


def test_mcp_mutate_folder_edit_requires_a_field():
    handlers = build_handlers(lambda: FolderStub())
    result = handlers["mutate_folder"](action="edit", folder_id="f1")
    assert result["isError"] is True
    assert "at least one of name, color, icon" in json.loads(result["content"][0]["text"])["error"]


def test_mcp_mutate_folder_delete_requires_confirm():
    client = FolderStub()
    handlers = build_handlers(lambda: client)
    result = handlers["mutate_folder"](action="delete", folder_id="f1")
    assert result["isError"] is True
    assert "confirm=true" in json.loads(result["content"][0]["text"])["error"]
    assert client.delete_call is None  # not deleted without confirm


def test_mcp_mutate_folder_delete_with_confirm():
    client = FolderStub()
    handlers = build_handlers(lambda: client)
    result = handlers["mutate_folder"](action="delete", folder_id="f1", confirm=True)
    payload = json.loads(result["content"][0]["text"])
    assert payload["ok"] is True
    assert client.delete_call == "f1"


def test_mcp_mutate_folder_rejects_unknown_action():
    handlers = build_handlers(lambda: FolderStub())
    result = handlers["mutate_folder"](action="obliterate")
    assert result["isError"] is True
    assert "unknown action" in json.loads(result["content"][0]["text"])["error"]


def test_cli_folder_create():
    client = FolderStub()
    output = run_cli(["folder", "create", "Clients", "--color", "#111", "--icon", "e627"], client)
    payload = json.loads(output)
    assert payload["ok"] is True
    assert client.create_call == ("Clients", "#111", "e627")


def test_cli_folder_edit():
    client = FolderStub()
    output = run_cli(["folder", "edit", "f1", "--name", "Renamed"], client)
    payload = json.loads(output)
    assert payload["action"] == "edit"
    assert client.update_call == ("f1", "Renamed", None, None)


def test_cli_folder_edit_requires_a_field():
    import pytest

    client = FolderStub()
    with pytest.raises(ValueError, match="at least one of --name, --color, --icon"):
        run_cli(["folder", "edit", "f1"], client)


def test_cli_folder_delete_requires_yes():
    import pytest

    client = FolderStub()
    with pytest.raises(ValueError, match="cannot be undone"):
        run_cli(["folder", "delete", "f1"], client)
    assert client.delete_call is None


def test_cli_folder_delete_with_yes():
    client = FolderStub()
    output = run_cli(["folder", "delete", "f1", "--yes"], client)
    payload = json.loads(output)
    assert payload["ok"] is True
    assert client.delete_call == "f1"


# ---------------------------------------------------------------------------
# #149 — upload partial success: a post-upload folder-move failure must not
# lose the newly created recording id (via the shared upload_with_transcode
# helper in transcode.py).
# ---------------------------------------------------------------------------


class FolderFailingUploadStubClient(UploadStubClient):
    def set_recording_folder(self, recording_id, folder_id):
        raise ValueError(f"folder not found: {folder_id}")


def test_cli_upload_folder_move_failure_is_partial_success(tmp_path):
    mp3_file = tmp_path / "test.mp3"
    mp3_file.write_bytes(b"fake mp3 data")
    client = FolderFailingUploadStubClient()
    output = run_cli(["upload", str(mp3_file), "--folder-id", "bad-folder", "--detach"], client)
    payload = json.loads(output)
    # The upload itself succeeded — the recording id must not be lost.
    assert payload["ok"] is True
    assert payload["recording_id"] == "new-rec"
    assert "folder not found" in payload["folder_error"]


class FolderFailingMutateStub(MutateStub):
    def set_recording_folder(self, recording_id, folder_id):
        raise ValueError(f"folder not found: {folder_id}")


def test_mcp_upload_recording_folder_move_failure_is_partial_success(tmp_path):
    mp3_file = tmp_path / "audio.mp3"
    mp3_file.write_bytes(b"fake mp3")
    client = FolderFailingMutateStub()
    handlers = build_handlers(lambda: client)
    result = handlers["upload_recording"](str(mp3_file), folder_id="bad-folder")
    payload = json.loads(result["content"][0]["text"])
    assert payload["ok"] is True
    assert payload["recording_id"] == "mcp-rec"
    assert "folder not found" in payload["folder_error"]
    # This must NOT be reported as a tool error — the upload succeeded.
    assert "isError" not in result


# ---------------------------------------------------------------------------
# #158 — `update` must refuse cleanly in a frozen (PyInstaller) bundle instead
# of spawning itself with `-m pip install ...` as bogus CLI arguments.
# ---------------------------------------------------------------------------


def test_cli_update_refuses_when_frozen(monkeypatch, capsys):
    import pytest

    import plaud_tools.cli as cli_module

    monkeypatch.setattr(cli_module.sys, "frozen", True, raising=False)
    # _handle_update never returns — it always calls sys.exit(), frozen or not.
    with pytest.raises(SystemExit) as exc_info:
        main(["update"])
    assert exc_info.value.code == 1
    captured = capsys.readouterr()
    assert "tray" in captured.err.lower() or "install.ps1" in captured.err


def test_cli_update_still_runs_pip_when_not_frozen(monkeypatch):
    import subprocess

    import pytest

    import plaud_tools.cli as cli_module

    monkeypatch.delattr(cli_module.sys, "frozen", raising=False)

    class FakeCompletedProcess:
        returncode = 0

    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        return FakeCompletedProcess()

    monkeypatch.setattr(subprocess, "run", fake_run)

    with pytest.raises(SystemExit) as exc_info:
        main(["update"])
    assert exc_info.value.code == 0
    assert calls and calls[0][1:3] == ["-m", "pip"]


# ---------------------------------------------------------------------------
# #155 — stdout must be reconfigured to UTF-8 so redirected/piped output on
# Windows (cp1252 console code page) doesn't crash on non-ASCII transcript text.
# ---------------------------------------------------------------------------


def test_reconfigure_stdout_utf8_calls_reconfigure(monkeypatch):
    import plaud_tools.cli as cli_module

    calls = []

    class FakeStdout:
        def reconfigure(self, **kwargs):
            calls.append(kwargs)

    monkeypatch.setattr(cli_module.sys, "stdout", FakeStdout())
    cli_module._reconfigure_stdout_utf8()
    assert calls == [{"encoding": "utf-8"}]


def test_reconfigure_stdout_utf8_swallows_reconfigure_errors(monkeypatch):
    import plaud_tools.cli as cli_module

    class FakeStdout:
        def reconfigure(self, **kwargs):
            raise ValueError("stream does not support reconfigure")

    monkeypatch.setattr(cli_module.sys, "stdout", FakeStdout())
    cli_module._reconfigure_stdout_utf8()  # must not raise


def test_reconfigure_stdout_utf8_noop_when_unsupported(monkeypatch):
    import plaud_tools.cli as cli_module

    class FakeStdout:
        pass

    monkeypatch.setattr(cli_module.sys, "stdout", FakeStdout())
    cli_module._reconfigure_stdout_utf8()  # must not raise


def test_main_transcript_non_ascii_survives_cp1252_stdout(monkeypatch, tmp_path):
    """Regression for #155: without the UTF-8 reconfigure, printing non-ASCII
    transcript text to a stdout hardwired to cp1252 raises UnicodeEncodeError
    (a ValueError subclass), which main() would silently turn into exit code 1
    instead of the transcript actually reaching the caller."""
    import io

    import plaud_tools.cli as cli_module

    class TranscriptClient(StubClient):
        def fetch_transcript(self, recording_id):
            return "café — 日本語 — résumé"

    monkeypatch.setattr(cli_module, "_build_runtime_client", lambda store: TranscriptClient())
    monkeypatch.setattr(
        cli_module,
        "SessionStore",
        lambda: SessionStore(tmp_path / "session.json", service_name="x-155", account_name="y"),
    )
    buf = io.BytesIO()
    # newline="" disables universal-newline translation on write so the
    # assertion below doesn't have to account for os.linesep — irrelevant to
    # the #155 fix, which is only about the character *encoding*.
    wrapper = io.TextIOWrapper(buf, encoding="cp1252", errors="strict", newline="")
    monkeypatch.setattr(cli_module.sys, "stdout", wrapper)

    code = main(["transcript", "rec1"])
    wrapper.flush()

    assert code == 0
    assert buf.getvalue().decode("utf-8") == "café — 日本語 — résumé\n"


# ---------------------------------------------------------------------------
# StubClient / PlaudClient signature sync (Wave 5, §5.4)
#
# StubClient hand-rolls the PlaudClient facade for every test in this file.
# It has drifted before (rename_recording's second parameter was called
# `new_name` here vs `filename` on the real client -- harmless today because
# both call sites pass it positionally, but a future keyword-argument test or
# refactor could silently start exercising a code path the real client
# doesn't support). These tests assert every StubClient method exists on
# PlaudClient with a compatible signature so that drift fails CI instead of
# rotting silently.
# ---------------------------------------------------------------------------


def _stub_client_public_methods() -> list[str]:
    return sorted(
        name
        for name, value in vars(StubClient).items()
        if not name.startswith("_") and inspect.isfunction(value)
    )


@pytest.mark.parametrize("method_name", _stub_client_public_methods())
def test_stub_client_method_exists_on_plaud_client(method_name: str):
    assert hasattr(PlaudClient, method_name), (
        f"StubClient.{method_name} has no PlaudClient.{method_name} counterpart -- "
        f"the test double no longer matches the real client facade."
    )


@pytest.mark.parametrize("method_name", _stub_client_public_methods())
def test_stub_client_signature_is_compatible_with_plaud_client(method_name: str):
    """Every named parameter StubClient declares must exist on PlaudClient too.

    Comparison is by parameter *name* only (not order, kind, or defaults) --
    StubClient methods are deliberately looser than the real client (e.g.
    fewer defaults) but must not accept a name PlaudClient doesn't recognise,
    since any test written against that name would pass here while silently
    not matching what production code can actually call. A stub parameter
    named ``kwargs``/``args`` (a ``**kwargs``/``*args`` catch-all) is exempt --
    it is intentionally more permissive than the real signature.
    """
    real = getattr(PlaudClient, method_name, None)
    if real is None:
        pytest.skip(f"no PlaudClient.{method_name} (covered by the existence test above)")

    stub_sig = inspect.signature(getattr(StubClient, method_name))
    real_params = set(inspect.signature(real).parameters) - {"self"}

    catch_all = {p.name for p in stub_sig.parameters.values() if p.kind in (p.VAR_POSITIONAL, p.VAR_KEYWORD)}
    stub_params = (set(stub_sig.parameters) - {"self"}) - catch_all

    extra = stub_params - real_params
    assert not extra, (
        f"StubClient.{method_name} declares parameter(s) {sorted(extra)} that "
        f"PlaudClient.{method_name}{inspect.signature(real)} does not accept -- "
        f"the stub has drifted from the real client."
    )
