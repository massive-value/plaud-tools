from __future__ import annotations

import base64
import gzip
import io
import json
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlsplit

import pytest

import plaud_tools.core.client as client_mod
from plaud_tools.core.client import PlaudClient, PlaudRecordingQuery, to_file_id
from plaud_tools.core.errors import PlaudApiError, PlaudSessionExpiredError, PlaudWaitTimeoutError
from plaud_tools.core.folders import plan_colon_nesting
from plaud_tools.core.models import AudioLink, Folder, region_for_api_domain
from plaud_tools.core.session import FileSessionStore, PlaudSession, SessionManager, SessionStore
from plaud_tools.core.transport import HttpResponse

BASE = "https://api-euc1.plaud.ai"
V4 = "/file-app/v4"


class StubTransport:
    """Plays back one queued response (or exception) per request, in order."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def request(self, method, url, headers, body=None, *, timeout=None):
        self.calls.append({"method": method, "url": url, "headers": headers, "body": body})
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


class RouteTransport:
    """Answers by ``"METHOD /path"`` (query string ignored), for the many-request v4 flows.

    A route value is a payload dict (sent as a 200 JSON reply), an HttpResponse,
    an exception to raise, a callable taking the full URL, or a list of those
    played in order (the last one repeats).  A request nobody routed fails loudly.
    """

    def __init__(self, routes):
        self.routes = {key: list(v) if isinstance(v, list) else v for key, v in routes.items()}
        self.calls = []

    @staticmethod
    def key(method, url):
        return f"{method} {url.removeprefix(BASE).split('?')[0]}"

    def request(self, method, url, headers, body=None, *, timeout=None):
        self.calls.append({"method": method, "url": url, "headers": headers, "body": body})
        key = self.key(method, url)
        if key not in self.routes:
            raise AssertionError(f"unrouted request: {key}")
        reply = self.routes[key]
        if isinstance(reply, list):
            reply = reply.pop(0) if len(reply) > 1 else reply[0]
        if callable(reply):
            reply = reply(url)
        if isinstance(reply, Exception):
            raise reply
        if isinstance(reply, dict):
            reply = HttpResponse(200, json.dumps(reply).encode(), {})
        return reply

    def sent(self, key):
        """JSON bodies sent to *key*, oldest first."""
        return [
            json.loads(call["body"])
            for call in self.calls
            if self.key(call["method"], call["url"]) == key and call["body"]
        ]

    def hit(self, key):
        return any(self.key(c["method"], c["url"]) == key for c in self.calls)


def make_jwt(days=300):
    payload = {"exp": 2_000_000_000 + days * 86400}
    encoded = base64.urlsafe_b64encode(json.dumps(payload).encode("utf-8")).decode("ascii").rstrip("=")
    return f"header.{encoded}.sig"


def make_manager(tmp_path: Path, region="eu"):
    path = tmp_path / "session.json"
    store = FileSessionStore(path)
    store.save(PlaudSession(access_token=make_jwt(), region=region, email="test@example.com"))
    return SessionManager(store), store


def routed(tmp_path, routes):
    transport = RouteTransport(routes)
    return PlaudClient(make_manager(tmp_path)[0], transport=transport), transport


def ok(data=None):
    return {"status": 0, "data": data}


def json_response(payload):
    return HttpResponse(200, json.dumps(payload).encode(), {})


EMPTY_LIST = ok({"items": []})


@pytest.fixture(autouse=True)
def _instant_retries(monkeypatch):
    monkeypatch.setattr(client_mod, "_sleep", lambda s: None)
    monkeypatch.setattr(client_mod, "_jitter", lambda lo, hi: lo)


def children(by_parent):
    """Route for ``folders/children``: answers by the ``node_id`` in the query."""

    def reply(url):
        node_id = parse_qs(urlsplit(url).query)["node_id"][0]
        return ok({"items": by_parent.get(node_id, [])})

    return reply


def obj(object_type, url=None, *, object_id="o1", content_id="c1", version_ms=500, task_status=None):
    return {
        "object_type": object_type,
        "object_id": object_id,
        "content_id": content_id,
        "version_ms": version_ms,
        "content_url": url,
        "task_status": task_status,
    }


def detail_payload(*objects, tasks=None, file_id="of_1", duration=0):
    """A ``files/detail`` reply: file node on_f_1 in container on_w_1, shown in folder on_fd_1."""
    return ok(
        {
            "node": {
                "node_id": "on_f_1",
                "name": "Meeting",
                "version_ms": 100,
                "folder": {"folder_id": "on_w_1"},
                "display_folder": {"folder_id": "on_fd_1"},
            },
            "meta": {"file_id": file_id, "start_time": 1_750_000_000_000, "duration": duration},
            "objects": list(objects),
            "task": {"in_progress_tasks": tasks or []},
        }
    )


def filed_routes(*, folder_items=None):
    """Routes every recording write needs: detail of of_1 plus its container's version (200)."""
    return {
        f"GET {V4}/files/detail/of_1": detail_payload(),
        f"GET {V4}/nodes/ancestor-items": ok({"flat_items": [{"node_id": "on_w_1", "version_ms": 200}]}),
        f"GET {V4}/folders/children": children(
            folder_items or {"root": [{"node_id": "on_sys_rec", "type": 1, "system_folder_type": 1}]}
        ),
    }


def transcript_routes(segments, *, summary=None):
    """Routes for reading and writing the transcript (and optionally summary) of of_1."""
    objects = [obj("TRANSCRIPT", "https://s3.fake/t.json")]
    routes = {
        "GET https://s3.fake/t.json": json_response(segments),
        f"PATCH {V4}/files/update/content/of_1": ok(),
    }
    if summary is not None:
        objects.append(
            obj("SUMMARY", "https://s3.fake/s.md", object_id="o2", content_id="c2", version_ms=600)
        )
        routes["GET https://s3.fake/s.md"] = HttpResponse(200, summary.encode(), {})
    routes[f"GET {V4}/files/detail/of_1"] = detail_payload(*objects)
    return routes


SEGMENTS = [
    {"speaker": "Kadin Bullock", "original_speaker": "Speaker 1", "content": "Flourish Cache account"},
    {"speaker": "Speaker 2", "original_speaker": "Speaker 2", "content": "no match here"},
    {"speaker": "Kadin Bullock", "original_speaker": "Speaker 1", "content": "Cache and Cache"},
]


# ---------------------------------------------------------------------------
# Ids
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("given", "expected"),
    [
        ("ab" * 16, "of_" + "ab" * 16),  # pre-4.0 bare hex
        ("of_" + "ab" * 16, "of_" + "ab" * 16),
        ("f_" + "cd" * 16, "f_" + "cd" * 16),
        ("on_f_" + "ab" * 16, "of_" + "ab" * 16),  # migrated file node
        ("on_w_" + "ab" * 16, "of_" + "ab" * 16),  # migrated container
    ],
)
def test_to_file_id_prefixes_bare_hex_and_passes_prefixed_ids_through(given, expected):
    assert to_file_id(given) == expected


# ---------------------------------------------------------------------------
# Listing
# ---------------------------------------------------------------------------


def test_list_recordings_maps_v4_items_and_sends_browser_headers(tmp_path):
    item = {
        "file_id": "of_1",
        "name": "Weekly",
        "created_at_show_ms": 1000,
        "duration_ms": 2000,
        "node_id": "on_f_1",
        "parent_id": "on_w_1",
        "parent_folder": {"folder_id": "on_fd_1"},
        "version_ms": 9,
    }
    client, transport = routed(tmp_path, {f"GET {V4}/recordings/all": ok({"items": [item]})})
    (rec,) = client.list_recordings()
    assert (rec.id, rec.filename, rec.start_time, rec.duration) == ("of_1", "Weekly", 1000, 2000)
    assert (rec.folder_id, rec.node_id, rec.wrapper_id, rec.version_ms) == ("on_fd_1", "on_f_1", "on_w_1", 9)
    headers = transport.calls[0]["headers"]
    assert headers["app-platform"] == "web"
    assert headers["edit-from"] == "web"
    assert headers["User-Agent"].startswith("Mozilla/5.0")
    assert headers["x-timezone-iana"]


def test_list_recordings_follows_the_cursor_across_pages(tmp_path):
    def page(ids, cursor=None):
        return ok({"items": [{"file_id": i, "name": i} for i in ids], "next_cursor": cursor})

    client, transport = routed(
        tmp_path, {f"GET {V4}/recordings/all": [page(["of_1", "of_2"], "c2"), page(["f_3"])]}
    )
    assert [r.id for r in client.list_recordings()] == ["of_1", "of_2", "f_3"]
    assert "cursor" not in transport.calls[0]["url"]
    assert "cursor=c2" in transport.calls[1]["url"]


def test_list_recordings_window_stops_walking_once_satisfied(tmp_path):
    items = [{"file_id": f"of_{i}", "name": "x"} for i in range(5)]
    client, transport = routed(
        tmp_path, {f"GET {V4}/recordings/all": ok({"items": items[:3], "next_cursor": "more"})}
    )
    assert [r.id for r in client.list_recordings(PlaudRecordingQuery(skip=1, limit=2))] == ["of_1", "of_2"]
    assert len(transport.calls) == 1
    assert "page_size=3" in transport.calls[0]["url"]


