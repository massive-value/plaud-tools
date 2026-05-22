"""Tests for UrllibTransport error handling — issue #42."""
from __future__ import annotations

import io
import json
from unittest.mock import patch
from urllib.error import HTTPError, URLError

import pytest

from plaud_tools.errors import PlaudApiError
from plaud_tools.transport import UrllibTransport, _http_error_to_api_error


# ---------------------------------------------------------------------------
# Helper: build a urllib.error.HTTPError with a readable body
# ---------------------------------------------------------------------------

def _make_http_error(status: int, body: bytes, content_type: str = "application/json") -> HTTPError:
    headers = {"Content-Type": content_type}
    fp = io.BytesIO(body)
    # HTTPError signature: url, code, msg, hdrs, fp
    return HTTPError(url="https://example.com/api", code=status, msg="Error", hdrs=headers, fp=fp)


# ---------------------------------------------------------------------------
# _http_error_to_api_error unit tests
# ---------------------------------------------------------------------------

class TestHttpErrorToApiError:
    def test_json_envelope_with_msg(self):
        body = json.dumps({"status": -1, "msg": "rate limit exceeded, retry after 30s"}).encode()
        exc = _make_http_error(429, body)
        err = _http_error_to_api_error(exc)

        assert err.http_status == 429
        assert err.plaud_msg == "rate limit exceeded, retry after 30s"
        assert err.plaud_code == -1
        assert "rate limit exceeded, retry after 30s" in str(err)
        assert "429" in str(err)

    def test_json_envelope_with_code_field(self):
        body = json.dumps({"code": "INVALID_FIELD", "msg": "field 'name' is required"}).encode()
        exc = _make_http_error(422, body)
        err = _http_error_to_api_error(exc)

        assert err.http_status == 422
        assert err.plaud_code == "INVALID_FIELD"
        assert err.plaud_msg == "field 'name' is required"
        assert "field 'name' is required" in str(err)

    def test_opaque_json_no_msg_field(self):
        body = json.dumps({"error": "something went wrong", "details": [1, 2, 3]}).encode()
        exc = _make_http_error(500, body)
        err = _http_error_to_api_error(exc)

        assert err.http_status == 500
        assert err.plaud_msg is None
        # raw body should appear in message since no msg field
        assert "something went wrong" in str(err)
        assert err.raw_body is not None

    def test_non_json_body(self):
        body = b"<html><body>Bad Gateway</body></html>"
        exc = _make_http_error(502, body, content_type="text/html")
        err = _http_error_to_api_error(exc)

        assert err.http_status == 502
        assert err.plaud_msg is None
        assert err.plaud_code is None
        # raw body text should appear in message
        assert "Bad Gateway" in str(err)
        assert err.raw_body is not None

    def test_empty_body(self):
        exc = _make_http_error(503, b"")
        err = _http_error_to_api_error(exc)

        assert err.http_status == 503
        assert err.plaud_msg is None
        assert err.raw_body is None
        assert "503" in str(err)

    def test_body_truncated_to_500_chars(self):
        long_body = b"x" * 1000
        exc = _make_http_error(400, long_body, content_type="text/plain")
        err = _http_error_to_api_error(exc)

        assert err.raw_body is not None
        # raw_body stored on the error is truncated
        assert len(err.raw_body) <= 501  # 500 chars + possible ellipsis char
        assert err.raw_body.endswith("…")

    def test_is_plaud_api_error_subclass(self):
        exc = _make_http_error(401, b"Unauthorized")
        err = _http_error_to_api_error(exc)
        assert isinstance(err, PlaudApiError)

    def test_message_field_used_as_fallback_for_msg(self):
        body = json.dumps({"message": "access denied"}).encode()
        exc = _make_http_error(403, body)
        err = _http_error_to_api_error(exc)

        assert err.plaud_msg == "access denied"
        assert "access denied" in str(err)


# ---------------------------------------------------------------------------
# UrllibTransport integration: verify raised errors carry the new attributes
# ---------------------------------------------------------------------------

class TestUrllibTransportErrorPropagation:
    def test_http_error_surfaces_structured_fields(self):
        body = json.dumps({"status": -429, "msg": "too many requests"}).encode()
        http_exc = _make_http_error(429, body)
        transport = UrllibTransport()

        with patch("plaud_tools.transport.urlopen", side_effect=http_exc):
            with pytest.raises(PlaudApiError) as exc_info:
                transport.request("GET", "https://example.com/api", {})

        err = exc_info.value
        assert err.http_status == 429
        assert err.plaud_msg == "too many requests"
        assert err.plaud_code == -429

    def test_url_error_raises_plain_plaud_api_error(self):
        url_exc = URLError("Connection refused")
        transport = UrllibTransport()

        with patch("plaud_tools.transport.urlopen", side_effect=url_exc):
            with pytest.raises(PlaudApiError) as exc_info:
                transport.request("GET", "https://example.com/api", {})

        err = exc_info.value
        assert err.http_status is None
        assert "Connection refused" in str(err)
