from __future__ import annotations

import base64
import importlib
import json
import os
from dataclasses import asdict, dataclass
from pathlib import Path
from time import time
from typing import Literal, Protocol

from .errors import PlaudSessionExpiredError

TOKEN_REFRESH_BUFFER_SECONDS = 30 * 24 * 60 * 60
_SECONDS_PER_DAY = 86_400


@dataclass(slots=True)
class PlaudSession:
    access_token: str
    region: str = "us"
    email: str | None = None


class SessionStoreProtocol(Protocol):
    def load(self) -> PlaudSession | None: ...
    def save(self, session: PlaudSession) -> None: ...


class FileSessionStore:
    def __init__(self, path: str | Path | None = None) -> None:
        self.path = Path(path or Path.home() / ".config" / "plaud-tools" / "session.json")

    def load(self) -> PlaudSession | None:
        if not self.path.exists():
            return None
        data = json.loads(self.path.read_text(encoding="utf-8"))
        return PlaudSession(**data)

    def save(self, session: PlaudSession) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(asdict(session), indent=2), encoding="utf-8")
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            pass


class SessionStore:
    def __init__(
        self,
        path: str | Path | None = None,
        *,
        service_name: str = "plaud-tools",
        account_name: str = "session",
    ) -> None:
        self.file_store = FileSessionStore(path)
        self.service_name = service_name
        self.account_name = account_name

    def load(self) -> PlaudSession | None:
        session, _ = self.load_with_source()
        return session

    def load_with_source(self) -> tuple[PlaudSession | None, Literal["env", "keyring", "file", "missing"]]:
        env_token = os.getenv("PLAUD_ACCESS_TOKEN")
        if env_token:
            return (
                PlaudSession(
                    access_token=env_token,
                    region=os.getenv("PLAUD_REGION", "us"),
                    email=os.getenv("PLAUD_EMAIL"),
                ),
                "env",
            )

        session = self._load_from_keyring()
        if session is not None:
            return session, "keyring"

        session = self.file_store.load()
        if session is not None:
            return session, "file"

        return None, "missing"

    def save(self, session: PlaudSession) -> None:
        if self._save_to_keyring(session):
            return
        self.file_store.save(session)

    def _load_keyring_module(self):
        try:
            return importlib.import_module("keyring")
        except ImportError:
            return None

    def _load_from_keyring(self) -> PlaudSession | None:
        keyring = self._load_keyring_module()
        if keyring is None:
            return None
        try:
            payload = keyring.get_password(self.service_name, self.account_name)
        except Exception:
            return None
        if not payload:
            return None
        try:
            parsed = json.loads(payload)
        except json.JSONDecodeError:
            return None
        try:
            return PlaudSession(**parsed)
        except TypeError:
            return None

    def _save_to_keyring(self, session: PlaudSession) -> bool:
        keyring = self._load_keyring_module()
        if keyring is None:
            return False
        try:
            keyring.set_password(self.service_name, self.account_name, json.dumps(asdict(session)))
        except Exception:
            return False
        return True

    def clear(self) -> None:
        keyring = self._load_keyring_module()
        if keyring is not None:
            try:
                keyring.delete_password(self.service_name, self.account_name)
            except Exception:
                pass
        try:
            self.file_store.path.unlink(missing_ok=True)
        except OSError:
            pass


class SessionManager:
    def __init__(self, store: SessionStoreProtocol) -> None:
        self.store = store
        self._cached_session: PlaudSession | None = None

    def require(self) -> PlaudSession:
        if self._cached_session is not None:
            return self._cached_session
        session = self.store.load()
        if not session:
            raise PlaudSessionExpiredError("No Plaud session available.")
        expires_at = self._decode_expiry(session.access_token)
        if expires_at is None:
            raise PlaudSessionExpiredError("Stored Plaud token is malformed.")
        if int(time()) + TOKEN_REFRESH_BUFFER_SECONDS > expires_at:
            raise PlaudSessionExpiredError("Plaud session expired or expiring soon.")
        self._cached_session = session
        return session

    def invalidate_cache(self) -> None:
        """Discard the in-memory session cache so the next ``require()`` reloads from the store."""
        self._cached_session = None

    def update_region(self, region: str) -> PlaudSession:
        # Bypass the cache to ensure we read the freshest token from the store,
        # then update both the store and the cache atomically.
        self._cached_session = None
        session = self.require()
        updated = PlaudSession(
            access_token=session.access_token,
            region=region,
            email=session.email,
        )
        self.store.save(updated)
        self._cached_session = updated
        return updated

    def days_until_expiry(self) -> int | None:
        """Return whole days until the stored token expires, or None if no session."""
        session = self.store.load()
        if session is None:
            return None
        exp = self._decode_expiry(session.access_token)
        if exp is None:
            return None
        remaining = exp - int(time())
        return max(0, remaining // _SECONDS_PER_DAY)

    def _decode_expiry(self, jwt: str) -> int | None:
        parts = jwt.split(".")
        if len(parts) != 3:
            return None
        payload = parts[1]
        payload += "=" * (-len(payload) % 4)
        try:
            decoded = base64.urlsafe_b64decode(payload.encode("ascii"))
            obj = json.loads(decoded.decode("utf-8"))
        except (ValueError, json.JSONDecodeError):
            return None
        exp = obj.get("exp")
        return int(exp) if isinstance(exp, int | float) else None