def test_list_trash_maps_every_kind_of_trashed_item(tmp_path):
    trash = [
        {"node_id": "on_w_9", "type": 1, "name": "Migrated", "version_ms": 7, "duration_ms": 1000},
        {"node_id": "n_c1", "type": 1, "name": "New", "version_ms": 8},
        {"node_id": "on_fd_2", "type": 1, "icon": "e627", "name": "A folder"},
        {"type": 2, "file_id": "of_5", "node_id": "on_f_5", "name": "Loose", "version_ms": 3},
    ]
    client, _ = routed(
        tmp_path,
        {
            f"GET {V4}/folders/content": ok({"items": trash}),
            # A note can come before the recording in its container.
            f"GET {V4}/folders/children": children(
                {
                    "n_c1": [
                        {"file_id": "of_n_1", "file_type": "MD"},
                        {"file_id": "f_new1", "file_type": "REC"},
                    ]
                }
            ),
        },
    )
    migrated, new, loose = client.list_trash()
    # A migrated container names its recording; a new one costs a lookup; folders are skipped.
    assert (migrated.id, migrated.wrapper_id, migrated.version_ms) == ("of_9", "on_w_9", 7)
    assert (new.id, new.wrapper_id) == ("f_new1", "n_c1")
    assert (loose.id, loose.node_id, loose.wrapper_id) == ("of_5", "on_f_5", "")


# ---------------------------------------------------------------------------
# Reading one recording
# ---------------------------------------------------------------------------


def test_get_recording_reads_gzip_transcript_and_summary_from_content_urls(tmp_path):
    segments = [
        {"speaker": "Alex", "original_speaker": "Speaker 1", "content": "Hi"},
        {"speaker": "", "original_speaker": "Speaker 2", "content": "Hello"},
    ]
    routes = transcript_routes(segments, summary="# Summary")
    routes[f"GET {V4}/folders/children"] = children({"on_w_1": []})
    routes["GET https://s3.fake/t.json"] = HttpResponse(
        200, gzip.compress(json.dumps(segments).encode()), {"content-encoding": "gzip"}
    )
    client, transport = routed(tmp_path, routes)
    detail = client.get_recording("of_1", include_transcript=True, include_summary=True)
    assert detail.transcript == "Alex: Hi\n\nSpeaker 2: Hello"
    assert detail.speakers == ["Alex", "Speaker 2"]
    assert detail.ai_content == "# Summary"
    assert detail.is_trans and detail.is_summary
    assert (detail.id, detail.folder_id, detail.node_id, detail.wrapper_id, detail.version_ms) == (
        "of_1",
        "on_fd_1",
        "on_f_1",
        "on_w_1",
        100,
    )


NOTE_LINK = "[](plaud://source?type=summary&fileId=f_1&start_time=125370&end_time=131290)"


def note_routes(summary="# Summary"):
    """of_1 with one MD note and one HIGHLIGHTS note next to it in container on_w_1."""
    routes = transcript_routes(SEGMENTS, summary=summary)
    routes[f"GET {V4}/folders/children"] = children(
        {
            "on_w_1": [
                {"file_id": "of_1", "file_type": "REC"},
                {"file_id": "of_n1", "file_type": "MD", "name": "Meeting - Minutes"},
                {"file_id": "of_n2", "file_type": "MD", "name": "Meeting - Highlights"},
            ]
        }
    )
    routes[f"GET {V4}/files/detail/of_n1"] = detail_payload(
        obj("MD", "https://s3.fake/n1.md", object_id="on1", content_id="cn1", version_ms=700)
    )
    routes[f"GET {V4}/files/detail/of_n2"] = detail_payload(obj("HIGHLIGHTS", "https://s3.fake/n2.md"))
    routes["GET https://s3.fake/n1.md"] = HttpResponse(200, f"Budget agreed {NOTE_LINK}".encode(), {})
    routes["GET https://s3.fake/n2.md"] = HttpResponse(200, b"# Highlights", {})
    routes[f"PATCH {V4}/files/update/content/of_n1"] = ok()
    return routes


def test_get_recording_with_summary_returns_the_containers_notes(tmp_path):
    client, _ = routed(tmp_path, note_routes())
    detail = client.get_recording("of_1", include_summary=True)
    assert [(n.note_id, n.name, n.content) for n in detail.notes] == [
        ("of_n1", "Meeting - Minutes", f"Budget agreed {NOTE_LINK}"),
        ("of_n2", "Meeting - Highlights", "# Highlights"),
    ]


def test_notes_are_found_through_the_parent_folder_when_the_detail_names_no_container(tmp_path):
    routes = note_routes()
    # Same file node, but the detail carries only parent_id (no separate container folder).
    routes[f"GET {V4}/files/detail/of_1"] = ok(
        {
            "node": {"node_id": "on_f_1", "parent_id": "on_w_1", "name": "Meeting", "version_ms": 100},
            "meta": {"file_id": "of_1"},
            "objects": [],
            "task": {"in_progress_tasks": []},
        }
    )
    client, _ = routed(tmp_path, routes)
    detail = client.get_recording("of_1", include_summary=True)
    assert [n.note_id for n in detail.notes] == ["of_n1", "of_n2"]


def test_correct_summary_on_a_note_edits_the_raw_note_and_keeps_its_links(tmp_path):
    client, transport = routed(tmp_path, note_routes())
    assert client.correct_summary("of_1", "Budget", "Plan", note_id="of_n1") == {"replacements": 1}
    (body,) = transport.sent(f"PATCH {V4}/files/update/content/of_n1")
    assert body == {
        "object_id": "on1",
        "content_id": "cn1",
        "content": f"Plan agreed {NOTE_LINK}",
        "origin_version": 700,
    }


def test_editing_a_note_of_another_recording_is_refused(tmp_path):
    client, transport = routed(tmp_path, note_routes())
    with pytest.raises(ValueError, match="is not a note of recording of_1"):
        client.set_summary("of_1", "# x", note_id="of_other")
    assert not transport.hit(f"PATCH {V4}/files/update/content/of_other")


def test_get_recording_sends_a_bare_hex_id_as_a_4_0_file_id(tmp_path):
    hex_id = "ab" * 16
    client, transport = routed(tmp_path, {f"GET {V4}/files/detail/of_{hex_id}": detail_payload()})
    client.get_recording(hex_id)
    assert transport.calls[0]["url"].endswith(f"{V4}/files/detail/of_{hex_id}")


def test_get_recording_fetches_nothing_extra_unless_asked(tmp_path):
    client, transport = routed(tmp_path, transcript_routes(SEGMENTS, summary="# Summary"))
    detail = client.get_recording("of_1")
    assert detail.is_summary and detail.ai_content is None and detail.speakers == []
    assert len(transport.calls) == 1


class TestTranscriptBlockSelection:
    """get_recording must fetch the requested transcript block, not always the raw one."""

    @staticmethod
    def routes():
        polished = [{"speaker": "Speaker 1", "content": "Polished."}]
        return {
            f"GET {V4}/files/detail/of_1": detail_payload(
                obj("TRANSCRIPT", "https://s3.fake/raw.json", task_status=1),
                obj("POLISHED_TRANSCRIPT", "https://s3.fake/polish.json", object_id="o3", task_status=1),
            ),
            "GET https://s3.fake/raw.json": json_response([{"speaker": "Speaker 1", "content": "um, raw"}]),
            "GET https://s3.fake/polish.json": json_response(polished),
            f"PATCH {V4}/files/update/content/of_1": ok(),
        }

    def test_default_is_raw_and_lists_both_blocks(self, tmp_path):
        client, transport = routed(tmp_path, self.routes())
        detail = client.get_recording("of_1", include_transcript=True)
        assert "um, raw" in detail.transcript
        assert not any("polish" in c["url"] for c in transport.calls)
        assert detail.transcript_blocks_available == ["transaction", "transaction_polish"]

    def test_polish_block_reads_the_polished_object(self, tmp_path):
        client, _ = routed(tmp_path, self.routes())
        detail = client.get_recording("of_1", include_transcript=True, transcript_block="transaction_polish")
        assert detail.transcript == "Speaker 1: Polished."

    def test_missing_block_is_empty_but_still_lists_available(self, tmp_path):
        routes = self.routes()
        routes[f"GET {V4}/files/detail/of_1"] = detail_payload(obj("TRANSCRIPT", "https://s3.fake/raw.json"))
        client, _ = routed(tmp_path, routes)
        detail = client.get_recording("of_1", include_transcript=True, transcript_block="transaction_polish")
        assert detail.transcript == ""
        assert detail.transcript_blocks_available == ["transaction"]

    def test_unknown_block_is_rejected_before_any_request(self, tmp_path):
        client, transport = routed(tmp_path, {})
        with pytest.raises(ValueError, match="transcript_block"):
            client.get_recording("of_1", include_transcript=True, transcript_block="outline")
        assert transport.calls == []

    def test_edits_always_read_the_raw_block(self, tmp_path):
        """Writing polished text back over the raw transcript would be silent data loss."""
        client, transport = routed(tmp_path, self.routes())
        client.rename_speaker("of_1", "Speaker 1", "Kadin")
        assert not any("polish" in c["url"] for c in transport.calls)


def test_finished_transcript_that_fails_to_download_raises_instead_of_reading_empty(tmp_path):
    """#210: a finished block that will not download is not an empty transcript."""
    routes = transcript_routes(SEGMENTS)
    routes["GET https://s3.fake/t.json"] = HttpResponse(403, b"<Error>AccessDenied</Error>", {})
    client, _ = routed(tmp_path, routes)
    with pytest.raises(PlaudApiError) as exc_info:
        client.get_recording("of_1", include_transcript=True)
    assert exc_info.value.http_status == 403


def test_transcript_that_is_not_json_raises_api_error(tmp_path):
    routes = transcript_routes(SEGMENTS)
    routes["GET https://s3.fake/t.json"] = HttpResponse(200, b"not json at all", {})
    client, _ = routed(tmp_path, routes)
    with pytest.raises(PlaudApiError, match="not JSON"):
        client.get_recording("of_1", include_transcript=True)


def test_get_task_status_maps_in_progress_tasks(tmp_path):
    task = {"task_id": "t1", "task_type": "trans", "task_stage": [{"code": "TRANSCRIPT"}]}
    client, _ = routed(tmp_path, {f"GET {V4}/files/detail/of_1": detail_payload(tasks=[task])})
    (status,) = client.get_task_status("of_1")
    assert (status.file_id, status.task_id, status.task_type) == ("of_1", "t1", "trans")
    assert status.stages == [{"code": "TRANSCRIPT"}]
    client, _ = routed(tmp_path, {f"GET {V4}/files/detail/of_1": detail_payload()})
    assert client.get_task_status("of_1") == []


