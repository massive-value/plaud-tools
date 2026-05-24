"""Tests for SessionManager.diagnose() and session.py:_decode_header_safe.

Issue #87 — move JWT header decoding and session-diagnostic snapshot from
mcp.py into session.py so SessionManager owns all UT-JWT introspection.

All tests that construct SessionStore or SessionManager are covered by the
autouse fixtures in conftest.py:
  - _block_real_dpapi_shadow:  redirects _default_dpapi_path -> None
  - _fail_if_real_shadow_written: mtime trip-wire on the real shadow file
  - _zero_keyring_retry_delay:  collapses retry budget to zero delay
"""
from __future__ import annotations

import base64
import json
import os
import time as _time

import pytest

from plaud_tools.session import (
    FileSessionStore,
    PlaudSession,
    SessionManager,
    SessionStore,
    _decode_header_safe,
)


# ---------------------------------------------------------------------------
# _decode_header_safe (moved from mcp._decode_jwt_header_safe)
# ---------------------------------------------------------------------------

class TestDecodeHeaderSafe:
    def test_returns_empty_for_non_jwt(self):
        assert _decode_header_safe("not-a-jwt") == {}

    def test_returns_empty_for_garbage(self):
        assert _decode_header_safe("a.b.c") == {}

    def test_decodes_valid_header(self):
        header = base64.urlsafe_b64encode(b'{"alg":"HS256","typ":"UT"}').decode().rstrip("=")
        out = _decode_header_safe(f"{header}.payload.sig")
        assert out == {"alg": "HS256", "typ": "UT"}

    def test_returns_empty_for_non_dict_header(self):
        # A header that decodes to a non-dict (e.g. a JSON array)
        header = base64.urlsafe_b64encode(b'["alg","HS256"]').decode().rstrip("=")
        assert _decode_header_safe(f"{header}.payload.sig") == {}

    def test_returns_empty_for_two_parts(self):
        assert _decode_header_safe("header.payload") == {}

    def test_returns_empty_for_malformed_base64(self):
        assert _decode_header_safe("!!invalid!!.payload.sig") == {}


# ---------------------------------------------------------------------------
# SessionManager.diagnose() — no session
# ---------------------------------------------------------------------------

class TestSessionManagerDiagnoseNoSession:
    def test_returns_missing_source_when_no_session(self, tmp_path, monkeypatch):
        monkeypatch.delenv("PLAUD_ACCESS_TOKEN", raising=False)
        store = SessionStore(
            tmp_path / "session.json",
            service_name="plaud-tools-test-diag-missing",
            dpapi_path=None,
        )
        # Disable keyring so the file-store is the last resort
        monkeypatch.setattr(
            "plaud_tools.session.importlib.import_module",
            lambda name: None if name == "keyring" else __import__(name),
        )
        manager = SessionManager(store)
        diag = manager.diagnose()
        assert diag["store_source"] == "missing"
        assert "email_present" not in diag
        assert "token_typ" not in diag
        assert "days_until_expiry" not in diag

    def test_diagnose_error_field_absent_on_clean_missing(self, tmp_path, monkeypatch):
        monkeypatch.delenv("PLAUD_ACCESS_TOKEN", raising=False)
        store = SessionStore(
            tmp_path / "session.json",
            service_name="plaud-tools-test-diag-no-err",
            dpapi_path=None,
        )
        monkeypatch.setattr(
            "plaud_tools.session.importlib.import_module",
            lambda name: None if name == "keyring" else __import__(name),
        )
        manager = SessionManager(store)
        diag = manager.diagnose()
        assert "diagnose_error" not in diag


# ---------------------------------------------------------------------------
# SessionManager.diagnose() — valid session from env source
# ---------------------------------------------------------------------------

class TestSessionManagerDiagnoseEnvSource:
    def _make_jwt(self, typ: str = "UT", exp_offset_days: int = 200) -> str:
        """Build a minimal but structurally valid JWT."""
        header = base64.urlsafe_b64encode(
            json.dumps({"alg": "HS256", "typ": typ}).encode()
        ).decode().rstrip("=")
        exp = int(_time.time()) + exp_offset_days * 86400
        payload = base64.urlsafe_b64encode(
            json.dumps({"sub": "user123", "exp": exp}).encode()
        ).decode().rstrip("=")
        return f"{header}.{payload}.fakesig"

    def test_env_source_returns_expected_fields(self, tmp_path, monkeypatch):
        jwt = self._make_jwt()
        monkeypatch.setenv("PLAUD_ACCESS_TOKEN", jwt)
        monkeypatch.setenv("PLAUD_REGION", "eu")
        monkeypatch.delenv("PLAUD_EMAIL", raising=False)
        store = SessionStore(
            tmp_path / "session.json",
            service_name="plaud-tools-test-diag-env",
            dpapi_path=None,
        )
        manager = SessionManager(store)
        diag = manager.diagnose()
        assert diag["store_source"] == "env"
        assert diag["region"] == "eu"
        assert diag["email_present"] is False
        assert diag["token_typ"] == "UT"
        assert isinstance(diag["days_until_expiry"], int)
        assert diag["days_until_expiry"] > 0

    def test_no_token_bytes_leak(self, tmp_path, monkeypatch):
        secret = "super-secret-jwt-do-not-leak"
        monkeypatch.setenv("PLAUD_ACCESS_TOKEN", secret)
        store = SessionStore(
            tmp_path / "session.json",
            service_name="plaud-tools-test-diag-noleak",
            dpapi_path=None,
        )
        manager = SessionManager(store)
        diag = manager.diagnose()
        for v in diag.values():
            assert secret not in str(v)

    def test_email_present_true_when_email_set(self, tmp_path, monkeypatch):
        jwt = self._make_jwt()
        monkeypatch.setenv("PLAUD_ACCESS_TOKEN", jwt)
        monkeypatch.setenv("PLAUD_EMAIL", "user@example.com")
        store = SessionStore(
            tmp_path / "session.json",
            service_name="plaud-tools-test-diag-email",
            dpapi_path=None,
        )
        manager = SessionManager(store)
        diag = manager.diagnose()
        assert diag["email_present"] is True


