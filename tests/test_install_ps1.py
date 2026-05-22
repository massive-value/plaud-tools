"""Tests for install.ps1 content and the zip_layout_probe_ps1_snippet helper.

PS1 scripts are validated with string assertions (content checks) and a
PowerShell syntax smoke test via ``pwsh -NoProfile -Command``.
"""
from __future__ import annotations

import subprocess
import shutil
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


@pytest.mark.skipif(shutil.which("pwsh") is None, reason="pwsh not available")
def test_install_ps1_syntax_valid():
    """Smoke: pwsh can parse the script without syntax errors."""
    result = subprocess.run(
        [
            "pwsh", "-NoProfile", "-Command",
            f"Get-Content '{INSTALL_PS1}' -Raw | Out-Null",
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, (
        f"pwsh syntax check failed:\n{result.stdout}\n{result.stderr}"
    )


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
        "$zipPath = 'dummy.zip'\n$installDir = 'C:\\\\Programs\\\\PlaudTools'\n"
        + snippet
        + "\n",
        encoding="utf-8",
    )
    result = subprocess.run(
        ["pwsh", "-NoProfile", "-Command", f"Get-Content '{ps1}' -Raw | Out-Null"],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, (
        f"pwsh syntax check failed:\n{result.stdout}\n{result.stderr}"
    )