# ---------------------------------------------------------------------------
# Audio
# ---------------------------------------------------------------------------


def test_get_audio_link_returns_the_audio_objects_presigned_url(tmp_path):
    audio = obj("AUDIO", "https://s3.fake/a.ogg?sig=x")
    audio["mime_type"] = "audio/ogg"
    client, _ = routed(tmp_path, {f"GET {V4}/files/detail/of_1": detail_payload(audio)})
    link = client.get_audio_link("of_1")
    assert link == AudioLink(url="https://s3.fake/a.ogg?sig=x", mime_type="audio/ogg")
    assert link.extension == ".ogg"


def test_get_audio_link_is_none_without_audio(tmp_path):
    client, _ = routed(tmp_path, {f"GET {V4}/files/detail/of_1": detail_payload()})
    assert client.get_audio_link("of_1") is None


def test_download_audio_writes_bytes_without_a_plaud_auth_header(tmp_path):
    client, transport = routed(tmp_path, {"GET https://s3.fake/a.mp3": HttpResponse(200, b"ID3fake", {})})
    destination = tmp_path / "out" / "rec.mp3"
    assert client.download_audio(AudioLink(url="https://s3.fake/a.mp3"), destination) == destination
    assert destination.read_bytes() == b"ID3fake"
    # The signature is in the URL; sending Authorization can make S3 reject the request.
    assert "Authorization" not in transport.calls[0]["headers"]


def test_download_audio_propagates_s3_failure(tmp_path):
    client, _ = routed(tmp_path, {"GET https://s3.fake/a.mp3": HttpResponse(403, b"expired", {})})
    with pytest.raises(PlaudApiError) as exc_info:
        client.download_audio(AudioLink(url="https://s3.fake/a.mp3"), tmp_path / "rec.mp3")
    assert exc_info.value.http_status == 403


# ---------------------------------------------------------------------------
# Search
# ---------------------------------------------------------------------------


def test_search_content_maps_hit_ids_to_file_ids_and_flattens_hits(tmp_path):
    hex_id = "ab" * 16
    response = ok(
        {
            "query": "rollover",
            "keywords": ["rollover", "rol"],
            "list": [
                {
                    "id": hex_id,  # pre-4.0 bare hex
                    "title": "Transcript hit",
                    "start_time": 1_750_000_000_000,
                    "trans_chunks": [
                        {
                            "content": "[Advisor]Let's plan the rollover.\n[Client]Sounds good.",
                            "offset": 0,
                            "speakers": [
                                {"name": "Advisor", "start_time": 61_000, "end_time": 64_000},
                                {"name": "Client", "start_time": 64_500, "end_time": 65_000},
                            ],
                        }
                    ],
                },
                {
                    "id": "n_c1",  # a 4.0 recording is named by its container folder
                    "title": "Summary hit",
                    "start_time": 1_740_000_000_000,
                    "notes": [
                        {"id": "auto_sum:x", "chunks": [{"content": "ext rollover next steps", "offset": 90}]}
                    ],
                },
            ],
        }
    )
    client, transport = routed(
        tmp_path,
        {
            "POST /gsearch/v1/search": response,
            f"GET {V4}/folders/children": children({"n_c1": [{"file_id": "f_new1"}]}),
        },
    )
    transcript, summary = client.search_content("rollover", since_ms=1, until_ms=2)
    assert transport.sent("POST /gsearch/v1/search") == [
        {"query": "rollover", "date_from": 1, "date_to": 2, "from": ""}
    ]
    assert (transcript.id, transcript.source, transcript.start_ms) == ("of_" + hex_id, "transcript", 61_000)
    assert transcript.snippet == "[Advisor]Let's plan the rollover. [Client]Sounds good."
    assert (summary.id, summary.source, summary.start_ms) == ("f_new1", "summary", None)
    assert summary.snippet == "…rollover next steps"


def test_search_content_with_no_hits_returns_empty_list(tmp_path):
    client, _ = routed(tmp_path, {"POST /gsearch/v1/search": ok({"query": "x", "keywords": ["x"]})})
    assert client.search_content("x") == []


# ---------------------------------------------------------------------------
# Recording writes
# ---------------------------------------------------------------------------


def test_rename_recording_renames_the_file_node_and_its_container(tmp_path):
    routes = filed_routes() | {
        f"PATCH {V4}/nodes/rename/on_f_1": ok(),
        f"PATCH {V4}/nodes/rename/on_w_1": ok(),
    }
    client, transport = routed(tmp_path, routes)
    client.rename_recording("of_1", "New Name")
    assert transport.sent(f"PATCH {V4}/nodes/rename/on_f_1") == [{"name": "New Name", "origin_version": 100}]
    assert transport.sent(f"PATCH {V4}/nodes/rename/on_w_1") == [{"name": "New Name", "origin_version": 200}]


def test_rename_recording_rejects_blank_name(tmp_path):
    client, _ = routed(tmp_path, {})
    with pytest.raises(ValueError, match="filename cannot be empty"):
        client.rename_recording("of_1", "   ")


def test_set_recording_folder_moves_the_container_into_recordings_when_unfiling(tmp_path):
    client, transport = routed(tmp_path, filed_routes() | {f"POST {V4}/nodes/move": ok({"items": []})})
    client.set_recording_folder("of_1", None)
    assert transport.sent(f"POST {V4}/nodes/move") == [
        {"target_node_id": "on_sys_rec", "items": [{"node_id": "on_w_1", "origin_version": 200}]}
    ]


def test_move_to_trash_sends_container_and_its_version(tmp_path):
    client, transport = routed(tmp_path, filed_routes() | {f"DELETE {V4}/nodes/trash": ok({"items": []})})
    client.move_to_trash("of_1")
    assert transport.sent(f"DELETE {V4}/nodes/trash") == [
        {"items": [{"node_id": "on_w_1", "origin_version": 200}]}
    ]


def test_delete_recordings_trashes_a_live_recording_first_then_deletes_it(tmp_path):
    in_trash = ok({"items": [{"node_id": "on_w_1", "type": 1, "name": "Meeting", "version_ms": 300}]})
    routes = filed_routes() | {
        f"GET {V4}/folders/content": [EMPTY_LIST, in_trash],
        f"DELETE {V4}/nodes/trash": ok({"items": []}),
        f"DELETE {V4}/nodes/delete": ok({"items": []}),
    }
    client, transport = routed(tmp_path, routes)
    client.delete_recordings("of_1")
    assert transport.hit(f"DELETE {V4}/nodes/trash")
    assert transport.sent(f"DELETE {V4}/nodes/delete") == [
        {"items": [{"node_id": "on_w_1", "origin_version": 300}]}
    ]


def test_restore_rejects_a_recording_that_is_not_in_the_trash(tmp_path):
    client, _ = routed(tmp_path, {f"GET {V4}/folders/content": EMPTY_LIST})
    with pytest.raises(ValueError, match="not in the trash"):
        client.restore_from_trash("of_1")


@pytest.mark.parametrize("method", ["move_to_trash", "restore_from_trash", "delete_recordings"])
def test_batch_writes_reject_empty_ids(tmp_path, method):
    client, _ = routed(tmp_path, {})
    with pytest.raises(ValueError, match="recording_ids cannot be empty"):
        getattr(client, method)([])


def test_node_batch_raises_when_plaud_says_status_0_but_lists_failed_items(tmp_path):
    failed = {
        "node_id": "on_w_1",
        "name": "Meeting",
        "error_code": "-1800313",
        "error_message": "stale version",
    }
    client, _ = routed(tmp_path, filed_routes() | {f"DELETE {V4}/nodes/trash": ok({"failed": [failed]})})
    with pytest.raises(PlaudApiError, match="trash failed for 1 item.*Meeting: stale version") as info:
        client.move_to_trash("of_1")
    assert info.value.plaud_code == -1800313


# ---------------------------------------------------------------------------
# Transcript and summary edits
# ---------------------------------------------------------------------------


def written_segments(transport):
    (body,) = transport.sent(f"PATCH {V4}/files/update/content/of_1")
    return body, json.loads(body["content"])


def test_rename_speaker_writes_back_the_whole_transcript_with_its_version(tmp_path):
    client, transport = routed(tmp_path, transcript_routes(SEGMENTS))
    assert client.rename_speaker("of_1", "Speaker 1", "Advisor") == {"segments_updated": 2}
    body, written = written_segments(transport)
    assert (body["object_id"], body["content_id"], body["origin_version"]) == ("o1", "c1", 500)
    assert body["has_replaced_speaker"] is True
    assert [s["speaker"] for s in written] == ["Advisor", "Speaker 2", "Advisor"]
    assert written[0]["original_speaker"] == "Speaker 1"


def test_rename_speaker_matches_the_displayed_name_too(tmp_path):
    """Plaud auto-resolves enrolled voices, so the displayed name can differ from "Speaker N"."""
    client, transport = routed(tmp_path, transcript_routes(SEGMENTS))
    assert client.rename_speaker("of_1", "Kadin Bullock", "Advisor") == {"segments_updated": 2}
    assert [s["speaker"] for s in written_segments(transport)[1]] == ["Advisor", "Speaker 2", "Advisor"]


def test_rename_speaker_rejects_unknown_label_blank_inputs_and_missing_transcript(tmp_path):
    client, transport = routed(tmp_path, transcript_routes(SEGMENTS))
    with pytest.raises(ValueError, match='no segments found for speaker "Speaker 9"'):
        client.rename_speaker("of_1", "Speaker 9", "Alex")
    with pytest.raises(ValueError, match="original_label cannot be empty"):
        client.rename_speaker("of_1", "", "Alex")
    with pytest.raises(ValueError, match="new_name cannot be empty"):
        client.rename_speaker("of_1", "Speaker 1", "   ")
    assert not transport.hit(f"PATCH {V4}/files/update/content/of_1")

    client, _ = routed(tmp_path, {f"GET {V4}/files/detail/of_1": detail_payload()})
    with pytest.raises(ValueError, match="has no transcript yet"):
        client.rename_speaker("of_1", "Speaker 1", "Alex")