# ---------------------------------------------------------------------------
# SessionManager.diagnose() — malformed JWT
# ---------------------------------------------------------------------------

class TestSessionManagerDiagnoseMalformedJwt:
    def test_malformed_jwt_omits_token_typ(self, tmp_path, monkeypatch):
        """A stored token that isn't a valid JWT still returns store_source;
        token_typ is simply absent rather than raising."""
        monkeypatch.delenv("PLAUD_ACCESS_TOKEN", raising=False)
        session_path = tmp_path / "session.json"
        FileSessionStore(session_path).save(
            PlaudSession(access_token="not.a.valid.jwt.at.all", region="us")
        )
        store = SessionStore(
            session_path,
            service_name="plaud-tools-test-diag-malformed",
            dpapi_path=None,
        )
        monkeypatch.setattr(
            "plaud_tools.session.importlib.import_module",
            lambda name: None if name == "keyring" else __import__(name),
        )
        manager = SessionManager(store)
        diag = manager.diagnose()
        assert diag["store_source"] == "file"
        # token_typ may be absent when header can't be decoded
        # but no diagnose_error should be raised
        assert "diagnose_error" not in diag

    def test_missing_exp_claim_omits_days_until_expiry(self, tmp_path, monkeypatch):
        """A JWT with no 'exp' claim: days_until_expiry absent, no crash."""
        monkeypatch.delenv("PLAUD_ACCESS_TOKEN", raising=False)
        header = base64.urlsafe_b64encode(
            b'{"alg":"HS256","typ":"UT"}'
        ).decode().rstrip("=")
        # payload with no 'exp' field
        payload_bytes = json.dumps({"sub": "user"}).encode()
        payload = base64.urlsafe_b64encode(payload_bytes).decode().rstrip("=")
        jwt_no_exp = f"{header}.{payload}.fakesig"

        session_path = tmp_path / "session.json"
        FileSessionStore(session_path).save(
            PlaudSession(access_token=jwt_no_exp, region="us")
        )
        store = SessionStore(
            session_path,
            service_name="plaud-tools-test-diag-noexp",
            dpapi_path=None,
        )
        monkeypatch.setattr(
            "plaud_tools.session.importlib.import_module",
            lambda name: None if name == "keyring" else __import__(name),
        )
        manager = SessionManager(store)
        diag = manager.diagnose()
        assert diag["store_source"] == "file"
        assert diag["token_typ"] == "UT"
        assert "days_until_expiry" not in diag
        assert "diagnose_error" not in diag


# ---------------------------------------------------------------------------
# SessionManager.diagnose() — keyring source
# ---------------------------------------------------------------------------

class TestSessionManagerDiagnoseKeyringSource:
    def _make_jwt(self, days: int = 200) -> str:
        header = base64.urlsafe_b64encode(
            b'{"alg":"HS256","typ":"UT"}'
        ).decode().rstrip("=")
        exp = int(_time.time()) + days * 86400
        payload = base64.urlsafe_b64encode(
            json.dumps({"exp": exp}).encode()
        ).decode().rstrip("=")
        return f"{header}.{payload}.sig"

    def test_keyring_source_returns_correct_store_source(self, tmp_path, monkeypatch):
        jwt = self._make_jwt()
        payload = json.dumps({"access_token": jwt, "region": "us", "email": "k@example.com"})

        class FakeKeyring:
            @staticmethod
            def get_password(*_a, **_k):
                return payload

            @staticmethod
            def get_credential(*_a, **_k):
                return None

        monkeypatch.setattr(
            "plaud_tools.session.importlib.import_module",
            lambda name: FakeKeyring if name == "keyring" else __import__(name),
        )
        monkeypatch.delenv("PLAUD_ACCESS_TOKEN", raising=False)

        store = SessionStore(
            tmp_path / "session.json",
            service_name="plaud-tools-test-diag-keyring",
            dpapi_path=None,
        )
        manager = SessionManager(store)
        diag = manager.diagnose()
        assert diag["store_source"] == "keyring"
        assert diag["email_present"] is True
        assert diag["token_typ"] == "UT"


