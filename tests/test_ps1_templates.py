"""Tests for ps1_templates — bundled PS1 script location and dispatcher rendering."""

from __future__ import annotations

from plaud_tools.ps1_templates import (
    _ps_escape,
    render_uninstall_ps1,
    render_update_ps1,
    scripts_dir,
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


def test_update_ps1_does_not_self_delete():
    """update.ps1 must NOT delete itself.

    Earlier versions ended with ``Remove-Item $MyInvocation.MyCommand.Path``,
    which deleted the bundled update.ps1 from the install dir on every run.
    After the first successful in-app update the script was gone, so any
    subsequent in-app update silently failed because the dispatcher could not
    find update.ps1 to invoke. The %TEMP% dispatcher is cleaned up via the
    -DispatcherPath parameter instead.
    """
    content = (scripts_dir() / "update.ps1").read_text(encoding="utf-8")
    # Strip the comment-based historical reference so we only check actual code.
    code_only = "\n".join(
        line
        for line in content.splitlines()
        if not line.lstrip().startswith("#") and "$MyInvocation" not in line.split("#", 1)[-1]
    )
    assert "Remove-Item $MyInvocation.MyCommand.Path" not in code_only
    assert "Remove-Item $PSCommandPath" not in code_only


def test_update_ps1_has_transcript_logging():
    content = (scripts_dir() / "update.ps1").read_text(encoding="utf-8")
    assert "Start-Transcript" in content
    assert "plaud_update_" in content  # log filename pattern


def test_update_ps1_writes_failure_sentinel_on_error():
    content = (scripts_dir() / "update.ps1").read_text(encoding="utf-8")
    assert "plaud_update_failed.txt" in content
    assert "Write-FailureSentinel" in content


def test_update_ps1_clears_success_sentinel_on_failure():
    """A failed update must not leave plaud_just_updated.txt behind, otherwise
    the restarted old tray would falsely announce a successful upgrade.
    """
    content = (scripts_dir() / "update.ps1").read_text(encoding="utf-8")
    assert "plaud_just_updated.txt" in content


def test_update_ps1_retries_mcp_kill():
    """Kill loop must retry against external supervisors (Claude Desktop)
    that respawn plaud-mcp after Stop-Process.
    """
    content = (scripts_dir() / "update.ps1").read_text(encoding="utf-8")
    assert "Stop-PlaudMcpScoped" in content
    # MaxAttempts parameter must exist with a value > 1
    assert "MaxAttempts" in content


def test_update_ps1_restarts_tray_in_finally():
    """The tray must restart even when the update body throws, so the user
    is never stranded without a tray icon.
    """
    content = (scripts_dir() / "update.ps1").read_text(encoding="utf-8")
    assert "finally" in content
    # Restart call lives inside the finally block.
    finally_block = content.split("finally", 1)[1]
    assert "Start-Process" in finally_block
    assert "PlaudTools.exe" in finally_block


def test_update_ps1_accepts_dispatcher_path_param():
    content = (scripts_dir() / "update.ps1").read_text(encoding="utf-8")
    assert "DispatcherPath" in content


def test_update_ps1_accepts_new_version_param():
    content = (scripts_dir() / "update.ps1").read_text(encoding="utf-8")
    assert "NewVersion" in content


def test_update_ps1_is_ascii_only():
    """update.ps1 MUST be pure ASCII.

    The updater launches Windows PowerShell 5.1, which reads a BOM-less .ps1
    as the system ANSI codepage (Windows-1252), NOT UTF-8. A non-ASCII byte
    inside a string literal (e.g. an em-dash) misdecodes — the 0x94 trailing
    byte becomes a stray closing quote — and the whole script fails to parse,
    so update.ps1 silently never runs and the in-app update appears to do
    nothing. Keep this file 7-bit clean (issue #131).
    """
    raw = (scripts_dir() / "update.ps1").read_bytes()
    offenders = [(i, b) for i, b in enumerate(raw) if b > 0x7F]
    assert not offenders, f"non-ASCII bytes in update.ps1 at offsets {offenders[:5]}"


def test_uninstall_ps1_is_ascii_only():
    """uninstall.ps1 runs under the same Windows PowerShell 5.1 — keep it ASCII (issue #131)."""
    raw = (scripts_dir() / "uninstall.ps1").read_bytes()
    offenders = [(i, b) for i, b in enumerate(raw) if b > 0x7F]
    assert not offenders, f"non-ASCII bytes in uninstall.ps1 at offsets {offenders[:5]}"


def test_update_ps1_prunes_stale_dist_info():
    """Overlay extraction (Expand-Archive -Force) leaves the old version's
    plaud_tools-*.dist-info behind, so importlib.metadata resolves the OLD
    version. update.ps1 must prune the stale dist-info after extracting.
    """
    content = (scripts_dir() / "update.ps1").read_text(encoding="utf-8")
    assert "Remove-StaleDistInfo" in content
    assert "plaud_tools-*.dist-info" in content


def test_update_ps1_writes_success_sentinel_on_success():
    """The success sentinel must be written by update.ps1 AFTER a successful
    extraction (inside the try, before the catch), not pre-written by the tray.
    Otherwise a silently-failed update leaves the sentinel behind and the old
    tray falsely announces success.
    """
    content = (scripts_dir() / "update.ps1").read_text(encoding="utf-8")
    # Sentinel is written to $successSentinel, and must sit in the success path
    # — before the "Update succeeded" marker.
    assert "Write-NoBom -Path $successSentinel" in content
    assert content.index("Write-NoBom -Path $successSentinel") < content.index(
        'Write-Host "Update succeeded"'
    )


def test_update_ps1_writes_sentinels_without_bom():
    """Sentinels MUST be written BOM-less. Windows PowerShell 5.1's
    `Set-Content -Encoding UTF8` prepends a UTF-8 BOM (EF BB BF); the tray's
    version comparison and json.loads of the failure sentinel both break on a
    leading U+FEFF, so a successful update was falsely reported as failed.
    update.ps1 must route every sentinel write through Write-NoBom and never
    fall back to `Set-Content ... -Encoding UTF8` for them.
    """
    content = (scripts_dir() / "update.ps1").read_text(encoding="utf-8")
    assert "UTF8Encoding($false)" in content  # the BOM-less writer
    # No ACTIVE (non-comment) line may use `-Encoding UTF8` (PS 5.1 = BOM).
    # Comment lines are allowed to mention it for documentation.
    code_lines = [ln for ln in content.splitlines() if not ln.lstrip().startswith("#")]
    offenders = [ln for ln in code_lines if "-Encoding UTF8" in ln]
    assert not offenders, f"active line uses BOM-producing -Encoding UTF8: {offenders}"


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


def test_render_update_ps1_omits_dispatcher_path_when_not_provided():
    result = render_update_ps1(
        tray_pid=1,
        install_dir=r"C:\Programs\PlaudTools",
        zip_path=r"C:\Temp\update.zip",
        extract_dir=r"C:\Programs",
    )
    assert "-DispatcherPath" not in result


def test_render_update_ps1_includes_dispatcher_path_when_provided():
    result = render_update_ps1(
        tray_pid=1,
        install_dir=r"C:\Programs\PlaudTools",
        zip_path=r"C:\Temp\update.zip",
        extract_dir=r"C:\Programs",
        dispatcher_path=r"C:\Temp\plaud_update_1.ps1",
    )
    assert "-DispatcherPath" in result
    assert r"C:\Temp\plaud_update_1.ps1" in result


def test_render_update_ps1_escapes_single_quote_in_dispatcher_path():
    result = render_update_ps1(
        tray_pid=1,
        install_dir=r"C:\Programs\PlaudTools",
        zip_path=r"C:\Temp\update.zip",
        extract_dir=r"C:\Programs",
        dispatcher_path=r"C:\Temp\It's_dispatch.ps1",
    )
    assert "It''s_dispatch.ps1" in result


def test_render_update_ps1_omits_new_version_when_not_provided():
    result = render_update_ps1(
        tray_pid=1,
        install_dir=r"C:\Programs\PlaudTools",
        zip_path=r"C:\Temp\update.zip",
        extract_dir=r"C:\Programs",
    )
    assert "-NewVersion" not in result


def test_render_update_ps1_includes_new_version_when_provided():
    result = render_update_ps1(
        tray_pid=1,
        install_dir=r"C:\Programs\PlaudTools",
        zip_path=r"C:\Temp\update.zip",
        extract_dir=r"C:\Programs",
        new_version="0.3.3",
    )
    assert "-NewVersion '0.3.3'" in result


def test_render_update_ps1_escapes_single_quote_in_new_version():
    result = render_update_ps1(
        tray_pid=1,
        install_dir=r"C:\Programs\PlaudTools",
        zip_path=r"C:\Temp\update.zip",
        extract_dir=r"C:\Programs",
        new_version="1.0'rc",
    )
    assert "1.0''rc" in result


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
