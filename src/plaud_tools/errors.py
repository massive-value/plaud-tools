from __future__ import annotations

import json
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from urllib.error import HTTPError

_BODY_TRUNCATE = 500


class PlaudError(Exception):
    pass


class PlaudSessionExpiredError(PlaudError):
    code = "PLAUD_SESSION_EXPIRED"


class PlaudApiError(PlaudError):
    """Raised when the Plaud API returns an error response.

    Attributes
    ----------
    http_status:
        The HTTP status code of the error response (e.g. 429), or ``None``
        when the error was not caused by an HTTP response.
    plaud_code:
        The ``code`` field from the Plaud JSON envelope, when present.
    plaud_msg:
        The ``msg`` field from the Plaud JSON envelope, when present.
    raw_body:
        The raw response body text (truncated to 500 chars), when available.
    """

    def __init__(
        self,
        message: str,
        *,
        http_status: int | None = None,
        plaud_code: object = None,
        plaud_msg: str | None = None,
        raw_body: str | None = None,
    ) -> None:
        super().__init__(message)
        self.http_status = http_status
        self.plaud_code = plaud_code
        self.plaud_msg = plaud_msg
        self.raw_body = raw_body

    @classmethod
    def from_http_error(cls, exc: HTTPError) -> PlaudApiError:
        """Build a :class:`PlaudApiError` from a :class:`urllib.error.HTTPError`.

        Reads the response body, attempts a JSON parse, and extracts Plaud's
        conventional ``msg`` / ``code`` envelope fields where available.
        """
        try:
            raw_bytes = exc.read()
        except Exception:
            raw_bytes = b""

        raw_text: str | None = None
        if raw_bytes:
            try:
                raw_text = raw_bytes.decode("utf-8", errors="replace")
            except Exception:
                raw_text = None

        plaud_code: object = None
        plaud_msg: str | None = None
        parsed: object = None

        if raw_text:
            try:
                parsed = json.loads(raw_text)
            except (json.JSONDecodeError, ValueError):
                parsed = None

        if isinstance(parsed, dict):
            plaud_code = parsed.get("code") or parsed.get("status")
            raw_msg = parsed.get("msg") or parsed.get("message") or parsed.get("detail")
            if raw_msg is not None:
                plaud_msg = str(raw_msg)

        # Build a human-readable message: prefer plaud_msg, fall back to raw body.
        if plaud_msg:
            human = plaud_msg
        elif raw_text:
            truncated = raw_text[:_BODY_TRUNCATE]
            human = truncated if len(raw_text) <= _BODY_TRUNCATE else truncated + "…"
        else:
            human = f"HTTP {exc.code}"

        message = f"Plaud API error: HTTP {exc.code}: {human}"

        truncated_body: str | None = None
        if raw_text:
            truncated_body = (
                raw_text[:_BODY_TRUNCATE]
                if len(raw_text) <= _BODY_TRUNCATE
                else raw_text[:_BODY_TRUNCATE] + "…"
            )

        return cls(
            message,
            http_status=exc.code,
            plaud_code=plaud_code,
            plaud_msg=plaud_msg,
            raw_body=truncated_body,
        )

    def classify(self) -> tuple[str, bool]:
        """Return ``(error_code, retryable)`` for this error.

        Maps HTTP status to a classification that callers (e.g. the MCP facade)
        use to populate structured error payloads.

        Returns
        -------
        tuple[str, bool]
            A ``(code, retryable)`` pair where *code* is one of
            ``"not_found"``, ``"transient"``, or ``"api_error"``.
        """
        status = self.http_status
        if status == 404:
            return "not_found", False
        if status is not None and (status == 429 or status >= 500):
            return "transient", True
        return "api_error", False
