"""Tests for UrllibTransport error handling — issue #42."""

from __future__ import annotations

import io
import json
from unittest.mock import MagicMock, patch
from urllib.error import HTTPError, URLError

import pytest

from plaud_tools.core.errors import PlaudApiError
from plaud_tools.core.transport import _DEFAULT_TIMEOUT, UrllibTransport

# ---------------------------------------------------------------------------
# Helper: build a urllib.error.HTTPError with a readable body
# ---------------------------------------------------------------------------


def _make_http_error(status: int, body: bytes, content_type: str = "application/json") -> HTTPError:
    headers = {"Content-Type": content_type}
    fp = io.BytesIO(body)
    # HTTPError signature: url, code, msg, hdrs, fp
    return HTTPError(url="https://example.com/api", code=status, msg="Error", hdrs=headers, fp=fp)


# ---------------------------------------------------------------------------
# PlaudApiError.from_http_error unit tests (via transport layer)
# ---------------------------------------------------------------------------


class TestHttpErrorToApiError:
    def test_json_envelope_with_msg(self):
        body = json.dumps({"status": -1, "msg": "rate limit exceeded, retry after 30s"}).encode()
        exc = _make_http_error(429, body)
        err = PlaudApiError.from_http_error(exc)

        assert err.http_status == 429
        assert err.plaud_msg == "rate limit exceeded, retry after 30s"
        assert err.plaud_code == -1
        assert "rate limit exceeded, retry after 30s" in str(err)
        assert "429" in str(err)

    def test_json_envelope_with_code_field(self):
        body = json.dumps({"code": "INVALID_FIELD", "msg": "field 'name' is required"}).encode()
        exc = _make_http_error(422, body)
        err = PlaudApiError.from_http_error(exc)

        assert err.http_status == 422
        assert err.plaud_code == "INVALID_FIELD"
        assert err.plaud_msg == "field 'name' is required"
        assert "field 'name' is required" in str(err)

    def test_opaque_json_no_msg_field(self):
        body = json.dumps({"error": "something went wrong", "details": [1, 2, 3]}).encode()
        exc = _make_http_error(500, body)
        err = PlaudApiError.from_http_error(exc)

        assert err.http_status == 500
        assert err.plaud_msg is None
        # raw body should appear in message since no msg field
        assert "something went wrong" in str(err)
        assert err.raw_body is not None

    def test_non_json_body(self):
        body = b"<html><body>Bad Gateway</body></html>"
        exc = _make_http_error(502, body, content_type="text/html")
        err = PlaudApiError.from_http_error(exc)

        assert err.http_status == 502
        assert err.plaud_msg is None
        assert err.plaud_code is None
        # raw body text should appear in message
        assert "Bad Gateway" in str(err)
        assert err.raw_body is not None

    def test_empty_body(self):
        exc = _make_http_error(503, b"")
        err = PlaudApiError.from_http_error(exc)

        assert err.http_status == 503
        assert err.plaud_msg is None
        assert err.raw_body is None
        assert "503" in str(err)

    def test_body_truncated_to_500_chars(self):
        long_body = b"x" * 1000
        exc = _make_http_error(400, long_body, content_type="text/plain")
        err = PlaudApiError.from_http_error(exc)

        assert err.raw_body is not None
        # raw_body stored on the error is truncated
        assert len(err.raw_body) <= 501  # 500 chars + possible ellipsis char
        assert err.raw_body.endswith("…")

    def test_is_plaud_api_error_subclass(self):
        exc = _make_http_error(401, b"Unauthorized")
        err = PlaudApiError.from_http_error(exc)
        assert isinstance(err, PlaudApiError)

    def test_message_field_used_as_fallback_for_msg(self):
        body = json.dumps({"message": "access denied"}).encode()
        exc = _make_http_error(403, body)
        err = PlaudApiError.from_http_error(exc)

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

        with patch("plaud_tools.core.transport.urlopen", side_effect=http_exc):
            with pytest.raises(PlaudApiError) as exc_info:
                transport.request("GET", "https://example.com/api", {})

        err = exc_info.value
        assert err.http_status == 429
        assert err.plaud_msg == "too many requests"
        assert err.plaud_code == -429

    def test_url_error_raises_plain_plaud_api_error(self):
        url_exc = URLError("Connection refused")
        transport = UrllibTransport()

        with patch("plaud_tools.core.transport.urlopen", side_effect=url_exc):
            with pytest.raises(PlaudApiError) as exc_info:
                transport.request("GET", "https://example.com/api", {})

        err = exc_info.value
        assert err.http_status is None
        assert "Connection refused" in str(err)


# ---------------------------------------------------------------------------
# Timeout parameter threading — Wave 0 / A1
# ---------------------------------------------------------------------------


def _make_successful_response() -> MagicMock:
    """Return a minimal mock that satisfies the urlopen context-manager protocol."""
    resp = MagicMock()
    resp.getcode.return_value = 200
    resp.read.return_value = b"{}"
    resp.headers.items.return_value = []
    resp.__enter__ = lambda s: s
    resp.__exit__ = MagicMock(return_value=False)
    return resp


