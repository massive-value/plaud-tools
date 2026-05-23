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


def test_load_retries_then_falls_through_on_persistent_none(tmp_path, monkeypatch):
    """A consistently empty keyring exhausts the retry budget and is then
    treated as a legitimate "no entry" response.

    Confirmed in production: a transient None can look identical to a real
    absence, so SessionStore retries up to ``len(_KEYRING_RETRY_DELAYS_S)+1`` times
    before accepting None.  This test exercises the fall-through path —
    nothing is stored, so every read returns None and we expect exactly the
    configured number of reads.
    """
    from plaud_tools.session import SessionStore

    call_count = {"n": 0}

    class EmptyKeyring:
        @staticmethod
        def get_password(*_a, **_k):
            call_count["n"] += 1
            return None  # consistently absent — should settle after the retry budget

    monkeypatch.setattr(
        "plaud_tools.session.importlib.import_module",
        lambda name: EmptyKeyring if name == "keyring" else __import__(name),
    )
    monkeypatch.setattr("plaud_tools.session.SessionStore._KEYRING_RETRY_DELAY_S", 0.0)

    store = SessionStore(tmp_path / "session.json", service_name="plaud-tools-test-no-retry")
    assert store.load() is None
    assert call_count["n"] == (len(SessionStore._KEYRING_RETRY_DELAYS_S) + 1), (
        f"Expected {(len(SessionStore._KEYRING_RETRY_DELAYS_S) + 1)} keyring reads, got {call_count['n']}"
    )


def test_load_recovers_from_late_keyring(tmp_path, monkeypatch):
    """The MCP cold-start window can leave Windows Credential Manager
    returning None for several reads in a row before settling.  The retry
    budget must be wide enough to ride out a multi-hundred-millisecond stall
    without prematurely concluding the user is signed out.
    """
    import json
    from dataclasses import asdict

    from plaud_tools.session import PlaudSession, SessionStore

    session_data = json.dumps(asdict(PlaudSession(access_token="tok", email="cold@example.com")))
    call_count = {"n": 0}
    settle_after = 4  # first 3 reads return None, 4th returns the entry

    class SlowKeyring:
        @staticmethod
        def get_password(*_a, **_k):
            call_count["n"] += 1
            return None if call_count["n"] < settle_after else session_data

    monkeypatch.setattr(
        "plaud_tools.session.importlib.import_module",
        lambda name: SlowKeyring if name == "keyring" else __import__(name),
    )
    monkeypatch.setattr("plaud_tools.session.SessionStore._KEYRING_RETRY_DELAY_S", 0.0)

    store = SessionStore(tmp_path / "session.json", service_name="plaud-tools-test-late-arrival")
    result = store.load()
    assert result is not None, "Retry budget must cover late-arriving keyring reads"
    assert result.email == "cold@example.com"
    assert call_count["n"] == settle_after


def test_load_recovers_from_transient_none(tmp_path, monkeypatch):
    """First get_password call returns None (Windows Credential Manager hiccup);
    second call succeeds — mirrors the post-v0.2.5 production incident.
    """
    import json
    from dataclasses import asdict

    from plaud_tools.session import PlaudSession, SessionStore

    session_data = json.dumps(asdict(PlaudSession(access_token="tok", email="u@example.com")))
    call_count = {"n": 0}

    class TransientKeyring:
        @staticmethod
        def get_password(*_a, **_k):
            call_count["n"] += 1
            return None if call_count["n"] == 1 else session_data

    monkeypatch.setattr(
        "plaud_tools.session.importlib.import_module",
        lambda name: TransientKeyring if name == "keyring" else __import__(name),
    )
    monkeypatch.setattr("plaud_tools.session.SessionStore._KEYRING_RETRY_DELAY_S", 0.0)

    store = SessionStore(tmp_path / "session.json", service_name="plaud-tools-test-transient-none")
    result = store.load()
    assert result is not None, "Expected retry to recover from transient None"
    assert result.email == "u@example.com"
    assert call_count["n"] == 2


