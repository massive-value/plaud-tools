"""Tests for issue #44: log rotation, test-connection timeout, narrow mcp exceptions."""

from __future__ import annotations

import json
import logging
import logging.handlers
import sys
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

# ---------------------------------------------------------------------------
# 1. Log rotation — _setup_logging uses RotatingFileHandler
# ---------------------------------------------------------------------------


def test_setup_logging_uses_rotating_file_handler(tmp_path, monkeypatch):
    """_setup_logging must attach a RotatingFileHandler, not a plain FileHandler."""
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))

    # Reset the root logger so basicConfig runs fresh.
    root = logging.getLogger()
    original_handlers = root.handlers[:]
    root.handlers.clear()
    try:
        from plaud_tools.tray_app import _setup_logging

        _setup_logging()

        handler_types = [type(h) for h in logging.getLogger().handlers]
        assert logging.handlers.RotatingFileHandler in handler_types, (
            "Expected RotatingFileHandler but got: " + str(handler_types)
        )
    finally:
        # Restore original handlers so other tests are unaffected.
        logging.getLogger().handlers = original_handlers


def test_setup_logging_rotating_handler_limits(tmp_path, monkeypatch):
    """RotatingFileHandler must use maxBytes=1_000_000 and backupCount=3."""
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))

    root = logging.getLogger()
    original_handlers = root.handlers[:]
    root.handlers.clear()
    try:
        from plaud_tools.tray_app import _setup_logging

        _setup_logging()

        rfh = next(
            h for h in logging.getLogger().handlers if isinstance(h, logging.handlers.RotatingFileHandler)
        )
        assert rfh.maxBytes == 1_000_000
        assert rfh.backupCount == 3
    finally:
        logging.getLogger().handlers = original_handlers


def test_setup_logging_creates_log_file_in_localappdata(tmp_path, monkeypatch):
    """Log file must land at <LOCALAPPDATA>/PlaudTools/tray.log.

    Pin sys.platform to win32 so appdata.data_dir() honours the LOCALAPPDATA
    override on Linux CI (which would otherwise use platformdirs.user_data_dir).
    """
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))

    root = logging.getLogger()
    original_handlers = root.handlers[:]
    root.handlers.clear()
    try:
        from plaud_tools.tray_app import _setup_logging

        _setup_logging()

        rfh = next(
            h for h in logging.getLogger().handlers if isinstance(h, logging.handlers.RotatingFileHandler)
        )
        log_path = Path(rfh.baseFilename)
        assert log_path == tmp_path / "PlaudTools" / "tray.log"
    finally:
        logging.getLogger().handlers = original_handlers


# ---------------------------------------------------------------------------
# Session-expired event diagnostic formatting (issue #78)
# ---------------------------------------------------------------------------


def test_format_session_expired_diag_strips_type_and_ts():
    from plaud_tools.tray.background import _format_session_expired_diag

    out = _format_session_expired_diag(
        {
            "type": "session_expired",
            "ts": 1779462640.5,
            "reason": "no_session",
            "store_source": "missing",
            "mcp_pid": 1234,
        }
    )
    assert "type" not in out
    assert "ts" not in out
    assert "reason='no_session'" in out
    assert "store_source='missing'" in out
    assert "mcp_pid=1234" in out


def test_format_session_expired_diag_stable_ordering():
    """Keys are sorted so log lines are diff-friendly across runs."""
    from plaud_tools.tray.background import _format_session_expired_diag

    out = _format_session_expired_diag(
        {
            "type": "session_expired",
            "ts": 1.0,
            "zebra": 1,
            "alpha": 2,
        }
    )
    assert out.index("alpha=") < out.index("zebra=")


def test_format_session_expired_diag_empty_when_no_diag():
    from plaud_tools.tray.background import _format_session_expired_diag

    assert _format_session_expired_diag({"type": "session_expired", "ts": 0.0}) == ""


# ---------------------------------------------------------------------------
# 2. Test-connection timeout
# ---------------------------------------------------------------------------


class _FakePlaudClient:
    """Simulates PlaudClient.get_user_info() blocking for a configurable duration."""

    def __init__(self, delay: float = 0.0, raises: Exception | None = None):
        self._delay = delay
        self._raises = raises

    def get_user_info(self):
        if self._delay:
            time.sleep(self._delay)
        if self._raises:
            raise self._raises
        return {"user": "test"}


def _run_test_connection(fake_client, timeout_seconds=15):
    """Helper: create a minimal TrayApp, inject a fake client, run _test_connection."""
    from plaud_tools.tray_app import TrayApp

    app = TrayApp.__new__(TrayApp)
    app._root = None  # no tkinter needed — callback fires on root.after(0, ...)
    app._manager = MagicMock()

    results: list[tuple[bool, str]] = []
    callback_event = threading.Event()

    def on_done(ok: bool, msg: str) -> None:
        results.append((ok, msg))
        callback_event.set()

    # Monkey-patch root.after so the lambda fires immediately without tkinter.
    root_mock = MagicMock()

    def fake_after(delay, fn):
        fn()

    root_mock.after.side_effect = fake_after
    app._root = root_mock

    with patch("plaud_tools.tray_app.PlaudClient", return_value=fake_client):
        app._test_connection(on_done)

    # Wait up to (timeout + 2) seconds for the callback.
    callback_event.wait(timeout=timeout_seconds + 2)
    return results


