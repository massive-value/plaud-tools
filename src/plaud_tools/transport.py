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
            raise PlaudApiError.from_http_error(exc) from exc
        except URLError as exc:
            raise PlaudApiError(f"Plaud API request failed: {exc.reason}") from exc
