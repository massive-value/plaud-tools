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