def test_test_connection_succeeds_when_api_responds():
    results = _run_test_connection(_FakePlaudClient(delay=0.0))
    assert len(results) == 1
    ok, msg = results[0]
    assert ok is True
    assert "connected" in msg.lower()


def test_test_connection_reports_api_error():
    from plaud_tools.errors import PlaudApiError

    results = _run_test_connection(_FakePlaudClient(raises=PlaudApiError("bad auth")))
    assert len(results) == 1
    ok, msg = results[0]
    assert ok is False
    assert "bad auth" in msg


def test_test_connection_reports_timeout_after_deadline(monkeypatch):
    """When the API hangs past the deadline the callback must fire with a timeout message."""
    import plaud_tools.tray_app as tray_module

    # Shrink the timeout to 0.2 s so the test completes quickly.
    monkeypatch.setattr(tray_module, "_TEST_CONNECTION_TIMEOUT", 0.2)

    # Slow client: sleeps well past the deadline.
    results = _run_test_connection(_FakePlaudClient(delay=5.0), timeout_seconds=3)

    assert len(results) == 1
    ok, msg = results[0]
    assert ok is False
    assert "timed out" in msg.lower()


def test_test_connection_timeout_constant_is_fifteen():
    from plaud_tools.tray_app import _TEST_CONNECTION_TIMEOUT

    assert _TEST_CONNECTION_TIMEOUT == 15


# ---------------------------------------------------------------------------
# 3. Narrow RuntimeError in mcp._call
#
# NOTE: PR #33 superseded the original intent of letting RuntimeError propagate
# from `_call`. MCP tool handlers now return a structured `api_error` result
# rather than raising. The `_call`-level RuntimeError test was removed; the
# upload-specific RuntimeError test below still applies.
# ---------------------------------------------------------------------------


def test_mcp_upload_recording_catches_ffmpeg_runtime_error(tmp_path, monkeypatch):
    """upload_recording must convert RuntimeError from transcode_to_mp3 to an MCP error."""
    from plaud_tools.mcp import build_handlers

    mp3_file = tmp_path / "audio.mp3"
    mp3_file.write_bytes(b"fake mp3")

    # Patch get_file_type to say this file needs transcoding (so transcode_to_mp3 is called).
    import plaud_tools.transcode as transcode_module

    monkeypatch.setattr(
        transcode_module,
        "get_file_type",
        lambda path: ("MP3", True),
    )
    monkeypatch.setattr(
        transcode_module,
        "transcode_to_mp3",
        lambda data, suffix: (_ for _ in ()).throw(RuntimeError("ffmpeg not found")),
    )

    # Patch the mcp module's import of transcode to use the monkeypatched version.

    def patched_inner_upload(client):
        from plaud_tools import transcode as t

        path = mp3_file
        file_type, needs_transcode = t.get_file_type(path)
        raw_bytes = path.read_bytes()
        try:
            t.transcode_to_mp3(raw_bytes, path.suffix)
        except RuntimeError as exc:
            from plaud_tools.mcp import _json_result

            return _json_result({"error": str(exc)}, is_error=True)

    client_mock = MagicMock()
    handlers = build_handlers(lambda: client_mock)

    # More direct: test _call does NOT catch it, and upload_recording handler does.
    # We already tested _call above; here just verify the handler returns isError.
    result = handlers["upload_recording"](str(mp3_file))
    # The file exists but transcode_to_mp3 is monkeypatched to raise RuntimeError.
    assert result.get("isError") is True
    payload = json.loads(result["content"][0]["text"])
    assert "ffmpeg" in payload["error"]


def test_mcp_call_still_catches_plaud_api_error():
    """_call must still catch PlaudApiError and return an MCP error dict."""
    from plaud_tools.errors import PlaudApiError
    from plaud_tools.mcp import _call

    def get_client():
        return MagicMock()

    def fn(client):
        raise PlaudApiError("rate limited")

    result = _call(get_client, fn)
    assert result.get("isError") is True
    payload = json.loads(result["content"][0]["text"])
    assert "rate limited" in payload["error"]


def test_mcp_call_still_catches_value_error():
    """_call must still catch ValueError and return an MCP error dict."""
    from plaud_tools.mcp import _call

    def get_client():
        return MagicMock()

    def fn(client):
        raise ValueError("invalid param")

    result = _call(get_client, fn)
    assert result.get("isError") is True
    payload = json.loads(result["content"][0]["text"])
    assert "invalid param" in payload["error"]
