"""Tests for the Wave 4c expiry-runway UX fixes (§6.1).

Before this fix, ``TrayApp._tray_state`` warned at <=3 days and
``SessionManager.require()`` (session.py) refused at the same <=3-day
buffer -- the warning and the breakage started on the same day, giving users
no runway to react. The buffer shrank to 24h and the warning threshold moved
to `TRAY_EXPIRY_WARNING_DAYS` (5 days, see test_session_token_buffer.py for
the ordering-invariant pin). These tests pin the tray-side consumers of that
threshold: ``_tray_state`` (icon/menu state) and ``_session_label``
(HomeWindow's session line) -- and that the label never claims the token is
"valid" once inside the warning window (the label-truth bug in §6.1).

Built via ``TrayApp.__new__`` with a mocked ``SessionManager`` -- same
pattern as ``_run_test_connection`` in test_tray_correctness.py -- so no real
Tk/keyring/pystray plumbing is needed.
"""

from __future__ import annotations

from unittest.mock import MagicMock

from plaud_tools.session import TRAY_EXPIRY_WARNING_DAYS
from plaud_tools.tray.app import TrayApp


def _make_app(days: int | None, signed_in: bool = True) -> TrayApp:
    app = TrayApp.__new__(TrayApp)
    app._manager = MagicMock()
    app._manager.days_until_expiry.return_value = days
    app._session = MagicMock(email="test@example.com") if signed_in else None
    return app


class TestTrayStateThreshold:
    def test_signed_out_state(self):
        app = _make_app(days=None, signed_in=False)
        assert app._tray_state() == "signed-out"

    def test_zero_days_is_expired_not_expiring(self):
        app = _make_app(days=0)
        assert app._tray_state() == "expired"

    def test_none_days_is_expired(self):
        """A malformed/undecodable token: treat the same as expired."""
        app = _make_app(days=None)
        assert app._tray_state() == "expired"

    def test_at_warning_threshold_is_expiring(self):
        app = _make_app(days=TRAY_EXPIRY_WARNING_DAYS)
        assert app._tray_state() == "expiring"

    def test_one_day_past_warning_threshold_is_signed_in(self):
        app = _make_app(days=TRAY_EXPIRY_WARNING_DAYS + 1)
        assert app._tray_state() == "signed-in"

    def test_one_day_is_expiring(self):
        app = _make_app(days=1)
        assert app._tray_state() == "expiring"


class TestSessionLabelTruth:
    """The label must never say "valid" while require() is (or is about to
    start) refusing the same token."""

    def test_zero_days_never_claims_valid(self):
        app = _make_app(days=0)
        label = app._session_label()
        assert "valid" not in label.lower()
        assert "expires" in label.lower()

    def test_within_warning_window_says_expires_not_valid(self):
        app = _make_app(days=TRAY_EXPIRY_WARNING_DAYS)
        label = app._session_label()
        assert "valid" not in label.lower()
        assert f"{TRAY_EXPIRY_WARNING_DAYS} days" in label

    def test_outside_warning_window_says_valid(self):
        app = _make_app(days=TRAY_EXPIRY_WARNING_DAYS + 10)
        label = app._session_label()
        assert "valid" in label.lower()

    def test_not_signed_in(self):
        app = _make_app(days=None, signed_in=False)
        assert app._session_label() == "Not signed in."

    def test_undecodable_token_omits_day_count(self):
        app = _make_app(days=None)
        label = app._session_label()
        assert "Signed in as test@example.com." == label
