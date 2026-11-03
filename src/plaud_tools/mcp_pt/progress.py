"""MCP progress notifications for the server's long waits (#221).

``call_tool`` runs each handler in a worker thread, while the SDK's
``ctx.report_progress`` is async and must run on the event loop.  This module
bridges the two once, for every tool: ``call_tool`` wraps the handler in
:func:`progress_reporting`, and the handlers read :func:`current_progress` to
hand a ``ProgressCallback`` to the client's upload and wait methods.

The SDK sends a notification only when the caller's request carried a
progress token, and does nothing otherwise, so no tool needs to check.
Outside ``call_tool`` (tests calling a handler directly, the CLI) there is
no callback and the waits run silently.  Progress is extra: a failed send is
logged and ignored, and never changes what a tool returns (ADR 007).
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any

from ..core.client import ProgressCallback

log = logging.getLogger("plaud_tools.mcp")

# Seconds the worker thread will wait for the event loop to send one notification.
_SEND_TIMEOUT_S = 5.0

_current: ContextVar[ProgressCallback | None] = ContextVar("plaud_mcp_progress", default=None)


def current_progress() -> ProgressCallback | None:
    """The callback that reports progress for the tool call running now, if any."""
    return _current.get()


def shifted(callback: ProgressCallback | None, offset: float) -> ProgressCallback | None:
    """*callback* with *offset* added to progress and total.

    Lets a call that waits in stages under one shared budget (transcription,
    then summary) report a single steadily rising count instead of restarting
    at zero for each stage.
    """
    if callback is None:
        return None
    return lambda progress, total, message: callback(
        offset + progress, None if total is None else offset + total, message
    )


@contextmanager
def progress_reporting(ctx: Any) -> Iterator[None]:
    """Make *ctx*'s progress reporting available to the handler about to run in a thread.

    Call from the async ``call_tool`` before ``asyncio.to_thread``: the
    thread inherits this context, and sends go back to this loop.
    """
    loop = asyncio.get_running_loop()

    def send(progress: float, total: float | None, message: str) -> None:
        try:
            # The session is scoped to this one request; the SDK drops the
            # call when the request carried no progress token.
            asyncio.run_coroutine_threadsafe(
                ctx.session.report_progress(progress, total, message), loop
            ).result(_SEND_TIMEOUT_S)
        except Exception:  # noqa: BLE001 — progress must never fail a tool call
            log.debug("progress notification not sent", exc_info=True)

    token = _current.set(send)
    try:
        yield
    finally:
        _current.reset(token)