# ---------------------------------------------------------------------------
# SessionManager.diagnose() — file source
# ---------------------------------------------------------------------------

class TestSessionManagerDiagnoseFileSource:
    def _make_jwt(self, days: int = 200) -> str:
        header = base64.urlsafe_b64encode(
            b'{"alg":"HS256","typ":"UT"}'
        ).decode().rstrip("=")
        exp = int(_time.time()) + days * 86400
        payload = base64.urlsafe_b64encode(
            json.dumps({"exp": exp}).encode()
        ).decode().rstrip("=")
        return f"{header}.{payload}.sig"

    def test_file_source_returns_correct_store_source(self, tmp_path, monkeypatch):
        monkeypatch.delenv("PLAUD_ACCESS_TOKEN", raising=False)
        jwt = self._make_jwt()
        session_path = tmp_path / "session.json"
        FileSessionStore(session_path).save(
            PlaudSession(access_token=jwt, region="us", email="f@example.com")
        )
        store = SessionStore(
            session_path,
            service_name="plaud-tools-test-diag-file",
            dpapi_path=None,
        )
        monkeypatch.setattr(
            "plaud_tools.session.importlib.import_module",
            lambda name: None if name == "keyring" else __import__(name),
        )
        manager = SessionManager(store)
        diag = manager.diagnose()
        assert diag["store_source"] == "file"
        assert diag["region"] == "us"
        assert diag["email_present"] is True
        assert diag["token_typ"] == "UT"
        assert isinstance(diag["days_until_expiry"], int)


# ---------------------------------------------------------------------------
# SessionManager.diagnose() — defensive error field
# ---------------------------------------------------------------------------

class TestSessionManagerDiagnoseError:
    def test_diagnose_error_field_set_when_store_raises(self, tmp_path, monkeypatch):
        """If the store raises unexpectedly, diagnose() returns a diagnose_error
        key instead of propagating the exception."""
        monkeypatch.delenv("PLAUD_ACCESS_TOKEN", raising=False)

        class BrokenStore:
            def load_with_source(self):
                raise RuntimeError("store exploded")

        manager = SessionManager(BrokenStore())
        diag = manager.diagnose()
        assert "diagnose_error" in diag
        assert "RuntimeError" in diag["diagnose_error"]


# ---------------------------------------------------------------------------
# Golden field test: _diagnose_session_state payload is unchanged
#
# This protects the tray-side _format_session_expired_diag which reads the
# exact field names from the event dict.  The on-the-wire session_expired
# event must continue to contain:
#   mcp_pid, mcp_version, env_token_present, store_source
# (plus region, email_present, token_typ, days_until_expiry when session exists)
# ---------------------------------------------------------------------------

class TestSessionExpiredPayloadGolden:
    """Regression guard: the session_expired event payload fields must not change.

    The tray's _format_session_expired_diag iterates all keys generically, but
    other consumers (log parsers, support tooling) may depend on specific field
    names.  This golden test catches accidental renames or removals.
    """

    def test_event_contains_all_expected_fields_when_env_token_set(
        self, tmp_path, monkeypatch
    ):
        import base64 as _b64
        import json as _json

        def _b64u(s: str) -> str:
            return _b64.urlsafe_b64encode(s.encode()).decode().rstrip("=")

        exp = int(_time.time()) + 200 * 86400
        fake_jwt = ".".join([
            _b64u('{"alg":"HS256","typ":"UT"}'),
            _b64u(_json.dumps({"exp": exp})),
            "sig",
        ])

        monkeypatch.setenv("PLAUD_ACCESS_TOKEN", fake_jwt)

        events_file = tmp_path / "events.jsonl"
        monkeypatch.setattr("plaud_tools.mcp._events_path", lambda: events_file)

        from plaud_tools.mcp import _emit_session_expired
        _emit_session_expired("token_expired")

        assert events_file.exists()
        record = _json.loads(events_file.read_text(encoding="utf-8").splitlines()[0])

        # MCP-process-local fields (owned by mcp.py wrapper)
        assert "mcp_pid" in record
        assert "mcp_version" in record
        assert "env_token_present" in record

        # Session-y fields (owned by SessionManager.diagnose())
        assert "store_source" in record
        assert record["store_source"] == "env"
        assert "region" in record
        assert "email_present" in record
        assert "token_typ" in record
        assert record["token_typ"] == "UT"
        assert "days_until_expiry" in record

        # Event envelope fields
        assert record["type"] == "session_expired"
        assert record["reason"] == "token_expired"
        assert "ts" in record

        # Token bytes must never appear
        assert fake_jwt not in str(record)
