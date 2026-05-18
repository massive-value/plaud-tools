from __future__ import annotations

from urllib.parse import urlencode

from .errors import PlaudApiError
from .models import BASE_URLS, BROWSER_USER_AGENT
from .session import PlaudSession, SessionStoreProtocol
from .transport import Transport, UrllibTransport


class PlaudAuth:
    def __init__(self, store: SessionStoreProtocol, transport: Transport | None = None) -> None:
        self.store = store
        self.transport = transport or UrllibTransport()

    def login(self, email: str, password: str, region: str) -> PlaudSession:
        body = urlencode({"username": email, "password": password}).encode("utf-8")
        response = self.transport.request(
            method="POST",
            url=f"{BASE_URLS.get(region, BASE_URLS['us'])}/auth/access-token",
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "User-Agent": BROWSER_USER_AGENT,
            },
            body=body,
        )
        if response.status_code < 200 or response.status_code >= 300:
            raise PlaudApiError(f"Login request failed: HTTP {response.status_code}")

        payload = response.json()
        if not isinstance(payload, dict):
            raise PlaudApiError("Login response was not a JSON object.")

        token = payload.get("access_token")
        if payload.get("status") != 0 or not isinstance(token, str) or not token:
            raise PlaudApiError(str(payload.get("msg") or f"Login failed (status {payload.get('status')})"))

        session = PlaudSession(access_token=token, region=region, email=email)
        self.store.save(session)
        return session
