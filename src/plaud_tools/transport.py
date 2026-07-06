from __future__ import annotations

import gzip
import json
from dataclasses import dataclass
from typing import Protocol
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .errors import PlaudApiError

# Default network timeout for all Plaud API calls (seconds).  Chosen to be
# generous enough for slow links while still failing fast on hung connections.
# Callers that need a different budget (e.g. S3 chunk uploads — see client.py)
# can override per-call via the ``timeout`` parameter on ``request``.
_DEFAULT_TIMEOUT: float = 30.0


@dataclass(slots=True)
class HttpResponse:
    status_code: int
    body: bytes
    headers: dict[str, str]

    def _decoded_body(self) -> bytes:
        encoding = self.headers.get("content-encoding", "").lower()
        if encoding == "gzip" or self.body[:2] == b"\x1f\x8b":
            return gzip.decompress(self.body)
        return self.body

    def json(self) -> object:
        return json.loads(self._decoded_body().decode("utf-8"))

    def text(self) -> str:
        return self._decoded_body().decode("utf-8")


class Transport(Protocol):
    def request(
        self,
        method: str,
        url: str,
        headers: dict[str, str],
        body: bytes | None = None,
        *,
        timeout: float | None = None,
    ) -> HttpResponse: ...


class UrllibTransport:
    """urllib-based HTTP transport with an explicit per-request timeout.

    The constructor default (``timeout``) applies to every call unless the
    caller provides a per-call override via ``request(..., timeout=...)``.
    Passing ``None`` as either falls back to ``_DEFAULT_TIMEOUT``; there is
    intentionally no way to opt out of a timeout entirely — a hung socket
    should never block the CLI or MCP server indefinitely.
    """

    def __init__(self, *, timeout: float = _DEFAULT_TIMEOUT) -> None:
        self._timeout = timeout

    def request(
        self,
        method: str,
        url: str,
        headers: dict[str, str],
        body: bytes | None = None,
        *,
        timeout: float | None = None,
    ) -> HttpResponse:
        effective_timeout = timeout if timeout is not None else self._timeout
        req = Request(url=url, method=method, headers=headers, data=body)
        try:
            with urlopen(req, timeout=effective_timeout) as res:
                return HttpResponse(
                    status_code=res.getcode(),
                    body=res.read(),
                    headers={k.lower(): v for k, v in res.headers.items()},
                )
        except HTTPError as exc:
            raise PlaudApiError.from_http_error(exc) from exc
        except TimeoutError as exc:
            # socket.timeout is a subclass of OSError on Python 3.11+ but may
            # NOT be a URLError — catch it explicitly so callers always get a
            # PlaudApiError rather than a raw socket exception.  Flagged
            # network_error=True (#143) so classify() treats a transient
            # blip as retryable instead of aborting a long poll/merge wait.
            raise PlaudApiError(
                f"Plaud API request timed out after {effective_timeout}s", network_error=True
            ) from exc
        except URLError as exc:
            # No HTTP response was received at all (DNS failure, connection
            # refused, etc.) — also a transient transport failure, not a
            # structural API problem.  See network_error=True note above.
            raise PlaudApiError(f"Plaud API request failed: {exc.reason}", network_error=True) from exc
