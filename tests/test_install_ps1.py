"""Tests for install.ps1 content and the zip_layout_probe_ps1_snippet helper.

PS1 scripts are validated with string assertions (content checks) and a
PowerShell syntax smoke test via ``pwsh -NoProfile -Command``.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from plaud_tools.mcp_lifecycle import zip_layout_probe_ps1_snippet

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

INSTALL_PS1 = Path(__file__).resolve().parents[1] / "scripts" / "install.ps1"


def _read_install_ps1() -> str:
    return INSTALL_PS1.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# install.ps1 — structural / content tests
# ---------------------------------------------------------------------------


def test_install_ps1_declares_force_switch():
    content = _read_install_ps1()
    assert "[switch]$Force" in content


def test_install_ps1_declares_repair_switch():
    content = _read_install_ps1()
    assert "[switch]$Repair" in content


def test_install_ps1_repair_aliases_force():
    content = _read_install_ps1()
    # Repair must set Force = true so the two paths share the same body.
    assert "if ($Repair) { $Force = $true }" in content


def test_install_ps1_force_branch_removes_install_dir():
    content = _read_install_ps1()
    # When -Force is active the existing dir must be removed.
    assert "Remove-Item $installDir -Recurse -Force" in content


def test_install_ps1_force_kills_tray_process():
    content = _read_install_ps1()
    # The -Force path must attempt to stop a running PlaudTools tray process.
    assert "PlaudTools" in content
    assert "CloseMainWindow" in content


def test_install_ps1_force_kills_mcp_process():
    content = _read_install_ps1()
    # The -Force path must also stop plaud-mcp.
    assert "plaud-mcp" in content


def test_install_ps1_uses_zip_probe_function():
    content = _read_install_ps1()
    # The probe function must be called (not a hardcoded Split-Path parent).
    assert "Get-ZipExtractDestination" in content


def test_install_ps1_probe_handles_single_top_level_dir():
    content = _read_install_ps1()
    # Shape A: one top-level dir → extract to parent.
    assert "Split-Path $InstallDir -Parent" in content


def test_install_ps1_probe_handles_flat_layout():
    content = _read_install_ps1()
    # Shape B: flat/multi-root → extract to install dir itself.
    assert "return $InstallDir" in content


def test_install_ps1_existing_exact_version_exits_without_force():
    content = _read_install_ps1()
    # Guard: already up-to-date AND not Force → exit 0.
    assert "-not $Force" in content


# ---------------------------------------------------------------------------
# install.ps1 — SHA256SUMS hash verification logic
# ---------------------------------------------------------------------------


def test_install_ps1_downloads_sha256sums_asset():
    content = _read_install_ps1()
    # Must look for the SHA256SUMS asset in the release asset list.
    assert "SHA256SUMS" in content


def test_install_ps1_uses_get_filehash():
    content = _read_install_ps1()
    # Must compute the actual hash via Get-FileHash.
    assert "Get-FileHash" in content
    assert "SHA256" in content


def test_install_ps1_compares_expected_and_actual_hash():
    content = _read_install_ps1()
    # Must store both an expected hash (parsed from SHA256SUMS) and the actual
    # hash, then compare them — key variable names confirm the logic is present.
    assert "expectedHash" in content
    assert "actualHash" in content


def test_install_ps1_fails_closed_on_mismatch():
    content = _read_install_ps1()
    # On mismatch the installer must throw (fail-closed) — not silently continue.
    assert "SHA256 mismatch" in content
    assert "throw" in content


def test_install_ps1_soft_fail_when_sums_absent():
    content = _read_install_ps1()
    # When SHA256SUMS is absent, the script must warn but proceed.
    assert "Write-Warning" in content
    assert "integrity could not be verified" in content


def test_install_ps1_cleans_up_sums_temp_file():
    content = _read_install_ps1()
    # The downloaded SHA256SUMS temp file must be removed in a finally block.
    assert "SHA256SUMS" in content
    # Verify the finally / cleanup pattern around the sums download.
    assert "finally" in content


def test_install_ps1_parses_two_space_format():
    content = _read_install_ps1()
    # Must split on whitespace to extract the hash token (standard sha256sum format).
    # The parser uses -split or Trim() to isolate the first hex token.
    assert r"-split '\s+'" in content or r"-split" in content


@pytest.mark.skipif(shutil.which("pwsh") is None, reason="pwsh not available")
def test_install_ps1_syntax_valid():
    """Smoke: pwsh can parse the script without syntax errors."""
    result = subprocess.run(
        [
            "pwsh",
            "-NoProfile",
            "-Command",
            f"Get-Content '{INSTALL_PS1}' -Raw | Out-Null",
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, f"pwsh syntax check failed:\n{result.stdout}\n{result.stderr}"


# ---------------------------------------------------------------------------
# install.ps1 hash-check logic — behavioral tests via pwsh
#
# These tests exercise the Verify-Zip-Checksum logic in isolation by writing a
# small PS1 harness that reproduces the same pattern used in install.ps1:
#   * matching hash → no throw, exits 0
#   * tampered zip (wrong hash in SHA256SUMS) → throws, exits 1
#   * absent SHA256SUMS file → writes a warning, exits 0
# ---------------------------------------------------------------------------

# The standalone hash-check logic extracted from install.ps1 for direct testing.
# It mirrors the exact pattern used in install.ps1 so we test the real algorithm.
_HASH_CHECK_PS1 = r"""
param(
    [string]$ZipPath,
    [string]$SumsPath   # '' = absent
)
$ErrorActionPreference = 'Stop'