def test_load_retry_gives_up_after_persistent_failures(tmp_path, caplog, monkeypatch):
    """If every attempt raises, surface the failure (return None, fall back to file)."""
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
    assert call_count["n"] == (len(SessionStore._KEYRING_RETRY_DELAYS_S) + 1)


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


# ---------------------------------------------------------------------------
# Legacy plaud-toolkit credential migration
# ---------------------------------------------------------------------------

class _LegacyCred:
    """Minimal stand-in for keyring's Credential class."""
    def __init__(self, username: str, password: str) -> None:
        self.username = username
        self.password = password


class _FakeLegacyKeyring:
    """In-memory keyring fake supporting target+username lookup.

    Mirrors the subset of the keyring module's surface used by
    ``SessionStore``: ``get_password``, ``set_password``,
    ``delete_password``, and ``get_credential(service, None)``.
    """

    def __init__(self, initial: "dict[tuple[str, str], str] | None" = None) -> None:
        self.entries: dict[tuple[str, str], str] = dict(initial or {})
        self.deleted: list[tuple[str, str]] = []
        self.saved: list[tuple[str, str, str]] = []

    def get_password(self, service, account):
        return self.entries.get((service, account))

    def set_password(self, service, account, value):
        self.entries[(service, account)] = value
        self.saved.append((service, account, value))

    def delete_password(self, service, account):
        if (service, account) not in self.entries:
            raise KeyError(f"no such entry {service!r}/{account!r}")
        del self.entries[(service, account)]
        self.deleted.append((service, account))

    def get_credential(self, service, username):
        # Mimic Windows backend: when username is None, return any credential
        # whose target == service.
        for (svc, acct), value in self.entries.items():
            if svc == service and (username is None or username == acct):
                return _LegacyCred(acct, value)
        return None


def test_legacy_keyring_migration_happy_path(tmp_path, monkeypatch, caplog):
    """When only legacy entries exist, load() rewrites under the new naming
    and deletes the legacy entries — a one-shot migration.
    """
    import logging

    from plaud_tools.session import SessionStore

    fake = _FakeLegacyKeyring({
        ("jwt.plaud-toolkit", "jwt"): "header.payload.sig",
        ("profile.plaud-toolkit", "profile"): '{"email":"old@example.com","region":"us"}',
    })
    monkeypatch.setattr(
        "plaud_tools.session.importlib.import_module",
        lambda name: fake if name == "keyring" else __import__(name),
    )
    monkeypatch.setattr(SessionStore, "_KEYRING_RETRY_DELAY_S", 0.0)

    store = SessionStore(tmp_path / "session.json", service_name="plaud-tools", dpapi_path=None)

    with caplog.at_level(logging.INFO, logger="plaud_tools.session"):
        session, source = store.load_with_source()

    assert session is not None
    assert session.access_token == "header.payload.sig"
    assert session.email == "old@example.com"
    assert session.region == "us"
    assert source == "legacy_keyring"

    # New entry exists under the canonical naming.
    assert ("plaud-tools", "session") in fake.entries
    # Legacy entries were cleaned up.
    assert ("jwt.plaud-toolkit", "jwt") not in fake.entries
    assert ("profile.plaud-toolkit", "profile") not in fake.entries

    # Migration log line is emitted.
    assert any("Migrated plaud-toolkit credentials" in r.message for r in caplog.records)


def test_legacy_keyring_migration_idempotent_on_second_load(tmp_path, monkeypatch):
    """After the first load migrates, a second load reads from the new entry
    and never touches the legacy slots again.
    """
    from plaud_tools.session import SessionStore

    fake = _FakeLegacyKeyring({
        ("jwt.plaud-toolkit", "jwt"): "header.payload.sig",
        ("profile.plaud-toolkit", "profile"): '{"email":"old@example.com","region":"us"}',
    })
    monkeypatch.setattr(
        "plaud_tools.session.importlib.import_module",
        lambda name: fake if name == "keyring" else __import__(name),
    )
    monkeypatch.setattr(SessionStore, "_KEYRING_RETRY_DELAY_S", 0.0)

    store = SessionStore(tmp_path / "session.json", service_name="plaud-tools", dpapi_path=None)
    _, source1 = store.load_with_source()
    _, source2 = store.load_with_source()

    assert source1 == "legacy_keyring"
    assert source2 == "keyring"
    # Only one migration save occurred.
    assert sum(1 for (s, a, _v) in fake.saved if s == "plaud-tools" and a == "session") == 1


