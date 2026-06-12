"""Unit tests for the update-download host allowlist in plaud_tools.tray.updater.

Task D6 requires that _check_download_host() accepts only github.com and
objects.githubusercontent.com; all other hosts must be refused.

All tests are pure Python (no network, no subprocess, no tkinter).
"""

from __future__ import annotations

import pytest

from plaud_tools.tray.updater import (
    _ALLOWED_UPDATE_HOSTS,
    _check_download_host,
)

# ---------------------------------------------------------------------------
# _check_download_host — allowed hosts (must pass without error)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        # Standard GitHub release asset
        "https://github.com/massive-value/plaud-tools/releases/download/v1.0.0/PlaudTools.zip",
        # GitHub raw objects CDN (browser_download_url origin)
        "https://objects.githubusercontent.com/github-production-release-asset-2e65be/123/PlaudTools.zip",
        # With explicit port — parsed hostname strips port, so still allowed
        "https://github.com:443/massive-value/plaud-tools/releases/download/v1.0.0/PlaudTools.zip",
    ],
)
def test_check_download_host_allows_good_url(url: str) -> None:
    """Good GitHub URLs must not raise."""
    _check_download_host(url)  # Must not raise.


# ---------------------------------------------------------------------------
# _check_download_host — disallowed hosts (must raise ValueError)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        # Completely unrelated host
        "https://evil.com/PlaudTools.zip",
        # Subdomain confusion: host ENDS with github.com but is not github.com
        "https://github.com.evil.com/PlaudTools.zip",
        # Prefix confusion: host STARTS with github.com but is not github.com
        "https://github.com.fake/PlaudTools.zip",
        # Merely contains "github.com" as a substring
        "https://notgithub.com/PlaudTools.zip",
        # HTTP (not HTTPS) from an untrusted host
        "http://evil.com/PlaudTools.zip",
        # Lookalike unicode domain (punycode would differ but test the ASCII form)
        "https://g1thub.com/PlaudTools.zip",
        # githubusercontent misspelled (missing "objects." prefix)
        "https://githubusercontent.com/PlaudTools.zip",
        # githubusercontent with an extra subdomain (not the exact allowed host)
        "https://cdn.objects.githubusercontent.com/PlaudTools.zip",
    ],
)
def test_check_download_host_refuses_bad_url(url: str) -> None:
    """Evil or unrecognised hosts must raise ValueError."""
    with pytest.raises(ValueError, match="untrusted host"):
        _check_download_host(url)


# ---------------------------------------------------------------------------
# _check_download_host — error message quality
# ---------------------------------------------------------------------------


def test_check_download_host_error_message_names_host() -> None:
    """The ValueError message must include the offending host for diagnostics."""
    url = "https://evil.com/PlaudTools.zip"
    with pytest.raises(ValueError) as exc_info:
        _check_download_host(url)
    assert "evil.com" in str(exc_info.value)


def test_check_download_host_error_message_lists_allowed_hosts() -> None:
    """The ValueError message must list allowed hosts so operators know what to expect."""
    url = "https://evil.com/PlaudTools.zip"
    with pytest.raises(ValueError) as exc_info:
        _check_download_host(url)
    msg = str(exc_info.value)
    assert "github.com" in msg


# ---------------------------------------------------------------------------
# _ALLOWED_UPDATE_HOSTS — allowlist membership
# ---------------------------------------------------------------------------


def test_allowed_update_hosts_contains_github_com() -> None:
    assert "github.com" in _ALLOWED_UPDATE_HOSTS


def test_allowed_update_hosts_contains_objects_githubusercontent_com() -> None:
    assert "objects.githubusercontent.com" in _ALLOWED_UPDATE_HOSTS


def test_allowed_update_hosts_does_not_contain_evil_variants() -> None:
    """Sanity-check: evil lookalikes are not accidentally in the allowlist."""
    assert "github.com.evil.com" not in _ALLOWED_UPDATE_HOSTS
    assert "notgithub.com" not in _ALLOWED_UPDATE_HOSTS
    assert "evil.com" not in _ALLOWED_UPDATE_HOSTS