def test_correct_transcript_replaces_content_only_and_leaves_speakers(tmp_path):
    client, transport = routed(tmp_path, transcript_routes(SEGMENTS))
    assert client.correct_transcript("of_1", "Cache", "Cash") == {"replacements": 3, "segments_changed": 2}
    body, written = written_segments(transport)
    assert "has_replaced_speaker" not in body
    assert [s["content"] for s in written] == ["Flourish Cash account", "no match here", "Cash and Cash"]
    assert written[0]["speaker"] == "Kadin Bullock"


def test_correct_transcript_rejects_empty_find_and_no_match(tmp_path):
    client, _ = routed(tmp_path, transcript_routes(SEGMENTS))
    with pytest.raises(ValueError, match="find text cannot be empty"):
        client.correct_transcript("of_1", "", "x")
    with pytest.raises(ValueError, match='no occurrences of "zzz" found'):
        client.correct_transcript("of_1", "zzz", "x")


def test_count_transcript_matches_counts_content_not_speaker_labels(tmp_path):
    """The dry-run count must equal what correct_transcript would replace."""
    segments = [
        {"content": "Ann said hi to Ann", "speaker": "Ann", "original_speaker": "Speaker 1"},
        {"content": "bye", "speaker": "Ann", "original_speaker": "Speaker 1"},
    ]
    client, transport = routed(tmp_path, transcript_routes(segments))
    assert client.count_transcript_matches("of_1", "Ann") == 2
    assert not transport.hit(f"PATCH {V4}/files/update/content/of_1")


def test_correct_summary_replaces_text_and_writes_the_summary_object(tmp_path):
    client, transport = routed(tmp_path, transcript_routes(SEGMENTS, summary="Suzan met with Suzan again."))
    assert client.correct_summary("of_1", "Suzan", "Susan") == {"replacements": 2}
    (body,) = transport.sent(f"PATCH {V4}/files/update/content/of_1")
    assert body == {
        "object_id": "o2",
        "content_id": "c2",
        "content": "Susan met with Susan again.",
        "origin_version": 600,
    }


def test_correct_summary_rejects_empty_find_no_match_and_missing_summary(tmp_path):
    client, _ = routed(tmp_path, transcript_routes(SEGMENTS, summary="nothing to change"))
    with pytest.raises(ValueError, match="find text cannot be empty"):
        client.correct_summary("of_1", "", "x")
    with pytest.raises(ValueError, match='no occurrences of "zzz" found in summary'):
        client.correct_summary("of_1", "zzz", "x")

    client, _ = routed(tmp_path, transcript_routes(SEGMENTS))
    with pytest.raises(ValueError, match="has no summary yet"):
        client.correct_summary("of_1", "a", "b")


def test_set_summary_overwrites_full_content_and_rejects_blank(tmp_path):
    client, transport = routed(tmp_path, transcript_routes(SEGMENTS, summary="old summary"))
    client.set_summary("of_1", "# Brand New\n\n- point")
    assert transport.sent(f"PATCH {V4}/files/update/content/of_1")[0]["content"] == "# Brand New\n\n- point"
    with pytest.raises(ValueError, match="summary content cannot be empty"):
        client.set_summary("of_1", "   ")


# ---------------------------------------------------------------------------
# Folders
# ---------------------------------------------------------------------------


def folder_node(node_id, name, *, version_ms=100, **extra):
    return {"node_id": node_id, "type": 1, "name": name, "icon": "e627", "version_ms": version_ms, **extra}


def test_list_folders_walks_the_tree_and_skips_recording_containers(tmp_path):
    client, _ = routed(
        tmp_path,
        {
            f"GET {V4}/folders/children": children(
                {
                    "root": [
                        folder_node("on_fd_a", "A"),
                        {"node_id": "on_sys", "type": 1, "system_folder_type": 1},
                    ],
                    "on_fd_a": [
                        folder_node("on_fd_b", "B", parent_id="on_fd_a"),
                        {"node_id": "on_w_1", "type": 1},
                    ],
                    "on_sys": [folder_node("on_fd_c", "C")],
                }
            )
        },
    )
    assert [f.name for f in client.list_folders()] == ["A", "B", "C"]


def test_create_folder_posts_name_icon_color_parent_and_defaults(tmp_path):
    reply = ok({"items": [folder_node("on_fd_9", "Clients", color="#f9a251", icon="e708")]})
    client, transport = routed(tmp_path, {f"POST {V4}/nodes/folder": reply})
    folder = client.create_folder("Clients", color="#f9a251", icon="e708", parent_id="on_fd_1")
    assert (folder.id, folder.name, folder.color) == ("on_fd_9", "Clients", "#f9a251")
    client.create_folder("Plain")
    first, second = transport.sent(f"POST {V4}/nodes/folder")
    assert first == {"parent_node_id": "on_fd_1", "name": "Clients", "icon": "e708", "color": "#f9a251"}
    assert second == {
        "parent_node_id": "root",
        "name": "Plain",
        "icon": client_mod._DEFAULT_FOLDER_ICON,
        "color": client_mod._DEFAULT_FOLDER_COLOR,
    }


def test_folder_writes_reject_bad_input(tmp_path):
    client, _ = routed(tmp_path, {})
    with pytest.raises(ValueError, match="folder name cannot be empty"):
        client.create_folder("   ")
    with pytest.raises(ValueError, match="at least one of name, color, icon"):
        client.update_folder("on_fd_1")
    with pytest.raises(ValueError, match="folder_id cannot be empty"):
        client.update_folder("", name="X")
    with pytest.raises(ValueError, match="folder_id cannot be empty"):
        client.delete_folder("")


def test_update_folder_keeps_current_values_and_sends_its_version(tmp_path):
    current = folder_node("on_fd_1", "Old", version_ms=100, color="#111111")
    renamed = folder_node("on_fd_1", "Renamed", version_ms=110, color="#111111")
    client, transport = routed(
        tmp_path,
        {
            f"GET {V4}/folders/children": children({"root": [current]}),
            f"PATCH {V4}/nodes/rename/on_fd_1": ok({"items": [renamed]}),
        },
    )
    assert client.update_folder("on_fd_1", name="Renamed").name == "Renamed"
    assert transport.sent(f"PATCH {V4}/nodes/rename/on_fd_1") == [
        {"name": "Renamed", "color": "#111111", "icon": "e627", "origin_version": 100}
    ]


def test_update_folder_unknown_id_raises(tmp_path):
    client, _ = routed(tmp_path, {f"GET {V4}/folders/children": children({})})
    with pytest.raises(ValueError, match="no folder with id"):
        client.update_folder("on_fd_x", name="X")


def test_move_folder_sends_its_version_and_returns_the_new_one(tmp_path):
    client, transport = routed(
        tmp_path,
        {
            f"GET {V4}/folders/children": children(
                {"root": [folder_node("on_fd_1", "Prospects", version_ms=100)]}
            ),
            # The move reply carries only the id and new version.
            f"POST {V4}/nodes/move": ok({"items": [{"node_id": "on_fd_1", "version_ms": 120}]}),
        },
    )
    moved = client.move_folder("on_fd_1", "on_fd_2")
    assert (moved.name, moved.parent_id, moved.version_ms) == ("Prospects", "on_fd_2", 120)
    assert transport.sent(f"POST {V4}/nodes/move") == [
        {"target_node_id": "on_fd_2", "items": [{"node_id": "on_fd_1", "origin_version": 100}]}
    ]
    with pytest.raises(ValueError, match='pass "root"'):
        client.move_folder("on_fd_1", "")


def test_nest_colon_folders_creates_the_parent_then_moves_and_renames(tmp_path):
    folders = [
        Folder(
            id="on_fd_1",
            name="Clients:Prospects",
            color="#111111",
            icon="e627",
            parent_id="root",
            version_ms=100,
        ),
        Folder(
            id="on_fd_2",
            name="Clients:Active",
            color="#222222",
            icon="e627",
            parent_id="root",
            version_ms=200,
        ),
    ]
    client, transport = routed(
        tmp_path,
        {
            f"POST {V4}/nodes/folder": ok({"items": [folder_node("on_fd_9", "Clients")]}),
            f"POST {V4}/nodes/move": [
                ok({"items": [{"node_id": "on_fd_1", "version_ms": 300}]}),
                ok({"items": [{"node_id": "on_fd_2", "version_ms": 400}]}),
            ],
            f"PATCH {V4}/nodes/rename/on_fd_1": ok({"items": [folder_node("on_fd_1", "Prospects")]}),
            f"PATCH {V4}/nodes/rename/on_fd_2": ok({"items": [folder_node("on_fd_2", "Active")]}),
        },
    )
    client.nest_colon_folders(plan_colon_nesting(folders))
    assert [b["name"] for b in transport.sent(f"POST {V4}/nodes/folder")] == ["Clients"]
    assert transport.sent(f"POST {V4}/nodes/move") == [
        {"target_node_id": "on_fd_9", "items": [{"node_id": "on_fd_1", "origin_version": 100}]},
        {"target_node_id": "on_fd_9", "items": [{"node_id": "on_fd_2", "origin_version": 200}]},
    ]
    # Renamed after the move, with the version the move returned; color and icon kept.
    assert transport.sent(f"PATCH {V4}/nodes/rename/on_fd_1") == [
        {"name": "Prospects", "color": "#111111", "icon": "e627", "origin_version": 300}
    ]
    assert transport.sent(f"PATCH {V4}/nodes/rename/on_fd_2")[0]["name"] == "Active"