def test_legacy_keyring_missing_one_half_does_not_migrate(tmp_path, monkeypatch):
    """If only the jwt entry exists (no profile), no migration happens — we
    don't want to invent an email/region we don't have evidence for.
    """
    from plaud_tools.session import SessionStore

    fake = _FakeLegacyKeyring({
        ("jwt.plaud-toolkit", "jwt"): "header.payload.sig",
    })
    monkeypatch.setattr(
        "plaud_tools.session.importlib.import_module",
        lambda name: fake if name == "keyring" else __import__(name),
    )
    monkeypatch.setattr(SessionStore, "_KEYRING_RETRY_DELAY_S", 0.0)

    store = SessionStore(tmp_path / "session.json", service_name="plaud-tools", dpapi_path=None)
    session, source = store.load_with_source()

    assert session is None
    assert source == "missing"
    # Legacy entry left alone.
    assert ("jwt.plaud-toolkit", "jwt") in fake.entries


def test_legacy_keyring_migration_handles_malformed_profile(tmp_path, monkeypatch):
    """A non-JSON profile blob falls back to defaults (region='us', email=None);
    the JWT still migrates.
    """
    from plaud_tools.session import SessionStore

    fake = _FakeLegacyKeyring({
        ("jwt.plaud-toolkit", "jwt"): "header.payload.sig",
        ("profile.plaud-toolkit", "profile"): "this is not JSON",
    })
    monkeypatch.setattr(
        "plaud_tools.session.importlib.import_module",
        lambda name: fake if name == "keyring" else __import__(name),
    )
    monkeypatch.setattr(SessionStore, "_KEYRING_RETRY_DELAY_S", 0.0)

    store = SessionStore(tmp_path / "session.json", service_name="plaud-tools", dpapi_path=None)
    session, source = store.load_with_source()

    assert session is not None
    assert source == "legacy_keyring"
    assert session.access_token == "header.payload.sig"
    assert session.region == "us"
    assert session.email is None


# ---------------------------------------------------------------------------
# DPAPI shadow-file fallback (v0.2.7) — bypasses Windows Credential Manager's
# cold-start settling window by reading a user-DPAPI-encrypted file written
# alongside every keyring save.  All tests monkeypatch the ctypes-backed
# encrypt/decrypt helpers so the suite runs on every platform without
# touching the real DPAPI subsystem.
# ---------------------------------------------------------------------------

class _FakeDpapi:
    """Reversible XOR-with-marker stand-in for CryptProtectData/CryptUnprotectData.

    Not cryptographically meaningful — just a deterministic, asymmetric-looking
    transform so tests can verify that the blob on disk is not the plaintext
    JSON, and that the decrypt path actually runs.
    """
    _MARKER = b"DPAPI-FAKE-"

    @classmethod
    def protect(cls, plaintext: bytes) -> bytes:
        return cls._MARKER + plaintext[::-1]

    @classmethod
    def unprotect(cls, ciphertext: bytes) -> bytes:
        if not ciphertext.startswith(cls._MARKER):
            raise ValueError("not a fake-DPAPI blob")
        return ciphertext[len(cls._MARKER):][::-1]


def _patch_fake_dpapi(monkeypatch):
    monkeypatch.setattr("plaud_tools.session._dpapi_protect", _FakeDpapi.protect)
    monkeypatch.setattr("plaud_tools.session._dpapi_unprotect", _FakeDpapi.unprotect)


