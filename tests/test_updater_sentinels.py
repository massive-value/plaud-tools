"""Update sentinels written by update.ps1 under Windows PowerShell 5.1 (v0.3.4, v0.4.1).

PS 5.1 ``Set-Content -Encoding UTF8`` prepends a UTF-8 BOM.  Before v0.4.1 the
tray read the success sentinel with plain utf-8, so every successful update
looked like a version mismatch, and the BOM made ``json.loads`` reject the
failure sentinel, hiding genuine failures.
"""

from __future__ import annotations

import json

import pytest

from plaud_tools.tray.updater import read_update_failure, read_update_success

BOM = "\ufeff"


def _write(path, text: str, *, bom: bool) -> None:
    path.write_bytes(((BOM if bom else "") + text).encode("utf-8"))


@pytest.mark.parametrize("bom", [True, False])
def test_success_sentinel_matches_with_or_without_bom(tmp_path, bom):
    sentinel = tmp_path / "plaud_just_updated.txt"
    _write(sentinel, "0.4.0\r\n", bom=bom)

    assert read_update_success(sentinel, "0.4.0") == ("0.4.0", True)
    assert not sentinel.exists()


def test_success_sentinel_for_other_version_is_not_a_match(tmp_path):
    """A silently failed update leaves the old bits running; no success banner."""
    sentinel = tmp_path / "plaud_just_updated.txt"
    _write(sentinel, "0.4.0", bom=True)

    assert read_update_success(sentinel, "0.3.9") == ("0.4.0", False)


@pytest.mark.parametrize("bom", [True, False])
def test_failure_sentinel_parses_with_or_without_bom(tmp_path, bom):
    sentinel = tmp_path / "plaud_update_failed.txt"
    _write(sentinel, json.dumps({"reason": "swap failed", "log": "C:/logs/update.log"}), bom=bom)

    assert read_update_failure(sentinel) == ("swap failed", "C:/logs/update.log")
    assert not sentinel.exists()


def test_failure_sentinel_without_reason_gets_a_default(tmp_path):
    sentinel = tmp_path / "plaud_update_failed.txt"
    _write(sentinel, "{}", bom=True)

    reason, log_path = read_update_failure(sentinel)
    assert "unknown reason" in reason
    assert log_path == ""
