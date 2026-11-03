import importlib
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

# Stub `[tray]` extras so the `plaud_tools.tray.*` modules are importable in CI
# environments that only install `[dev]`. `tray.app` does `import pystray` and
# `from PIL import ...` at module top, which means any test that imports it
# would otherwise fail with ModuleNotFoundError. Per-test patches can still
# override these stubs.
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
    full budget per call, padding the suite by ~10 s.  Replacing the delays
    sequence with an empty tuple collapses both the wall-clock time *and* the
    retry count to a single attempt, keeping non-retry-shape tests instant.
    Tests that explicitly verify retry behaviour override this fixture with a
    zero-delay sequence of the appropriate length.
    """
    try:
        from plaud_tools.core.session import SessionStore
    except Exception:
        return
    monkeypatch.setattr(SessionStore, "_KEYRING_RETRY_DELAYS_S", (), raising=False)


# Capture the real production DPAPI shadow path once at import time, BEFORE
# any autouse fixture has monkeypatched ``appdata.dpapi_shadow_path``.  The
# lazy variant of this lookup would resolve to ``None`` inside the fixtures
# (the redirect fixture wins) and silently disable the trip-wire.
try:
    from plaud_tools.core.appdata import dpapi_shadow_path as _resolve_dpapi_shadow_path

    _REAL_DPAPI_SHADOW_PATH = _resolve_dpapi_shadow_path()
except Exception:
    _REAL_DPAPI_SHADOW_PATH = None


@pytest.fixture(autouse=True)
def _block_real_dpapi_shadow(monkeypatch):
    """Refuse to write to the user's real %LOCALAPPDATA%\\PlaudTools\\session.dat.

    Any ``SessionStore`` constructed without an explicit ``dpapi_path=`` on
    Windows defaults to the real production shadow path via
    ``appdata.dpapi_shadow_path()``.  Before v0.2.8, one such test
    (``test_session_store_prefers_keyring_when_available``) silently
    DPAPI-encrypted synthetic test data straight into the user's production
    shadow on every ``pytest`` run, triggering a session_expired toast and a
    sign-in prompt the next time the tray polled the session.  Redirecting
    the default to ``None`` here means any future regression that forgets
    ``dpapi_path=`` writes nothing — it does not corrupt the user's session.
    """
    monkeypatch.setattr("plaud_tools.core.appdata.dpapi_shadow_path", lambda: None)


@pytest.fixture(autouse=True)
def _block_real_session_path(monkeypatch, tmp_path):
    """Refuse to touch the user's real %LOCALAPPDATA%\\PlaudTools\\session.json.

    ``FileSessionStore()`` constructed without an explicit ``path=`` resolves to
    ``appdata.session_path()``.  Without this redirect, any test that constructs
    a bare ``FileSessionStore()`` would read from or write to the real user's
    session file.  Redirect to a per-test tmp directory so accidental omissions
    fail safely (file simply won't exist) rather than corrupting real state.
    """
    monkeypatch.setattr("plaud_tools.core.appdata.session_path", lambda: tmp_path / "session.json")


@pytest.fixture(autouse=True)
def _block_real_exports_dir(monkeypatch, tmp_path):
    """Keep ``export_transcript`` out of the user's real %LOCALAPPDATA%\\PlaudTools\\exports."""
    monkeypatch.setattr("plaud_tools.core.appdata.exports_dir", lambda: tmp_path / "exports")


@pytest.fixture(autouse=True)
def _fake_keyring_backend(monkeypatch):
    """Never let a test touch the real OS credential store.

    ``SessionStore`` lazily imports the ``keyring`` module and calls its
    module-level ``get_password``/``set_password``/``delete_password``,
    which delegate to whichever backend ``keyring.set_keyring()`` has
    configured. Several tests construct a bare ``SessionStore()`` (defaults
    to the production service name ``plaud-tools``) or a ``SessionStore``
    with a synthetic service name but no keyring stub — either way, without
    this fixture those calls hit the real Windows Credential Manager /
    macOS Keychain / Linux Secret Service. Swapping in an in-memory backend
    for the duration of every test makes that impossible by construction.

    Tests that monkeypatch ``session.importlib.import_module`` to inject a
    fake keyring module entirely bypass this backend and are unaffected.
    """
    import keyring
    import keyring.errors

    class _InMemoryKeyring(keyring.backend.KeyringBackend):
        priority = 9999  # highest priority so it's always selected

        def __init__(self):
            super().__init__()
            self._passwords: dict[tuple[str, str], str] = {}

        def get_password(self, service, username):
            return self._passwords.get((service, username))

        def set_password(self, service, username, password):
            self._passwords[(service, username)] = password

        def delete_password(self, service, username):
            try:
                del self._passwords[(service, username)]
            except KeyError:
                raise keyring.errors.PasswordDeleteError("not found") from None

    previous_backend = keyring.get_keyring()
    keyring.set_keyring(_InMemoryKeyring())

    # Belt-and-braces trip-wire: fail loudly if anything reaches a real OS
    # backend directly (bypassing keyring.get_keyring()), instead of quietly
    # touching production credentials.
    def _real_backend_forbidden(*_args, **_kwargs):
        raise AssertionError(
            "A test attempted to call a real OS keyring backend directly. "
            "Route keyring access through the autouse in-memory fixture instead."
        )

    for module_name, class_name in (
        ("keyring.backends.Windows", "WinVaultKeyring"),
        ("keyring.backends.macOS", "Keyring"),
        ("keyring.backends.SecretService", "Keyring"),
        ("keyring.backends.kwallet", "DBusKeyring"),
    ):
        try:
            backend_module = importlib.import_module(module_name)
        except ImportError:
            continue
        backend_cls = getattr(backend_module, class_name, None)
        if backend_cls is None:
            continue
        for method_name in ("get_password", "set_password", "delete_password"):
            if hasattr(backend_cls, method_name):
                monkeypatch.setattr(backend_cls, method_name, _real_backend_forbidden, raising=False)

    yield

    keyring.set_keyring(previous_backend)


@pytest.fixture(autouse=True)
def _fail_if_real_shadow_written():
    """Belt-and-braces trip-wire: fail loudly if the real shadow was touched.

    The ``_block_real_dpapi_shadow`` redirect handles the common case of a
    test forgetting ``dpapi_path=``.  This fixture catches the case where a
    future test *bypasses* the redirect — e.g. by monkeypatching
    ``appdata.dpapi_shadow_path`` back, or by manually constructing the
    production path.  Snapshot is per-test so the user's tray rewriting the
    shadow between tests does not produce false positives.
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


# Capture the real PowerShell profile paths once at import time, before the
# redirect fixture below swaps out the Documents known-folder lookup.
try:
    from plaud_tools.tray.setup import _all_ps_profile_paths as _resolve_real_profiles

    _REAL_PS_PROFILES = list(_resolve_real_profiles())
except Exception:
    _REAL_PS_PROFILES = []


@pytest.fixture(autouse=True)
def _block_real_ps_profiles(monkeypatch):
    """Keep tests away from the user's real PowerShell profiles.

    ``tray.setup._known_documents_dir()`` resolves the real (possibly
    OneDrive-redirected) Documents folder via the Windows shell API, which
    ``Path.home`` patches cannot redirect.  Returning None makes the profile
    helpers fall back to ``Path.home() / "Documents"``, which tests pin under
    ``tmp_path``.  The trip-wire below catches anything that slips through.
    """
    try:
        import plaud_tools.tray.setup as tray_setup
    except Exception:
        yield
        return
    monkeypatch.setattr(tray_setup, "_known_documents_dir", lambda: None)
    before = {p: p.stat().st_mtime if p.exists() else None for p in _REAL_PS_PROFILES}
    yield
    after = {p: p.stat().st_mtime if p.exists() else None for p in _REAL_PS_PROFILES}
    changed = [str(p) for p in _REAL_PS_PROFILES if before[p] != after[p]]
    if changed:
        raise AssertionError(
            f"A test modified the real PowerShell profile(s) {changed}. "
            "Pin Path.home() under tmp_path (or patch _known_documents_dir)."
        )


# Capture the real cross-process events file once at import time, before the
# redirect fixture below masks ``appdata.events_path``.
try:
    from plaud_tools.core.appdata import events_path as _resolve_events_path

    _REAL_EVENTS_PATH = _resolve_events_path()
except Exception:
    _REAL_EVENTS_PATH = None


@pytest.fixture(autouse=True)
def _block_real_events_file(monkeypatch, tmp_path):
    """Keep tests from writing to the running tray's real events.jsonl.

    The MCP handlers append ``session_expired`` events to
    ``appdata.events_path()``; the real PlaudTools tray polls that file and
    pops a sign-in toast for each line (issue #228: 393 spurious toasts in one
    afternoon of agent test runs).  ``mcp``, ``tray.background`` and
    ``tray.setup`` bind the function by name at import, so each binding is
    redirected as well as ``appdata`` itself.  The trip-wire afterwards fails
    the test if any event written by this process still reached the real file.
    """
    redirected = tmp_path / "events.jsonl"
    monkeypatch.setattr("plaud_tools.core.appdata.events_path", lambda: redirected)
    for module_name in ("plaud_tools.mcp_pt.mcp", "plaud_tools.tray.background", "plaud_tools.tray.setup"):
        module = sys.modules.get(module_name)
        if module is None:
            try:
                module = importlib.import_module(module_name)
            except Exception:
                continue
        monkeypatch.setattr(module, "_events_path", lambda: redirected, raising=False)

    yield

    real = _REAL_EVENTS_PATH
    if real is None or not real.exists():
        return
    try:
        lines = real.read_text(encoding="utf-8").splitlines()
    except OSError:
        return  # the tray may have just claimed the file
    mine = f'"mcp_pid": {os.getpid()},'
    if any(mine in line for line in lines):
        raise AssertionError(
            f"A test wrote an event to the real tray events file {real}. "
            "Route events through appdata.events_path() (redirected by _block_real_events_file)."
        )