def test_save_writes_keyring_and_dpapi_shadow(tmp_path, monkeypatch):
    """save() must persist to BOTH the keyring AND the DPAPI shadow file so
    a subsequent cold-start MCP load has a Credential-Manager-independent
    path to the session.
    """
    from plaud_tools.session import PlaudSession, SessionStore

    _patch_fake_dpapi(monkeypatch)
    fake = _FakeLegacyKeyring()
    monkeypatch.setattr(
        "plaud_tools.session.importlib.import_module",
        lambda name: fake if name == "keyring" else __import__(name),
    )

    shadow = tmp_path / "session.dat"
    store = SessionStore(tmp_path / "session.json", service_name="plaud-tools", dpapi_path=shadow)
    session = PlaudSession(access_token="header.payload.sig", region="us", email="u@example.com")
    store.save(session)

    # Keyring entry written.
    assert ("plaud-tools", "session") in fake.entries
    # DPAPI shadow file written and encrypted (not plaintext JSON).
    assert shadow.exists()
    blob = shadow.read_bytes()
    assert blob.startswith(_FakeDpapi._MARKER)
    assert b"header.payload.sig" not in blob, "shadow file must not store the token in plaintext"
    # Plaintext FileSessionStore path stays empty when an OS-protected store succeeded.
    assert not (tmp_path / "session.json").exists()


def test_load_falls_back_to_dpapi_when_keyring_returns_none(tmp_path, monkeypatch, caplog):
    """The whole point: when keyring returns None despite the credential
    being present, DPAPI rescues the load.
    """
    import logging

    from plaud_tools.session import PlaudSession, SessionStore

    _patch_fake_dpapi(monkeypatch)

    class EmptyKeyring:
        """Reads always return None (simulates the cold-start failure)."""
        entries = {}

        @staticmethod
        def get_password(*_a, **_k):
            return None

        @staticmethod
        def get_credential(*_a, **_k):
            return None

        @staticmethod
        def set_password(*_a, **_k):
            pass

    monkeypatch.setattr(
        "plaud_tools.session.importlib.import_module",
        lambda name: EmptyKeyring if name == "keyring" else __import__(name),
    )

    shadow = tmp_path / "session.dat"
    # Pre-seed the DPAPI shadow as if a prior healthy save() had populated it.
    payload = json.dumps({"access_token": "tok", "region": "us", "email": "rescue@example.com"}).encode()
    shadow.write_bytes(_FakeDpapi.protect(payload))

    store = SessionStore(tmp_path / "session.json", service_name="plaud-tools", dpapi_path=shadow)

    with caplog.at_level(logging.WARNING, logger="plaud_tools.session"):
        session, source = store.load_with_source()

    assert session is not None, "DPAPI fallback should have recovered the session"
    assert session.email == "rescue@example.com"
    assert source == "dpapi_file"
    # A telemetry-grade warning fires so we know the fallback was needed.
    assert any("DPAPI shadow file fallback fired" in r.message for r in caplog.records)


def test_dpapi_disabled_when_path_is_none(tmp_path, monkeypatch):
    """Passing dpapi_path=None opts out of the shadow entirely — required for
    tests that pin the plaintext file-store fallback and for non-Windows users
    who want zero filesystem residue beyond the keyring.
    """
    from plaud_tools.session import PlaudSession, SessionStore

    _patch_fake_dpapi(monkeypatch)
    fake = _FakeLegacyKeyring()
    monkeypatch.setattr(
        "plaud_tools.session.importlib.import_module",
        lambda name: fake if name == "keyring" else __import__(name),
    )

    store = SessionStore(tmp_path / "session.json", service_name="plaud-tools", dpapi_path=None)
    store.save(PlaudSession(access_token="t", region="us", email="u@example.com"))

    # No shadow file written anywhere under tmp_path.
    assert not list(tmp_path.glob("*.dat"))
    assert store.dpapi_path is None


def test_dpapi_not_enabled_for_non_canonical_service(tmp_path):
    """Auto-default is gated to service_name=='plaud-tools' so test fixtures
    using synthetic service names never touch the real user's session.dat.
    """
    from plaud_tools.session import SessionStore

    store = SessionStore(tmp_path / "session.json", service_name="plaud-tools-test-foo")
    assert store.dpapi_path is None


