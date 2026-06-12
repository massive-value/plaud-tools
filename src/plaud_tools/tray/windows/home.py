"""HomeWindow — tray left-click target with sign-out, test, repair, etc."""

from __future__ import annotations

import tkinter as tk
from collections.abc import Callable
from tkinter import ttk

from ... import __version__ as APP_VERSION
from ..setup import APP_NAME, EnvStatus, _set_app_icon


class HomeWindow:
    def __init__(
        self,
        root: tk.Tk,
        on_test_connection: Callable[[Callable[[bool, str], None]], None],
        on_check_for_update: Callable[[Callable[[bool, str], None]], None],
        on_open_update: Callable[[], None],
        on_open_wizard: Callable[[], None],
        on_sign_out: Callable[[], None],
        on_open_uninstall: Callable[[], None],
        on_repair_setup: Callable[[Callable[[bool, str], None]], None] | None,
        get_session_label: Callable[[], str],
        get_update_info: Callable[[], tuple[str, str, str | None] | None],
        get_env_status: Callable[[], EnvStatus | None],
        on_open_log_folder: Callable[[], None] | None = None,
        on_open_help: Callable[[], None] | None = None,
    ) -> None:
        self._root = root
        self._on_test_connection = on_test_connection
        self._on_check_for_update = on_check_for_update
        self._on_open_update = on_open_update
        self._on_open_wizard = on_open_wizard
        self._on_sign_out = on_sign_out
        self._on_open_uninstall = on_open_uninstall
        self._on_repair_setup = on_repair_setup
        self._get_session_label = get_session_label
        self._get_update_info = get_update_info
        self._get_env_status = get_env_status
        self._on_open_log_folder = on_open_log_folder
        self._on_open_help = on_open_help
        self._win: tk.Toplevel | None = None
        self._session_var: tk.StringVar | None = None
        self._status_var: tk.StringVar | None = None
        self._status_label: ttk.Label | None = None
        self._test_btn: ttk.Button | None = None
        self._update_btn: ttk.Button | None = None
        self._repair_btn: ttk.Button | None = None
        self._welcome_banner_armed: bool = False
        self._welcome_banner: tk.Frame | None = None
        # Setup-failure row: yellow banner at the top of HomeWindow shown when
        # _verify_env reports any missing entries.
        self._setup_failure_row: tk.Frame | None = None
        self._setup_failure_label: tk.Label | None = None

    def show(self) -> None:
        if self._win and self._win.winfo_exists():
            self._win.lift()
            self._win.focus_force()
            self._refresh_session()
            self._refresh_update_btn()
            self._refresh_repair_btn()
            self._refresh_setup_failure_row()
            return

        win = tk.Toplevel(self._root)
        _set_app_icon(win)
        win.title(APP_NAME)
        win.resizable(False, False)
        self._win = win

        frame = ttk.Frame(win, padding=20)
        frame.pack(fill="both", expand=True)

        # Setup-failure row — yellow banner at the top shown when _verify_env
        # detects any missing setup entries (PATH, completions, autostart).
        # Created here so it is always available; shown/hidden by
        # _refresh_setup_failure_row().
        self._setup_failure_row = tk.Frame(frame, background="#b45309", padx=10, pady=6)
        self._setup_failure_label = tk.Label(
            self._setup_failure_row,
            text="",
            background="#b45309",
            foreground="white",
            font=("Segoe UI", 9),
            cursor="hand2",
            wraplength=340,
            justify="left",
        )
        self._setup_failure_label.pack(anchor="w")
        self._setup_failure_label.bind("<Button-1>", lambda _e: self._handle_repair_setup())
        self._setup_failure_row.bind("<Button-1>", lambda _e: self._handle_repair_setup())
        # Rendered (shown or hidden) after the window is fully laid out.

        # Welcome banner — shown once after first install, dismissed on
        # "Configure AI Agents…" click.
        self._welcome_banner = None
        if self._welcome_banner_armed:
            banner = tk.Frame(frame, background="#1d4ed8", padx=10, pady=8)
            banner.pack(fill="x", pady=(0, 10))
            tk.Label(
                banner,
                text="Welcome to PlaudTools — connect your AI client below.",
                background="#1d4ed8",
                foreground="white",
                font=("Segoe UI", 9),
                wraplength=340,
                justify="left",
            ).pack(anchor="w")
            self._welcome_banner = banner

        self._session_var = tk.StringVar()
        ttk.Label(frame, textvariable=self._session_var, font=("", 10, "bold"), wraplength=360).pack(
            anchor="w"
        )
        self._refresh_session()

        ttk.Separator(frame, orient="horizontal").pack(fill="x", pady=10)

        btn_frame = ttk.Frame(frame)
        btn_frame.pack(fill="x")

        ttk.Button(btn_frame, text="Configure AI Agents…", command=self._handle_open_wizard).pack(
            fill="x", pady=(0, 6)
        )

        self._test_btn = ttk.Button(btn_frame, text="Test Connection", command=self._handle_test)
        self._test_btn.pack(fill="x", pady=(0, 6))

        self._update_btn = ttk.Button(btn_frame, text="Check for Updates", command=self._handle_check_update)
        self._update_btn.pack(fill="x", pady=(0, 6))
        self._refresh_update_btn()

        # Repair setup button — shown only when _verify_env detects missing entries
        self._repair_btn = ttk.Button(btn_frame, text="Repair setup", command=self._handle_repair_setup)
        self._refresh_repair_btn()
        self._refresh_setup_failure_row()

        if self._on_open_log_folder is not None:
            ttk.Button(btn_frame, text="View Logs", command=self._on_open_log_folder).pack(
                fill="x", pady=(0, 6)
            )

        if self._on_open_help is not None:
            ttk.Button(btn_frame, text="Help / Visit website", command=self._on_open_help).pack(fill="x")

        self._status_var = tk.StringVar()
        self._status_label = ttk.Label(
            frame, textvariable=self._status_var, foreground="#15803d", font=("Segoe UI", 9), wraplength=360
        )
        self._status_label.pack(anchor="w", pady=(6, 0))

        ttk.Separator(frame, orient="horizontal").pack(fill="x", pady=10)

        ttk.Button(frame, text="Sign out", command=self._handle_sign_out).pack(fill="x", pady=(0, 6))

        ttk.Separator(frame, orient="horizontal").pack(fill="x", pady=(0, 6))

        ttk.Button(frame, text="Uninstall…", command=self._handle_uninstall).pack(fill="x")

        footer = ttk.Frame(frame)
        footer.pack(fill="x", pady=(10, 0))
        ttk.Label(footer, text=f"v{APP_VERSION}", foreground="#6b7280", font=("Segoe UI", 8)).pack(
            side="right"
        )

        win.update_idletasks()
        win.geometry(f"400x{win.winfo_reqheight()}")
        win.lift()
        win.focus_force()

    def arm_welcome_banner(self) -> None:
        """Mark the next open() call as a first-run welcome.  No-op if the
        window is already open (the banner would have been shown already)."""
        self._welcome_banner_armed = True

    def _handle_open_wizard(self) -> None:
        """Open the AI-client wizard and dismiss the welcome banner on first click."""
        if self._welcome_banner_armed:
            self._welcome_banner_armed = False
            if self._welcome_banner is not None:
                try:
                    self._welcome_banner.destroy()
                except Exception:
                    pass
                self._welcome_banner = None
        self._on_open_wizard()

    def _refresh_session(self) -> None:
        if self._session_var is not None:
            self._session_var.set(self._get_session_label())

    def _refresh_update_btn(self) -> None:
        if self._update_btn is None:
            return
        info = self._get_update_info()
        if info is not None:
            self._update_btn.configure(
                state="normal",
                text=f"Update available: v{info[0]} — Install",
                command=self._on_open_update,
            )
        else:
            self._update_btn.configure(
                state="normal",
                text="Check for Updates",
                command=self._handle_check_update,
            )

    def _refresh_repair_btn(self) -> None:
        if self._repair_btn is None:
            return
        status = self._get_env_status()
        if status is not None and not status.all_ok and self._on_repair_setup is not None:
            missing = ", ".join(status.missing_labels())
            self._repair_btn.configure(text=f"Repair setup ({missing})", state="normal")
            self._repair_btn.pack(fill="x", pady=(0, 6))
        else:
            self._repair_btn.pack_forget()

    def _refresh_setup_failure_row(self) -> None:
        """Show or hide the yellow setup-failure banner at the top of HomeWindow.

        The row is visible when ``_verify_env`` has reported at least one missing
        entry AND a repair callback is available.  On success the row is hidden
        (after a brief green message displayed via ``_set_status``).

        Because the row widget is created first inside the frame in ``show()``,
        calling ``pack()`` on it will place it above all other widgets.
        """
        if self._setup_failure_row is None or self._setup_failure_label is None:
            return
        status = self._get_env_status()
        if status is not None and not status.all_ok and self._on_repair_setup is not None:
            missing = ", ".join(status.missing_labels())
            self._setup_failure_label.configure(
                text=f"Some setup is missing ({missing}) — click to repair.",
                background="#b45309",
                foreground="white",
                cursor="hand2",
            )
            self._setup_failure_row.pack(fill="x", pady=(0, 10))
        else:
            self._setup_failure_row.pack_forget()

    def _set_status(self, msg: str, ok: bool = True) -> None:
        if self._status_var is None or self._status_label is None:
            return
        self._status_label.configure(foreground="#15803d" if ok else "#c0392b")
        self._status_var.set(msg)

    def _handle_test(self) -> None:
        if self._test_btn is None:
            return
        self._test_btn.configure(state="disabled", text="Testing…")
        if self._status_var is not None:
            self._status_var.set("")

        def _done(ok: bool, msg: str) -> None:
            if self._test_btn is None:
                return
            self._test_btn.configure(state="normal", text="Test connection")
            self._set_status(msg, ok)
            if self._win and self._win.winfo_exists():
                self._win.after(4000, lambda: self._status_var.set("") if self._status_var else None)

        self._on_test_connection(_done)

    def _handle_check_update(self) -> None:
        if self._update_btn is None:
            return
        self._update_btn.configure(state="disabled", text="Checking…")
        if self._status_var is not None:
            self._status_var.set("")

        def _done(found: bool, msg: str) -> None:
            if self._update_btn is None:
                return
            if found:
                self._refresh_update_btn()
                self._set_status(msg, ok=True)
                self._on_open_update()
            else:
                self._update_btn.configure(state="normal", text="Check for Updates")
                self._set_status(msg, ok=True)
                if self._win and self._win.winfo_exists():
                    self._win.after(4000, lambda: self._status_var.set("") if self._status_var else None)

        self._on_check_for_update(_done)

    def _handle_repair_setup(self) -> None:
        if self._on_repair_setup is None:
            return
        if self._repair_btn is not None:
            self._repair_btn.configure(state="disabled", text="Repairing…")
        if self._status_var is not None:
            self._status_var.set("")
        # Show "Repairing…" state in the setup-failure row while the worker runs.
        if self._setup_failure_row is not None and self._setup_failure_label is not None:
            self._setup_failure_label.configure(
                text="Repairing setup…",
                cursor="",
            )
            self._setup_failure_row.pack(fill="x", pady=(0, 10))

        def _done(ok: bool, msg: str) -> None:
            if ok:
                # Show a brief green "Setup complete" in the failure row, then
                # dismiss it after a few seconds.
                if self._setup_failure_row is not None and self._setup_failure_label is not None:
                    self._setup_failure_label.configure(
                        text="Setup complete.",
                        background="#15803d",
                        foreground="white",
                        cursor="",
                    )
                    self._setup_failure_row.configure(background="#15803d")
                    self._setup_failure_row.pack(fill="x", pady=(0, 10))
                    if self._win and self._win.winfo_exists():

                        def _dismiss_row() -> None:
                            self._refresh_setup_failure_row()
                            # Reset colour for next time the row might be shown.
                            if self._setup_failure_row is not None:
                                self._setup_failure_row.configure(background="#b45309")
                            if self._setup_failure_label is not None:
                                self._setup_failure_label.configure(background="#b45309")

                        self._win.after(4000, _dismiss_row)
            else:
                # On failure: show the error in the row; add log-folder hint.
                if self._setup_failure_row is not None and self._setup_failure_label is not None:
                    hint = ""
                    if self._on_open_log_folder is not None:
                        hint = " — see logs for details."
                    self._setup_failure_label.configure(
                        text=f"Repair failed: {msg}{hint}",
                        cursor="hand2" if self._on_open_log_folder else "",
                    )
                    if self._on_open_log_folder is not None:
                        # Rebind click to open log folder when repair failed.
                        self._setup_failure_label.bind(
                            "<Button-1>",
                            lambda _e: self._on_open_log_folder() if self._on_open_log_folder else None,
                        )
                    self._setup_failure_row.pack(fill="x", pady=(0, 10))
            self._set_status(msg, ok)
            self._refresh_repair_btn()
            if self._win and self._win.winfo_exists():
                self._win.after(5000, lambda: self._status_var.set("") if self._status_var else None)

        self._on_repair_setup(_done)

    def _handle_sign_out(self) -> None:
        self._on_sign_out()
        if self._win and self._win.winfo_exists():
            self._win.destroy()

    def _handle_uninstall(self) -> None:
        self._on_open_uninstall()


__all__ = ["HomeWindow"]
