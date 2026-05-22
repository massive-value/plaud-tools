from __future__ import annotations


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
