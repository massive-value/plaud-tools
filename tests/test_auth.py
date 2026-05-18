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