def test_delete_folder_moves_children_to_recordings_then_trashes_and_deletes(tmp_path):
    routes = {
        f"GET {V4}/folders/children": children(
            {
                "root": [
                    folder_node("on_fd_1", "Clients", version_ms=100),
                    {"node_id": "on_sys_rec", "type": 1, "system_folder_type": 1},
                ],
                "on_fd_1": [
                    {"node_id": "on_w_a", "version_ms": 150},
                    {"node_id": "on_w_b", "version_ms": 160},
                ],
            }
        ),
        # The move bumps the folder's own version, which a fresh listing may not show yet.
        f"POST {V4}/nodes/move": ok(
            {"items": [{"node_id": "on_w_a", "version_ms": 900}, {"node_id": "on_fd_1", "version_ms": 950}]}
        ),
        f"DELETE {V4}/nodes/trash": ok({"items": [{"node_id": "on_fd_1", "version_ms": 1000}]}),
        f"DELETE {V4}/nodes/delete": ok({"items": []}),
    }
    client, transport = routed(tmp_path, routes)
    client.delete_folder("on_fd_1")
    assert transport.sent(f"POST {V4}/nodes/move") == [
        {
            "target_node_id": "on_sys_rec",
            "items": [
                {"node_id": "on_w_a", "origin_version": 150},
                {"node_id": "on_w_b", "origin_version": 160},
            ],
        }
    ]
    # Trash with the highest version the move reported; delete with the version the trash reported.
    assert transport.sent(f"DELETE {V4}/nodes/trash") == [
        {"items": [{"node_id": "on_fd_1", "origin_version": 950}]}
    ]
    assert transport.sent(f"DELETE {V4}/nodes/delete") == [
        {"items": [{"node_id": "on_fd_1", "origin_version": 1000}]}
    ]


def test_delete_folder_moves_subfolders_up_not_into_recordings(tmp_path):
    sub = folder_node("on_fd_2", "Sub", version_ms=170, parent_id="on_fd_1")
    routes = {
        f"GET {V4}/folders/children": children(
            {
                "root": [
                    folder_node("on_fd_1", "Clients", version_ms=100, parent_id="root"),
                    {"node_id": "on_sys_rec", "type": 1, "system_folder_type": 1},
                ],
                "on_fd_1": [{"node_id": "on_w_a", "version_ms": 150}, sub],
                "on_fd_2": [{"node_id": "on_w_b", "version_ms": 180}],
            }
        ),
        f"POST {V4}/nodes/move": ok({"items": []}),
        f"DELETE {V4}/nodes/trash": ok({"items": []}),
        f"DELETE {V4}/nodes/delete": ok({"items": []}),
    }
    client, transport = routed(tmp_path, routes)
    # Top level: the subfolder goes to the top level, the recording to Recordings.
    client.delete_folder("on_fd_1")
    assert [
        (m["target_node_id"], [i["node_id"] for i in m["items"]])
        for m in transport.sent(f"POST {V4}/nodes/move")
    ] == [("root", ["on_fd_2"]), ("on_sys_rec", ["on_w_a"])]

    # Nested: everything goes to the parent folder.
    client, transport = routed(tmp_path, routes)
    client.delete_folder("on_fd_2")
    assert [m["target_node_id"] for m in transport.sent(f"POST {V4}/nodes/move")] == ["on_fd_1"]


def test_delete_parent_right_after_its_child_sends_the_version_the_child_delete_reported(tmp_path):
    # Live: trashing a child bumps its parent's version (reply parent_items),
    # but the root listing still shows the old one for about a second.
    parent_bump = {"node_id": "on_fd_p", "version_ms": 200}
    routes = {
        f"GET {V4}/folders/children": children(
            {
                "root": [folder_node("on_fd_p", "Parent", version_ms=100)],
                "on_fd_p": [folder_node("on_fd_c", "Child", version_ms=110, parent_id="on_fd_p")],
            }
        ),
        f"POST {V4}/nodes/move": ok({"items": []}),
        f"DELETE {V4}/nodes/trash": [
            ok({"items": [{"node_id": "on_fd_c", "version_ms": 200}], "parent_items": [parent_bump]}),
            ok({"items": [{"node_id": "on_fd_p", "version_ms": 300}]}),
        ],
        f"DELETE {V4}/nodes/delete": ok({"items": []}),
    }
    client, _ = routed(tmp_path, routes)
    client.delete_folder("on_fd_c")
    # A new client, as each MCP call gets, over the same stale listing.
    client, transport = routed(tmp_path, routes)
    client.delete_folder("on_fd_p")
    assert transport.sent(f"DELETE {V4}/nodes/trash") == [
        {"items": [{"node_id": "on_fd_p", "origin_version": 200}]}
    ]


def test_delete_empty_folder_skips_the_move(tmp_path):
    routes = {
        f"GET {V4}/folders/children": children({"root": [folder_node("on_fd_1", "Empty", version_ms=100)]}),
        f"DELETE {V4}/nodes/trash": ok({"items": []}),
        f"DELETE {V4}/nodes/delete": ok({"items": []}),
    }
    client, transport = routed(tmp_path, routes)
    client.delete_folder("on_fd_1")
    assert not transport.hit(f"POST {V4}/nodes/move")
    assert transport.sent(f"DELETE {V4}/nodes/delete") == [
        {"items": [{"node_id": "on_fd_1", "origin_version": 100}]}
    ]


# ---------------------------------------------------------------------------
# Processing, upload, merge, waits
# ---------------------------------------------------------------------------


def test_transcribe_and_summarize_starts_whatever_is_missing(tmp_path):
    routes = {f"GET {V4}/files/detail/of_1": detail_payload(), f"POST {V4}/tasks/trans-sum": ok()}
    client, transport = routed(tmp_path, routes)
    assert client.transcribe_and_summarize("of_1", language="en-US", diarization=False, llm="gpt-5") is True
    (body,) = transport.sent(f"POST {V4}/tasks/trans-sum")
    assert (body["file_id"], body["language"], body["diarization"], body["summary_ai_model"]) == (
        "of_1",
        "en",
        0,
        "gpt-5",
    )

    routes = {
        f"GET {V4}/files/detail/of_1": detail_payload(obj("TRANSCRIPT", "https://s3.fake/t.json")),
        f"POST {V4}/tasks/summary": ok(),
    }
    client, transport = routed(tmp_path, routes)
    assert client.transcribe_and_summarize("of_1", template_id="MEETING", verbosity="high") is True
    assert client.transcribe_and_summarize("of_1", template_id="790a48f29350244d1baae3837c4035de") is True
    assert client.transcribe_and_summarize("of_1", template_id="tpl_9", template_kind="custom") is True
    # Plaud rejects a template id sent without its kind (-400): it is
    # inferred for official codes and community hex ids, or given.
    official, community, custom = transport.sent(f"POST {V4}/tasks/summary")
    assert (official["template_id"], official["template_type"]) == ("MEETING", "official")
    assert official["summary_verbosity"] == "high"
    assert community["template_type"] == "community"
    assert (custom["template_id"], custom["template_type"]) == ("tpl_9", "custom")


def test_summary_options_refuse_what_plaud_would_reject(tmp_path):
    client, transport = routed(tmp_path, {})
    with pytest.raises(ValueError, match="only to its default template"):
        client.generate_note("of_1", template_id="MEETING", note_name="Minutes", instruction="Be brief")
    with pytest.raises(ValueError, match="300"):
        client.resummarize("of_1", instruction="x" * 301)
    with pytest.raises(ValueError, match="template_kind must be one of"):
        client.transcribe_and_summarize("of_1", template_id="MEETING", template_kind="shared")
    with pytest.raises(ValueError, match="verbosity"):
        client.resummarize("of_1", verbosity="huge")
    assert transport.calls == []  # refused before any request


def test_resummarize_replaces_an_existing_summary_with_the_new_options(tmp_path):
    summarized = detail_payload(
        obj("TRANSCRIPT", "https://s3.fake/t.json"),
        obj("POLISHED_TRANSCRIPT", "https://s3.fake/p.json", task_status=1),
        obj("SUMMARY", "https://s3.fake/s.md"),
    )
    routes = {
        f"GET {V4}/files/detail/of_1": summarized,
        f"POST {V4}/tasks/summary-change-retry": ok({"task_id": "t_9"}),
    }
    client, transport = routed(tmp_path, routes)
    assert (
        client.resummarize("of_1", llm="claude-opus-5", verbosity="low", instruction="Flag risks.") == "t_9"
    )
    (body,) = transport.sent(f"POST {V4}/tasks/summary-change-retry")
    assert (body["summary_ai_model"], body["summary_verbosity"], body["custom_instruction"]) == (
        "claude-opus-5",
        "low",
        "Flag risks.",
    )
    assert body["use_polished_transcript"] is True
    assert "template_id" not in body


def test_resummarize_needs_a_transcript(tmp_path):
    client, transport = routed(tmp_path, {f"GET {V4}/files/detail/of_1": detail_payload()})
    with pytest.raises(ValueError, match="no transcript yet"):
        client.resummarize("of_1")
    assert len(transport.calls) == 1


def test_generate_note_names_the_note_and_returns_its_job(tmp_path):
    routes = {
        f"GET {V4}/files/detail/of_1": detail_payload(obj("TRANSCRIPT", "https://s3.fake/t.json")),
        f"POST {V4}/tasks/template-note": ok({"task_id": "t_n", "note_file_id": "f_note"}),
    }
    client, transport = routed(tmp_path, routes)
    job = client.generate_note("of_1", template_id="CUSTOMER-CONSULTATION", note_name="Client Needs")
    assert (job.task_id, job.note_id, job.name) == ("t_n", "f_note", "Client Needs")
    # The default template is sent by name only.
    assert client.generate_note("of_1", instruction="List action items.").name == "Intelligent Generation"
    with_template, default = transport.sent(f"POST {V4}/tasks/template-note")
    assert (with_template["template_name"], with_template["template_type"]) == ("Client Needs", "official")
    assert "template_id" not in default and default["custom_instruction"] == "List action items."
    with pytest.raises(ValueError, match="note_name is required"):
        client.generate_note("of_1", template_id="CUSTOMER-CONSULTATION")


