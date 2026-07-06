"""Tests for issue #33 — structured MCP error codes + session_expired tray toast."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from plaud_tools.errors import PlaudApiError, PlaudSessionExpiredError
from plaud_tools.mcp import (
    _diagnose_session_state,
    _error_result,
    _write_event,
    build_handlers,
)

# ---------------------------------------------------------------------------
# PlaudApiError.classify() mapping (formerly _classify_api_error in mcp.py)
# ---------------------------------------------------------------------------


class TestClassifyApiError:
    """Verify that HTTP status codes map to the correct error codes."""

    def _make_err(self, http_status: int | None) -> PlaudApiError:
        return PlaudApiError("some error", http_status=http_status)

    def test_404_maps_to_not_found(self):
        code, retryable = self._make_err(404).classify()
        assert code == "not_found"
        assert retryable is False

    def test_429_maps_to_transient_and_retryable(self):
        code, retryable = self._make_err(429).classify()
        assert code == "transient"
        assert retryable is True

    def test_500_maps_to_transient_and_retryable(self):
        code, retryable = self._make_err(500).classify()
        assert code == "transient"
        assert retryable is True

    def test_503_maps_to_transient_and_retryable(self):
        code, retryable = self._make_err(503).classify()
        assert code == "transient"
        assert retryable is True

    def test_400_maps_to_api_error(self):
        code, retryable = self._make_err(400).classify()
        assert code == "api_error"
        assert retryable is False

    def test_401_maps_to_api_error(self):
        code, retryable = self._make_err(401).classify()
        assert code == "api_error"
        assert retryable is False

    def test_403_maps_to_api_error(self):
        code, retryable = self._make_err(403).classify()
        assert code == "api_error"
        assert retryable is False

    def test_none_status_maps_to_api_error(self):
        code, retryable = self._make_err(None).classify()
        assert code == "api_error"
        assert retryable is False


# ---------------------------------------------------------------------------
# _error_result structure
# ---------------------------------------------------------------------------


class TestErrorResult:
    def test_contains_error_code_and_retryable(self):
        result = _error_result("oops", error_code="not_found", retryable=False)
        payload = json.loads(result["content"][0]["text"])
        assert payload["error_code"] == "not_found"
        assert payload["retryable"] is False
        assert payload["error"] == "oops"
        assert result.get("isError") is True

    def test_http_status_included_when_provided(self):
        result = _error_result("rate limited", error_code="transient", retryable=True, http_status=429)
        payload = json.loads(result["content"][0]["text"])
        assert payload["http_status"] == 429

    def test_http_status_omitted_when_none(self):
        result = _error_result("oops", error_code="validation", retryable=False)
        payload = json.loads(result["content"][0]["text"])
        assert "http_status" not in payload


# ---------------------------------------------------------------------------
# Handler-level error propagation via _call
# ---------------------------------------------------------------------------


def _make_handlers(side_effect):
    """Build handlers with a get_client that always returns a client that raises side_effect."""
    mock_client = MagicMock()
    mock_client.list_recordings.side_effect = side_effect
    return build_handlers(lambda: mock_client)


class TestCallErrorPropagation:
    def test_session_expired_error_returns_session_expired_code(self):
        handlers = _make_handlers(PlaudSessionExpiredError("token expired"))
        result = handlers["browse_recordings"]()
        payload = json.loads(result["content"][0]["text"])
        assert payload["error_code"] == "session_expired"
        assert payload["retryable"] is False
        assert result["isError"] is True

    def test_404_api_error_returns_not_found_code(self):
        handlers = _make_handlers(PlaudApiError("not found", http_status=404))
        result = handlers["browse_recordings"]()
        payload = json.loads(result["content"][0]["text"])
        assert payload["error_code"] == "not_found"
        assert payload["retryable"] is False
        assert payload["http_status"] == 404

    def test_429_api_error_returns_transient_and_retryable(self):
        handlers = _make_handlers(PlaudApiError("rate limited", http_status=429))
        result = handlers["browse_recordings"]()
        payload = json.loads(result["content"][0]["text"])
        assert payload["error_code"] == "transient"
        assert payload["retryable"] is True

    def test_503_api_error_returns_transient_and_retryable(self):
        handlers = _make_handlers(PlaudApiError("server error", http_status=503))
        result = handlers["browse_recordings"]()
        payload = json.loads(result["content"][0]["text"])
        assert payload["error_code"] == "transient"
        assert payload["retryable"] is True

    def test_generic_api_error_returns_api_error_code(self):
        handlers = _make_handlers(PlaudApiError("something failed", http_status=400))
        result = handlers["browse_recordings"]()
        payload = json.loads(result["content"][0]["text"])
        assert payload["error_code"] == "api_error"
        assert payload["retryable"] is False

    def test_value_error_returns_validation_code(self):
        handlers = _make_handlers(ValueError("bad input"))
        result = handlers["browse_recordings"]()
        payload = json.loads(result["content"][0]["text"])
        assert payload["error_code"] == "validation"
        assert payload["retryable"] is False

    def test_runtime_error_returns_api_error_code(self):
        handlers = _make_handlers(RuntimeError("unexpected"))
        result = handlers["browse_recordings"]()
        payload = json.loads(result["content"][0]["text"])
        assert payload["error_code"] == "api_error"
        assert payload["retryable"] is False

    def test_no_session_returns_session_expired_code(self):
        handlers = build_handlers(lambda: None)
        result = handlers["browse_recordings"]()
        payload = json.loads(result["content"][0]["text"])
        assert payload["error_code"] == "session_expired"
        assert payload["retryable"] is False


# ---------------------------------------------------------------------------
# Inline validation errors in handlers use structured codes
# ---------------------------------------------------------------------------


class TestMutateRecordingValidation:
    def _handlers(self):
        mock_client = MagicMock()
        return build_handlers(lambda: mock_client)

    def test_rename_without_new_name_returns_validation_code(self):
        handlers = self._handlers()
        result = handlers["mutate_recording"](recording_id="x", mutation="rename")
        payload = json.loads(result["content"][0]["text"])
        assert payload["error_code"] == "validation"
        assert payload["retryable"] is False

    def test_unknown_mutation_returns_validation_code(self):
        handlers = self._handlers()
        result = handlers["mutate_recording"](recording_id="x", mutation="explode")
        payload = json.loads(result["content"][0]["text"])
        assert payload["error_code"] == "validation"
        assert payload["retryable"] is False


class TestProcessRecordingValidation:
    def _handlers(self):
        mock_client = MagicMock()
        return build_handlers(lambda: mock_client)

    def test_invalid_wait_mode_returns_validation_code(self):
        handlers = self._handlers()
        result = handlers["process_recording"](recording_id="x", wait="invalid")
        payload = json.loads(result["content"][0]["text"])
        assert payload["error_code"] == "validation"
        assert payload["retryable"] is False


# ---------------------------------------------------------------------------
# _write_event writes session_expired events to file
# ---------------------------------------------------------------------------


class TestWriteEvent:
    def test_writes_session_expired_event(self, tmp_path, monkeypatch):
        events_file = tmp_path / "events.jsonl"
        monkeypatch.setattr("plaud_tools.mcp._events_path", lambda: events_file)

        _write_event("session_expired", reason="token_expired")

        assert events_file.exists()
        lines = events_file.read_text(encoding="utf-8").splitlines()
        assert len(lines) == 1
        record = json.loads(lines[0])
        assert record["type"] == "session_expired"
        assert record["reason"] == "token_expired"
        assert "ts" in record

    def test_write_event_never_raises(self, tmp_path, monkeypatch):
        """_write_event must not propagate even when the path is unwritable."""
        monkeypatch.setattr(
            "plaud_tools.mcp._events_path",
            lambda: Path("/this/does/not/exist/events.jsonl"),
        )
        # Should not raise
        _write_event("session_expired", reason="test")

    def test_session_expired_via_handler_writes_event(self, tmp_path, monkeypatch):
        """Hitting PlaudSessionExpiredError through _call writes an event."""
        events_file = tmp_path / "events.jsonl"
        monkeypatch.setattr("plaud_tools.mcp._events_path", lambda: events_file)

        mock_client = MagicMock()
        mock_client.list_recordings.side_effect = PlaudSessionExpiredError("expired")
        handlers = build_handlers(lambda: mock_client)
        handlers["browse_recordings"]()

        assert events_file.exists()
        lines = events_file.read_text(encoding="utf-8").splitlines()
        assert len(lines) == 1
        record = json.loads(lines[0])
        assert record["type"] == "session_expired"
        # Diagnostic context (issue #78) — every event carries the MCP's view
        # of session state so the tray log + mcp.log carry enough info to root
        # cause without needing to reproduce.
        assert record["reason"] == "token_expired"
        assert "mcp_pid" in record
        assert "mcp_version" in record
        assert "env_token_present" in record
        assert "store_source" in record

    def test_no_session_event_includes_diagnostic(self, tmp_path, monkeypatch):
        events_file = tmp_path / "events.jsonl"
        monkeypatch.setattr("plaud_tools.mcp._events_path", lambda: events_file)
        handlers = build_handlers(lambda: None)
        handlers["browse_recordings"]()
        record = json.loads(events_file.read_text(encoding="utf-8").splitlines()[0])
        assert record["reason"] == "no_session"
        assert "mcp_pid" in record
        assert "mcp_version" in record
        assert "store_source" in record


# ---------------------------------------------------------------------------
# Session diagnostics (issue #78) — _diagnose_session_state / _emit_session_expired
# ---------------------------------------------------------------------------


class TestDiagnoseSessionState:
    """The diagnostic snapshot must include identifying metadata without leaking the token."""

    def test_includes_pid_and_version(self, monkeypatch):
        monkeypatch.delenv("PLAUD_ACCESS_TOKEN", raising=False)
        diag = _diagnose_session_state()
        assert isinstance(diag["mcp_pid"], int)
        assert isinstance(diag["mcp_version"], str)
        assert diag["env_token_present"] is False

    def test_env_token_present_when_set(self, monkeypatch):
        monkeypatch.setenv("PLAUD_ACCESS_TOKEN", "abc123")
        diag = _diagnose_session_state()
        assert diag["env_token_present"] is True
        # env source populates store_source via the env branch in load_with_source
        assert diag["store_source"] == "env"

    def test_no_token_bytes_leak(self, monkeypatch):
        monkeypatch.setenv("PLAUD_ACCESS_TOKEN", "secret-token-do-not-leak")
        diag = _diagnose_session_state()
        # Walk all values; none should contain the token bytes.
        for v in diag.values():
            assert "secret-token-do-not-leak" not in str(v)

    def test_token_typ_extracted_from_jwt(self, monkeypatch, tmp_path):
        """When a stored token is a JWT, the typ header claim is surfaced."""
        import base64 as _b64

        def _b64u(s: str) -> str:
            return _b64.urlsafe_b64encode(s.encode()).decode().rstrip("=")

        fake_jwt = ".".join([_b64u('{"alg":"HS256","typ":"UT"}'), _b64u('{"exp":9999999999}'), "sig"])
        from plaud_tools.session import FileSessionStore, PlaudSession, SessionStore

        session_path = tmp_path / "session.json"
        FileSessionStore(session_path).save(PlaudSession(access_token=fake_jwt, region="us"))

        # Force the diagnostic to use the file store by making keyring unavailable.
        import plaud_tools.mcp as mcp_mod

        class _StoreFromTmp(SessionStore):
            def __init__(self):
                # Disable DPAPI so this test pins the file_store fallback
                # specifically; the JWT typ extraction is what we care about.
                super().__init__(path=session_path, dpapi_path=None)
                self._load_keyring_module = lambda: None  # type: ignore[method-assign]

        monkeypatch.setattr(mcp_mod, "SessionStore", _StoreFromTmp)
        monkeypatch.delenv("PLAUD_ACCESS_TOKEN", raising=False)
        diag = _diagnose_session_state()
        assert diag["store_source"] == "file"
        assert diag["token_typ"] == "UT"


# ---------------------------------------------------------------------------
# Tray: _show_session_expired_toast is called when event is consumed
# ---------------------------------------------------------------------------


class TestTraySessionExpiredToast:
    """Verify that the tray event poll loop calls _show_session_expired_toast."""

    def test_show_session_expired_toast_winrt_path(self, monkeypatch):
        """If winrt is importable, CreateToastNotifier is used.

        After the module-level winrt detection refactor, tests patch the cached
        winrt names on the toasts module directly.
        """
        from plaud_tools.tray import toasts

        mock_notifier = MagicMock()
        mock_manager = MagicMock()
        mock_manager.create_toast_notifier.return_value = mock_notifier
        mock_xml_doc_cls = MagicMock(return_value=MagicMock())
        mock_toast_cls = MagicMock(return_value=MagicMock())

        monkeypatch.setattr(toasts, "_WINRT_AVAILABLE", True)
        monkeypatch.setattr(toasts, "_WINRT_TNM", mock_manager)
        monkeypatch.setattr(toasts, "_WINRT_TN", mock_toast_cls)
        monkeypatch.setattr(toasts, "_WINRT_XML", mock_xml_doc_cls)

        import plaud_tools.tray_app as tray_app

        tray_app._show_session_expired_toast()

        mock_manager.create_toast_notifier.assert_called_once_with("PlaudTools.TrayApp")
        mock_notifier.show.assert_called_once()

    def test_show_session_expired_toast_powershell_fallback(self, monkeypatch):
        """Without winrt, a hidden PowerShell process is spawned."""
        import sys

        if sys.platform != "win32":
            pytest.skip("PowerShell fallback is Windows-only")

        from plaud_tools.tray import toasts

        monkeypatch.setattr(toasts, "_WINRT_AVAILABLE", False)

        spawned: list[tuple] = []

        def fake_popen(args, **kwargs):
            spawned.append(tuple(args))
            return MagicMock()

        monkeypatch.setattr("plaud_tools.tray.process_launch.subprocess.Popen", fake_popen)

        import plaud_tools.tray_app as tray_app

        tray_app._show_session_expired_toast()

        assert any("powershell" in a[0].lower() for a in spawned)

    def test_show_session_expired_toast_no_exception_on_failure(self, monkeypatch):
        """_show_session_expired_toast must never propagate exceptions."""
        import sys

        if sys.platform != "win32":
            pytest.skip("PowerShell fallback is Windows-only")

        from plaud_tools.tray import toasts

        monkeypatch.setattr(toasts, "_WINRT_AVAILABLE", False)

        def boom(*a, **kw):
            raise OSError("no powershell")

        monkeypatch.setattr("plaud_tools.tray.process_launch.subprocess.Popen", boom)

        import plaud_tools.tray_app as tray_app

        # Should not raise
        tray_app._show_session_expired_toast()

    def test_event_poll_loop_calls_toast_on_session_expired(self, tmp_path, monkeypatch):
        """_event_poll_loop reads events.jsonl, fires toast, and opens LoginWindow."""
        import json
        import time

        # Write a session_expired event to a temp events file
        events_file = tmp_path / "events.jsonl"
        events_file.write_text(
            json.dumps({"type": "session_expired", "ts": time.time(), "reason": "token_expired"}) + "\n",
            encoding="utf-8",
        )

        import plaud_tools.tray_app as tray_app

        toast_calls: list[int] = []
        login_calls: list[int] = []

        monkeypatch.setattr(tray_app, "_events_path", lambda: events_file)
        monkeypatch.setattr(tray_app, "_show_session_expired_toast", lambda: toast_calls.append(1))

        app = tray_app.TrayApp.__new__(tray_app.TrayApp)
        app._root = MagicMock()
        # Capture the call to _open_login via root.after
        app._root.after = MagicMock(side_effect=lambda delay, fn: login_calls.append(1))
        app._login_win = MagicMock()

        # Patch time.sleep so the loop runs immediately without waiting 5 s
        sleep_count = [0]

        def fast_sleep(n):
            sleep_count[0] += 1
            if sleep_count[0] > 1:
                raise SystemExit("stop loop")

        monkeypatch.setattr(tray_app.time, "sleep", fast_sleep)

        with pytest.raises(SystemExit):
            app._event_poll_loop()

        assert len(toast_calls) == 1, "toast should have been called once"
        # The events file is claimed via an atomic rename-then-read (#162),
        # not read-then-truncate in place, so after processing the original
        # path is gone rather than left behind empty. _write_event (mcp.py)
        # recreates it on the next append via `path.open("a", ...)`.
        assert not events_file.exists()


# ---------------------------------------------------------------------------
# _write_event size-based rotation (Wave 0 / A4)
# ---------------------------------------------------------------------------


class TestWriteEventRotation:
    """Verify that _write_event rotates events.jsonl → events.jsonl.1 past the cap.

    All paths are pinned under tmp_path so the real user state is never touched.
    """

    def test_rotation_occurs_when_file_exceeds_cap(self, tmp_path, monkeypatch):
        """After crossing _EVENTS_MAX_BYTES, the next write rotates the file.

        Pre-create a file that is exactly at the cap, write one event, and
        assert that:
        - events.jsonl.1 now exists (the old file was renamed there)
        - events.jsonl exists and contains exactly the new event
        - no exception escaped _write_event
        """
        from plaud_tools import mcp as mcp_mod

        events_file = tmp_path / "events.jsonl"
        monkeypatch.setattr(mcp_mod, "_events_path", lambda: events_file)

        # Pre-fill the file to exactly the rotation threshold so the next
        # write triggers rotation.
        events_file.write_bytes(b"x" * mcp_mod._EVENTS_MAX_BYTES)

        # Must not raise
        _write_event("session_expired", reason="rotation_test")

        rotated = tmp_path / "events.jsonl.1"
        assert rotated.exists(), "events.jsonl.1 should exist after rotation"
        assert rotated.stat().st_size == mcp_mod._EVENTS_MAX_BYTES, (
            "events.jsonl.1 should contain the original (pre-rotation) content"
        )

        assert events_file.exists(), "events.jsonl should exist after rotation"
        lines = events_file.read_text(encoding="utf-8").splitlines()
        assert len(lines) == 1, "new events.jsonl should contain exactly the new event"
        record = json.loads(lines[0])
        assert record["type"] == "session_expired"
        assert record["reason"] == "rotation_test"

    def test_rotation_replaces_existing_dot_one(self, tmp_path, monkeypatch):
        """If events.jsonl.1 already exists it is silently overwritten.

        On Windows Path.rename raises FileExistsError when the destination is
        present; os.replace must be used instead.  This test confirms no
        exception escapes and the stale .1 is replaced.
        """
        from plaud_tools import mcp as mcp_mod

        events_file = tmp_path / "events.jsonl"
        rotated = tmp_path / "events.jsonl.1"
        monkeypatch.setattr(mcp_mod, "_events_path", lambda: events_file)

        # Existing .1 with distinct content so we can verify it was replaced.
        stale_content = b"stale content from a previous rotation"
        rotated.write_bytes(stale_content)

        # events.jsonl at the cap so the next write triggers rotation.
        events_file.write_bytes(b"y" * mcp_mod._EVENTS_MAX_BYTES)

        _write_event("session_expired", reason="replace_test")

        assert rotated.exists()
        # The stale content must be gone — .1 now holds the pre-rotation file.
        assert rotated.read_bytes() != stale_content, "events.jsonl.1 should have been replaced by os.replace"
        assert rotated.stat().st_size == mcp_mod._EVENTS_MAX_BYTES

        lines = events_file.read_text(encoding="utf-8").splitlines()
        assert len(lines) == 1
        record = json.loads(lines[0])
        assert record["reason"] == "replace_test"

    def test_failed_rotation_does_not_raise(self, tmp_path, monkeypatch):
        """A broken os.replace (e.g. locked file, read-only dir) must not propagate.

        The outer try/except in _write_event wraps both rotation and append, so
        even if the rotation fails the append can still succeed.  But the key
        guarantee — no exception escapes — must hold regardless.
        """
        import plaud_tools.mcp as _mcp_module
        from plaud_tools import mcp as mcp_mod

        events_file = tmp_path / "events.jsonl"
        monkeypatch.setattr(mcp_mod, "_events_path", lambda: events_file)

        # File at cap so rotation is attempted.
        events_file.write_bytes(b"z" * mcp_mod._EVENTS_MAX_BYTES)

        def _boom(*args, **kwargs):
            raise OSError("simulated locked file")

        monkeypatch.setattr(_mcp_module.os, "replace", _boom)

        # Must not raise even though os.replace is broken.
        _write_event("session_expired", reason="never_raises_test")

    def test_no_rotation_below_cap(self, tmp_path, monkeypatch):
        """Files smaller than _EVENTS_MAX_BYTES must not be rotated."""
        from plaud_tools import mcp as mcp_mod

        events_file = tmp_path / "events.jsonl"
        monkeypatch.setattr(mcp_mod, "_events_path", lambda: events_file)

        # Write initial event without hitting the cap.
        events_file.write_bytes(b"a" * (mcp_mod._EVENTS_MAX_BYTES - 1))

        _write_event("session_expired", reason="no_rotation_test")

        rotated = tmp_path / "events.jsonl.1"
        assert not rotated.exists(), "events.jsonl.1 must NOT exist below cap"


# ---------------------------------------------------------------------------
# delete_recording confirm gate (Wave 2 / C6 — Decision D4)
# ---------------------------------------------------------------------------


class TestDeleteRecordingConfirmGate:
    """Verify that delete_recording enforces the confirm=True gate (D4).

    The MCP surface cannot show interactive prompts over stdio, so the handler
    must block deletes unless the caller explicitly passes confirm=True —
    the LLM-native equivalent of the CLI's --yes flag.
    """

    def _handlers(self):
        mock_client = MagicMock()
        # delete_recordings must not be called unless confirm=True
        mock_client.delete_recordings = MagicMock()
        return build_handlers(lambda: mock_client), mock_client

    def test_delete_without_confirm_returns_validation_error(self):
        """Calling delete_recording without confirm → validation error, no delete."""
        handlers, mock_client = self._handlers()

        result = handlers["delete_recording"](recording_id="rec-abc")

        payload = json.loads(result["content"][0]["text"])
        assert payload["error_code"] == "validation"
        assert payload["retryable"] is False
        assert "confirm" in payload["error"].lower()
        # The actual delete must NOT have been attempted.
        mock_client.delete_recordings.assert_not_called()

    def test_delete_with_confirm_false_returns_validation_error(self):
        """Passing confirm=False explicitly also triggers the gate."""
        handlers, mock_client = self._handlers()

        result = handlers["delete_recording"](recording_id="rec-abc", confirm=False)

        payload = json.loads(result["content"][0]["text"])
        assert payload["error_code"] == "validation"
        assert payload["retryable"] is False
        mock_client.delete_recordings.assert_not_called()

    def test_delete_with_confirm_true_proceeds(self):
        """With confirm=True the handler invokes the client delete method."""
        handlers, mock_client = self._handlers()
        # Make delete_recordings return None (success)
        mock_client.delete_recordings.return_value = None

        result = handlers["delete_recording"](recording_id="rec-abc", confirm=True)

        payload = json.loads(result["content"][0]["text"])
        assert payload.get("ok") is True
        assert payload["recording_id"] == "rec-abc"
        # Exactly one call with the correct ID list.
        mock_client.delete_recordings.assert_called_once_with(["rec-abc"])
