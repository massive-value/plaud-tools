"""Tests for _BackgroundMixin correctness fixes (#152, #162).

Both bugs are in ``plaud_tools/tray/background.py`` and only bite in
production because they depend on timing (a background thread reporting
failure, or a concurrent append to the tray events file) that a synchronous
unit test would otherwise sail past.
"""

from __future__ import annotations

import json
import threading
from unittest.mock import MagicMock, patch

from plaud_tools.tray.background import _BackgroundMixin

# ---------------------------------------------------------------------------
# #152 — repair-failure lambda NameError (deferred `exc`)
# ---------------------------------------------------------------------------


class _StubApp(_BackgroundMixin):
    """Minimal TrayApp stand-in with the attributes _repair_env touches."""

    def __init__(self) -> None:
        self._root = MagicMock()
        # Run the scheduled callback immediately, synchronously, so the test
        # doesn't need to pump a real Tk mainloop.
        self._root.after.side_effect = lambda _delay, fn: fn()
        self._env_status = None


def _run_repair_env_and_wait(app: _StubApp) -> tuple[bool, str]:
    """Run _repair_env's background thread and block until on_done fires."""
    done_event = threading.Event()
    result: list[tuple[bool, str]] = []

    def on_done(ok: bool, msg: str) -> None:
        result.append((ok, msg))
        done_event.set()

    app._repair_env(on_done)
    assert done_event.wait(timeout=5), "on_done was never called"
    return result[0]


class TestRepairEnvFailureCallback:
    def test_failure_message_includes_exception_text_not_nameerror(self):
        """Regression witness for #152: before the fix, the failure lambda
        referenced the `except ... as exc:` binding directly. CPython runs an
        implicit `del exc` when the except block exits (to avoid a traceback
        reference cycle), so by the time root.after() actually invoked the
        lambda, `exc` no longer existed -- the callback raised NameError
        instead of delivering the intended message, and the Home window hung
        on "Repairing setup..." forever with no visible error.
        """
        app = _StubApp()
        with (
            patch(
                "plaud_tools.tray.background._setup_cli_path",
                side_effect=RuntimeError("registry write denied"),
            ),
            patch("plaud_tools.tray.background._setup_ps_completions"),
            patch("plaud_tools.tray.background._autostart_enabled", return_value=True),
        ):
            ok, msg = _run_repair_env_and_wait(app)

        assert ok is False
        assert "registry write denied" in msg
        assert "Repair failed" in msg

    def test_success_path_still_reports_ok(self):
        """No regression: the success branch is untouched by the fix."""
        app = _StubApp()
        with (
            patch("plaud_tools.tray.background._setup_cli_path"),
            patch("plaud_tools.tray.background._setup_ps_completions"),
            patch("plaud_tools.tray.background._autostart_enabled", return_value=True),
            patch("plaud_tools.tray.background._verify_env", return_value=MagicMock(all_ok=True)),
        ):
            ok, msg = _run_repair_env_and_wait(app)

        assert ok is True
        assert "repaired successfully" in msg.lower()


# ---------------------------------------------------------------------------
# #162 — event-file read-then-truncate race (rename-then-read atomic claim)
# ---------------------------------------------------------------------------


class _EventLoopStubApp(_BackgroundMixin):
    def __init__(self, root: MagicMock) -> None:
        self._root = root
        self._session = None
        self._home_win = None
        self._login_win = None

    def _open_login(self) -> None:
        pass


class TestEventPollLoopAtomicClaim:
    """_event_poll_loop must claim the events file via rename (not
    read-then-truncate in place) so a write landing between the read and the
    clear cannot be silently wiped."""

    def test_concurrent_append_during_processing_is_not_lost(self, tmp_path, monkeypatch):
        """Simulate the exact race: something appends to the *original* path
        while the loop is mid-processing a claimed copy. With rename-then-read,
        the concurrent append creates a fresh file at the original path (since
        the claimed copy was already moved away) and must survive to the next
        poll -- read-then-truncate would have wiped it.
        """
        events_path = tmp_path / "events.jsonl"
        events_path.write_text(
            json.dumps({"type": "session_expired", "ts": 1.0, "reason": "first"}) + "\n",
            encoding="utf-8",
        )

        root = MagicMock()
        root.after.side_effect = lambda _delay, fn: fn()
        app = _EventLoopStubApp(root)

        monkeypatch.setattr("plaud_tools.tray.background._events_path", lambda: events_path)
        monkeypatch.setattr("plaud_tools.tray.background._show_session_expired_toast", lambda: None)

        # After the first claim (events_path.replace(claim_path)) happens,
        # a "concurrent" writer creates a brand-new events.jsonl before the
        # loop's second iteration -- this must NOT be clobbered.
        real_replace = type(events_path).replace
        appended_second_event = {"done": False}

        def racy_replace(self, target):
            result = real_replace(self, target)
            if not appended_second_event["done"]:
                appended_second_event["done"] = True
                events_path.write_text(
                    json.dumps({"type": "session_expired", "ts": 2.0, "reason": "second"}) + "\n",
                    encoding="utf-8",
                )
            return result

        monkeypatch.setattr(type(events_path), "replace", racy_replace)

        sleep_calls = [0]

        def fast_sleep(_n):
            sleep_calls[0] += 1
            # Allow exactly one full iteration (which triggers the racy
            # append mid-processing), then stop BEFORE a second iteration
            # would consume the concurrently-written event too.
            if sleep_calls[0] > 1:
                raise SystemExit("stop loop")

        monkeypatch.setattr("plaud_tools.tray.background.time.sleep", fast_sleep)

        try:
            app._event_poll_loop()
        except SystemExit:
            pass

        # The "second" event (written concurrently with the first claim) must
        # still be sitting in the events file, ready for the next poll --
        # read-then-truncate would have destroyed it by writing "" over it.
        remaining = events_path.read_text(encoding="utf-8")
        assert "second" in remaining

    def test_missing_events_file_is_a_no_op(self, tmp_path, monkeypatch):
        root = MagicMock()
        app = _EventLoopStubApp(root)
        monkeypatch.setattr("plaud_tools.tray.background._events_path", lambda: tmp_path / "nope.jsonl")

        sleep_calls = [0]

        def fast_sleep(_n):
            sleep_calls[0] += 1
            if sleep_calls[0] > 1:
                raise SystemExit("stop loop")

        monkeypatch.setattr("plaud_tools.tray.background.time.sleep", fast_sleep)

        try:
            app._event_poll_loop()
        except SystemExit:
            pass
        # No exception -- that's the whole test.

    def test_claim_file_is_cleaned_up_after_read(self, tmp_path, monkeypatch):
        events_path = tmp_path / "events.jsonl"
        events_path.write_text(
            json.dumps({"type": "noop"}) + "\n",
            encoding="utf-8",
        )
        root = MagicMock()
        root.after.side_effect = lambda _delay, fn: fn()
        app = _EventLoopStubApp(root)
        monkeypatch.setattr("plaud_tools.tray.background._events_path", lambda: events_path)

        sleep_calls = [0]

        def fast_sleep(_n):
            sleep_calls[0] += 1
            if sleep_calls[0] > 1:
                raise SystemExit("stop loop")

        monkeypatch.setattr("plaud_tools.tray.background.time.sleep", fast_sleep)

        try:
            app._event_poll_loop()
        except SystemExit:
            pass

        claim_path = events_path.with_name(events_path.name + ".processing")
        assert not claim_path.exists(), "the .processing claim file must not be left behind"
        assert not events_path.exists(), "the original path is consumed, not left empty"
