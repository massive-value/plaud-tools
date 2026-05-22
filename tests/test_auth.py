from __future__ import annotations

import json

import pytest

from plaud_tools.auth import PlaudAuth
from plaud_tools.errors import PlaudApiError
from plaud_tools.session import SessionStore
from plaud_tools.transport import HttpResponse


class StubTransport:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def request(self, method, url, headers, body=None):
        self.calls.append({"method": method, "url": url, "headers": headers, "body": body})
        return self.responses.pop(0)


def test_login_stores_session_and_uses_browser_like_headers(tmp_path):
    transport = StubTransport(
        [
            HttpResponse(
                200,
                json.dumps({"status": 0, "access_token": "header.payload.sig", "token_type": "bearer"}).encode(),
                {},
            )
        ]
    )
    store = SessionStore(tmp_path / "session.json", service_name="plaud-tools-test-auth-store", account_name="session")
    auth = PlaudAuth(store, transport=transport)
    session = auth.login("user@example.com", "pw", "eu")
    assert session.email == "user@example.com"
    stored = store.load()
    assert stored.region == "eu"
    headers = transport.calls[0]["headers"]
    assert headers["Content-Type"] == "application/x-www-form-urlencoded"
    assert headers["User-Agent"].startswith("Mozilla/5.0")
    assert transport.calls[0]["body"] == b"username=user%40example.com&password=pw"


def test_login_raises_on_bad_credentials_without_storing(tmp_path):
    transport = StubTransport(
        [HttpResponse(200, json.dumps({"status": -2, "msg": "wrong account or password"}).encode(), {})]
    )
    store = SessionStore(tmp_path / "session.json", service_name="plaud-tools-test-auth", account_name="session")
    auth = PlaudAuth(store, transport=transport)
    with pytest.raises(PlaudApiError, match="wrong account or password"):
        auth.login("user@example.com", "bad", "eu")
    assert store.load() is None


def test_login_raises_clean_error_on_http_failure(tmp_path):
    transport = StubTransport([HttpResponse(502, b"", {})])
    auth = PlaudAuth(
        SessionStore(tmp_path / "session.json", service_name="plaud-tools-test-auth", account_name="session"),
        transport=transport,
    )
    with pytest.raises(PlaudApiError, match="HTTP 502"):
        auth.login("user@example.com", "pw", "us")


def test_load_retries_keyring_on_transient_failure(tmp_path, caplog, monkeypatch):
    """Pin the v0.2.2-follow-up retry: a transient keyring exception must not
    cause a spurious logout when the second read succeeds.

    Real-world trigger from issue #78: Windows Credential Manager occasionally
    raises under load. The MCP would treat the resulting ``None`` as
    "user is logged out" and pop a sign-in prompt, even though the very next
    call returned the session cleanly.
    """
    import logging

    from plaud_tools.session import PlaudSession, SessionStore

    payload = json.dumps({"access_token": "header.payload.sig", "region": "us", "email": "u@example.com"})
    call_count = {"n": 0}

    class FlakeyKeyring:
        @staticmethod
        def get_password(*_a, **_k):
            call_count["n"] += 1
            if call_count["n"] == 1:
                raise RuntimeError("transient credential manager hiccup")
            return payload

    monkeypatch.setattr(
        "plaud_tools.session.importlib.import_module",
        lambda name: FlakeyKeyring if name == "keyring" else __import__(name),
    )
    # Don't let the retry delay slow the suite down.
    monkeypatch.setattr(SessionStore, "_KEYRING_RETRY_DELAY_S", 0.0)

    store = SessionStore(tmp_path / "session.json", service_name="plaud-tools-test-retry")

    with caplog.at_level(logging.WARNING, logger="plaud_tools.session"):
        session = store.load()

    assert session is not None, "Expected retry to recover the session"
    assert session.email == "u@example.com"
    assert call_count["n"] == 2, f"Expected exactly 2 keyring reads (1 failure + 1 success), got {call_count['n']}"
    assert any("retrying" in r.message for r in caplog.records), (
        "Expected a warning about retrying; got: " + ", ".join(r.message for r in caplog.records)
    )


def test_load_does_not_retry_on_clean_none(tmp_path, monkeypatch):
    """A clean ``None`` from keyring is the legitimate 'no entry exists' path
    and must not trigger a retry — that would slow every actually-signed-out
    state by the retry delay.
    """
    from plaud_tools.session import SessionStore

    call_count = {"n": 0}

    class EmptyKeyring:
        @staticmethod
        def get_password(*_a, **_k):
            call_count["n"] += 1
            return None  # legitimate "no entry"

    monkeypatch.setattr(
        "plaud_tools.session.importlib.import_module",
        lambda name: EmptyKeyring if name == "keyring" else __import__(name),
    )

    store = SessionStore(tmp_path / "session.json", service_name="plaud-tools-test-no-retry")
    assert store.load() is None
    assert call_count["n"] == 1, f"Expected exactly 1 keyring read for clean None, got {call_count['n']}"


def test_load_retry_gives_up_after_two_consecutive_failures(tmp_path, caplog, monkeypatch):
    """If both attempts raise, surface the failure (return None, fall back to file)."""
    import logging

    from plaud_tools.session import SessionStore

    call_count = {"n": 0}

    class BrokenKeyring:
        @staticmethod
        def get_password(*_a, **_k):
            call_count["n"] += 1
            raise RuntimeError("persistent failure")

    monkeypatch.setattr(
        "plaud_tools.session.importlib.import_module",
        lambda name: BrokenKeyring if name == "keyring" else __import__(name),
    )
    monkeypatch.setattr(SessionStore, "_KEYRING_RETRY_DELAY_S", 0.0)

    store = SessionStore(tmp_path / "session.json", service_name="plaud-tools-test-broken")
    with caplog.at_level(logging.WARNING, logger="plaud_tools.session"):
        assert store.load() is None
    assert call_count["n"] == 2


def test_save_logs_warning_on_keyring_failure(tmp_path, caplog, monkeypatch):
    """Pin the keyring-failure log line.

    Before this was added, `_save_to_keyring` swallowed every exception
    silently and fell back to the file store, which made "saved keyring
    OK but session is gone next launch" symptoms impossible to diagnose
    from the tray log.
    """
    import logging

    from plaud_tools.session import PlaudSession, SessionStore

    class BrokenKeyring:
        @staticmethod
        def set_password(*_a, **_k):
            raise RuntimeError("simulated keyring backend failure")

        @staticmethod
        def get_password(*_a, **_k):
            return None

    monkeypatch.setattr(
        "plaud_tools.session.importlib.import_module",
        lambda name: BrokenKeyring if name == "keyring" else __import__(name),
    )

    store = SessionStore(tmp_path / "session.json", service_name="plaud-tools-test-keyring-warn")
    session = PlaudSession(access_token="header.payload.sig", region="us", email="user@example.com")

    with caplog.at_level(logging.WARNING, logger="plaud_tools.session"):
        store.save(session)

    # File store wrote the fallback.
    assert (tmp_path / "session.json").exists()
    # And the warning was emitted.
    matching = [r for r in caplog.records if "keyring.set_password failed" in r.message]
    assert matching, (
        "Expected a 'keyring.set_password failed' warning when the keyring "
        "backend raises; got none.  Records: "
        + ", ".join(r.message for r in caplog.records)
    )
