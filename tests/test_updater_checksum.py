"""Unit tests for verify_zip_checksum in plaud_tools.tray.updater.

These tests cover the three rollout scenarios for Wave 2 / C1 (hash verification):
  - Matching hash → passes silently.
  - Tampered zip (wrong hash in SHA256SUMS) → raises ChecksumMismatch (fail-closed).
  - Absent SHA256SUMS URL (None) → warns and proceeds (soft-fail for older releases).

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


def _mock_urlopen(sums_text: str):
    """Return a context-manager mock that yields a response with *sums_text*."""
    resp = MagicMock()
    resp.read.return_value = sums_text.encode("utf-8")
    resp.__enter__ = lambda s: s
    resp.__exit__ = MagicMock(return_value=False)
    return resp


# ---------------------------------------------------------------------------
# verify_zip_checksum — matching hash
# ---------------------------------------------------------------------------


def test_verify_zip_checksum_matching_hash_passes(tmp_path: Path) -> None:
    """When the zip hash matches SHA256SUMS, verify_zip_checksum returns None (no error)."""
    payload = b"genuine zip content"
    zip_path = _make_zip(tmp_path, payload)
    expected = _sha256_hex(payload)
    sums_text = f"{expected}  PlaudTools.zip\n"

    with patch("plaud_tools.tray.updater.urllib.request.urlopen", return_value=_mock_urlopen(sums_text)):
        # Must not raise.
        result = verify_zip_checksum(zip_path, sums_url="https://example.com/SHA256SUMS")

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

    with patch("plaud_tools.tray.updater.urllib.request.urlopen", return_value=_mock_urlopen(sums_text)):
        with pytest.raises(ChecksumMismatch) as exc_info:
            verify_zip_checksum(zip_path, sums_url="https://example.com/SHA256SUMS")

    msg = str(exc_info.value).lower()
    assert "mismatch" in msg


def test_verify_zip_checksum_mismatch_message_contains_expected_and_actual(tmp_path: Path) -> None:
    """The ChecksumMismatch message must include both expected and actual hashes for diagnostics."""
    payload = b"genuine zip content"
    zip_path = _make_zip(tmp_path, payload)
    tampered_hash = "b" * 64
    sums_text = f"{tampered_hash}  PlaudTools.zip\n"

    with patch("plaud_tools.tray.updater.urllib.request.urlopen", return_value=_mock_urlopen(sums_text)):
        with pytest.raises(ChecksumMismatch) as exc_info:
            verify_zip_checksum(zip_path, sums_url="https://example.com/SHA256SUMS")

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

    with patch("plaud_tools.tray.updater.urllib.request.urlopen", return_value=_mock_urlopen(sums_text)):
        # Must not raise despite case mismatch between upper expected and lower actual.
        verify_zip_checksum(zip_path, sums_url="https://example.com/SHA256SUMS")


# ---------------------------------------------------------------------------
# verify_zip_checksum — absent SHA256SUMS (soft-fail)
# ---------------------------------------------------------------------------


def test_verify_zip_checksum_absent_sums_url_warns_and_proceeds(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """When sums_url is None (pre-A3 release), verify_zip_checksum must warn and return normally.

    This is the soft-fail branch that allows older releases (without a SHA256SUMS
    asset) to continue installing.  It must be removed two releases after SHA256SUMS
    ships to all supported release branches.
    """
    zip_path = _make_zip(tmp_path)

    import logging

    with caplog.at_level(logging.WARNING, logger="plaud_tools.tray.updater"):
        result = verify_zip_checksum(zip_path, sums_url=None)

    assert result is None  # Must not raise.
    # A warning must be emitted so operators can see the soft-fail in logs.
    assert any("SHA256SUMS" in r.message or "integrity" in r.message for r in caplog.records)


def test_verify_zip_checksum_absent_sums_url_does_not_call_network(tmp_path: Path) -> None:
    """When sums_url is None, no network call must be made."""
    zip_path = _make_zip(tmp_path)

    with patch("plaud_tools.tray.updater.urllib.request.urlopen") as mock_urlopen:
        verify_zip_checksum(zip_path, sums_url=None)

    mock_urlopen.assert_not_called()


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

    with patch("plaud_tools.tray.updater.urllib.request.urlopen", return_value=_mock_urlopen(sums_text)):
        verify_zip_checksum(zip_path, sums_url="https://example.com/SHA256SUMS")  # Must not raise.


def test_verify_zip_checksum_ignores_filename_column(tmp_path: Path) -> None:
    """Only the first whitespace-delimited token (the hash) is used; filename is ignored."""
    payload = b"filename ignored test"
    zip_path = _make_zip(tmp_path, payload)
    expected = _sha256_hex(payload)
    # Different filename column — must still pass.
    sums_text = f"{expected}  SomethingElse.zip\n"

    with patch("plaud_tools.tray.updater.urllib.request.urlopen", return_value=_mock_urlopen(sums_text)):
        verify_zip_checksum(zip_path, sums_url="https://example.com/SHA256SUMS")  # Must not raise.


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
