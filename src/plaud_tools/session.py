from __future__ import annotations

import base64
import ctypes
import importlib
import json
import logging
import os
from dataclasses import asdict, dataclass
from pathlib import Path
from time import time
from typing import Any, Literal, Protocol

from . import appdata
from .errors import PlaudSessionExpiredError

log = logging.getLogger(__name__)

# Hard gate: require() refuses a token within this window of expiry.  Plaud
# now issues 30-day tokens (was ~291 days), so a 30-day buffer rejected every
# freshly-issued token and bricked the MCP; that was fixed by shrinking the
# buffer to 3 days.  3 days still burned 10% of every token's life, and the
# tray's expiry *warning* fired at the same 3-day threshold — so the warning
# and the breakage started on the same day, giving users no runway to react.
# 24 hours is enough lead time to force a re-sign-in immediately before Plaud
# would reject the token outright, while leaving room for
# TRAY_EXPIRY_WARNING_DAYS (below) to warn well in advance of this buffer.
TOKEN_REFRESH_BUFFER_SECONDS = 24 * 60 * 60

# Soft gate: the tray/HomeWindow show an expiry warning once the token has
# this many days or fewer left.  Must strictly exceed
# TOKEN_REFRESH_BUFFER_SECONDS (in days) so the warning always precedes
# require() refusing the token -- see the buffer comment above for the
# incident this pairs with.
TRAY_EXPIRY_WARNING_DAYS = 5

_SECONDS_PER_DAY = 86_400


def _decode_jwt_segment(jwt: str, index: int) -> dict[str, Any] | None:
    """Decode JWT segment *index* (0=header, 1=payload) without verifying the signature.

    Returns None on any parse error or malformed structure. Shared by
    ``_decode_header_safe`` and ``SessionManager._decode_expiry``, which
    previously duplicated this exact base64url-pad-and-decode dance for
    different segment indices (Wave 5, 2026-07-06 audit, S7.7).
    """
    parts = jwt.split(".")
    if len(parts) != 3:
        return None
    segment = parts[index] + "=" * (-len(parts[index]) % 4)
    try:
        decoded = base64.urlsafe_b64decode(segment.encode("ascii"))
        obj = json.loads(decoded.decode("utf-8"))
        return obj if isinstance(obj, dict) else None
    except (ValueError, json.JSONDecodeError):
        return None


def _decode_header_safe(jwt: str) -> dict[str, Any]:
    """Decode the header segment of a JWT without verifying the signature.

    Returns an empty dict on any parse error.
    """
    return _decode_jwt_segment(jwt, 0) or {}


# Legacy credentials written by the predecessor TypeScript tool
# (``plaud-toolkit``).  Stored as two separate Windows Credential Manager
# entries — see ``SessionStore._load_from_legacy_keyring``.
_LEGACY_KEYRING_JWT_TARGET = "jwt.plaud-toolkit"
_LEGACY_KEYRING_JWT_USERNAME = "jwt"
_LEGACY_KEYRING_PROFILE_TARGET = "profile.plaud-toolkit"
_LEGACY_KEYRING_PROFILE_USERNAME = "profile"


# DPAPI helpers — see ``SessionStore._load_from_dpapi`` for the rationale.
# These wrap ``Crypt32.CryptProtectData`` / ``CryptUnprotectData`` in user
# scope so the encrypted blob is bound to the current Windows user account
# and decrypts from any spawned process under that account — *without*
# touching the Credential Manager service, which is the root cause of the
# transient ``get_password`` failures we are working around.
class _DATA_BLOB(ctypes.Structure):
    _fields_ = [
        ("cbData", ctypes.c_ulong),
        ("pbData", ctypes.POINTER(ctypes.c_byte)),
    ]


