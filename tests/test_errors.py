"""Tests for PlaudApiError.from_http_error and PlaudApiError.classify — issue #86.

Acceptance criteria:
- PlaudApiError.from_http_error(exc) classmethod encodes body-parsing + Plaud envelope extraction
- PlaudApiError.classify() -> tuple[str, bool] encodes 404/429/5xx mapping
- Old _http_error_to_api_error and _classify_api_error symbols are completely removed
"""
from __future__ import annotations

import importlib
import io
import json
from urllib.error import HTTPError

import pytest

from plaud_tools.errors import PlaudApiError


# ---------------------------------------------------------------------------
# Helper: build a urllib.error.HTTPError with a readable body
# ---------------------------------------------------------------------------

def _make_http_error(status: int, body: bytes, content_type: str = "application/json") -> HTTPError:
    headers = {"Content-Type": content_type}
    fp = io.BytesIO(body)
    return HTTPError(url="https://example.com/api", code=status, msg="Error", hdrs=headers, fp=fp)


# ---------------------------------------------------------------------------
# PlaudApiError.from_http_error classmethod
# ---------------------------------------------------------------------------

class TestFromHttpError:
    def test_json_envelope_with_msg(self):
        body = json.dumps({"status": -1, "msg": "rate limit exceeded, retry after 30s"}).encode()
        exc = _make_http_error(429, body)
        err = PlaudApiError.from_http_error(exc)

        assert isinstance(err, PlaudApiError)
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
        assert "something went wrong" in str(err)
        assert err.raw_body is not None

    def test_non_json_body(self):
        body = b"<html><body>Bad Gateway</body></html>"
        exc = _make_http_error(502, body, content_type="text/html")
        err = PlaudApiError.from_http_error(exc)

        assert err.http_status == 502
        assert err.plaud_msg is None
        assert err.plaud_code is None
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
        assert len(err.raw_body) <= 501  # 500 chars + possible ellipsis char
        assert err.raw_body.endswith("…")

    def test_returns_plaud_api_error_instance(self):
        exc = _make_http_error(401, b"Unauthorized")
        err = PlaudApiError.from_http_error(exc)
        assert isinstance(err, PlaudApiError)

    def test_message_field_used_as_fallback_for_msg(self):
        body = json.dumps({"message": "access denied"}).encode()
        exc = _make_http_error(403, body)
        err = PlaudApiError.from_http_error(exc)

        assert err.plaud_msg == "access denied"
        assert "access denied" in str(err)

    def test_status_only_path(self):
        """When body is missing entirely, falls back to status-code string."""
        exc = _make_http_error(404, b"")
        err = PlaudApiError.from_http_error(exc)

        assert err.http_status == 404
        assert "404" in str(err)
        assert err.raw_body is None
        assert err.plaud_msg is None


# ---------------------------------------------------------------------------
# PlaudApiError.classify() method
# ---------------------------------------------------------------------------

class TestClassify:
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

    def test_returns_tuple(self):
        result = self._make_err(500).classify()
        assert isinstance(result, tuple)
        assert len(result) == 2


# ---------------------------------------------------------------------------
# Deletion test: old symbols must NOT exist
# ---------------------------------------------------------------------------

class TestOldSymbolsDeleted:
    def test_http_error_to_api_error_gone_from_transport(self):
        """_http_error_to_api_error must not be importable from transport."""
        import plaud_tools.transport as transport_mod
        assert not hasattr(transport_mod, "_http_error_to_api_error"), (
            "_http_error_to_api_error still exists in transport — delete it"
        )

    def test_classify_api_error_gone_from_mcp(self):
        """_classify_api_error must not be importable from mcp."""
        import plaud_tools.mcp as mcp_mod
        assert not hasattr(mcp_mod, "_classify_api_error"), (
            "_classify_api_error still exists in mcp — delete it"
        )
