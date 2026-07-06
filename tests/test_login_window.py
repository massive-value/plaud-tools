"""Tests for LoginWindow's threading fix (#161).

Before the fix, ``do_login()`` called ``PlaudAuth.login()`` (a network round
trip that can take ~30s) directly on the Tk main thread -- freezing every
window and every menu click in the whole tray for the duration. The fix
moves the call onto a daemon worker thread and marshals the result back to
the Tk thread via ``root.after()``.

``LoginWindow.show()`` builds real tkinter widgets, which needs a working Tk
display. Following the precedent set by ``test_uninstall_dialog.py``
(``TestWarningUpdateCallback``), no test in this suite constructs a real
``tk.Tk()`` -- CI's Linux/macOS legs have no display. These tests instead
replicate the ``do_login`` / ``_worker`` / ``_finish`` closures exactly as
they appear in ``windows/login.py``, using ``MagicMock`` stand-ins for every
tkinter object, so the threading/marshaling *logic* is pinned without a
display. A source-guard test additionally pins that the production closure
still spawns a thread rather than calling ``auth.login`` inline.
"""

from __future__ import annotations

import inspect
import threading
import time
from unittest.mock import MagicMock

from plaud_tools.errors import PlaudApiError
from plaud_tools.tray.setup import _widget_alive
from plaud_tools.tray.windows import login as login_mod

# ---------------------------------------------------------------------------
# Source guard: the network call must be inside a spawned thread
# ---------------------------------------------------------------------------


def test_source_calls_auth_login_inside_a_thread_worker():
    """Pin that `auth.login(...)` is textually inside a `_worker` function
    that is passed to `threading.Thread(...)`, not called inline in
    `do_login` on the calling (Tk) thread.
    """
    src = inspect.getsource(login_mod)
    worker_start = src.index("def _worker")
    worker_end = src.index("def _finish")
    worker_body = src[worker_start:worker_end]
    assert "auth.login(" in worker_body
    assert "threading.Thread(target=_worker" in src


# ---------------------------------------------------------------------------
# Closure replication: same shape as windows/login.py's do_login/_worker/_finish
# ---------------------------------------------------------------------------


def _build_do_login(root, store, on_success, btn, error_var, win, auth_cls):
    """Replicate do_login() exactly as implemented in windows/login.py."""

    def do_login(email: str, password: str, region: str) -> None:
        if not email and not password:
            error_var.set("Please enter your email and password.")
            return
        if not email:
            error_var.set("Please enter your email.")
            return
        if not password:
            error_var.set("Please enter your password.")
            return
        btn.config(state="disabled", text="Signing in…")
        error_var.set("")

        def _worker() -> None:
            error = None
            try:
                auth = auth_cls(store)
                auth.login(email, password, region)
            except Exception as exc:  # noqa: BLE001
                error = exc
            if root:
                root.after(0, lambda: _finish(error))

        def _finish(error) -> None:
            if error is None:
                if _widget_alive(win):
                    win.destroy()
                on_success()
                return
            if not _widget_alive(win):
                return
            error_var.set(str(error))
            btn.config(state="normal", text="Sign in")

        threading.Thread(target=_worker, daemon=True).start()

    return do_login


class _SlowAuth:
    """Stand-in for PlaudAuth whose login() blocks so tests can observe that
    the Tk-thread caller is never blocked."""

    calls: list[tuple[str, threading.Thread]] = []

    def __init__(self, store):
        self._store = store

    def login(self, email, password, region):
        type(self).calls.append((email, threading.current_thread()))
        time.sleep(0.2)