def _dpapi_protect(plaintext: bytes) -> bytes:
    """Encrypt ``plaintext`` with DPAPI (user scope). Raises on failure."""
    crypt32 = ctypes.windll.crypt32  # type: ignore[attr-defined]
    kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
    buf = ctypes.create_string_buffer(plaintext, len(plaintext))
    blob_in = _DATA_BLOB(len(plaintext), ctypes.cast(buf, ctypes.POINTER(ctypes.c_byte)))
    blob_out = _DATA_BLOB()
    if not crypt32.CryptProtectData(ctypes.byref(blob_in), None, None, None, None, 0, ctypes.byref(blob_out)):
        raise ctypes.WinError()
    try:
        return ctypes.string_at(blob_out.pbData, blob_out.cbData)
    finally:
        kernel32.LocalFree(blob_out.pbData)


def _dpapi_unprotect(ciphertext: bytes) -> bytes:
    """Decrypt a DPAPI blob produced by ``_dpapi_protect``. Raises on failure."""
    crypt32 = ctypes.windll.crypt32  # type: ignore[attr-defined]
    kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
    buf = ctypes.create_string_buffer(ciphertext, len(ciphertext))
    blob_in = _DATA_BLOB(len(ciphertext), ctypes.cast(buf, ctypes.POINTER(ctypes.c_byte)))
    blob_out = _DATA_BLOB()
    if not crypt32.CryptUnprotectData(
        ctypes.byref(blob_in), None, None, None, None, 0, ctypes.byref(blob_out)
    ):
        raise ctypes.WinError()
    try:
        return ctypes.string_at(blob_out.pbData, blob_out.cbData)
    finally:
        kernel32.LocalFree(blob_out.pbData)


# Sentinel used by ``SessionStore.__init__`` to distinguish "caller did not
# pass a dpapi_path, please auto-default" from "caller explicitly passed None
# to disable DPAPI."  Without this, tests cannot opt out of the auto-default
# without also changing the service_name.
_DPAPI_PATH_DEFAULT: object = object()


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
        self.path = Path(path) if path is not None else appdata.session_path()

    @staticmethod
    def _legacy_session_path() -> Path:
        """Pre-ADR-004 location: ``~/.config/plaud-tools/session.json``."""
        return Path.home() / ".config" / "plaud-tools" / "session.json"

    @staticmethod
    def _read_session_file(path: Path) -> PlaudSession | None:
        """Best-effort read of a single session file; never raises (#145).

        A BOM, truncated write (crash mid-save), or otherwise corrupt/partial
        JSON must fall through as "no session here" rather than crashing the
        caller — this is a last-resort store and a bad file on disk should
        degrade to "please sign in again", not an unhandled exception.
        ``utf-8-sig`` transparently strips a leading BOM when present and
        behaves exactly like ``utf-8`` when it is not.
        """
        if not path.exists():
            return None
        try:
            text = path.read_text(encoding="utf-8-sig")
        except OSError:
            log.warning("Failed to read session file %s; treating as missing", path, exc_info=True)
            return None
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            log.warning("Session file %s contains invalid/corrupt JSON; treating as missing", path)
            return None
        if not isinstance(data, dict):
            log.warning("Session file %s has unexpected shape; treating as missing", path)
            return None
        try:
            return PlaudSession(**data)
        except TypeError:
            log.warning("Session file %s has unexpected fields; treating as missing", path)
            return None

    def load(self) -> PlaudSession | None:
        session = self._read_session_file(self.path)
        if session is not None:
            return session
        # One-shot legacy-path read: if the new appdata path does not exist (or
        # is unreadable/corrupt) but the old ~/.config/plaud-tools/session.json
        # does, read from there. Subsequent saves go to self.path (the new
        # appdata path).  This handles the rare users who relied on
        # FileSessionStore as last-resort fallback (both keyring AND DPAPI
        # unavailable) and whose session.json lives at the pre-ADR-004 location.
        return self._read_session_file(self._legacy_session_path())

    def save(self, session: PlaudSession) -> None:
        """Write *session* atomically: temp file + ``os.replace`` (#145).

        Writing directly to ``self.path`` can leave a truncated/partial file
        behind if the process is killed mid-write (power loss, forced
        shutdown) — exactly the corruption ``_read_session_file`` above has to
        tolerate.  Writing to a sibling temp file first and swapping it in
        with ``os.replace`` (atomic on both POSIX and Windows) means readers
        only ever see either the old complete file or the new complete file.
        """
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = self.path.with_name(self.path.name + ".tmp")
        tmp_path.write_text(json.dumps(asdict(session), indent=2), encoding="utf-8")
        try:
            os.chmod(tmp_path, 0o600)
        except OSError:
            pass
        os.replace(tmp_path, self.path)

    def clear(self) -> None:
        """Delete both the primary session file and the legacy pre-ADR-004 file.

        Sign-out must leave no plaintext token recoverable anywhere: deleting
        only ``self.path`` let ``load()``'s legacy-path fallback resurrect a
        cleared session on the very next read for users who still have a file
        at the old location (#144).
        """
        for path in (self.path, self._legacy_session_path()):
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass


