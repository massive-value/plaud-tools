"""LoginWindow — collects email/password/region and signs in via PlaudAuth."""

from __future__ import annotations

import logging
import threading
import tkinter as tk
from collections.abc import Callable
from tkinter import ttk

from ...auth import PlaudAuth
from ...errors import PlaudApiError, PlaudSessionExpiredError
from ...session import SessionStore
from ..setup import APP_NAME, _set_app_icon, _widget_alive

# Plaud's password-based login endpoint returns a bare HTTP 401 for Google-SSO
# accounts (they have no password set at all) -- indistinguishable, on the
# wire, from a plain wrong-password 401. Users stuck here have nowhere in the
# product to learn that "Forgot password" on web.plaud.ai is the fix; that
# guidance previously existed only in docs/TROUBLESHOOTING.md, not where the
# stuck user actually is (§6.2). Surfacing it unconditionally on 401 is a
# false positive for genuine typos, but a much better default than silence.
_GOOGLE_SSO_HINT = (
    'If you signed up for Plaud with Google, use "Forgot password" on '
    "web.plaud.ai to set a password first — PlaudTools sign-in requires a "
    "password, not Google sign-in."
)


def _error_message_with_hints(error: Exception) -> str:
    """Return *error*'s message, appending the Google-SSO hint for a 401."""
    message = str(error)
    if "401" in message:
        return f"{message}\n\n{_GOOGLE_SSO_HINT}"
    return message


class LoginWindow:
    def __init__(self, root: tk.Tk, store: SessionStore, on_success: Callable) -> None:
        self._root = root
        self._store = store
        self._on_success = on_success
        self._win: tk.Toplevel | None = None

    def show(self) -> None:
        if self._win and self._win.winfo_exists():
            self._win.lift()
            self._win.focus_force()
            return
        win = tk.Toplevel(self._root)
        _set_app_icon(win)
        win.title(f"{APP_NAME} — Sign in")
        win.resizable(False, False)
        # Tall enough for the Google-SSO hint (§6.2), which can wrap to three
        # lines below the normal one-line "wrong password" error.
        win.geometry("360x280")
        self._win = win  # set before widget build so partial state is always visible

        frame = ttk.Frame(win, padding=16)
        frame.pack(fill="both", expand=True)

        ttk.Label(frame, text="Email").grid(row=0, column=0, sticky="w", pady=4)
        email_var = tk.StringVar()
        ttk.Entry(frame, textvariable=email_var, width=32).grid(row=0, column=1, padx=8, pady=4)

        ttk.Label(frame, text="Password").grid(row=1, column=0, sticky="w", pady=4)
        password_var = tk.StringVar()
        ttk.Entry(frame, textvariable=password_var, show="•", width=32).grid(row=1, column=1, padx=8, pady=4)

        ttk.Label(frame, text="Region").grid(row=2, column=0, sticky="w", pady=4)
        region_var = tk.StringVar(value="us")
        ttk.Combobox(frame, textvariable=region_var, values=["us", "eu"], state="readonly", width=10).grid(
            row=2, column=1, sticky="w", padx=8, pady=4
        )

        error_var = tk.StringVar()
        error_label = ttk.Label(frame, textvariable=error_var, foreground="#c0392b", wraplength=320)
        error_label.grid(row=3, column=0, columnspan=2, pady=4)

        btn = ttk.Button(frame, text="Sign in")
        btn.grid(row=4, column=0, columnspan=2, pady=8)

        def do_login() -> None:
            email = email_var.get().strip()
            password = password_var.get()
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

            # auth.login() makes a network call that can take ~30s; running it
            # on the Tk main thread freezes the entire tray (every window,
            # every menu click) for the duration (#161). Do the network work
            # on a daemon thread and marshal the result back via root.after().
            region = region_var.get()

            def _worker() -> None:
                error: Exception | None = None
                try:
                    auth = PlaudAuth(self._store)
                    auth.login(email, password, region)
                except Exception as exc:  # noqa: BLE001  # re-raised via _finish on the Tk thread
                    error = exc
                if self._root:
                    self._root.after(0, lambda: _finish(error))

            def _finish(error: Exception | None) -> None:
                if error is None:
                    if _widget_alive(win):
                        win.destroy()
                    self._on_success()
                    return
                if not _widget_alive(win):
                    return  # window closed while sign-in was in flight
                if isinstance(error, (PlaudApiError, PlaudSessionExpiredError)):
                    error_var.set(_error_message_with_hints(error))
                else:
                    # Defensive: anything not already wrapped as a Plaud error
                    # (e.g. network errors that slipped past the transport,
                    # JSON parsing failures, keyring backend bugs) should not
                    # surface to the user as a raw traceback.  Log full
                    # details, show a short friendly message inline.
                    logging.error("Unexpected error during sign-in", exc_info=error)
                    error_var.set(f"Sign-in failed: {error}")
                btn.config(state="normal", text="Sign in")

            threading.Thread(target=_worker, daemon=True).start()

        btn.config(command=do_login)
        win.bind("<Return>", lambda _: do_login())

        # Bring to front after the event loop has drawn the window, then grab.
        # grab_set() requires the window to be "viewable"; deferring 50 ms
        # ensures it is mapped before we attempt to grab.
        win.lift()
        win.focus_force()
        win.after(50, lambda: win.grab_set() if win.winfo_exists() else None)


__all__ = ["LoginWindow"]