class TestLoginRunsOffTkThread:
    def setup_method(self):
        _SlowAuth.calls = []

    def test_do_login_returns_immediately_without_blocking(self):
        """do_login() must return control to the caller (the Tk event loop)
        well before the ~30s network call could complete -- the whole point
        of #161 is that the tray stays responsive during sign-in.
        """
        root = MagicMock()
        scheduled: list = []
        root.after.side_effect = lambda _delay, fn: scheduled.append(fn)
        btn = MagicMock()
        error_var = MagicMock()
        win = MagicMock()
        win.winfo_exists.return_value = True
        on_success = MagicMock()

        do_login = _build_do_login(root, MagicMock(), on_success, btn, error_var, win, _SlowAuth)

        started = time.monotonic()
        do_login("user@example.com", "hunter2", "us")
        elapsed = time.monotonic() - started

        assert elapsed < 0.1, "do_login must not block the calling (Tk) thread"
        btn.config.assert_called_once_with(state="disabled", text="Signing in…")

    def test_login_call_happens_on_a_different_thread(self):
        root = MagicMock()
        scheduled: list = []
        root.after.side_effect = lambda _delay, fn: scheduled.append(fn)
        btn = MagicMock()
        error_var = MagicMock()
        win = MagicMock()
        win.winfo_exists.return_value = True
        on_success = MagicMock()

        do_login = _build_do_login(root, MagicMock(), on_success, btn, error_var, win, _SlowAuth)
        do_login("user@example.com", "hunter2", "us")

        # Wait for the worker thread to reach auth.login().
        deadline = time.monotonic() + 2
        while not _SlowAuth.calls and time.monotonic() < deadline:
            time.sleep(0.01)

        assert _SlowAuth.calls, "auth.login() was never called"
        _email, call_thread = _SlowAuth.calls[0]
        assert call_thread is not threading.current_thread()
        assert call_thread.daemon is True

    def test_success_marshals_back_and_destroys_window(self):
        root = MagicMock()
        scheduled: list = []
        root.after.side_effect = lambda _delay, fn: scheduled.append(fn)
        btn = MagicMock()
        error_var = MagicMock()
        win = MagicMock()
        win.winfo_exists.return_value = True
        on_success = MagicMock()

        class _FastAuth:
            def __init__(self, store):
                pass

            def login(self, *a, **kw):
                return None

        do_login = _build_do_login(root, MagicMock(), on_success, btn, error_var, win, _FastAuth)
        do_login("user@example.com", "hunter2", "us")

        # Wait for the worker to schedule its root.after() callback, then run it
        # (simulating the Tk mainloop pumping the scheduled callback).
        deadline = time.monotonic() + 2
        while not scheduled and time.monotonic() < deadline:
            time.sleep(0.01)
        assert scheduled, "worker never scheduled a callback via root.after()"
        scheduled[0]()

        win.destroy.assert_called_once()
        on_success.assert_called_once()

    def test_failure_reports_message_and_re_enables_button(self):
        root = MagicMock()
        scheduled: list = []
        root.after.side_effect = lambda _delay, fn: scheduled.append(fn)
        btn = MagicMock()
        error_var = MagicMock()
        win = MagicMock()
        win.winfo_exists.return_value = True
        on_success = MagicMock()

        class _FailingAuth:
            def __init__(self, store):
                pass

            def login(self, *a, **kw):
                raise PlaudApiError("bad credentials")

        do_login = _build_do_login(root, MagicMock(), on_success, btn, error_var, win, _FailingAuth)
        do_login("user@example.com", "wrong", "us")

        deadline = time.monotonic() + 2
        while not scheduled and time.monotonic() < deadline:
            time.sleep(0.01)
        scheduled[0]()

        error_var.set.assert_called_with("bad credentials")
        btn.config.assert_called_with(state="normal", text="Sign in")
        win.destroy.assert_not_called()
        on_success.assert_not_called()

    def test_failure_skips_widget_touch_when_window_closed(self):
        """If the user closes the login window while sign-in is in flight,
        the marshaled failure callback must not touch the dead widgets."""
        root = MagicMock()
        scheduled: list = []
        root.after.side_effect = lambda _delay, fn: scheduled.append(fn)
        btn = MagicMock()
        error_var = MagicMock()
        win = MagicMock()
        win.winfo_exists.return_value = False  # closed while sign-in was running
        on_success = MagicMock()

        class _FailingAuth:
            def __init__(self, store):
                pass

            def login(self, *a, **kw):
                raise PlaudApiError("bad credentials")

        do_login = _build_do_login(root, MagicMock(), on_success, btn, error_var, win, _FailingAuth)
        do_login("user@example.com", "wrong", "us")

        deadline = time.monotonic() + 2
        while not scheduled and time.monotonic() < deadline:
            time.sleep(0.01)
        scheduled[0]()

        error_var.set.assert_called_once_with("")  # only the initial clear, not the error
        btn.config.assert_called_once_with(state="disabled", text="Signing in…")