def test_wait_for_task_returns_when_done_and_raises_when_failed(tmp_path):
    status = f"GET {V4}/task-status/t_1"
    client, _ = routed(tmp_path, {status: [ok({"task_status": 0}), ok({"task_status": 1})]})
    client.wait_for_task("t_1", what="note", timeout_s=60)

    client, _ = routed(tmp_path, {status: ok({"task_status": -1800002})})
    with pytest.raises(PlaudApiError, match="note job failed") as info:
        client.wait_for_task("t_1", what="note", timeout_s=60)
    assert not info.value.is_soft_deadline_timeout()

    client, _ = routed(tmp_path, {status: ok({"task_status": 0})})
    with pytest.raises(PlaudWaitTimeoutError) as timeout:
        client.wait_for_task("t_1", what="note", timeout_s=0)
    assert timeout.value.task_id == "t_1"


def test_search_templates_reads_each_kind_the_way_the_web_app_does(tmp_path):
    hits = [
        {
            "type": "official",
            "template": {
                "type": "CUSTOMER-CONSULTATION",
                "template_id": "00000035c5824c8f87c2e87133fcd989",
                "name": "Client Needs",
                "description_short": "From needs to actions.",
                "category": "CONSULTING",
            },
        },
        {
            "type": "community",
            "template": {
                "id": "9276c8f76532d5ed065c248666a836b4",
                "latest_published_version": {
                    "template_id": "9276c8f76532d5ed065c248666a836b4",
                    "title": "Discovery Notes",
                    "category": "Financial",
                },
            },
        },
        {"type": "custom", "template": {"id": "tpl_9", "name": "My Template"}},
        {"type": "something-new", "template": {}},
    ]
    routes = {
        "POST /summary/community/search": ok(hits),
        "GET /summary/v4/models/config": ok([{"model_name": "auto", "name": "Auto", "des": "Picks one"}]),
    }
    client, transport = routed(tmp_path, routes)
    found = [(t.id, t.kind, t.name) for t in client.search_templates("client needs")]
    # Official templates go by their code, not their hex template_id.
    assert found == [
        ("CUSTOMER-CONSULTATION", "official", "Client Needs"),
        ("9276c8f76532d5ed065c248666a836b4", "community", "Discovery Notes"),
        ("tpl_9", "custom", "My Template"),
    ]
    assert transport.sent("POST /summary/community/search")[0]["query"] == "client needs"
    assert [(m.model, m.name) for m in client.list_summary_models()] == [("auto", "Auto")]


def test_transcribe_and_summarize_starts_nothing_when_done_or_running(tmp_path):
    done = detail_payload(obj("TRANSCRIPT", "https://s3.fake/t.json"), obj("SUMMARY", "https://s3.fake/s.md"))
    running = detail_payload(tasks=[{"task_id": "t1", "task_type": "trans"}])
    for payload in (done, running):
        client, transport = routed(tmp_path, {f"GET {V4}/files/detail/of_1": payload})
        assert client.transcribe_and_summarize("of_1") is False
        assert len(transport.calls) == 1  # no POST: any would be an unrouted request


def upload_routes(**overrides):
    routes = {
        f"POST {V4}/upload/create": ok(
            {"upload_id": "u1", "part_urls": ["https://s3.fake/p1", "https://s3.fake/p2"], "chunk_size": 6}
        ),
        "PUT https://s3.fake/p1": HttpResponse(200, b"", {"etag": '"e1"'}),
        "PUT https://s3.fake/p2": HttpResponse(200, b"", {"etag": '"e2"'}),
        f"POST {V4}/upload/merge-part": ok(),
        f"POST {V4}/upload/confirm-audio-web": ok(
            {"file_id": "f_x", "node_id": "n_f", "folder_node_id": "n_w", "version_ms": 5}
        ),
    }
    return routes | overrides


def test_upload_recording_runs_create_put_merge_confirm(tmp_path):
    audio = tmp_path / "a.mp3"
    audio.write_bytes(b"abcdefghij")
    client, transport = routed(tmp_path, upload_routes())
    rec = client.upload_recording(
        audio, "Call", "MP3", start_time=1234, timezone_offset=-6, folder_id="on_fd_1"
    )
    assert (rec.id, rec.node_id, rec.wrapper_id, rec.version_ms, rec.folder_id) == (
        "f_x",
        "n_f",
        "n_w",
        5,
        "on_fd_1",
    )
    puts = [c["body"] for c in transport.calls if c["method"] == "PUT"]
    assert puts == [b"abcdef", b"ghij"]
    assert transport.sent(f"POST {V4}/upload/create")[0]["file_size"] == 10
    assert transport.sent(f"POST {V4}/upload/merge-part") == [
        {"upload_id": "u1", "parts": [{"etag": "e1", "part_number": 1}, {"etag": "e2", "part_number": 2}]}
    ]
    confirm = transport.sent(f"POST {V4}/upload/confirm-audio-web")[0]
    assert (confirm["file_name"], confirm["start_time"], confirm["target_node_id"]) == (
        "Call",
        1234,
        "on_fd_1",
    )
    assert confirm["conflict_strategy"] == "keep_both"
    assert confirm["file_id"].startswith("f_")
    # Upload confirm fails without the IANA zone; -6 hours is Etc/GMT+6.
    assert transport.calls[-1]["headers"]["x-timezone-iana"] == "Etc/GMT+6"


def test_upload_recording_refuses_to_merge_a_truncated_file(tmp_path):
    audio = tmp_path / "a.mp3"
    audio.write_bytes(b"abcdefghijklmnop")  # 16 bytes: 3 chunks of 6, but only 2 part URLs
    client, transport = routed(tmp_path, upload_routes())
    with pytest.raises(PlaudApiError, match="more data"):
        client.upload_recording(audio, "Call", "MP3")
    assert not transport.hit(f"POST {V4}/upload/merge-part")


def test_upload_recording_confirm_network_error_warns_about_a_possible_duplicate(tmp_path):
    audio = tmp_path / "a.mp3"
    audio.write_bytes(b"abcdefghij")
    boom = PlaudApiError("connection reset", network_error=True)
    client, _ = routed(tmp_path, upload_routes(**{f"POST {V4}/upload/confirm-audio-web": boom}))
    with pytest.raises(PlaudApiError, match="may have completed anyway") as info:
        client.upload_recording(audio, "Call", "MP3")
    assert info.value.network_error is False


def merge_routes(merged_detail):
    return {
        f"GET {V4}/files/detail/of_1": detail_payload(),
        f"POST {V4}/tasks/audio-merge": ok({"task_id": "t1", "file_id": "f_m"}),
        f"GET {V4}/files/detail/f_m": merged_detail,
    }


def test_merge_recordings_posts_ids_and_target_folder_and_waits_for_the_result(tmp_path):
    client, transport = routed(tmp_path, merge_routes(detail_payload(file_id="f_m", duration=5000)))
    merged = client.merge_recordings(["of_1", "of_2"], "Merged", poll_interval_s=0)
    assert (merged.id, merged.duration) == ("f_m", 5000)
    (body,) = transport.sent(f"POST {V4}/tasks/audio-merge")
    assert (body["source_file_ids"], body["target_node_id"], body["file_name"]) == (
        ["of_1", "of_2"],
        "on_fd_1",
        "Merged",
    )


def test_merge_recordings_timeout_carries_the_task_id(tmp_path):
    not_found = {"status": -1800311, "msg": "file not found"}  # the merged file does not exist yet
    client, _ = routed(tmp_path, merge_routes(not_found))
    with pytest.raises(PlaudWaitTimeoutError, match="merge timed out") as info:
        client.merge_recordings(["of_1", "of_2"], "Merged", timeout_s=0)
    assert info.value.task_id == "t1"


def test_merge_recordings_rejects_bad_input(tmp_path):
    client, _ = routed(tmp_path, {})
    with pytest.raises(ValueError, match="at least 2"):
        client.merge_recordings(["of_1"], "Merged")
    with pytest.raises(ValueError, match="filename cannot be empty"):
        client.merge_recordings(["of_1", "of_2"], " ")


def trim_routes(copy_detail):
    return {
        f"GET {V4}/files/detail/of_1": detail_payload(duration=60_000),
        f"POST {V4}/audio/trim": ok({"task_id": "t1", "target_file_id": "f_copy"}),
        f"GET {V4}/files/detail/f_copy": copy_detail,
    }


def test_trim_recording_posts_keep_ranges_and_waits_for_the_copy(tmp_path):
    client, transport = routed(tmp_path, trim_routes(detail_payload(file_id="f_copy", duration=25_000)))
    copy = client.trim_recording("of_1", [(0, 10_000), (30_000, 45_000)], poll_interval_s=0)
    assert (copy.id, copy.duration) == ("f_copy", 25_000)
    (body,) = transport.sent(f"POST {V4}/audio/trim")
    assert body["file_id"] == "of_1"
    assert body["segments"] == [{"start_ms": 0, "end_ms": 10_000}, {"start_ms": 30_000, "end_ms": 45_000}]


def test_trim_recording_timeout_carries_the_copys_id(tmp_path):
    not_found = {"status": -1800311, "msg": "file not found"}  # the copy does not exist yet
    client, _ = routed(tmp_path, trim_routes(not_found))
    with pytest.raises(PlaudWaitTimeoutError, match="trim timed out") as info:
        client.trim_recording("of_1", [(0, 1000)], timeout_s=0)
    assert info.value.task_id == "f_copy"