def test_clear_removes_dpapi_shadow(tmp_path, monkeypatch):
    """clear() must wipe the keyring entry AND the DPAPI shadow AND the
    plaintext file — sign-out should leave no recoverable session anywhere.
    """
    from plaud_tools.session import PlaudSession, SessionStore

    _patch_fake_dpapi(monkeypatch)
    fake = _FakeLegacyKeyring()
    monkeypatch.setattr(
        "plaud_tools.session.importlib.import_module",
        lambda name: fake if name == "keyring" else __import__(name),
    )

    shadow = tmp_path / "session.dat"
    plaintext = tmp_path / "session.json"
    store = SessionStore(plaintext, service_name="plaud-tools", dpapi_path=shadow)
    store.save(PlaudSession(access_token="t", region="us", email="u@example.com"))
    assert shadow.exists()

    store.clear()

    assert not shadow.exists()
    assert ("plaud-tools", "session") not in fake.entries
    assert not plaintext.exists()


def test_load_ignores_dpapi_when_decryption_fails(tmp_path, monkeypatch, caplog):
    """A shadow file written by a different Windows user (or otherwise
    corrupted) must not crash load() — it should log a warning and treat
    the file as if it wasn't there.
    """
    import logging

    from plaud_tools.session import SessionStore

    def _broken_unprotect(_blob):
        raise OSError("DPAPI decryption failed (simulated)")

    monkeypatch.setattr("plaud_tools.session._dpapi_unprotect", _broken_unprotect)

    shadow = tmp_path / "session.dat"
    shadow.write_bytes(b"\x01\x02\x03\x04")  # bytes the broken unprotect will reject

    class EmptyKeyring:
        @staticmethod
        def get_password(*_a, **_k):
            return None

        @staticmethod
        def get_credential(*_a, **_k):
            return None

    monkeypatch.setattr(
        "plaud_tools.session.importlib.import_module",
        lambda name: EmptyKeyring if name == "keyring" else __import__(name),
    )

    store = SessionStore(tmp_path / "session.json", service_name="plaud-tools", dpapi_path=shadow)
    with caplog.at_level(logging.WARNING, logger="plaud_tools.session"):
        session, source = store.load_with_source()

    assert session is None
    assert source == "missing"
    assert any("DPAPI decryption failed" in r.message for r in caplog.records)


@pytest.mark.skipif(__import__("sys").platform != "win32", reason="DPAPI is Windows-only")
def test_real_dpapi_roundtrip(tmp_path):
    """Pin the live ctypes wrapper for CryptProtectData/CryptUnprotectData.

    The behaviour-focused tests above mock the encrypt/decrypt helpers so the
    suite runs on every platform; this one exercises the real wrapper so a
    breaking change to the Win32 call signature or struct layout shows up in
    CI on windows-latest instead of only when a user actually signs in to the
    frozen bundle.
    """
    from plaud_tools.session import _dpapi_protect, _dpapi_unprotect

    plaintext = b'{"access_token":"x","region":"us","email":"u@example.com"}'
    blob = _dpapi_protect(plaintext)
    assert blob != plaintext
    assert _dpapi_unprotect(blob) == plaintext


def test_keyring_load_self_heals_missing_dpapi_shadow(tmp_path, monkeypatch):
    """Existing v0.2.6 users upgrading to v0.2.7 already have a healthy keyring
    session but no DPAPI shadow.  The first successful keyring load must
    populate the shadow so the fallback is available on the *next* cold-start
    MCP read — without forcing the user to sign out + sign in.
    """
    from plaud_tools.session import PlaudSession, SessionStore

    _patch_fake_dpapi(monkeypatch)
    payload = json.dumps({"access_token": "tok", "region": "us", "email": "u@example.com"})

    class HealthyKeyring:
        @staticmethod
        def get_password(*_a, **_k):
            return payload

        @staticmethod
        def get_credential(*_a, **_k):
            return None

    monkeypatch.setattr(
        "plaud_tools.session.importlib.import_module",
        lambda name: HealthyKeyring if name == "keyring" else __import__(name),
    )

    shadow = tmp_path / "session.dat"
    assert not shadow.exists()

    store = SessionStore(tmp_path / "session.json", service_name="plaud-tools", dpapi_path=shadow)
    session, source = store.load_with_source()

    assert source == "keyring"  # primary path wasn't disturbed
    assert shadow.exists(), "DPAPI shadow must be self-healed on first keyring load"
    # And the shadow contains the session we just read, not garbage.
    decrypted = _FakeDpapi.unprotect(shadow.read_bytes()).decode()
    assert "u@example.com" in decrypted


