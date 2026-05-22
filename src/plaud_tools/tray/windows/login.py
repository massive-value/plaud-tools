"""LoginWindow — collects email/password/region and signs in via PlaudAuth."""
from __future__ import annotations

import logging
import tkinter as tk
from tkinter import ttk
from typing import Callable

from ...auth import PlaudAuth
from ...errors import PlaudApiError, PlaudSessionExpiredError
from ...session import SessionStore
from ..setup import APP_NAME, _set_app_icon


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
        win.geometry("340x220")
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
        error_label = ttk.Label(frame, textvariable=error_var, foreground="#c0392b", wraplength=280)
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
            win.update()
            try:
                auth = PlaudAuth(self._store)
                auth.login(email, password, region_var.get())
                win.destroy()
                self._on_success()
            except (PlaudApiError, PlaudSessionExpiredError) as exc:
                error_var.set(str(exc))
                btn.config(state="normal", text="Sign in")
            except Exception as exc:
                # Defensive: anything not already wrapped as a Plaud error
                # (e.g. network errors that slipped past the transport, JSON
                # parsing failures, keyring backend bugs) should not surface
                # to the user as a raw traceback.  Log full details, show a
                # short friendly message inline.
                logging.exception("Unexpected error during sign-in")
                error_var.set(f"Sign-in failed: {exc}")
                btn.config(state="normal", text="Sign in")

        btn.config(command=do_login)
        win.bind("<Return>", lambda _: do_login())

        # Bring to front after the event loop has drawn the window, then grab.
        # grab_set() requires the window to be "viewable"; deferring 50 ms
        # ensures it is mapped before we attempt to grab.
        win.lift()
        win.focus_force()
        win.after(50, lambda: win.grab_set() if win.winfo_exists() else None)


__all__ = ["LoginWindow"]