def test_trim_recording_rejects_bad_ranges(tmp_path):
    client, transport = routed(tmp_path, trim_routes(detail_payload()))
    for keep in ([], [(5000, 5000)], [(-1, 1000)], [(0, 61_000)]):
        with pytest.raises(ValueError, match="trim"):
            client.trim_recording("of_1", keep)
    assert not transport.hit(f"POST {V4}/audio/trim")


def attach_routes(**overrides):
    return (
        upload_routes(
            **{
                f"GET {V4}/files/detail/of_1": detail_payload(),
                f"POST {V4}/upload/confirm-file-web": ok({"file_id": "f_doc", "node_id": "n_doc"}),
            }
        )
        | overrides
    )


def test_attach_document_uploads_then_confirms_into_the_recordings_container(tmp_path):
    doc = tmp_path / "statement.pdf"
    doc.write_bytes(b"abcdefghij")
    client, transport = routed(tmp_path, attach_routes())
    attachment = client.attach_document("of_1", doc)
    assert (attachment.id, attachment.filename, attachment.file_type) == ("f_doc", "statement.pdf", "PDF")
    assert transport.sent(f"POST {V4}/upload/create")[0]["file_type"] == "application/pdf"
    confirm = transport.sent(f"POST {V4}/upload/confirm-file-web")[0]
    assert (confirm["file_name"], confirm["target_node_id"], confirm["upload_id"]) == (
        "statement.pdf",
        "on_w_1",
        "u1",
    )
    assert not transport.hit(f"POST {V4}/upload/confirm-audio-web")


def test_attach_document_rejects_unsupported_and_oversized_files_before_uploading(tmp_path):
    client, transport = routed(tmp_path, attach_routes())
    exe = tmp_path / "setup.exe"
    exe.write_bytes(b"x")
    with pytest.raises(ValueError, match="cannot attach a .exe"):
        client.attach_document("of_1", exe)
    big_text = tmp_path / "big.txt"
    big_text.write_bytes(b"x" * (2 * 1024 * 1024 + 1))
    with pytest.raises(ValueError, match="text attachments are limited to 2 MB"):
        client.attach_document("of_1", big_text)
    assert not transport.hit(f"POST {V4}/upload/create")


# An attachment can sort ahead of the recording in its container.
CONTAINER_FILES = [
    {"file_id": "f_doc", "name": "statement.pdf", "file_type": "PDF"},
    {"file_id": "f_note", "name": "Notes", "file_type": "MD"},
    {"file_id": "of_1", "name": "Meeting", "file_type": "REC"},
]


def test_list_attachments_skips_the_recording_and_its_notes(tmp_path):
    routes = {
        f"GET {V4}/files/detail/of_1": detail_payload(),
        f"GET {V4}/folders/children": children({"on_w_1": CONTAINER_FILES}),
    }
    client, _ = routed(tmp_path, routes)
    assert [(a.id, a.filename, a.file_type) for a in client.list_attachments("of_1")] == [
        ("f_doc", "statement.pdf", "PDF")
    ]


def test_attachments_do_not_hide_the_recording_from_trash_listing(tmp_path):
    container = {"node_id": "n_c", "type": 1, "name": "Meeting"}
    routes = {
        f"GET {V4}/folders/content": ok({"items": [container]}),
        f"GET {V4}/folders/children": children({"n_c": CONTAINER_FILES}),
    }
    client, _ = routed(tmp_path, routes)
    assert [rec.id for rec in client.list_trash()] == ["of_1"]


def test_wait_for_transcription_returns_once_done_and_times_out_when_not(tmp_path):
    pending = detail_payload()
    done = detail_payload(obj("TRANSCRIPT", "https://s3.fake/t.json"))
    client, _ = routed(tmp_path, {f"GET {V4}/files/detail/of_1": [pending, done]})
    client.wait_for_transcription("of_1", poll_interval_s=0)

    client, _ = routed(tmp_path, {f"GET {V4}/files/detail/of_1": pending})
    with pytest.raises(PlaudWaitTimeoutError, match="transcription timed out"):
        client.wait_for_transcription("of_1", timeout_s=0)


class TestPollLoopSurvival:
    """PlaudClient._poll_until backs the transcription, summary, and merge waits."""

    def test_skips_transient_errors_then_returns_result(self, tmp_path):
        """A 429 and a network blip count as skipped polls, not failures."""
        client = PlaudClient(make_manager(tmp_path)[0], transport=StubTransport([]))
        outcomes = iter(
            [
                _transient_error(429),
                PlaudApiError("Plaud API request timed out after 30.0s", network_error=True),
                None,
                "done",
            ]
        )

        def check():
            outcome = next(outcomes)
            if isinstance(outcome, Exception):
                raise outcome
            return outcome

        assert client._poll_until(check, what="job", timeout_s=60, poll_interval_s=0) == "done"

    def test_non_transient_error_propagates(self, tmp_path):
        client = PlaudClient(make_manager(tmp_path)[0], transport=StubTransport([]))
        err_404 = PlaudApiError.from_http_error(_make_http_error(404, b"not found"))

        def check():
            raise err_404

        with pytest.raises(PlaudApiError) as info:
            client._poll_until(check, what="job", timeout_s=60, poll_interval_s=0)
        assert info.value is err_404

    def test_timeout_checks_once_then_raises_wait_timeout(self, tmp_path):
        """Even a zero budget polls once; then it's a soft deadline, not a failure."""
        client = PlaudClient(make_manager(tmp_path)[0], transport=StubTransport([]))
        calls = []

        with pytest.raises(PlaudWaitTimeoutError, match="job timed out after 0s") as info:
            client._poll_until(
                lambda: calls.append(1), what="job", timeout_s=0, poll_interval_s=0, task_id="t1"
            )
        assert calls == [1]
        assert info.value.task_id == "t1"
        assert info.value.is_soft_deadline_timeout()


# ---------------------------------------------------------------------------
# Transport: errors, retry, region redirect
# ---------------------------------------------------------------------------


def _make_http_error(status: int, body: bytes, headers: dict[str, str] | None = None) -> HTTPError:
    """Build a urllib.error.HTTPError with a readable body and optional headers."""
    from email.message import Message

    msg = Message()
    for k, v in (headers or {}).items():
        msg[k] = v
    return HTTPError(url="https://example.com/api", code=status, msg="Error", hdrs=msg, fp=io.BytesIO(body))


def _transient_error(status: int = 429, retry_after: float | None = None) -> PlaudApiError:
    """Build a PlaudApiError that classify() deems retryable."""
    headers = {} if retry_after is None else {"Retry-After": str(int(retry_after))}
    return PlaudApiError.from_http_error(_make_http_error(status, b"rate limited", headers=headers))


def test_missing_session_raises(tmp_path):
    manager = SessionManager(FileSessionStore(tmp_path / "missing.json"))
    client = PlaudClient(manager, transport=StubTransport([]))
    with pytest.raises(PlaudSessionExpiredError):
        client.list_recordings()


def test_error_message_names_status_method_and_path_without_the_query(tmp_path):
    client, _ = routed(tmp_path, {f"GET {V4}/recordings/all": {"status": -1, "msg": "boom"}})
    with pytest.raises(PlaudApiError) as info:
        client.list_recordings()
    message = str(info.value)
    assert "boom" in message
    assert f"status -1, GET {V4}/recordings/all)" in message
    assert info.value.plaud_code == -1


def test_migrated_account_status_hints_to_update_plaud_tools(tmp_path):
    client, _ = routed(
        tmp_path, {f"GET {V4}/recordings/all": {"status": -1800907, "msg": "Account migrated to 4.0"}}
    )
    with pytest.raises(PlaudApiError, match="update plaud-tools"):
        client.list_recordings()


def test_unknown_file_status_classifies_as_not_found(tmp_path):
    client, _ = routed(
        tmp_path, {f"GET {V4}/files/detail/of_1": {"status": -1800311, "msg": "file not found"}}
    )
    with pytest.raises(PlaudApiError) as info:
        client.get_recording("of_1")
    assert info.value.classify() == ("not_found", False)


def test_non_json_2xx_body_raises_plaud_api_error_not_a_value_error(tmp_path):
    """#146: an HTML error page behind a proxy is a Plaud-side failure, not a caller input error."""
    transport = StubTransport([HttpResponse(200, b"<html>upstream hiccup</html>", {})])
    client = PlaudClient(make_manager(tmp_path)[0], transport=transport)
    with pytest.raises(PlaudApiError) as exc_info:
        client.list_recordings()
    assert not isinstance(exc_info.value, json.JSONDecodeError)
    assert exc_info.value.http_status == 200