def test_self_heal_does_not_overwrite_existing_shadow(tmp_path, monkeypatch):
    """If the shadow file already exists, a keyring load must NOT touch it —
    that protects a freshly-saved shadow against accidental rewrites from a
    racing stale keyring read.
    """
    from plaud_tools.session import SessionStore

    _patch_fake_dpapi(monkeypatch)
    new_payload = json.dumps({"access_token": "new", "region": "us", "email": "new@example.com"})
    sentinel = b"PRE-EXISTING-SHADOW-MUST-NOT-CHANGE"

    class HealthyKeyring:
        @staticmethod
        def get_password(*_a, **_k):
            return new_payload

        @staticmethod
        def get_credential(*_a, **_k):
            return None

    monkeypatch.setattr(
        "plaud_tools.session.importlib.import_module",
        lambda name: HealthyKeyring if name == "keyring" else __import__(name),
    )

    shadow = tmp_path / "session.dat"
    shadow.write_bytes(sentinel)

    store = SessionStore(tmp_path / "session.json", service_name="plaud-tools", dpapi_path=shadow)
    store.load_with_source()

    assert shadow.read_bytes() == sentinel, "Self-heal must not overwrite an existing shadow"


def test_save_succeeds_when_keyring_fails_but_dpapi_works(tmp_path, monkeypatch):
    """On Windows, a DPAPI shadow is enough on its own — the plaintext
    file-store fallback should NOT fire when DPAPI took the save.  This
    closes the door on a security regression where a keyring outage forced
    us to write the token plaintext to ~/.config/plaud-tools/session.json.
    """
    from plaud_tools.session import PlaudSession, SessionStore

    _patch_fake_dpapi(monkeypatch)

    class BrokenKeyring:
        @staticmethod
        def set_password(*_a, **_k):
            raise RuntimeError("keyring write broken")

        @staticmethod
        def get_password(*_a, **_k):
            return None

        @staticmethod
        def get_credential(*_a, **_k):
            return None

    monkeypatch.setattr(
        "plaud_tools.session.importlib.import_module",
        lambda name: BrokenKeyring if name == "keyring" else __import__(name),
    )

    shadow = tmp_path / "session.dat"
    plaintext = tmp_path / "session.json"
    store = SessionStore(plaintext, service_name="plaud-tools", dpapi_path=shadow)
    store.save(PlaudSession(access_token="t", region="us", email="u@example.com"))

    assert shadow.exists(), "DPAPI shadow must absorb the save when keyring fails"
    assert not plaintext.exists(), "plaintext fallback must not fire when DPAPI succeeded"


# ---------------------------------------------------------------------------
# prime_dpapi_shadow (v0.2.8) — eager, low-latency self-heal called from the
# tray entry script before pystray/PIL load so the shadow exists earlier in
# tray-startup, tightening the v0.2.6 → v0.2.7 upgrade race window.
# ---------------------------------------------------------------------------


def test_prime_writes_shadow_when_keyring_healthy_and_shadow_missing(tmp_path, monkeypatch):
    from plaud_tools.session import SessionStore

    _patch_fake_dpapi(monkeypatch)
    payload = json.dumps({"access_token": "tok", "region": "us", "email": "u@example.com"})

    class HealthyKeyring:
        @staticmethod
        def get_password(*_a, **_k):
            return payload

    monkeypatch.setattr(
        "plaud_tools.session.importlib.import_module",
        lambda name: HealthyKeyring if name == "keyring" else __import__(name),
    )

    shadow = tmp_path / "session.dat"
    store = SessionStore(tmp_path / "session.json", service_name="plaud-tools", dpapi_path=shadow)

    assert store.prime_dpapi_shadow() is True
    assert shadow.exists()
    decrypted = _FakeDpapi.unprotect(shadow.read_bytes()).decode()
    assert "u@example.com" in decrypted


