from __future__ import annotations

import gzip
import json
from dataclasses import dataclass
from typing import Protocol
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .errors import PlaudApiError


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
    ) -> HttpResponse: ...


_BODY_TRUNCATE = 500


def _http_error_to_api_error(exc: HTTPError) -> PlaudApiError:
    """Convert a :class:`urllib.error.HTTPError` into a :class:`PlaudApiError`.

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
        truncated_body = raw_text[:_BODY_TRUNCATE] if len(raw_text) <= _BODY_TRUNCATE else raw_text[:_BODY_TRUNCATE] + "…"

    return PlaudApiError(
        message,
        http_status=exc.code,
        plaud_code=plaud_code,
        plaud_msg=plaud_msg,
        raw_body=truncated_body,
    )


class UrllibTransport:
    def request(
        self,
        method: str,
        url: str,
        headers: dict[str, str],
        body: bytes | None = None,
    ) -> HttpResponse:
        req = Request(url=url, method=method, headers=headers, data=body)
        try:
            with urlopen(req) as res:
                return HttpResponse(
                    status_code=res.getcode(),
                    body=res.read(),
                    headers={k.lower(): v for k, v in res.headers.items()},
                )
        except HTTPError as exc:
            raise _http_error_to_api_error(exc) from exc
        except URLError as exc:
            raise PlaudApiError(f"Plaud API request failed: {exc.reason}") from exc
