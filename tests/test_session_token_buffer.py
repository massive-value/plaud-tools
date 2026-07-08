"""Boundary + regression tests for TOKEN_REFRESH_BUFFER_SECONDS (#138, Wave 4c).

The v0.5.0 incident ("bricked MCP") happened because the buffer used to
reject a freshly-issued 30-day token outright. That was fixed by shrinking
the buffer to 3 days, but the fix itself had never been pinned with a
boundary test — every existing test used tokens that were either far in the
future (~200-300 days) or already expired, so a regression at the boundary
would not have been caught.

Wave 4c shrank the buffer further, from 3 days to 24 hours (§6.1 of the
2026-07-06 audit): a 3-day refuse buffer combined with a 3-day tray warning
threshold meant the warning and the breakage started on the *same* day. The
boundary tests below were updated (not weakened) to pin the new 24h value,
and a new class pins the invariant that the tray warning threshold must
strictly precede the refuse buffer.

This file also pins #138 itself: the SessionManager cache's mtime probe made
the expiry re-check unreachable on the hot path, so a long-lived MCP process
that cached a session before it crossed the refresh buffer would keep serving
that now-stale-enough-to-refuse token forever, as long as the backing file
never changed.
"""

from __future__ import annotations

import base64
import json
import time as _time
from pathlib import Path

import pytest

from plaud_tools.core.errors import PlaudSessionExpiredError
from plaud_tools.core.session import (
    TOKEN_REFRESH_BUFFER_SECONDS,
    TRAY_EXPIRY_WARNING_DAYS,
    FileSessionStore,
    PlaudSession,
    SessionManager,
)


def _make_jwt(seconds_from_now: float) -> str:
    """Build a minimal structurally-valid JWT whose *exp* claim is
    ``seconds_from_now`` seconds from the current wall-clock time."""
    header = base64.urlsafe_b64encode(b'{"alg":"HS256","typ":"UT"}').decode().rstrip("=")
    exp = int(_time.time() + seconds_from_now)
    payload = base64.urlsafe_b64encode(json.dumps({"exp": exp}).encode()).decode().rstrip("=")
    return f"{header}.{payload}.fakesig"


_DAY = 86_400
_HOUR = 3_600


