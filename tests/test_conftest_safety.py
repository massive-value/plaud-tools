"""Trip-wire meta-tests for tests/conftest.py's OS-default-path redirects.

Every test in this suite runs under three autouse conftest.py fixtures that
redirect (or trip-wire) the real per-user paths a bare SessionStore /
FileSessionStore would otherwise resolve to:

  - _block_real_dpapi_shadow:       redirects appdata.dpapi_shadow_path() -> None
  - _block_real_session_path:       redirects appdata.session_path() -> tmp_path
  - _fail_if_real_shadow_written:   mtime trip-wire on the real shadow file

A prior incident (see conftest.py's _block_real_dpapi_shadow docstring) had a
test construct a SessionStore() with no explicit path, which silently
DPAPI-encrypted synthetic test data into the user's real production shadow
file on every pytest run. These meta-tests assert the redirects are actually
active for an ordinary test in this suite, so a future conftest.py refactor
that accidentally drops or narrows one of the autouse fixtures fails loudly
here instead of only surfacing the next time someone forgets
`dpapi_path=`/`path=` in a brand-new test (Wave 5, 2026-07-06 audit, §5.8).
"""

from __future__ import annotations

from pathlib import Path

from plaud_tools.core import appdata


def test_dpapi_shadow_path_is_redirected_to_none():
    """_block_real_dpapi_shadow must be active: the real DPAPI shadow path is masked."""
    assert appdata.dpapi_shadow_path() is None


def test_session_path_is_redirected_away_from_real_appdata(tmp_path):
    """_block_real_session_path must be active: session_path() must not resolve
    to the real per-user path a bare FileSessionStore() would read/write."""
    real_fallback = Path.home() / ".config" / "plaud-tools" / "session.json"
    redirected = appdata.session_path()
    assert redirected != real_fallback
    assert redirected.name == "session.json"
    # The fixture redirects into *this test's* tmp_path fixture instance, so
    # the resolved path must live somewhere under pytest's tmp root, never
    # under the real home directory.
    assert str(Path.home()) not in str(redirected) or str(tmp_path) in str(redirected)


def test_path_redirect_fixtures_are_registered_autouse():
    """Guard against the redirect/trip-wire fixtures losing autouse=True.

    Reads conftest.py's own source rather than exercising the fixtures
    indirectly -- this is a static structural check on the safety net itself
    (the thing meant to catch a *future* regression), not a behavioural test
    of production code.
    """
    conftest_src = (Path(__file__).parent / "conftest.py").read_text(encoding="utf-8")
    for fixture_name in (
        "_block_real_dpapi_shadow",
        "_block_real_session_path",
        "_fail_if_real_shadow_written",
    ):
        def_marker = f"def {fixture_name}("
        assert def_marker in conftest_src, f"conftest.py no longer defines {fixture_name}"
        idx = conftest_src.index(def_marker)
        preceding = conftest_src[:idx]
        decorator_start = preceding.rindex("@pytest.fixture")
        decorator_text = preceding[decorator_start:idx]
        assert "autouse=True" in decorator_text, (
            f"{fixture_name} must stay @pytest.fixture(autouse=True) -- without "
            f"autouse, any test that forgets to request it explicitly is unprotected "
            f"against writing to the user's real session/DPAPI-shadow files."
        )
