"""Tests for SessionManager cache freshness (D5 — mtime/TTL revalidation).

These tests verify:
1. Hot path: within TTL / unchanged mtime, require() does NOT hit the store again.
2. Mtime pickup: a second store instance saves a new session; the first
   SessionManager picks it up after the file mtime changes.
3. TTL fallback: when no probe path exists (pure protocol store), the cache
   expires after _CACHE_TTL_SECONDS.
4. invalidate_cache() still works correctly.

All tests follow the established pattern from conftest.py:
 - Use tmp_path for session files (never real user paths).
 - Use a fake keyring (never the real Windows Credential Manager).
 - autouse fixtures from conftest.py redirect appdata defaults to tmp_path /
   None, and the mtime trip-wire guards the real shadow file.
"""

from __future__ import annotations

import base64
import json
import os
import time as _time
from pathlib import Path
from typing import Any

import pytest

from plaud_tools.session import (
    FileSessionStore,
    PlaudSession,
    SessionManager,
    SessionStore,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_jwt(days: int = 200) -> str:
    """Build a minimal structurally-valid JWT with an *exp* claim."""
    header = (
        base64.urlsafe_b64encode(b'{"alg":"HS256","typ":"UT"}').decode().rstrip("=")
    )
    exp = int(_time.time()) + days * 86400
    payload = (
        base64.urlsafe_b64encode(json.dumps({"exp": exp}).encode()).decode().rstrip("=")
    )
    return f"{header}.{payload}.fakesig"


def _keyring_patch(monkeypatch: pytest.MonkeyPatch, kr: Any) -> None:
    """Patch importlib so that ``import keyring`` returns *kr*."""
    real_import = __import__
    monkeypatch.setattr(
        "plaud_tools.session.importlib.import_module",
        lambda n: kr if n == "keyring" else real_import(n),
    )


# ---------------------------------------------------------------------------
# FileSessionStore-backed SessionManager (no keyring, uses file probe path)
# ---------------------------------------------------------------------------


class TestMtimeProbeWithFileStore:
    """SessionManager backed by a FileSessionStore: uses file mtime for staleness."""

    def test_hot_path_no_extra_load_within_unchanged_mtime(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Within the unchanged-mtime window, require() must not call store.load() again."""
        monkeypatch.delenv("PLAUD_ACCESS_TOKEN", raising=False)
        session_path = tmp_path / "session.json"
        jwt = _make_jwt()
        FileSessionStore(session_path).save(PlaudSession(access_token=jwt, region="us"))

        # Instrument the file store's load() to count calls.
        file_store = FileSessionStore(session_path)
        load_count: list[int] = [0]
        original_load = file_store.load

        def counting_load() -> PlaudSession | None:
            load_count[0] += 1
            return original_load()

        file_store.load = counting_load  # type: ignore[method-assign]
        manager = SessionManager(file_store)

        # First call — must hit the store.
        s1 = manager.require()
        assert load_count[0] == 1
        assert s1.access_token == jwt

        # Second and third calls — file unchanged; must NOT hit the store again.
        s2 = manager.require()
        s3 = manager.require()
        assert s2 is s1
        assert s3 is s1
        assert load_count[0] == 1, (
            "Hot path must not call store.load() when mtime is unchanged"
        )

    def test_cross_instance_pickup_after_mtime_changes(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A second FileSessionStore saving a new session causes the first
        SessionManager to pick up the updated session on the next require()."""
        monkeypatch.delenv("PLAUD_ACCESS_TOKEN", raising=False)
        session_path = tmp_path / "session.json"

        jwt_v1 = _make_jwt(days=200)
        FileSessionStore(session_path).save(
            PlaudSession(access_token=jwt_v1, region="us", email="v1@example.com")
        )

        manager = SessionManager(FileSessionStore(session_path))

        # Prime the cache.
        s1 = manager.require()
        assert s1.access_token == jwt_v1

        # A second process / store instance writes a new session.
        jwt_v2 = _make_jwt(days=199)
        store2 = FileSessionStore(session_path)
        store2.save(
            PlaudSession(access_token=jwt_v2, region="eu", email="v2@example.com")
        )
        # Force mtime to strictly increase even if the write was within the same
        # second (coarse-clock safety on FAT32 / CI overlays).
        new_mtime = session_path.stat().st_mtime + 1.0
        os.utime(session_path, (new_mtime, new_mtime))

        # Next require() on the original manager must detect the mtime change
        # and reload from the file.
        s2 = manager.require()
        assert s2.access_token == jwt_v2
        assert s2.region == "eu"

    def test_invalidate_cache_clears_entry(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("PLAUD_ACCESS_TOKEN", raising=False)
        session_path = tmp_path / "session.json"
        jwt = _make_jwt()
        FileSessionStore(session_path).save(PlaudSession(access_token=jwt, region="us"))

        file_store = FileSessionStore(session_path)
        load_count: list[int] = [0]
        original_load = file_store.load

        def counting_load() -> PlaudSession | None:
            load_count[0] += 1
            return original_load()

        file_store.load = counting_load  # type: ignore[method-assign]
        manager = SessionManager(file_store)

        manager.require()
        assert load_count[0] == 1

        manager.invalidate_cache()
        manager.require()
        assert load_count[0] == 2, "After invalidate_cache, require() must reload from store"


# ---------------------------------------------------------------------------
# SessionStore-backed (keyring disabled, dpapi=None): file_store path probe
# ---------------------------------------------------------------------------


class TestMtimeProbeWithSessionStore:
    """SessionManager backed by a SessionStore (no keyring, no DPAPI) —
    uses the file_store probe path for mtime checks."""

    def test_cross_instance_pickup_no_extra_keyring_reads(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Hot-path: no keyring reads after cache is warm and mtime unchanged.
        Cross-instance: second store saves → first manager reloads on next call."""
        monkeypatch.delenv("PLAUD_ACCESS_TOKEN", raising=False)
        session_path = tmp_path / "session.json"

        jwt_v1 = _make_jwt(days=200)
        store1 = SessionStore(
            session_path,
            service_name="plaud-tools-test-d5-mtime",
            dpapi_path=None,
        )
        # Disable keyring so load() falls through to file_store.
        monkeypatch.setattr(
            "plaud_tools.session.importlib.import_module",
            lambda name: None if name == "keyring" else __import__(name),
        )
        store1.file_store.save(
            PlaudSession(access_token=jwt_v1, region="us", email="v1@example.com")
        )

        manager = SessionManager(store1)

        # -- prime the cache --
        s1 = manager.require()
        assert s1.access_token == jwt_v1

        # -- hot path: mtime unchanged, no store.load() --
        # Instrument file_store.load to count calls for the hot path check.
        orig_load = store1.file_store.load
        file_load_count: list[int] = [0]

        def counting_file_load() -> PlaudSession | None:
            file_load_count[0] += 1
            return orig_load()

        store1.file_store.load = counting_file_load  # type: ignore[method-assign]

        s_hot = manager.require()
        assert s_hot is s1
        # store1.load() routes through keyring (disabled) then file_store.load().
        # Because mtime is unchanged, the cache hit must prevent any file_store.load().
        assert file_load_count[0] == 0, (
            "No extra store reads within unchanged-mtime window"
        )

        # -- cross-instance write by store2 --
        jwt_v2 = _make_jwt(days=199)
        store2 = SessionStore(
            session_path,
            service_name="plaud-tools-test-d5-mtime",
            dpapi_path=None,
        )
        store2.file_store.save(
            PlaudSession(access_token=jwt_v2, region="eu", email="v2@example.com")
        )
        # Ensure mtime strictly increases (coarse-clock safety).
        new_mtime = session_path.stat().st_mtime + 1.0
        os.utime(session_path, (new_mtime, new_mtime))

        # manager (backed by store1) must detect the mtime change and reload.
        s2 = manager.require()
        assert s2.access_token == jwt_v2
        assert s2.region == "eu"


# ---------------------------------------------------------------------------
# TTL fallback: store with no probe path
# ---------------------------------------------------------------------------


class TestTtlFallbackWithMinimalStore:
    """When the store has no file_store or dpapi_path, fall back to TTL."""

    class _CountingStore:
        """Minimal SessionStoreProtocol that counts load() calls."""

        def __init__(self, session: PlaudSession) -> None:
            self._session = session
            self.load_count = 0

        def load(self) -> PlaudSession | None:
            self.load_count += 1
            return self._session

        def save(self, session: PlaudSession) -> None:
            self._session = session

    def test_hot_path_within_ttl_no_extra_load(self) -> None:
        """Within the TTL window, require() must not call store.load() again."""
        jwt = _make_jwt()
        session = PlaudSession(access_token=jwt, region="us")
        store = self._CountingStore(session)
        manager = SessionManager(store)

        s1 = manager.require()
        assert store.load_count == 1

        # Multiple calls within TTL — should not hit the store.
        manager.require()
        manager.require()
        assert store.load_count == 1, "No extra loads within TTL window"

        assert s1 is manager.require()

    def test_reload_after_ttl_expires(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """After TTL elapses, require() must reload from the store."""
        jwt = _make_jwt()
        session = PlaudSession(access_token=jwt, region="us")
        store = self._CountingStore(session)
        manager = SessionManager(store)

        manager.require()
        assert store.load_count == 1

        # Fast-forward time past the TTL by patching `time()` inside session.py.
        original_time = _time.time()
        monkeypatch.setattr(
            "plaud_tools.session.time",
            lambda: original_time + SessionManager._CACHE_TTL_SECONDS + 1.0,
        )

        manager.require()
        assert store.load_count == 2, "Must reload after TTL expires"

    def test_cross_instance_ttl_pickup(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Two managers sharing a minimal store: second manager writes, first
        manager picks up after TTL expires."""
        jwt_v1 = _make_jwt(days=200)
        jwt_v2 = _make_jwt(days=199)
        session_v1 = PlaudSession(access_token=jwt_v1, region="us")
        session_v2 = PlaudSession(access_token=jwt_v2, region="eu")

        store = self._CountingStore(session_v1)
        manager = SessionManager(store)

        # Prime cache.
        s1 = manager.require()
        assert s1.access_token == jwt_v1

        # Another "process" updates the store.
        store.save(session_v2)

        # Still within TTL — old cached value returned.
        s_cached = manager.require()
        assert s_cached.access_token == jwt_v1

        # Fast-forward past TTL.
        original_time = _time.time()
        monkeypatch.setattr(
            "plaud_tools.session.time",
            lambda: original_time + SessionManager._CACHE_TTL_SECONDS + 1.0,
        )

        s2 = manager.require()
        assert s2.access_token == jwt_v2, "Must pick up store update after TTL"
