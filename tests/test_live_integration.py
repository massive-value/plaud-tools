from __future__ import annotations

import os

import pytest

from plaud_tools.core.client import PlaudClient, PlaudRecordingQuery
from plaud_tools.core.session import FileSessionStore, SessionManager

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(
        os.getenv("PLAUD_LIVE_READS") != "1",
        reason="Set PLAUD_LIVE_READS=1 to run live Plaud read tests against sacrificial data.",
    ),
]


def test_live_list_and_detail_roundtrip():
    session_path = os.getenv("PLAUD_SESSION_PATH")
    if not session_path:
        # Skip cleanly rather than falling through to FileSessionStore(None),
        # which resolves to appdata.session_path() -- redirected to an empty
        # tmp_path by conftest.py's autouse fixture in every test process, so
        # the failure mode without this guard is a confusing
        # PlaudSessionExpiredError, not an actionable skip reason (§5.6).
        pytest.skip("Set PLAUD_SESSION_PATH to run this test.")
    store = FileSessionStore(session_path)
    client = PlaudClient(SessionManager(store))

    recordings = client.list_recordings(
        PlaudRecordingQuery(limit=5, is_trash=0, sort_by="start_time", is_desc=True)
    )
    assert isinstance(recordings, list)
    if not recordings:
        pytest.skip("No sacrificial recordings available for live validation.")

    detail = client.get_recording(recordings[0].id, include_transcript=False)
    assert detail.id == recordings[0].id