class TestTokenRefreshBufferBoundary:
    """require() must reject tokens inside the buffer and accept tokens outside it.

    Wave 4c shrank TOKEN_REFRESH_BUFFER_SECONDS from 3 days to 24 hours
    (§6.1): a 3-day refuse buffer left ~10% of every 30-day token's life
    unusable with no warning runway. These boundary cases were updated (not
    removed) to pin the new 24h value at the same precision as before.
    """

    def test_twenty_three_hours_out_is_rejected(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Just inside the 24h buffer: must be refused."""
        monkeypatch.delenv("PLAUD_ACCESS_TOKEN", raising=False)
        session_path = tmp_path / "session.json"
        jwt = _make_jwt(23 * _HOUR)
        FileSessionStore(session_path).save(PlaudSession(access_token=jwt, region="us"))

        manager = SessionManager(FileSessionStore(session_path))
        with pytest.raises(PlaudSessionExpiredError):
            manager.require()

    def test_twenty_five_hours_out_is_accepted(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Just outside the 24h buffer: must be accepted."""
        monkeypatch.delenv("PLAUD_ACCESS_TOKEN", raising=False)
        session_path = tmp_path / "session.json"
        jwt = _make_jwt(25 * _HOUR)
        FileSessionStore(session_path).save(PlaudSession(access_token=jwt, region="us"))

        manager = SessionManager(FileSessionStore(session_path))
        session = manager.require()
        assert session.access_token == jwt

    def test_twenty_nine_days_out_is_accepted(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """The exact v0.5.0 incident: Plaud issues 30-day tokens — a freshly
        issued token (29 days remaining, allowing for clock skew) must not be
        refused."""
        monkeypatch.delenv("PLAUD_ACCESS_TOKEN", raising=False)
        session_path = tmp_path / "session.json"
        jwt = _make_jwt(29 * _DAY)
        FileSessionStore(session_path).save(PlaudSession(access_token=jwt, region="us"))

        manager = SessionManager(FileSessionStore(session_path))
        session = manager.require()
        assert session.access_token == jwt

    def test_buffer_constant_is_24_hours(self) -> None:
        """Trip-wire: if someone changes the buffer, the boundary tests above
        must be revisited — pin the current value explicitly."""
        assert TOKEN_REFRESH_BUFFER_SECONDS == 24 * 60 * 60


class TestExpiryWarningPrecedesRefusal:
    """Pin §6.1's core UX invariant: the tray warning must fire days before
    require() would ever refuse the same token, so users get real runway to
    react instead of the warning and the breakage landing on the same day."""

    def test_warning_threshold_strictly_exceeds_refresh_buffer(self) -> None:
        assert TRAY_EXPIRY_WARNING_DAYS * _DAY > TOKEN_REFRESH_BUFFER_SECONDS

    def test_warning_threshold_is_five_days(self) -> None:
        """Trip-wire pin for the chosen value, alongside the 24h buffer above."""
        assert TRAY_EXPIRY_WARNING_DAYS == 5


class TestCachedSessionCrossesBuffer:
    """Pin #138: a cached session must be re-validated against expiry even
    when the backing file's mtime never changes."""

    def test_require_raises_once_cached_token_crosses_the_buffer(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Regression for #138.

        Before the fix, SessionManager._is_stale() only reloaded from disk
        when the probed file's mtime changed (or a TTL elapsed). The expiry
        check in require() ran only on a *reload*, so once a session was
        cached, an MCP process that never wrote a new session file would keep
        serving the same (now within-buffer, i.e. Plaud-refusing) token
        forever — exactly the "bricked MCP" class v0.5.0 fixed, reintroduced
        via the cache path.
        """
        monkeypatch.delenv("PLAUD_ACCESS_TOKEN", raising=False)
        session_path = tmp_path / "session.json"
        # Valid now: just outside the buffer.
        jwt = _make_jwt(TOKEN_REFRESH_BUFFER_SECONDS + 60)
        FileSessionStore(session_path).save(PlaudSession(access_token=jwt, region="us"))

        manager = SessionManager(FileSessionStore(session_path))

        # Prime the cache — succeeds, token is valid.
        first = manager.require()
        assert first.access_token == jwt

        # Time passes; the token is now within the refresh buffer. The file on
        # disk is untouched — mtime is unchanged — so the old mtime-only probe
        # would have kept serving the cached (now-refusable) session.
        real_time = _time.time
        monkeypatch.setattr(
            "plaud_tools.core.session.time",
            lambda: real_time() + TOKEN_REFRESH_BUFFER_SECONDS + 120,
        )

        with pytest.raises(PlaudSessionExpiredError):
            manager.require()

    def test_require_still_hits_hot_path_when_well_outside_buffer(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Control: the new expiry check must not defeat the hot-path
        optimisation for a token that is nowhere near the buffer — no extra
        store.load() call when nothing has changed."""
        monkeypatch.delenv("PLAUD_ACCESS_TOKEN", raising=False)
        session_path = tmp_path / "session.json"
        jwt = _make_jwt(200 * _DAY)
        FileSessionStore(session_path).save(PlaudSession(access_token=jwt, region="us"))

        file_store = FileSessionStore(session_path)
        load_count = [0]
        original_load = file_store.load

        def counting_load():
            load_count[0] += 1
            return original_load()

        file_store.load = counting_load  # type: ignore[method-assign]
        manager = SessionManager(file_store)

        manager.require()
        manager.require()
        manager.require()
        assert load_count[0] == 1, "Hot path must not reload when nowhere near the expiry buffer"