class SessionStore:
    def __init__(
        self,
        path: str | Path | None = None,
        *,
        service_name: str = "plaud-tools",
        account_name: str = "session",
        dpapi_path: str | Path | None | object = _DPAPI_PATH_DEFAULT,
    ) -> None:
        self.file_store = FileSessionStore(path)
        self.service_name = service_name
        self.account_name = account_name
        # Production default: enable DPAPI shadow only for the canonical
        # service name, so test fixtures and dev experiments with custom
        # service names don't write to (or read from) the real user's
        # %LOCALAPPDATA%\PlaudTools\session.dat.  Tests that exercise the
        # DPAPI path pass an explicit ``dpapi_path`` under ``tmp_path``; tests
        # that want DPAPI explicitly disabled pass ``dpapi_path=None``.
        if dpapi_path is _DPAPI_PATH_DEFAULT:
            dpapi_path = appdata.dpapi_shadow_path() if service_name == "plaud-tools" else None
        self.dpapi_path: Path | None = Path(dpapi_path) if dpapi_path else None  # type: ignore[arg-type]

    def load(self) -> PlaudSession | None:
        session, _ = self.load_with_source()
        return session

    def load_with_source(
        self,
    ) -> tuple[
        PlaudSession | None, Literal["env", "keyring", "legacy_keyring", "dpapi_file", "file", "missing"]
    ]:
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
            # Best-effort DPAPI shadow self-heal: existing users upgrading from
            # <v0.2.7 already have a healthy keyring session but no shadow file,
            # so the DPAPI fallback wouldn't help them until their next sign-in
            # (~9 months away given 291-day tokens).  Write the shadow once on
            # first healthy keyring load.  Skip if it already exists so we
            # never overwrite a freshly-saved shadow with an older read.
            if self.dpapi_path is not None and not self.dpapi_path.exists():
                self._save_to_dpapi(session)
            return session, "keyring"

        # One-shot migration from the predecessor TypeScript tool's credentials.
        # If found, rewrite under the new naming and delete the legacy entries.
        session = self._load_from_legacy_keyring()
        if session is not None:
            self._migrate_legacy_session(session)
            return session, "legacy_keyring"

        # DPAPI shadow file — bypasses the Credential Manager service entirely,
        # which is the failure mode we are working around.  Only kicks in when
        # the keyring retry budget has already given up and we are still on
        # Windows under the canonical service name.
        session = self._load_from_dpapi()
        if session is not None:
            log.warning(
                "DPAPI shadow file fallback fired; keyring read returned None "
                "but DPAPI decrypted cleanly.  Windows Credential Manager is "
                "likely unhealthy in this process — investigate if this fires "
                "repeatedly."
            )
            return session, "dpapi_file"

        session = self.file_store.load()
        if session is not None:
            return session, "file"

        return None, "missing"

    def save(self, session: PlaudSession) -> None:
        keyring_ok = self._save_to_keyring(session)
        # Always also write the DPAPI shadow on Windows so the next cold-start
        # MCP read has a Credential-Manager-independent path to the session.
        dpapi_ok = self._save_to_dpapi(session)
        if keyring_ok or dpapi_ok:
            return
        # Last resort: plaintext JSON.  Only reached if BOTH the OS-protected
        # stores are unavailable (non-Windows without a working keyring, or a
        # Windows box where DPAPI is broken too).
        self.file_store.save(session)

    def _load_keyring_module(self) -> Any:
        """Import the keyring module lazily; returns the module or None on ImportError."""
        try:
            return importlib.import_module("keyring")
        except ImportError:
            log.warning("keyring module not importable; falling back to file store")
            return None

    # Windows Credential Manager occasionally returns transient errors *or*
    # a spurious None under load.  Multiple cases observed in production:
    # - issue #78: get_password raised, was swallowed, the next call worked.
    # - post-v0.2.5: get_password returned None despite the entry existing
    #   (confirmed by a diagnostic call 50 ms later returning the same entry
    #   with 299 days remaining).
    # - post-v0.2.6: the MCP cold-start window (process spawned by an AI
    #   client) sometimes returns None for hundreds of milliseconds before
    #   the credential service settles, even though a diagnostic call shortly
    #   after succeeds with the same entry and 291 days remaining.
    #
    # The retry uses a progressive backoff so we cover a wide total window
    # (~3.5 s) without hammering the credential service every 100 ms while
    # it's still warming up.  Most legitimate transients recover in the
    # first two probes (200 ms wall); the longer waits exist for the
    # tail-of-distribution cases.  The cost is paid only by the genuinely
    # empty case (signed-out user), which is rare.  Exceptions and Nones
    # are treated the same — both look transient in practice and the loop
    # handles them uniformly.
    _KEYRING_RETRY_DELAYS_S: tuple[float, ...] = (0.1, 0.1, 0.2, 0.4, 0.8, 1.0, 1.0)

    @property
    def _keyring_retry_attempts(self) -> int:
        """Total reads attempted before giving up (delays length + 1)."""
        return len(self._KEYRING_RETRY_DELAYS_S) + 1

    def _get_password_with_retry(self, keyring: Any) -> str | None:
        from time import sleep

        delays = self._KEYRING_RETRY_DELAYS_S
        total = len(delays) + 1
        for attempt in range(1, total + 1):
            try:
                result = keyring.get_password(self.service_name, self.account_name)
            except Exception:
                if attempt < total:
                    delay = delays[attempt - 1]
                    log.warning(
                        "keyring.get_password raised on attempt %d of %d for service=%r "
                        "account=%r; retrying in %.3fs",
                        attempt,
                        total,
                        self.service_name,
                        self.account_name,
                        delay,
                        exc_info=True,
                    )
                    sleep(delay)
                    continue
                log.warning(
                    "keyring.get_password raised on final attempt %d of %d for service=%r "
                    "account=%r; giving up",
                    attempt,
                    total,
                    self.service_name,
                    self.account_name,
                    exc_info=True,
                )
                return None
            if result is not None:
                return str(result)  # keyring.get_password returns Any; we know it's a str here
            if attempt < total:
                delay = delays[attempt - 1]
                log.warning(
                    "keyring.get_password returned None on attempt %d of %d for service=%r "
                    "account=%r; retrying in %.3fs",
                    attempt,
                    total,
                    self.service_name,
                    self.account_name,
                    delay,
                )
                sleep(delay)
        return None

    def _load_from_keyring(self) -> PlaudSession | None:
        keyring = self._load_keyring_module()
        if keyring is None:
            return None
        payload = self._get_password_with_retry(keyring)
        if not payload:
            return None
        try:
            parsed = json.loads(payload)
        except json.JSONDecodeError:
            log.warning("keyring payload is not valid JSON; treating as missing")
            return None
        try:
            return PlaudSession(**parsed)
        except TypeError:
            log.warning("keyring payload has unexpected shape; treating as missing")
            return None

    def _load_from_legacy_keyring(self) -> PlaudSession | None:
        """Read credentials written by the predecessor TypeScript tool.

        ``plaud-toolkit`` stored two separate Windows Credential Manager
        entries:

        - target=``jwt.plaud-toolkit`` (username=``jwt``) — the JWT access token
          as a bare string.
        - target=``profile.plaud-toolkit`` (username=``profile``) — a JSON blob
          like ``{"email": "...", "region": "us"}``.

        Returns a ``PlaudSession`` if both are readable, else ``None``.  Does
        not validate the JWT's expiry — that's up to ``SessionManager``.

        Only the production ``plaud-tools`` service name participates in the
        migration: SessionStore instances created with custom service names
        (test fixtures, CLI experiments) must not poke at the user's real
        legacy credentials.
        """
        if self.service_name != "plaud-tools":
            return None
        keyring = self._load_keyring_module()
        if keyring is None:
            return None
        try:
            jwt_cred = keyring.get_credential(_LEGACY_KEYRING_JWT_TARGET, None)
            profile_cred = keyring.get_credential(_LEGACY_KEYRING_PROFILE_TARGET, None)
        except Exception:
            log.warning("legacy plaud-toolkit credential lookup raised", exc_info=True)
            return None
        if jwt_cred is None or profile_cred is None:
            return None
        access_token = (jwt_cred.password or "").strip()
        if not access_token:
            return None
        try:
            profile = json.loads(profile_cred.password or "{}")
        except json.JSONDecodeError:
            profile = {}
        email = profile.get("email") if isinstance(profile, dict) else None
        region = profile.get("region") if isinstance(profile, dict) else None
        return PlaudSession(
            access_token=access_token,
            region=region or "us",
            email=email if isinstance(email, str) else None,
        )

    def _migrate_legacy_session(self, session: PlaudSession) -> None:
        """Persist a legacy session under the new naming and clean up the old entries.

        Cleanup is best-effort — failures are logged but do not raise, since
        the migration's main goal (making the new entry available) has already
        succeeded by the time we attempt deletion.
        """
        saved_to_keyring = self._save_to_keyring(session)
        if not saved_to_keyring:
            # Save failed — fall back to file store and leave the legacy
            # entries in place as a safety net.
            self.file_store.save(session)
            log.info(
                "Migrated plaud-toolkit credentials to file store (keyring save unavailable); "
                "legacy entries left in place"
            )
            return

        keyring = self._load_keyring_module()
        if keyring is not None:
            for target, username in (
                (_LEGACY_KEYRING_JWT_TARGET, _LEGACY_KEYRING_JWT_USERNAME),
                (_LEGACY_KEYRING_PROFILE_TARGET, _LEGACY_KEYRING_PROFILE_USERNAME),
            ):
                try:
                    keyring.delete_password(target, username)
                except Exception:
                    log.info(
                        "Could not delete legacy plaud-toolkit entry target=%r",
                        target,
                        exc_info=True,
                    )
        log.info(
            "Migrated plaud-toolkit credentials to service=%r account=%r (email=%s region=%s)",
            self.service_name,
            self.account_name,
            session.email,
            session.region,
        )

    def _save_to_keyring(self, session: PlaudSession) -> bool:
        keyring = self._load_keyring_module()
        if keyring is None:
            return False
        try:
            keyring.set_password(self.service_name, self.account_name, json.dumps(asdict(session)))
        except Exception:
            # Surfacing this is the only signal that explains a "logged in, then
            # gone after process exit" symptom — the bare swallow used to make
            # silent fall-through to the file store indistinguishable from
            # success.  We still return False so the caller falls back, but the
            # log line tells us a keyring backend bug exists.
            log.warning(
                "keyring.set_password failed for service=%r account=%r; falling back to file store",
                self.service_name,
                self.account_name,
                exc_info=True,
            )
            return False
        return True

    # ------------------------------------------------------------------
    # DPAPI shadow file — Windows-only secondary store that bypasses the
    # Credential Manager service entirely.  Same crypto primitive Credential
    # Manager uses internally, but accessed directly so spawned MCP processes
    # have a path to the session that does not depend on the credential
    # service's cold-start settling window.
    # ------------------------------------------------------------------

    def prime_dpapi_shadow(self) -> bool:
        """Best-effort, low-latency DPAPI shadow self-heal for tray-startup.

        Why this exists: ``load_with_source`` also writes the shadow on a
        first successful keyring load, but it pays the full keyring retry
        budget (~3.6 s worst-case) and only runs after the tray finishes
        importing pystray/PIL — a ~3-5 s window during which an AI client
        respawning its MCP child can still hit the cold-start
        ``session_expired`` path because the shadow file does not yet exist.
        This entry point is meant to be called from the tray exe's entry
        script *before* the heavy imports load, with a single non-retrying
        keyring read so signed-out users do not pay the retry budget here.

        Returns True iff this call wrote the shadow.  Safe to call any
        number of times — when the shadow already exists, returns False
        without touching keyring at all.
        """
        if self.dpapi_path is None:
            return False
        if self.dpapi_path.exists():
            return False
        keyring = self._load_keyring_module()
        if keyring is None:
            return False
        try:
            payload = keyring.get_password(self.service_name, self.account_name)
        except Exception:
            return False
        if not payload:
            return False
        try:
            parsed = json.loads(payload)
            session = PlaudSession(**parsed)
        except (json.JSONDecodeError, TypeError):
            return False
        return self._save_to_dpapi(session)

    def _save_to_dpapi(self, session: PlaudSession) -> bool:
        if self.dpapi_path is None:
            return False
        try:
            blob = _dpapi_protect(json.dumps(asdict(session)).encode("utf-8"))
        except Exception:
            log.warning(
                "DPAPI encryption failed for shadow file %s; skipping",
                self.dpapi_path,
                exc_info=True,
            )
            return False
        try:
            self.dpapi_path.parent.mkdir(parents=True, exist_ok=True)
            self.dpapi_path.write_bytes(blob)
        except OSError:
            log.warning(
                "DPAPI shadow file write failed for %s; skipping",
                self.dpapi_path,
                exc_info=True,
            )
            return False
        return True

    def _load_from_dpapi(self) -> PlaudSession | None:
        if self.dpapi_path is None or not self.dpapi_path.exists():
            return None
        try:
            blob = self.dpapi_path.read_bytes()
        except OSError:
            log.warning(
                "DPAPI shadow file read failed for %s; ignoring",
                self.dpapi_path,
                exc_info=True,
            )
            return None
        if not blob:
            return None
        try:
            plaintext = _dpapi_unprotect(blob)
        except Exception:
            log.warning(
                "DPAPI decryption failed for shadow file %s; ignoring "
                "(file may have been written by a different Windows user)",
                self.dpapi_path,
                exc_info=True,
            )
            return None
        try:
            parsed = json.loads(plaintext.decode("utf-8"))
            return PlaudSession(**parsed)
        except (UnicodeDecodeError, json.JSONDecodeError, TypeError):
            log.warning(
                "DPAPI shadow file %s has unexpected payload shape; ignoring",
                self.dpapi_path,
            )
            return None

    def clear(self) -> None:
        keyring = self._load_keyring_module()
        if keyring is not None:
            try:
                keyring.delete_password(self.service_name, self.account_name)
            except Exception:
                pass
        # Delegates to FileSessionStore.clear(), which also removes the
        # legacy pre-ADR-004 session file (#144) — not just self.path.
        self.file_store.clear()
        if self.dpapi_path is not None:
            try:
                self.dpapi_path.unlink(missing_ok=True)
            except OSError:
                pass


