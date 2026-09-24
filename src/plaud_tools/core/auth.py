from __future__ import annotations

from typing import Any
from urllib.parse import urlencode

from .errors import PlaudApiError
from .models import BROWSER_USER_AGENT, base_url, redirect_api_domain, region_for_api_domain
from .session import PlaudSession, SessionStoreProtocol
from .transport import Transport, UrllibTransport


class PlaudAuth:
    def __init__(self, store: SessionStoreProtocol, transport: Transport | None = None) -> None:
        self.store = store
        self.transport = transport or UrllibTransport()

    def login(self, email: str, password: str, region: str) -> PlaudSession:
        """Sign in with email + password and save the session.

        If the account lives in another region, Plaud answers ``status: -302``
        with the right API host in ``data.domains.api``. Login follows that
        once and stores the region it actually signed in to.

        A wrong email or password comes back as HTTP 200 with ``status: -2``
        and ``msg: "wrong account or password"`` (verified live 2026-09-24),
        not a 401. That surfaces as a plain PlaudApiError carrying Plaud's
        message and ``plaud_code``, never as an expired-session error.
        """
        payload = self._post_login(email, password, region)
        if payload.get("status") == -302:
            next_region = region_for_api_domain(redirect_api_domain(payload))
            if next_region is None:
                raise PlaudApiError("Login redirected to an unrecognized API host.")
            region = next_region
            payload = self._post_login(email, password, region)
            if payload.get("status") == -302:
                raise PlaudApiError("region redirect loop")

        token = payload.get("access_token")
        status = payload.get("status")
        if status != 0 or not isinstance(token, str) or not token:
            msg = str(payload.get("msg") or f"Login failed (status {status})")
            raise PlaudApiError(msg, plaud_code=status, plaud_msg=msg)

        session = PlaudSession(access_token=token, region=region, email=email)
        self.store.save(session)
        return session

    def _post_login(self, email: str, password: str, region: str) -> dict[str, Any]:
        body = urlencode({"username": email, "password": password}).encode("utf-8")
        response = self.transport.request(
            method="POST",
            url=f"{base_url(region)}/auth/access-token",
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
        return payload