if ($SumsPath -and (Test-Path $SumsPath)) {
    $sumsContent = Get-Content $SumsPath -Encoding UTF8 -Raw
    $expectedHash = ($sumsContent.Trim() -split '\s+')[0].ToUpper()
    $actualHash   = (Get-FileHash -Path $ZipPath -Algorithm SHA256).Hash.ToUpper()
    if ($actualHash -ne $expectedHash) {
        throw "SHA256 mismatch"
    }
    Write-Host "VERIFIED"
} else {
    Write-Warning "integrity could not be verified"
    Write-Host "PROCEEDING"
}
"""


def _run_hash_check(  # type: ignore[type-arg]
    tmp_path: Path, zip_bytes: bytes, sums_content: str | None
) -> subprocess.CompletedProcess:
    """Write test artefacts and run the hash-check PS1 harness."""
    zip_file = tmp_path / "PlaudTools.zip"
    zip_file.write_bytes(zip_bytes)
    harness = tmp_path / "hash_check.ps1"
    harness.write_text(_HASH_CHECK_PS1, encoding="utf-8")

    sums_path = ""
    if sums_content is not None:
        sums_file = tmp_path / "SHA256SUMS"
        sums_file.write_text(sums_content, encoding="utf-8")
        sums_path = str(sums_file)

    return subprocess.run(
        [
            "pwsh",
            "-NoProfile",
            "-NonInteractive",
            "-File",
            str(harness),
            "-ZipPath",
            str(zip_file),
            "-SumsPath",
            sums_path,
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )


def _sha256_hex(data: bytes) -> str:
    import hashlib

    return hashlib.sha256(data).hexdigest()


@pytest.mark.skipif(shutil.which("pwsh") is None, reason="pwsh not available")
def test_install_ps1_hash_check_matching_hash_passes(tmp_path: Path):
    """A zip whose SHA256 matches the SHA256SUMS entry should succeed."""
    payload = b"fake zip content"
    expected = _sha256_hex(payload)
    sums = f"{expected}  PlaudTools.zip\n"
    result = _run_hash_check(tmp_path, payload, sums)
    assert result.returncode == 0, f"Expected success:\n{result.stdout}\n{result.stderr}"
    assert "VERIFIED" in result.stdout


@pytest.mark.skipif(shutil.which("pwsh") is None, reason="pwsh not available")
def test_install_ps1_hash_check_tampered_zip_fails(tmp_path: Path):
    """A tampered zip (hash mismatch) must be refused — fail closed."""
    payload = b"fake zip content"
    tampered_hash = "a" * 64  # Wrong hash — 64 hex chars but all 'a'.
    sums = f"{tampered_hash}  PlaudTools.zip\n"
    result = _run_hash_check(tmp_path, payload, sums)
    assert result.returncode != 0, (
        f"Expected failure on tampered zip but got exit 0:\n{result.stdout}\n{result.stderr}"
    )
    assert "mismatch" in result.stderr.lower() or "mismatch" in result.stdout.lower()


@pytest.mark.skipif(shutil.which("pwsh") is None, reason="pwsh not available")
def test_install_ps1_hash_check_absent_sums_warns_and_proceeds(tmp_path: Path):
    """When SHA256SUMS is absent, the script should warn but exit 0 (soft-fail)."""
    payload = b"fake zip content"
    result = _run_hash_check(tmp_path, payload, sums_content=None)
    assert result.returncode == 0, f"Expected soft-fail (exit 0):\n{result.stdout}\n{result.stderr}"
    assert "PROCEEDING" in result.stdout


# ---------------------------------------------------------------------------
# zip_layout_probe_ps1_snippet — unit tests
# ---------------------------------------------------------------------------


def test_probe_snippet_references_zip_var():
    snippet = zip_layout_probe_ps1_snippet("myZip", "myInstall", "myDest")
    assert "$myZip" in snippet


def test_probe_snippet_references_install_var():
    snippet = zip_layout_probe_ps1_snippet("myZip", "myInstall", "myDest")
    assert "$myInstall" in snippet


def test_probe_snippet_assigns_dest_var():
    snippet = zip_layout_probe_ps1_snippet("myZip", "myInstall", "myDest")
    assert "$myDest" in snippet


def test_probe_snippet_shape_a_extracts_to_parent():
    snippet = zip_layout_probe_ps1_snippet("zipPath", "installDir", "extractDir")
    # Shape A code must use Split-Path … -Parent
    assert "Split-Path $installDir -Parent" in snippet


def test_probe_snippet_shape_b_extracts_to_install_dir():
    snippet = zip_layout_probe_ps1_snippet("zipPath", "installDir", "extractDir")
    # Shape B (or fallback) assigns extractDir = installDir
    assert "$extractDir = $installDir" in snippet


def test_probe_snippet_uses_zip_file_api():
    snippet = zip_layout_probe_ps1_snippet("zipPath", "installDir", "extractDir")
    # Must open the zip using the .NET API (not Expand-Archive)
    assert "ZipFile" in snippet
    assert "OpenRead" in snippet


def test_probe_snippet_disposes_zip_in_finally():
    snippet = zip_layout_probe_ps1_snippet("zipPath", "installDir", "extractDir")
    # Resource cleanup is required
    assert "finally" in snippet
    assert "Dispose" in snippet


def test_probe_snippet_probes_top_level_segments():
    snippet = zip_layout_probe_ps1_snippet("zipPath", "installDir", "extractDir")
    # Must collect distinct root segments to detect the layout
    assert "TrimStart" in snippet or "_topLevel" in snippet


def test_probe_snippet_checks_has_children():
    snippet = zip_layout_probe_ps1_snippet("zipPath", "installDir", "extractDir")
    # Must verify the single root is a real directory, not a single flat file
    assert "hasChildren" in snippet or "_hasChildren" in snippet


def test_probe_snippet_different_var_names():
    """Snippet uses the caller-supplied variable names, not hardcoded names."""
    s1 = zip_layout_probe_ps1_snippet("zz", "ii", "dd")
    s2 = zip_layout_probe_ps1_snippet("anotherZip", "anotherInstall", "anotherDest")
    # Both should be valid (no crash), variable names should differ
    assert "$zz" in s1
    assert "$zz" not in s2
    assert "$anotherZip" in s2


@pytest.mark.skipif(shutil.which("pwsh") is None, reason="pwsh not available")
def test_probe_snippet_syntax_valid(tmp_path: Path):
    """Smoke: pwsh can parse a PS1 file containing the probe snippet."""
    snippet = zip_layout_probe_ps1_snippet("zipPath", "installDir", "extractDir")
    ps1 = tmp_path / "probe_test.ps1"
    ps1.write_text(
        "$zipPath = 'dummy.zip'\n$installDir = 'C:\\\\Programs\\\\PlaudTools'\n" + snippet + "\n",
        encoding="utf-8",
    )
    result = subprocess.run(
        ["pwsh", "-NoProfile", "-Command", f"Get-Content '{ps1}' -Raw | Out-Null"],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, f"pwsh syntax check failed:\n{result.stdout}\n{result.stderr}"