class SessionManager:
    # When the backing store does not expose a probe path, fall back to a
    # time-based TTL so we still revalidate after another process may have
    # written a new session.  Five minutes balances freshness against the cost
    # of hitting the keyring on every MCP call.
    _CACHE_TTL_SECONDS: float = 300.0

    def __init__(self, store: SessionStoreProtocol) -> None:
        self.store = store
        # Cache entry: (session, wall-clock time of load, file mtime at load or
        # None, cached token's decoded exp claim or None).  None means no entry
        # is cached.  Using a tuple avoids separate fields and keeps the
        # hot-path check allocation-free.
        self._cache: tuple[PlaudSession, float, float | None, int | None] | None = None

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _probe_path(self) -> Path | None:
        """Return the best file path available for a cheap mtime staleness probe.

        Prefers the DPAPI shadow (always written on save, survives keyring
        hiccups) over the plaintext session file.  Returns None when the store
        does not expose either path (e.g. a custom ``SessionStoreProtocol``
        implementation or an env-var-only store).
        """
        store = self.store
        # SessionStore exposes both dpapi_path and file_store.path.
        dpapi: Path | None = getattr(store, "dpapi_path", None)
        if dpapi is not None and dpapi.exists():
            return dpapi
        # Check for a nested file_store (SessionStore pattern).
        file_store = getattr(store, "file_store", None)
        if file_store is not None:
            fpath: Path | None = getattr(file_store, "path", None)
            if fpath is not None and fpath.exists():
                return fpath
        # Check for a direct path attribute (FileSessionStore used as a store directly).
        direct_path: Path | None = getattr(store, "path", None)
        if direct_path is not None and direct_path.exists():
            return direct_path
        return None

    @staticmethod
    def _mtime(path: Path) -> float | None:
        """Return the mtime of *path*, or None if it cannot be statted."""
        try:
            return path.stat().st_mtime
        except OSError:
            return None

    def _is_stale(self, loaded_at: float, mtime_at_load: float | None, expires_at: int | None) -> bool:
        """Return True when the cached session should be discarded.

        The expiry check runs unconditionally, before the mtime/TTL probe
        (#138): the mtime probe only detects that the *backing file* changed,
        it says nothing about the passage of time toward the *cached token's*
        own expiry.  A long-lived MCP process that loads a session once and
        then never sees the file change would previously serve that same
        (now-expired) token forever — the mtime check made the expiry
        re-check unreachable.  Checking expiry first, on every call, is a
        single cheap int comparison and costs nothing on the hot path.

        Falls back to the mtime probe when available (zero-allocation, no
        keyring contact), then to a wall-clock TTL when no path can be probed.
        """
        if expires_at is not None and int(time()) + TOKEN_REFRESH_BUFFER_SECONDS > expires_at:
            return True
        path = self._probe_path()
        if path is not None:
            current_mtime = self._mtime(path)
            # File changed (or disappeared) since the cache was populated.
            return current_mtime != mtime_at_load
        # No path available — fall back to wall-clock TTL.
        return time() - loaded_at > self._CACHE_TTL_SECONDS

    def _make_cache_entry(
        self, session: PlaudSession
    ) -> tuple[PlaudSession, float, float | None, int | None]:
        """Build a fresh cache tuple for *session* using the current probe state."""
        path = self._probe_path()
        mtime = self._mtime(path) if path is not None else None
        expires_at = self._decode_expiry(session.access_token)
        return (session, time(), mtime, expires_at)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def require(self) -> PlaudSession:
        if self._cache is not None:
            cached_session, loaded_at, mtime_at_load, expires_at = self._cache
            if not self._is_stale(loaded_at, mtime_at_load, expires_at):
                # Hot path: cached token is not within the expiry buffer, the
                # backing store is unchanged, and TTL has not elapsed. No
                # keyring contact, no file I/O beyond the mtime stat above.
                return cached_session
        session = self.store.load()
        if not session:
            raise PlaudSessionExpiredError("No Plaud session available.")
        expires_at = self._decode_expiry(session.access_token)
        if expires_at is None:
            raise PlaudSessionExpiredError("Stored Plaud token is malformed.")
        if int(time()) + TOKEN_REFRESH_BUFFER_SECONDS > expires_at:
            raise PlaudSessionExpiredError("Plaud session expired or expiring soon.")
        # Safe under concurrent asyncio.to_thread handler calls: all threads
        # compute and assign the same validated session object (idempotent
        # assignment); the GIL makes the attribute write itself atomic.
        self._cache = self._make_cache_entry(session)
        return session

    def invalidate_cache(self) -> None:
        """Discard the in-memory session cache so the next ``require()`` reloads from the store."""
        self._cache = None

    def update_region(self, region: str) -> PlaudSession:
        # Bypass the cache to ensure we read the freshest token from the store,
        # then update both the store and the cache atomically.
        self._cache = None
        session = self.require()
        updated = PlaudSession(
            access_token=session.access_token,
            region=region,
            email=session.email,
        )
        self.store.save(updated)
        self._cache = self._make_cache_entry(updated)
        return updated

    def days_until_expiry(self) -> int | None:
        """Return whole days until the stored token expires, or None if no session."""
        session = self.store.load()
        if session is None:
            return None
        exp = self._decode_expiry(session.access_token)
        if exp is None:
            return None
        return self._days_remaining(exp)

    @staticmethod
    def _days_remaining(exp: int) -> int:
        """Return whole days remaining until *exp* (epoch seconds), floored at 0.

        Shared by ``days_until_expiry`` and ``diagnose`` so the latter no
        longer re-derives the same day-count arithmetic inline (Wave 5,
        2026-07-06 audit, S7.7).
        """
        remaining = exp - int(time())
        return max(0, remaining // _SECONDS_PER_DAY)

    def _decode_expiry(self, jwt: str) -> int | None:
        obj = _decode_jwt_segment(jwt, 1)
        if obj is None:
            return None
        exp = obj.get("exp")
        return int(exp) if isinstance(exp, int | float) else None

    def diagnose(self) -> dict[str, Any]:
        """Best-effort snapshot of the session as seen by this SessionManager.

        Returns a dict with the following fields (all are session-y; MCP-local
        fields like ``mcp_pid`` and ``mcp_version`` are added by the caller):

        Always present:
          - ``store_source``: one of ``env``, ``keyring``, ``legacy_keyring``,
            ``dpapi_file``, ``file``, or ``missing``.

        Present when a session was loaded:
          - ``region``: the session region string.
          - ``email_present``: bool — True if the session carries an email.
          - ``token_typ``: the ``typ`` JWT header claim, if decodable.
          - ``days_until_expiry``: whole days until expiry, if decodable.

        Present only on unexpected failure:
          - ``diagnose_error``: ``"ExcType: message"`` string.
        """
        diag: dict[str, Any] = {}
        try:
            store = self.store
            if hasattr(store, "load_with_source"):
                session, source = store.load_with_source()
            else:
                session = store.load()
                source = "missing" if session is None else "file"
            diag["store_source"] = source
            if session is not None:
                diag["region"] = session.region
                diag["email_present"] = bool(session.email)
                header = _decode_header_safe(session.access_token)
                if header:
                    diag["token_typ"] = header.get("typ")
                exp = self._decode_expiry(session.access_token)
                if exp is not None:
                    diag["days_until_expiry"] = self._days_remaining(exp)
        except Exception as exc:
            diag["diagnose_error"] = f"{type(exc).__name__}: {exc}"
        return diag