def test_prime_is_noop_when_shadow_already_exists(tmp_path, monkeypatch):
    """If the shadow is already on disk, prime must not touch keyring at all —
    that protects a freshly-saved shadow against an accidental rewrite from a
    racing keyring read inside prime.
    """
    from plaud_tools.session import SessionStore

    _patch_fake_dpapi(monkeypatch)
    sentinel = b"PRE-EXISTING-SHADOW"

    keyring_calls = {"n": 0}

    class TrackingKeyring:
        @staticmethod
        def get_password(*_a, **_k):
            keyring_calls["n"] += 1
            return None

    monkeypatch.setattr(
        "plaud_tools.session.importlib.import_module",
        lambda name: TrackingKeyring if name == "keyring" else __import__(name),
    )

    shadow = tmp_path / "session.dat"
    shadow.write_bytes(sentinel)

    store = SessionStore(tmp_path / "session.json", service_name="plaud-tools", dpapi_path=shadow)

    assert store.prime_dpapi_shadow() is False
    assert shadow.read_bytes() == sentinel
    assert keyring_calls["n"] == 0, "prime must short-circuit before touching keyring"


def test_prime_is_noop_when_keyring_returns_none(tmp_path, monkeypatch):
    """Signed-out users (keyring legitimately empty) must not be charged any
    retry budget here — prime must read exactly once and give up.
    """
    from plaud_tools.session import SessionStore

    _patch_fake_dpapi(monkeypatch)

    keyring_calls = {"n": 0}

    class EmptyKeyring:
        @staticmethod
        def get_password(*_a, **_k):
            keyring_calls["n"] += 1
            return None

    monkeypatch.setattr(
        "plaud_tools.session.importlib.import_module",
        lambda name: EmptyKeyring if name == "keyring" else __import__(name),
    )

    shadow = tmp_path / "session.dat"
    store = SessionStore(tmp_path / "session.json", service_name="plaud-tools", dpapi_path=shadow)

    assert store.prime_dpapi_shadow() is False
    assert not shadow.exists()
    assert keyring_calls["n"] == 1, "prime must do exactly one keyring read, no retry budget"


def test_prime_swallows_keyring_exception(tmp_path, monkeypatch):
    """Keyring raising on a transient cold-start must NOT bubble out of prime —
    the tray entry script wraps prime in its own try/except but the contract
    is that prime itself is fully best-effort.
    """
    from plaud_tools.session import SessionStore

    _patch_fake_dpapi(monkeypatch)

    class BrokenKeyring:
        @staticmethod
        def get_password(*_a, **_k):
            raise RuntimeError("vault locked")

    monkeypatch.setattr(
        "plaud_tools.session.importlib.import_module",
        lambda name: BrokenKeyring if name == "keyring" else __import__(name),
    )

    shadow = tmp_path / "session.dat"
    store = SessionStore(tmp_path / "session.json", service_name="plaud-tools", dpapi_path=shadow)

    assert store.prime_dpapi_shadow() is False
    assert not shadow.exists()


def test_prime_is_noop_when_dpapi_disabled(tmp_path, monkeypatch):
    from plaud_tools.session import SessionStore

    _patch_fake_dpapi(monkeypatch)
    payload = json.dumps({"access_token": "tok", "region": "us", "email": "u@example.com"})

    class HealthyKeyring:
        @staticmethod
        def get_password(*_a, **_k):
            return payload

    monkeypatch.setattr(
        "plaud_tools.session.importlib.import_module",
        lambda name: HealthyKeyring if name == "keyring" else __import__(name),
    )

    store = SessionStore(tmp_path / "session.json", service_name="plaud-tools", dpapi_path=None)
    assert store.prime_dpapi_shadow() is False
