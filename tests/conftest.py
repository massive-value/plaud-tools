import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

# Stub `[tray]` extras so `plaud_tools.tray_app` is importable in CI environments
# that only install `[dev]`. tray_app does `import pystray` and `from PIL import ...`
# at module top, which means any test that imports tray_app would otherwise fail
# with ModuleNotFoundError. Per-test patches can still override these stubs.
for _name in (
    "pystray",
    "PIL",
    "PIL.Image",
    "PIL.ImageDraw",
):
    sys.modules.setdefault(_name, MagicMock())


@pytest.fixture(autouse=True)
def _zero_keyring_retry_delay(monkeypatch):
    """Make the SessionStore retry budget instant in tests.

    The production retry delays cover ~3.5 s of progressive backoff to ride
    out Windows Credential Manager hiccups on cold-start.  Tests that
    exercise the "no session" path against the real keyring (e.g. CLI
    invocations under a synthetic service name) would otherwise pay that
    full budget per call, padding the suite by ~10 s.  Forcing the base
    delay to 0 keeps the retry *shape* (attempt count, log lines, ordering)
    identical while collapsing wall-clock time.
    """
    try:
        from plaud_tools.session import SessionStore
    except Exception:
        return
    monkeypatch.setattr(SessionStore, "_KEYRING_RETRY_DELAY_S", 0.0, raising=False)


# Capture the real production DPAPI shadow path once at import time, BEFORE
# any autouse fixture has monkeypatched ``_default_dpapi_path``.  The lazy
# variant of this lookup would resolve to ``None`` inside the fixtures (the
# redirect fixture wins) and silently disable the trip-wire.
try:
    from plaud_tools.session import _default_dpapi_path as _resolve_default_dpapi_path
    _REAL_DPAPI_SHADOW_PATH = _resolve_default_dpapi_path()
except Exception:
    _REAL_DPAPI_SHADOW_PATH = None


@pytest.fixture(autouse=True)
def _block_real_dpapi_shadow(monkeypatch):
    """Refuse to write to the user's real %LOCALAPPDATA%\\PlaudTools\\session.dat.

    Any ``SessionStore`` constructed without an explicit ``dpapi_path=`` on
    Windows defaults to the real production shadow path via
    ``_default_dpapi_path()``.  Before v0.2.8, one such test
    (``test_session_store_prefers_keyring_when_available``) silently
    DPAPI-encrypted synthetic test data straight into the user's production
    shadow on every ``pytest`` run, triggering a session_expired toast and a
    sign-in prompt the next time the tray polled the session.  Redirecting
    the default to ``None`` here means any future regression that forgets
    ``dpapi_path=`` writes nothing — it does not corrupt the user's session.
    """
    monkeypatch.setattr("plaud_tools.session._default_dpapi_path", lambda: None)


@pytest.fixture(autouse=True)
def _fail_if_real_shadow_written():
    """Belt-and-braces trip-wire: fail loudly if the real shadow was touched.

    The ``_block_real_dpapi_shadow`` redirect handles the common case of a
    test forgetting ``dpapi_path=``.  This fixture catches the case where a
    future test *bypasses* the redirect — e.g. by monkeypatching
    ``_default_dpapi_path`` back, or by manually constructing the production
    path.  Snapshot is per-test so the user's tray rewriting the shadow
    between tests does not produce false positives.
    """
    shadow = _REAL_DPAPI_SHADOW_PATH
    if shadow is None:
        yield
        return
    before = shadow.stat().st_mtime if shadow.exists() else None
    yield
    after = shadow.stat().st_mtime if shadow.exists() else None
    if after != before:
        raise AssertionError(
            f"A test wrote to the real DPAPI shadow at {shadow} "
            f"(mtime {before!r} -> {after!r}).  Tests must pin "
            f"dpapi_path under tmp_path or pass dpapi_path=None."
        )