class TestUrllibTransportTimeout:
    """Verify that the timeout value actually reaches urlopen (issue A1)."""

    def test_default_timeout_is_30s(self):
        """Constructor default must be _DEFAULT_TIMEOUT (30 s)."""
        transport = UrllibTransport()
        assert transport._timeout == _DEFAULT_TIMEOUT
        assert _DEFAULT_TIMEOUT == 30.0

    def test_constructor_timeout_is_forwarded_to_urlopen(self):
        """A custom constructor timeout must arrive at urlopen unchanged."""
        transport = UrllibTransport(timeout=45.0)

        with patch(
            "plaud_tools.core.transport.urlopen", return_value=_make_successful_response()
        ) as mock_open:
            transport.request("GET", "https://example.com/api", {})

        _req, kwargs_or_pos = mock_open.call_args[0], mock_open.call_args  # noqa: F841  # destructuring for clarity
        # urlopen(req, timeout=...) — timeout is the second positional arg
        assert mock_open.call_args[1].get("timeout") == 45.0 or mock_open.call_args[0][1] == 45.0

    def test_per_call_timeout_overrides_constructor(self):
        """A per-call timeout= kwarg must take precedence over the constructor default."""
        transport = UrllibTransport(timeout=5.0)

        with patch(
            "plaud_tools.core.transport.urlopen", return_value=_make_successful_response()
        ) as mock_open:
            transport.request("GET", "https://example.com/api", {}, timeout=99.0)

        args = mock_open.call_args
        actual = args[1].get("timeout") if args[1] else args[0][1]
        assert actual == 99.0

    def test_default_30s_reaches_urlopen_without_override(self):
        """With no constructor or call-site override, urlopen sees 30 s."""
        transport = UrllibTransport()

        with patch(
            "plaud_tools.core.transport.urlopen", return_value=_make_successful_response()
        ) as mock_open:
            transport.request("PUT", "https://s3.example.com/upload", {}, b"data")

        args = mock_open.call_args
        actual = args[1].get("timeout") if args[1] else args[0][1]
        assert actual == 30.0


class TestUrllibTransportTimeoutErrors:
    """TimeoutError / socket.timeout must surface as PlaudApiError, not a raw exception."""

    def test_timeout_error_raises_plaud_api_error(self):
        """Built-in TimeoutError (not a URLError subclass) must become PlaudApiError."""
        transport = UrllibTransport(timeout=30.0)

        with patch("plaud_tools.core.transport.urlopen", side_effect=TimeoutError("timed out")):
            with pytest.raises(PlaudApiError) as exc_info:
                transport.request("GET", "https://example.com/api", {})

        err = exc_info.value
        assert err.http_status is None
        assert "timed out" in str(err).lower() or "30" in str(err)

    def test_socket_timeout_raises_plaud_api_error(self):
        """socket.timeout (which may surface directly from urlopen) must become PlaudApiError."""
        transport = UrllibTransport(timeout=30.0)

        with patch("plaud_tools.core.transport.urlopen", side_effect=TimeoutError("socket timed out")):
            with pytest.raises(PlaudApiError) as exc_info:
                transport.request("GET", "https://example.com/api", {})

        err = exc_info.value
        assert err.http_status is None
        assert "timed out" in str(err).lower() or "30" in str(err)

    def test_url_error_with_socket_timeout_reason_raises_plaud_api_error(self):
        """URLError whose reason is a socket.timeout must also surface as PlaudApiError."""
        transport = UrllibTransport()
        cause = TimeoutError("read timed out")
        url_exc = URLError(reason=cause)

        with patch("plaud_tools.core.transport.urlopen", side_effect=url_exc):
            with pytest.raises(PlaudApiError) as exc_info:
                transport.request("GET", "https://example.com/api", {})

        # URLError path raises PlaudApiError (existing behaviour preserved)
        assert exc_info.value.http_status is None

    def test_timeout_message_includes_budget(self):
        """Error message must include the effective timeout value for observability."""
        transport = UrllibTransport(timeout=120.0)

        with patch("plaud_tools.core.transport.urlopen", side_effect=TimeoutError()):
            with pytest.raises(PlaudApiError) as exc_info:
                transport.request("GET", "https://example.com/api", {})

        assert "120" in str(exc_info.value)

    def test_timeout_error_flags_network_error_and_classifies_transient(self):
        """#143: a socket timeout must set network_error=True so classify()
        treats it as a retryable transient rather than a non-retryable
        api_error — a 30s blip must not abort a multi-minute wait."""
        transport = UrllibTransport(timeout=30.0)

        with patch("plaud_tools.core.transport.urlopen", side_effect=TimeoutError("timed out")):
            with pytest.raises(PlaudApiError) as exc_info:
                transport.request("GET", "https://example.com/api", {})

        err = exc_info.value
        assert err.network_error is True
        code, retryable = err.classify()
        assert code == "transient"
        assert retryable is True

    def test_url_error_flags_network_error_and_classifies_transient(self):
        """#143: a connection-level failure (DNS, connection refused, etc.)
        must also be flagged as a retryable transient."""
        transport = UrllibTransport()

        with patch("plaud_tools.core.transport.urlopen", side_effect=URLError("Connection refused")):
            with pytest.raises(PlaudApiError) as exc_info:
                transport.request("GET", "https://example.com/api", {})

        err = exc_info.value
        assert err.network_error is True
        code, retryable = err.classify()
        assert code == "transient"
        assert retryable is True

    def test_http_error_does_not_set_network_error_flag(self):
        """A structured HTTP error response (we did get a reply) must not be
        flagged as a network_error — its classification comes from the
        status code alone."""
        body = json.dumps({"status": -1, "msg": "server error"}).encode()
        http_exc = _make_http_error(500, body)
        transport = UrllibTransport()

        with patch("plaud_tools.core.transport.urlopen", side_effect=http_exc):
            with pytest.raises(PlaudApiError) as exc_info:
                transport.request("GET", "https://example.com/api", {})

        assert exc_info.value.network_error is False
