"""Tests for ps1_templates — bundled PS1 script location and dispatcher rendering."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

from plaud_tools.ps1_templates import (
    scripts_dir,
    render_update_ps1,
    render_uninstall_ps1,
    _ps_escape,
)


# ---------------------------------------------------------------------------
# scripts_dir — must resolve to a real directory containing the PS1 files
# ---------------------------------------------------------------------------

def test_scripts_dir_exists():
    d = scripts_dir()
    assert d.exists(), f"scripts_dir() returned non-existent path: {d}"


def test_scripts_dir_contains_update_ps1():
    d = scripts_dir()
    assert (d / "update.ps1").exists(), f"update.ps1 not found in {d}"


def test_scripts_dir_contains_uninstall_ps1():
    d = scripts_dir()
    assert (d / "uninstall.ps1").exists(), f"uninstall.ps1 not found in {d}"


# ---------------------------------------------------------------------------
# update.ps1 content — standalone script validation
# ---------------------------------------------------------------------------

def test_update_ps1_has_param_block():
    content = (scripts_dir() / "update.ps1").read_text(encoding="utf-8")
    assert "param(" in content.lower() or "param(" in content


def test_update_ps1_accepts_tray_pid_param():
    content = (scripts_dir() / "update.ps1").read_text(encoding="utf-8")
    assert "TrayPid" in content


def test_update_ps1_accepts_install_dir_param():
    content = (scripts_dir() / "update.ps1").read_text(encoding="utf-8")
    assert "InstallDir" in content


def test_update_ps1_accepts_zip_path_param():
    content = (scripts_dir() / "update.ps1").read_text(encoding="utf-8")
    assert "ZipPath" in content


def test_update_ps1_accepts_extract_dir_param():
    content = (scripts_dir() / "update.ps1").read_text(encoding="utf-8")
    assert "ExtractDir" in content


def test_update_ps1_waits_for_tray_pid():
    content = (scripts_dir() / "update.ps1").read_text(encoding="utf-8")
    assert "TrayPid" in content
    assert "Get-Process" in content


def test_update_ps1_uses_scoped_mcp_shutdown():
    content = (scripts_dir() / "update.ps1").read_text(encoding="utf-8")
    # Must use scoped shutdown (Where-Object / Path filter), not blanket kill
    assert "Where-Object" in content
    assert "Path" in content
    assert "plaud-mcp" in content.lower()


def test_update_ps1_no_blanket_stop_process():
    content = (scripts_dir() / "update.ps1").read_text(encoding="utf-8")
    assert "Stop-Process -Name plaud-mcp -Force\n" not in content


def test_update_ps1_graceful_shutdown_first():
    content = (scripts_dir() / "update.ps1").read_text(encoding="utf-8")
    assert "CloseMainWindow" in content


def test_update_ps1_expands_archive():
    content = (scripts_dir() / "update.ps1").read_text(encoding="utf-8")
    assert "Expand-Archive" in content


def test_update_ps1_starts_tray_after_update():
    content = (scripts_dir() / "update.ps1").read_text(encoding="utf-8")
    assert "Start-Process" in content
    assert "PlaudTools.exe" in content


def test_update_ps1_self_destructs():
    content = (scripts_dir() / "update.ps1").read_text(encoding="utf-8")
    assert "Remove-Item $MyInvocation.MyCommand.Path" in content


# ---------------------------------------------------------------------------
# uninstall.ps1 content — standalone script validation
# ---------------------------------------------------------------------------

def test_uninstall_ps1_has_param_block():
    content = (scripts_dir() / "uninstall.ps1").read_text(encoding="utf-8")
    assert "param(" in content.lower() or "param(" in content


def test_uninstall_ps1_accepts_tray_pid_param():
    content = (scripts_dir() / "uninstall.ps1").read_text(encoding="utf-8")
    assert "TrayPid" in content


def test_uninstall_ps1_accepts_install_dir_param():
    content = (scripts_dir() / "uninstall.ps1").read_text(encoding="utf-8")
    assert "InstallDir" in content


def test_uninstall_ps1_accepts_log_dirs_param():
    content = (scripts_dir() / "uninstall.ps1").read_text(encoding="utf-8")
    assert "LogDirs" in content


def test_uninstall_ps1_waits_for_tray_pid():
    content = (scripts_dir() / "uninstall.ps1").read_text(encoding="utf-8")
    assert "TrayPid" in content
    assert "Get-Process" in content


def test_uninstall_ps1_uses_scoped_mcp_shutdown():
    content = (scripts_dir() / "uninstall.ps1").read_text(encoding="utf-8")
    assert "Where-Object" in content
    assert "Path" in content
    assert "plaud-mcp" in content.lower()


def test_uninstall_ps1_no_blanket_stop_process():
    content = (scripts_dir() / "uninstall.ps1").read_text(encoding="utf-8")
    assert "Stop-Process -Name plaud-mcp -Force\n" not in content


def test_uninstall_ps1_graceful_shutdown_first():
    content = (scripts_dir() / "uninstall.ps1").read_text(encoding="utf-8")
    assert "CloseMainWindow" in content


def test_uninstall_ps1_deletes_install_dir():
    content = (scripts_dir() / "uninstall.ps1").read_text(encoding="utf-8")
    assert "Remove-Item" in content
    assert "InstallDir" in content


def test_uninstall_ps1_self_destructs():
    content = (scripts_dir() / "uninstall.ps1").read_text(encoding="utf-8")
    assert "Remove-Item $MyInvocation.MyCommand.Path" in content


# ---------------------------------------------------------------------------
# _ps_escape — single-quote safety
# ---------------------------------------------------------------------------

def test_ps_escape_doubles_single_quotes():
    assert _ps_escape("It's fine") == "It''s fine"


def test_ps_escape_no_change_when_no_quotes():
    assert _ps_escape(r"C:\Programs\PlaudTools") == r"C:\Programs\PlaudTools"


def test_ps_escape_multiple_single_quotes():
    assert _ps_escape("a'b'c") == "a''b''c"


# ---------------------------------------------------------------------------
# render_update_ps1 — dispatcher string content tests
# ---------------------------------------------------------------------------

def test_render_update_ps1_contains_tray_pid():
    result = render_update_ps1(
        tray_pid=12345,
        install_dir=r"C:\Programs\PlaudTools",
        zip_path=r"C:\Temp\plaud_update_12345.zip",
        extract_dir=r"C:\Programs",
    )
    assert "12345" in result


def test_render_update_ps1_contains_install_dir():
    result = render_update_ps1(
        tray_pid=1,
        install_dir=r"C:\Programs\PlaudTools",
        zip_path=r"C:\Temp\update.zip",
        extract_dir=r"C:\Programs",
    )
    assert r"C:\Programs\PlaudTools" in result


def test_render_update_ps1_contains_zip_path():
    result = render_update_ps1(
        tray_pid=1,
        install_dir=r"C:\Programs\PlaudTools",
        zip_path=r"C:\Temp\update.zip",
        extract_dir=r"C:\Programs",
    )
    assert r"C:\Temp\update.zip" in result


def test_render_update_ps1_contains_extract_dir():
    result = render_update_ps1(
        tray_pid=1,
        install_dir=r"C:\Programs\PlaudTools",
        zip_path=r"C:\Temp\update.zip",
        extract_dir=r"C:\Programs",
    )
    assert r"C:\Programs" in result


def test_render_update_ps1_invokes_update_script():
    result = render_update_ps1(
        tray_pid=1,
        install_dir=r"C:\Programs\PlaudTools",
        zip_path=r"C:\Temp\update.zip",
        extract_dir=r"C:\Programs",
    )
    assert "update.ps1" in result


def test_render_update_ps1_escapes_single_quotes_in_paths():
    result = render_update_ps1(
        tray_pid=1,
        install_dir=r"C:\Programs\It's PlaudTools",
        zip_path=r"C:\Temp\update.zip",
        extract_dir=r"C:\Programs",
    )
    # Single quote in path must be doubled for PS1 safety
    assert "It''s PlaudTools" in result


def test_render_update_ps1_uses_call_operator():
    result = render_update_ps1(
        tray_pid=1,
        install_dir=r"C:\Programs\PlaudTools",
        zip_path=r"C:\Temp\update.zip",
        extract_dir=r"C:\Programs",
    )
    # Must use & 'path' invocation style
    assert result.lstrip().startswith("&")


# ---------------------------------------------------------------------------
# render_uninstall_ps1 — dispatcher string content tests
# ---------------------------------------------------------------------------

def test_render_uninstall_ps1_contains_tray_pid():
    result = render_uninstall_ps1(
        tray_pid=99999,
        install_dir=r"C:\Programs\PlaudTools",
    )
    assert "99999" in result


def test_render_uninstall_ps1_contains_install_dir():
    result = render_uninstall_ps1(
        tray_pid=1,
        install_dir=r"C:\Programs\PlaudTools",
    )
    assert r"C:\Programs\PlaudTools" in result


def test_render_uninstall_ps1_invokes_uninstall_script():
    result = render_uninstall_ps1(
        tray_pid=1,
        install_dir=r"C:\Programs\PlaudTools",
    )
    assert "uninstall.ps1" in result


def test_render_uninstall_ps1_no_log_dirs_omits_flag():
    result = render_uninstall_ps1(
        tray_pid=1,
        install_dir=r"C:\Programs\PlaudTools",
        log_dirs=None,
    )
    assert "-LogDirs" not in result


def test_render_uninstall_ps1_includes_log_dirs_when_provided():
    result = render_uninstall_ps1(
        tray_pid=1,
        install_dir=r"C:\Programs\PlaudTools",
        log_dirs=[r"C:\Users\foo\AppData\Local\PlaudTools"],
    )
    assert "-LogDirs" in result
    assert "PlaudTools" in result


def test_render_uninstall_ps1_multiple_log_dirs_joined_by_semicolon():
    result = render_uninstall_ps1(
        tray_pid=1,
        install_dir=r"C:\Programs\PlaudTools",
        log_dirs=[
            r"C:\Users\foo\AppData\Local\PlaudTools",
            r"C:\Users\foo\AppData\Local\Plaud",
        ],
    )
    assert "PlaudTools;C:" in result or "PlaudTools;" in result


def test_render_uninstall_ps1_escapes_single_quotes():
    result = render_uninstall_ps1(
        tray_pid=1,
        install_dir=r"C:\It's PlaudTools",
    )
    assert "It''s PlaudTools" in result


def test_render_uninstall_ps1_uses_call_operator():
    result = render_uninstall_ps1(
        tray_pid=1,
        install_dir=r"C:\Programs\PlaudTools",
    )
    assert result.lstrip().startswith("&")
