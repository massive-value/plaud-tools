"""WizardWindow — connects/disconnects AI clients (Claude Desktop, etc.)."""

from __future__ import annotations

import tkinter as tk
from collections.abc import Callable
from tkinter import ttk

from ...core.ai_clients import CLIENTS, connect, disconnect, status_all
from ..setup import APP_NAME, _mcp_exe, _set_app_icon

_STATUS_BADGE: dict[str, tuple[str, str]] = {
    "not-detected": ("Not installed", "#6b7280"),
    "not-connected": ("Not connected", "#1d4ed8"),
    "connected": ("✓ Connected", "#15803d"),
    "stale": ("⚠ Path outdated", "#b45309"),
}


class WizardWindow:
    def __init__(
        self,
        root: tk.Tk,
        on_done: Callable,
    ) -> None:
        self._root = root
        self._on_done = on_done
        self._win: tk.Toplevel | None = None
        self._row_widgets: dict[str, dict[str, object]] = {}
        self._help_var: tk.StringVar | None = None
        self._help_label: ttk.Label | None = None

    def show(self) -> None:
        if self._win and self._win.winfo_exists():
            self._win.lift()
            self._win.focus_force()
            self._render()
            return

        win = tk.Toplevel(self._root)
        _set_app_icon(win)
        win.title(f"{APP_NAME} — Configure AI Agents")
        win.resizable(False, False)
        win.geometry("460x340")
        self._win = win

        frame = ttk.Frame(win, padding=16)
        frame.pack(fill="both", expand=True)

        ttk.Label(frame, text="Connect Plaud to your AI clients:", font=("", 9, "bold")).pack(
            anchor="w", pady=(0, 4)
        )

        rows_frame = ttk.Frame(frame)
        rows_frame.pack(fill="x")
        self._row_widgets.clear()
        for cid, label in CLIENTS.items():
            row = ttk.Frame(rows_frame)
            row.pack(fill="x", pady=4)
            name = ttk.Label(row, text=label, width=18)
            name.pack(side="left")
            badge_var = tk.StringVar()
            badge = ttk.Label(row, textvariable=badge_var)
            badge.pack(side="left", padx=(0, 8))
            btn = ttk.Button(row, text="…", width=12)
            btn.pack(side="right")
            self._row_widgets[cid] = {"badge": badge, "badge_var": badge_var, "btn": btn}

        self._help_var = tk.StringVar()
        help_label = ttk.Label(
            frame, textvariable=self._help_var, foreground="#15803d", wraplength=420, justify="left"
        )
        help_label.pack(anchor="w", pady=(8, 0))
        self._help_label = help_label

        ttk.Separator(frame, orient="horizontal").pack(fill="x", pady=10)

        action_row = ttk.Frame(frame)
        action_row.pack(fill="x")
        ttk.Button(action_row, text="Close", command=win.destroy).pack(side="right")

        win.lift()
        win.focus_force()
        self._render()

    # --- helpers ---

    def _render(self) -> None:
        mcp = _mcp_exe()
        statuses = status_all(mcp)
        for cid, status in statuses.items():
            self._apply_status(cid, status, mcp)

    def _apply_status(self, cid: str, status: str, mcp: str) -> None:
        widgets = self._row_widgets.get(cid)
        if not widgets:
            return
        text, color = _STATUS_BADGE.get(status, ("unknown", "#6b7280"))
        widgets["badge_var"].set(text)  # type: ignore[union-attr,attr-defined]  # widgets dict is untyped; values are tkinter StringVar / Label
        widgets["badge"].configure(foreground=color)  # type: ignore[union-attr,attr-defined]  # same

        btn: ttk.Button = widgets["btn"]  # type: ignore[assignment]
        # Default-argument lambdas (lambda c=cid: ...) let each iteration
        # capture its own value of cid, avoiding the classic closure-over-loop-var
        # bug.  The # type: ignore[misc] suppresses mypy's "Cannot infer type of
        # lambda" — tkinter Button command stubs expect no-arg callables but the
        # default arg makes the lambda unambiguously callable with zero args at
        # runtime.  This is a well-known mypy limitation with default-arg lambdas.
        if status == "not-detected":
            btn.configure(text="—", state="disabled", command=lambda: None)
        elif status == "connected":
            btn.configure(  # type: ignore[misc]
                text="Disconnect",
                state="normal",
                command=lambda c=cid: self._do(c, "disconnect", mcp),  # type: ignore[misc]
            )
        elif status == "stale":
            btn.configure(  # type: ignore[misc]
                text="Reconnect",
                state="normal",
                command=lambda c=cid: self._do(c, "connect", mcp),  # type: ignore[misc]
            )
        else:  # not-connected
            btn.configure(  # type: ignore[misc]
                text="Connect",
                state="normal",
                command=lambda c=cid: self._do(c, "connect", mcp),  # type: ignore[misc]
            )

    def _set_help(self, text: str, *, ok: bool = True) -> None:
        """Show *text* in the help area below the client rows.

        Replaces the old failure path of cramming the exception into the
        12-char-wide row button (``Failed: {exc}`` — unreadable and it never
        recovered the button to a usable state). Failures now render here,
        in red, with the button restored to its normal Connect/Disconnect
        label by the ``_render()`` call at each call site (§6.2).
        """
        if self._help_var is not None:
            self._help_var.set(text)
        if self._help_label is not None:
            self._help_label.configure(foreground="#c0392b" if not ok else "#15803d")

    def _do(self, cid: str, action: str, mcp: str) -> None:
        widgets = self._row_widgets[cid]
        btn: ttk.Button = widgets["btn"]  # type: ignore[assignment]
        btn.configure(state="disabled", text="Connecting…" if action == "connect" else "Disconnecting…")
        try:
            if action == "connect":
                connect(cid, mcp)
            else:
                disconnect(cid)
        except Exception as exc:
            label = CLIENTS[cid]
            verb = "connect" if action == "connect" else "disconnect"
            self._set_help(f"Could not {verb} {label}: {exc}", ok=False)
            # Restore the row's button to its actual current status instead of
            # leaving it stuck on "Connecting…"/"Disconnecting…".
            self._render()
            return
        self._render()
        if action == "connect":
            label = CLIENTS[cid]
            self._set_help(
                f"✓ Connected {label}. Restart {label} to load the new MCP server, "
                "then ask it about your Plaud notes to confirm.",
                ok=True,
            )
        else:
            self._set_help("", ok=True)
        self._on_done()


__all__ = ["WizardWindow"]