class TestRetryBackoff:
    """Idempotent GETs retry 429 / 5xx up to _MAX_ATTEMPTS; everything else gets one try."""

    def client(self, tmp_path, responses):
        transport = StubTransport(responses)
        return PlaudClient(make_manager(tmp_path)[0], transport=transport), transport

    def test_get_succeeds_after_transient_failures(self, tmp_path, monkeypatch):
        sleeps: list[float] = []
        monkeypatch.setattr(client_mod, "_sleep", sleeps.append)
        client, transport = self.client(
            tmp_path, [_transient_error(429), _transient_error(503), json_response(EMPTY_LIST)]
        )
        assert client.list_recordings() == []
        assert len(transport.calls) == 3
        assert sleeps == [0.75, 1.5]  # 1 s then 2 s backoff, jitter pinned to its low end

    def test_get_raises_after_exhausting_attempts(self, tmp_path):
        client, transport = self.client(tmp_path, [_transient_error(429)] * 4)
        with pytest.raises(PlaudApiError):
            client.list_recordings()
        assert len(transport.calls) == client_mod._MAX_ATTEMPTS

    def test_non_retryable_error_is_not_retried(self, tmp_path):
        client, transport = self.client(
            tmp_path, [PlaudApiError.from_http_error(_make_http_error(404, b"not found"))]
        )
        with pytest.raises(PlaudApiError):
            client.list_recordings()
        assert len(transport.calls) == 1

    @pytest.mark.parametrize("status", [429, 503])
    def test_post_is_never_retried(self, tmp_path, status):
        """Replaying a write that may already have landed risks doing it twice (#147)."""
        client, transport = self.client(tmp_path, [_transient_error(status)])
        with pytest.raises(PlaudApiError):
            client.create_folder("Clients")
        assert len(transport.calls) == 1

    def test_retry_after_is_honoured_and_never_under_slept(self, tmp_path, monkeypatch):
        sleeps: list[float] = []
        monkeypatch.setattr(client_mod, "_sleep", sleeps.append)
        client, _ = self.client(tmp_path, [_transient_error(429, retry_after=30), json_response(EMPTY_LIST)])
        client.list_recordings()
        assert sleeps == [30.0]

        sleeps.clear()
        monkeypatch.setattr(client_mod, "_jitter", lambda lo, hi: hi)
        client, _ = self.client(tmp_path, [_transient_error(429, retry_after=0), json_response(EMPTY_LIST)])
        client.list_recordings()
        assert sleeps == [1.25]

    def test_retry_after_header_parsing(self):
        assert (
            PlaudApiError.from_http_error(_make_http_error(429, b"", {"Retry-After": "45"})).retry_after
            == 45.0
        )
        assert PlaudApiError.from_http_error(_make_http_error(429, b"")).retry_after is None
        http_date = {"Retry-After": "Wed, 21 Oct 2015 07:28:00 GMT"}
        assert PlaudApiError.from_http_error(_make_http_error(429, b"", http_date)).retry_after is None


REDIRECT_TO_EU = {"status": -302, "data": {"domains": {"api": "api-euc1.plaud.ai"}}}


def test_region_redirect_persists_region_and_resends_the_body(tmp_path):
    manager, store = make_manager(tmp_path, region="us")
    created = ok({"items": [folder_node("on_fd_9", "N")]})
    transport = StubTransport([json_response(REDIRECT_TO_EU), json_response(created)])
    PlaudClient(manager, transport=transport).create_folder("N")
    assert transport.calls[0]["url"].startswith("https://api.plaud.ai/")
    assert transport.calls[1]["url"].startswith("https://api-euc1.plaud.ai/")
    assert transport.calls[1]["body"] == transport.calls[0]["body"]
    assert store.load().region == "eu"


def test_region_redirect_loop_raises_after_two_requests(tmp_path):
    manager, _ = make_manager(tmp_path, region="us")
    transport = StubTransport([json_response(REDIRECT_TO_EU)] * 3)
    with pytest.raises(PlaudApiError, match="region redirect loop"):
        PlaudClient(manager, transport=transport).list_recordings()
    assert len(transport.calls) == 2


def test_region_redirect_to_unlisted_plaud_host_uses_that_host(tmp_path):
    """A -302 to a Plaud API host we have no region key for is followed as-is;
    a non-Plaud host is refused."""
    manager, store = make_manager(tmp_path, region="us")

    def redirect(host):
        return json_response({"status": -302, "data": {"domains": {"api": host}}})

    transport = StubTransport(
        [redirect("api-apse1.plaud.ai"), json_response(EMPTY_LIST), redirect("evil.example.com")]
    )
    client = PlaudClient(manager, transport=transport)
    client.list_recordings()
    assert store.load().region == "api-apse1.plaud.ai"
    assert transport.calls[1]["url"].startswith("https://api-apse1.plaud.ai/")

    with pytest.raises(PlaudApiError, match="unrecognized API host"):
        client.list_recordings()
    assert store.load().region == "api-apse1.plaud.ai"


@pytest.mark.parametrize(
    ("domain", "expected"),
    [
        ("api-euc1.plaud.ai", "eu"),
        ("https://api.plaud.ai/", "us"),
        ("api-apse1.plaud.ai", "api-apse1.plaud.ai"),
        (".plaud.ai", None),
        ("a..plaud.ai", None),
        ("apiа.plaud.ai", None),  # Cyrillic "a"
        ("plaud.ai.evil.com", None),
        ("", None),
    ],
)
def test_region_for_api_domain_accepts_only_clean_plaud_hosts(domain, expected):
    assert region_for_api_domain(domain) == expected


# ---------------------------------------------------------------------------
# Session store and cache
# ---------------------------------------------------------------------------


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

    monkeypatch.setattr("plaud_tools.core.session.importlib.import_module", lambda name: FakeKeyring)
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

    monkeypatch.setattr("plaud_tools.core.session.importlib.import_module", raise_import_error)
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
    ok_response = HttpResponse(200, json.dumps({"status": 0, "data": {"items": []}}).encode(), {})
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


def test_update_region_does_not_persist_env_token_session(monkeypatch):
    """A PLAUD_ACCESS_TOKEN session must not be written over the user's real
    stored login when a -302 changes the region; the region lives in memory."""
    env_token = make_jwt()
    monkeypatch.setenv("PLAUD_ACCESS_TOKEN", env_token)
    store = CountingStore(PlaudSession(access_token=env_token, region="us"))
    manager = SessionManager(store)

    assert manager.update_region("eu").region == "eu"
    assert store.save_count == 0
    assert manager.require().region == "eu"


def test_session_cache_region_failover_loads_store_at_most_twice(tmp_path):
    """-302 region failover triggers update_region which reloads the store once,
    then the retry uses the cache.  Total store.load() calls: <= 2."""
    manager, counting_store = make_counting_manager(region="us")
    transport = StubTransport([json_response(REDIRECT_TO_EU), json_response(EMPTY_LIST)])
    PlaudClient(manager, transport=transport).list_recordings()
    assert counting_store.load_count <= 2


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
# Custom vocabulary and public share links
# ---------------------------------------------------------------------------


def hotword(word_id="hw_1", word="Okafor", heard_as="", language="en"):
    return {
        "id": word_id,
        "word": word,
        "replaced_word": heard_as,
        "language": language,
        "correction_count": 2,
    }


def test_vocabulary_add_sends_language_and_heard_as(tmp_path):
    reply = ok({"items": [{"item": hotword(heard_as="Oh car for"), "duplicated": False}]})
    client, transport = routed(tmp_path, {f"POST {V4}/hotwords/add": reply})
    entry = client.add_vocabulary("Okafor", heard_as="Oh car for")
    assert (entry.id, entry.word, entry.heard_as) == ("hw_1", "Okafor", "Oh car for")
    assert transport.sent(f"POST {V4}/hotwords/add") == [
        {"items": [{"word": "Okafor", "language": "en", "replaced_word": "Oh car for"}]}
    ]


def test_vocabulary_update_sends_the_whole_entry_and_can_clear_heard_as(tmp_path):
    routes = {
        f"GET {V4}/hotwords/list": ok({"items": [hotword(heard_as="Oh car for")]}),
        f"PATCH {V4}/hotwords/update/hw_1": ok({"item": hotword()}),
    }
    client, transport = routed(tmp_path, routes)
    entry = client.update_vocabulary("hw_1", heard_as="")
    assert entry.heard_as == ""
    assert transport.sent(f"PATCH {V4}/hotwords/update/hw_1") == [
        {"word": "Okafor", "language": "en", "replaced_word": ""}
    ]
    with pytest.raises(ValueError, match="no vocabulary entry"):
        client.update_vocabulary("hw_missing", word="X")


def test_vocabulary_delete_returns_the_deleted_count(tmp_path):
    client, transport = routed(tmp_path, {f"POST {V4}/hotwords/delete": ok({"deleted": 2})})
    assert client.delete_vocabulary(["hw_1", "hw_2"]) == 2
    assert transport.sent(f"POST {V4}/hotwords/delete") == [{"ids": ["hw_1", "hw_2"]}]


SHARE_DATA = {
    "share_url": "https://share.example/s/abc",
    "share_key": "abc",
    "expire_at": "2026-12-01T00:00:00Z",
    "content_config": {"transcript": True, "version": "4"},
}


def test_create_share_link_sends_expiry_and_content_config(tmp_path):
    client, transport = routed(tmp_path, {"POST /share/public/create": ok(SHARE_DATA)})
    link = client.create_share_link("ab" * 16, expire_days=7)
    body = transport.sent("POST /share/public/create")[0]
    assert body["object_id"] == "of_" + "ab" * 16 and body["object_type"] == "file"
    assert body["content_config"] == {"audio": False, "transcript": True, "summary": False, "version": "4"}
    assert body["expire_at"].endswith("Z")
    assert (link.url, link.transcript, link.audio) == ("https://share.example/s/abc", True, False)
    client.create_share_link("ab" * 16, expire_days=None)
    assert "expire_at" not in transport.sent("POST /share/public/create")[1]


def test_get_share_link_is_none_when_plaud_has_no_link(tmp_path):
    client, _ = routed(
        tmp_path, {"POST /share/public/get": {"status": 40400, "msg": "Public share not found"}}
    )
    assert client.get_share_link("f_1") is None


def test_update_share_link_merges_content_flags_and_clears_expiry_with_empty_string(tmp_path):
    routes = {"POST /share/public/get": ok(SHARE_DATA), "POST /share/public/update": ok(SHARE_DATA)}
    client, transport = routed(tmp_path, routes)
    client.update_share_link("f_1", never_expires=True, audio=True)
    body = transport.sent("POST /share/public/update")[0]
    assert body["expire_at"] == ""
    assert body["content_config"] == {"audio": True, "transcript": True, "summary": False, "version": "4"}
    with pytest.raises(ValueError, match="at least one of audio, transcript, summary"):
        client.update_share_link("f_1", transcript=False)


def test_revoke_share_link_posts_the_object_ref(tmp_path):
    client, transport = routed(tmp_path, {"POST /share/public/delete": ok({"object_id": "f_1"})})
    client.revoke_share_link("f_1")
    assert transport.sent("POST /share/public/delete") == [{"object_id": "f_1", "object_type": "file"}]
