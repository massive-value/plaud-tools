"""Unit tests for verify_zip_checksum in plaud_tools.tray.updater.

These tests cover the verification scenarios (Wave 2 / C1, fail-closed per #113):
  - Matching hash → passes silently.
  - Tampered zip (wrong hash in SHA256SUMS) → raises ChecksumMismatch (fail-closed).
  - Absent SHA256SUMS URL (None) → raises ChecksumMismatch (fail-closed, #113).

All tests are pure Python (no network, no subprocess, no tkinter); urllib is
patched so tests run in any environment.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from plaud_tools.tray.updater import ChecksumMismatch, verify_zip_checksum

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _make_zip(tmp_path: Path, content: bytes = b"fake zip payload") -> Path:
    """Write *content* to a temp file and return the Path."""
    p = tmp_path / "PlaudTools.zip"
    p.write_bytes(content)
    return p


SUMS_URL = "https://github.com/massive-value/plaud-tools/releases/download/v9.9.9/SHA256SUMS"
# Where GitHub actually serves the asset from after its redirect.
FINAL_URL = "https://release-assets.githubusercontent.com/github-production-release-asset/1/abc"


def _mock_urlopen(sums_text: str, final_url: str = FINAL_URL):
    """Return a context-manager mock that yields a response with *sums_text*."""
    resp = MagicMock()
    resp.read.return_value = sums_text.encode("utf-8")
    resp.geturl.return_value = final_url
    resp.__enter__ = lambda s: s
    resp.__exit__ = MagicMock(return_value=False)
    return resp


def _patch_opener(sums_text: str, final_url: str = FINAL_URL):
    """Patch the allowlisted update opener to serve *sums_text*."""
    opener = MagicMock()
    opener.open.return_value = _mock_urlopen(sums_text, final_url)
    return patch("plaud_tools.tray.updater._UPDATE_OPENER", opener)


# ---------------------------------------------------------------------------
# verify_zip_checksum — matching hash
# ---------------------------------------------------------------------------


def test_verify_zip_checksum_matching_hash_passes(tmp_path: Path) -> None:
    """When the zip hash matches SHA256SUMS, verify_zip_checksum returns None (no error)."""
    payload = b"genuine zip content"
    zip_path = _make_zip(tmp_path, payload)
    expected = _sha256_hex(payload)
    sums_text = f"{expected}  PlaudTools.zip\n"

    with _patch_opener(sums_text):
        # Must not raise.
        result = verify_zip_checksum(zip_path, sums_url=SUMS_URL)

    assert result is None


# ---------------------------------------------------------------------------
# verify_zip_checksum — tampered zip
# ---------------------------------------------------------------------------


def test_verify_zip_checksum_tampered_zip_raises(tmp_path: Path) -> None:
    """A tampered zip (hash mismatch) must raise ChecksumMismatch — fail-closed."""
    payload = b"genuine zip content"
    zip_path = _make_zip(tmp_path, payload)
    tampered_hash = "a" * 64  # 64 lowercase hex chars, all wrong.
    sums_text = f"{tampered_hash}  PlaudTools.zip\n"

    with _patch_opener(sums_text):
        with pytest.raises(ChecksumMismatch) as exc_info:
            verify_zip_checksum(zip_path, sums_url=SUMS_URL)

    msg = str(exc_info.value).lower()
    assert "mismatch" in msg


def test_verify_zip_checksum_mismatch_message_contains_expected_and_actual(tmp_path: Path) -> None:
    """The ChecksumMismatch message must include both expected and actual hashes for diagnostics."""
    payload = b"genuine zip content"
    zip_path = _make_zip(tmp_path, payload)
    tampered_hash = "b" * 64
    sums_text = f"{tampered_hash}  PlaudTools.zip\n"

    with _patch_opener(sums_text):
        with pytest.raises(ChecksumMismatch) as exc_info:
            verify_zip_checksum(zip_path, sums_url=SUMS_URL)

    msg = str(exc_info.value)
    # Both hashes must appear so the user can compare.
    assert tampered_hash in msg
    actual = _sha256_hex(payload)
    assert actual in msg


def test_verify_zip_checksum_case_insensitive_comparison(tmp_path: Path) -> None:
    """Hash comparison must be case-insensitive (SHA256SUMS may use UPPER or lower)."""
    payload = b"case test"
    zip_path = _make_zip(tmp_path, payload)
    # Supply UPPERCASE expected hash in the sums file.
    expected_upper = _sha256_hex(payload).upper()
    sums_text = f"{expected_upper}  PlaudTools.zip\n"

    with _patch_opener(sums_text):
        # Must not raise despite case mismatch between upper expected and lower actual.
        verify_zip_checksum(zip_path, sums_url=SUMS_URL)


# ---------------------------------------------------------------------------
# verify_zip_checksum — absent SHA256SUMS (fail-closed, #113)
# ---------------------------------------------------------------------------


def test_verify_zip_checksum_absent_sums_url_raises(tmp_path: Path) -> None:
    """When sums_url is None, verify_zip_checksum must raise — fail-closed (#113).

    Every release from v0.3.0 onward publishes SHA256SUMS, so an absent asset
    means the download's integrity cannot be established and the install must be
    refused rather than proceeding unverified.
    """
    zip_path = _make_zip(tmp_path)

    with pytest.raises(ChecksumMismatch) as exc_info:
        verify_zip_checksum(zip_path, sums_url=None)

    msg = str(exc_info.value).lower()
    assert "sha256sums" in msg or "integrity" in msg


def test_verify_zip_checksum_absent_sums_url_does_not_call_network(tmp_path: Path) -> None:
    """When sums_url is None, the failure must be raised before any network call."""
    zip_path = _make_zip(tmp_path)

    with patch("plaud_tools.tray.updater._UPDATE_OPENER") as mock_opener:
        with pytest.raises(ChecksumMismatch):
            verify_zip_checksum(zip_path, sums_url=None)

    mock_opener.open.assert_not_called()


# ---------------------------------------------------------------------------
# verify_zip_checksum — SHA256SUMS parsing
# ---------------------------------------------------------------------------


def test_verify_zip_checksum_parses_two_space_format(tmp_path: Path) -> None:
    """Standard sha256sum format uses two spaces between hash and filename."""
    payload = b"two space format test"
    zip_path = _make_zip(tmp_path, payload)
    expected = _sha256_hex(payload)
    # Two-space format: "<hash>  <filename>"
    sums_text = f"{expected}  PlaudTools.zip\n"

    with _patch_opener(sums_text):
        verify_zip_checksum(zip_path, sums_url=SUMS_URL)  # Must not raise.


def test_verify_zip_checksum_uses_the_plaudtools_zip_line(tmp_path: Path) -> None:
    """The hash comes from the PlaudTools.zip line, wherever it sits in the file,
    not from the first token of the file."""
    payload = b"multi-entry sums"
    zip_path = _make_zip(tmp_path, payload)
    sums_text = f"{'a' * 64}  PlaudTools-symbols.zip\n{_sha256_hex(payload)}  PlaudTools.zip\n"

    with _patch_opener(sums_text):
        verify_zip_checksum(zip_path, sums_url=SUMS_URL)  # Must not raise.


def test_verify_zip_checksum_refuses_sums_without_plaudtools_zip_line(tmp_path: Path) -> None:
    payload = b"no matching line"
    zip_path = _make_zip(tmp_path, payload)
    sums_text = f"{_sha256_hex(payload)}  SomethingElse.zip\n"

    with _patch_opener(sums_text):
        with pytest.raises(ChecksumMismatch, match="does not list PlaudTools.zip"):
            verify_zip_checksum(zip_path, sums_url=SUMS_URL)


def test_verify_zip_checksum_refuses_untrusted_sums_host(tmp_path: Path) -> None:
    """The SHA256SUMS URL is held to the same allowlist as the zip."""
    zip_path = _make_zip(tmp_path)
    with patch("plaud_tools.tray.updater._UPDATE_OPENER") as mock_opener:
        with pytest.raises(ValueError, match="untrusted host"):
            verify_zip_checksum(zip_path, sums_url="https://evil.example/SHA256SUMS")
    mock_opener.open.assert_not_called()


def test_verify_zip_checksum_refuses_redirect_to_untrusted_final_host(tmp_path: Path) -> None:
    payload = b"redirected"
    zip_path = _make_zip(tmp_path, payload)
    sums_text = f"{_sha256_hex(payload)}  PlaudTools.zip\n"

    with _patch_opener(sums_text, final_url="https://evil.example/SHA256SUMS"):
        with pytest.raises(ValueError, match="untrusted host"):
            verify_zip_checksum(zip_path, sums_url=SUMS_URL)


# ---------------------------------------------------------------------------
# ChecksumMismatch — is a subclass of ValueError
# ---------------------------------------------------------------------------


def test_checksum_mismatch_is_value_error() -> None:
    """ChecksumMismatch must be a ValueError so it propagates as a domain error."""
    exc = ChecksumMismatch("test")
    assert isinstance(exc, ValueError)


# ---------------------------------------------------------------------------
# _check_for_update — now returns 4-tuple including sums_url
# ---------------------------------------------------------------------------


def test_check_for_update_returns_four_tuple_when_update_available() -> None:
    """_check_for_update() must return a 4-tuple when an update is available."""
    from plaud_tools.tray.updater import _check_for_update

    fake_release = {
        "tag_name": "v99.0.0",
        "html_url": "https://github.com/massive-value/plaud-tools/releases/tag/v99.0.0",
        "assets": [
            {"name": "PlaudTools.zip", "browser_download_url": "https://example.com/PlaudTools.zip"},
            {"name": "SHA256SUMS", "browser_download_url": "https://example.com/SHA256SUMS"},
        ],
    }

    import json

    mock_resp = MagicMock()
    mock_resp.read.return_value = json.dumps(fake_release).encode("utf-8")
    mock_resp.__enter__ = lambda s: s
    mock_resp.__exit__ = MagicMock(return_value=False)

    with patch("plaud_tools.tray.updater.urllib.request.urlopen", return_value=mock_resp):
        result = _check_for_update()

    assert result is not None
    assert len(result) == 4
    _version, _url, zip_url, sums_url = result
    assert zip_url == "https://example.com/PlaudTools.zip"
    assert sums_url == "https://example.com/SHA256SUMS"


def test_check_for_update_sums_url_is_none_when_asset_absent() -> None:
    """When SHA256SUMS is not in the release assets, sums_url must be None."""
    from plaud_tools.tray.updater import _check_for_update

    fake_release = {
        "tag_name": "v99.0.0",
        "html_url": "https://github.com/massive-value/plaud-tools/releases/tag/v99.0.0",
        "assets": [
            {"name": "PlaudTools.zip", "browser_download_url": "https://example.com/PlaudTools.zip"},
            # No SHA256SUMS asset — older release.
        ],
    }

    import json

    mock_resp = MagicMock()
    mock_resp.read.return_value = json.dumps(fake_release).encode("utf-8")
    mock_resp.__enter__ = lambda s: s
    mock_resp.__exit__ = MagicMock(return_value=False)

    with patch("plaud_tools.tray.updater.urllib.request.urlopen", return_value=mock_resp):
        result = _check_for_update()

    assert result is not None
    _version, _url, zip_url, sums_url = result
    assert zip_url == "https://example.com/PlaudTools.zip"
    assert sums_url is None
