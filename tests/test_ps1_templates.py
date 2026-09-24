"""Tests for ps1_templates — bundled PS1 script location and dispatcher rendering."""

from __future__ import annotations

import inspect
import shutil
import subprocess
import sys

import pytest

from plaud_tools.tray.ps1_templates import (
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


def test_update_ps1_swaps_via_staging_dir_not_overlay():
    """The update is extracted into a sibling staging dir and swapped in, so
    it never overlays the live install (no rollback, stale files kept)."""
    content = (scripts_dir() / "update.ps1").read_text(encoding="utf-8")
    assert '$stagingDir = "$liveDir.staging"' in content
    assert "Expand-Archive -Path $ZipPath -DestinationPath $stagingDir" in content
    assert "-DestinationPath $destination" not in content


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


def test_uninstall_ps1_accepts_log_dir_param():
    content = (scripts_dir() / "uninstall.ps1").read_text(encoding="utf-8")
    assert "[string]$LogDir" in content


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


def test_uninstall_ps1_deletes_dispatcher_not_bundled_script():
    """The %TEMP% dispatcher is what lingers; the bundled script goes away
    with the install dir. Deleting $MyInvocation (the bundled copy) left the
    dispatcher behind in %TEMP% on every uninstall."""
    content = (scripts_dir() / "uninstall.ps1").read_text(encoding="utf-8")
    assert "Remove-Item -LiteralPath $DispatcherPath" in content
    assert "Remove-Item $MyInvocation.MyCommand.Path" not in content


def test_uninstall_ps1_only_deletes_log_files_never_the_data_dir():
    content = (scripts_dir() / "uninstall.ps1").read_text(encoding="utf-8")
    assert "Remove-Item -Path $dir -Recurse" not in content
    assert "Remove-PlaudLogFiles -Dir $LogDir" in content


def test_uninstall_ps1_checks_for_plaudtools_exe_before_deleting():
    content = (scripts_dir() / "uninstall.ps1").read_text(encoding="utf-8")
    guard = content.index("Join-Path $InstallDir 'PlaudTools.exe'")
    assert guard < content.index("Remove-Item -LiteralPath $InstallDir -Recurse")


# ---------------------------------------------------------------------------
# uninstall.ps1 — respawn-retry kill loop (#156)
#
# A single kill pass followed by a fixed-attempt Remove-Item retry is not
# enough: if an MCP client (Claude Desktop) has plaud-mcp registered, killing
# it once just causes the client to relaunch it almost immediately, and the
# respawned exe re-locks the DLLs Remove-Item is about to delete -- leaving a
# partially-deleted install directory with Claude Desktop still running.
# ---------------------------------------------------------------------------


def test_uninstall_ps1_has_respawn_retry_kill_loop():
    content = (scripts_dir() / "uninstall.ps1").read_text(encoding="utf-8")
    assert "Stop-ScopedProcesses" in content
    assert "MaxAttempts" in content


def test_uninstall_ps1_retry_loop_reconfirms_stability_before_declaring_dead():
    """The loop must wait and re-check (not declare victory on the first
    "nothing found") so a process that respawns a beat later is still caught.
    """
    content = (scripts_dir() / "uninstall.ps1").read_text(encoding="utf-8")
    assert "StableMs" in content


@pytest.mark.skipif(shutil.which("pwsh") is None, reason="pwsh not available")
def test_uninstall_ps1_stop_scoped_processes_retries_against_respawn(tmp_path):
    """Behavioral: extract Stop-ScopedProcesses and prove it retries against a
    supervisor that relaunches the killed process, instead of giving up after
    one kill (the exact #156 scenario -- Claude Desktop respawning plaud-mcp).
    """
    content = (scripts_dir() / "uninstall.ps1").read_text(encoding="utf-8")
    start = content.index("function Stop-ScopedProcesses")
    end = content.index("\n}\n", start) + len("\n}\n")
    func_src = content[start:end]

    harness = tmp_path / "stop_scoped_harness.ps1"
    harness.write_text(
        func_src
        + r"""
$script:findCalls = 0
$script:killCount = 0
function Get-Process {
    param([string]$Name, [string]$ErrorAction)
    $script:findCalls++
    if ($script:findCalls -le 2) {
        # "Respawns" for the first two lookups, then stays dead.
        $script:killCount++
        $p = New-Object PSObject
        $p | Add-Member -MemberType NoteProperty -Name Path -Value 'C:\Fake\PlaudTools\plaud-mcp.exe'
        $p | Add-Member -MemberType ScriptMethod -Name CloseMainWindow -Value { $true }
        return @($p)
    }
    return @()
}
function Stop-Process { param([switch]$Force, [string]$ErrorAction) }
$result = Stop-ScopedProcesses -InstallDir 'C:\Fake\PlaudTools' -MaxAttempts 8 -StableMs 10
Write-Host "RESULT=$result KILLS=$script:killCount"
""",
        encoding="ascii",
    )
    result = subprocess.run(
        ["pwsh", "-NoProfile", "-NonInteractive", "-File", str(harness)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, f"harness failed:\n{result.stdout}\n{result.stderr}"
    assert "RESULT=True" in result.stdout
    assert "KILLS=2" in result.stdout, "must have retried the kill against the respawned process"


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


def test_render_uninstall_ps1_no_log_dir_omits_flag():
    result = render_uninstall_ps1(tray_pid=1, install_dir=r"C:\Programs\PlaudTools")
    assert "-LogDir" not in result
    assert "-DispatcherPath" not in result


def test_render_uninstall_ps1_includes_log_dir_and_dispatcher_when_provided():
    result = render_uninstall_ps1(
        tray_pid=1,
        install_dir=r"C:\Programs\PlaudTools",
        log_dir=r"C:\Users\foo\AppData\Local\PlaudTools",
        dispatcher_path=r"C:\Temp\plaud_uninstall_1.ps1",
    )
    assert r"-LogDir 'C:\Users\foo\AppData\Local\PlaudTools'" in result
    assert r"-DispatcherPath 'C:\Temp\plaud_uninstall_1.ps1'" in result


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


# ---------------------------------------------------------------------------
# Runtime dispatcher BOM (#153) — non-ASCII path, real PowerShell 5.1 parse
#
# updater.py and uninstaller.py write the rendered dispatcher string to a
# %TEMP% .ps1 file that Windows PowerShell 5.1 then executes. A BOM-less
# write is interpreted by PS 5.1 as the system ANSI codepage, not UTF-8 (the
# same v0.3.4 update.ps1 class); a non-ASCII byte in a path embedded in the
# dispatcher (e.g. a non-ASCII Windows username under %TEMP%) can misdecode
# and fail to parse. Both write call sites must use "utf-8-sig" so the BOM
# is present. These tests pin the source-level fix and, where a PS 5.1
# engine is available, prove the BOM'd file actually parses.
# ---------------------------------------------------------------------------


def test_updater_writes_dispatcher_with_bom():
    """Source guard: updater.py's dispatcher write must request utf-8-sig."""
    import plaud_tools.tray.updater as updater_mod

    src = inspect.getsource(updater_mod)
    assert 'ps_path.write_text(ps_content, encoding="utf-8-sig")' in src
    # No stray BOM-less write of the dispatcher content left behind.
    assert 'ps_path.write_text(ps_content, encoding="utf-8")' not in src


def test_uninstaller_writes_dispatcher_with_bom():
    """Source guard: uninstaller.py's dispatcher write must request utf-8-sig."""
    import plaud_tools.tray.uninstaller as uninstaller_mod

    src = inspect.getsource(uninstaller_mod)
    assert 'ps_path.write_text(ps_content, encoding="utf-8-sig")' in src
    assert 'ps_path.write_text(ps_content, encoding="utf-8")' not in src


@pytest.mark.parametrize(
    "render_fn,kwargs",
    [
        (
            render_update_ps1,
            dict(
                tray_pid=1,
                install_dir="C:\\Users\\Jérôme\\AppData\\Local\\Programs\\PlaudTools",
                zip_path="C:\\Users\\Jérôme\\AppData\\Local\\Temp\\plaud_update_1.zip",
                extract_dir="C:\\Users\\Jérôme\\AppData\\Local\\Programs",
            ),
        ),
        (
            render_uninstall_ps1,
            dict(
                tray_pid=1,
                install_dir="C:\\Users\\Jérôme\\AppData\\Local\\Programs\\PlaudTools",
            ),
        ),
    ],
)
def test_dispatcher_with_non_ascii_path_written_with_bom_decodes_correctly(tmp_path, render_fn, kwargs):
    """Reproduces #153 end-to-end for a non-ASCII Windows username in the path."""
    ps_content = render_fn(**kwargs)
    assert "Jérôme" in ps_content  # sanity: the non-ASCII path made it into the content

    ps_path = tmp_path / "dispatcher.ps1"
    ps_path.write_text(ps_content, encoding="utf-8-sig")

    raw = ps_path.read_bytes()
    assert raw.startswith(b"\xef\xbb\xbf"), "dispatcher must be written with a UTF-8 BOM"
    # Round-trips correctly for any UTF-8-aware reader (utf-8-sig strips the BOM).
    assert ps_path.read_text(encoding="utf-8-sig") == ps_content


@pytest.mark.skipif(
    sys.platform != "win32" or shutil.which("powershell") is None,
    reason="Windows PowerShell 5.1 not available",
)
@pytest.mark.parametrize(
    "render_fn,kwargs",
    [
        (
            render_update_ps1,
            dict(
                tray_pid=1,
                install_dir="C:\\Users\\Jérôme\\AppData\\Local\\Programs\\PlaudTools",
                zip_path="C:\\Users\\Jérôme\\AppData\\Local\\Temp\\plaud_update_1.zip",
                extract_dir="C:\\Users\\Jérôme\\AppData\\Local\\Programs",
            ),
        ),
        (
            render_uninstall_ps1,
            dict(
                tray_pid=1,
                install_dir="C:\\Users\\Jérôme\\AppData\\Local\\Programs\\PlaudTools",
            ),
        ),
    ],
)
def test_bom_dispatcher_parses_under_windows_powershell_51(tmp_path, render_fn, kwargs):
    """The exact regression this fix targets: a non-ASCII path in the BOM'd
    dispatcher must parse cleanly under real Windows PowerShell 5.1 (not just
    pwsh 7, which defaults to UTF-8 regardless of BOM).
    """
    ps_content = render_fn(**kwargs)
    ps_path = tmp_path / "dispatcher.ps1"
    ps_path.write_text(ps_content, encoding="utf-8-sig")

    result = subprocess.run(
        [
            "powershell",
            "-NoProfile",
            "-Command",
            f"$errs = $null; [void][System.Management.Automation.Language.Parser]::ParseFile("
            f"'{ps_path}', [ref]$null, [ref]$errs); if ($errs.Count -gt 0) {{ $errs | Out-String }}",
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, f"powershell invocation failed:\n{result.stdout}\n{result.stderr}"
    assert result.stdout.strip() == "", f"PS 5.1 parse errors:\n{result.stdout}"


# ---------------------------------------------------------------------------
# update.ps1 staged swap — behavioral, real Windows PowerShell 5.1 (what the
# tray launches). TEMP is redirected so sentinels never reach the real %TEMP%.
# ---------------------------------------------------------------------------

_BUNDLE_FILES = ("PlaudTools.exe", "mcp/plaud-mcp.exe", "cli/plaud-tools.exe")


def _make_install(root, version: str, extra: tuple[str, ...] = ()) -> None:
    for rel in (*_BUNDLE_FILES, *extra):
        f = root / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(version, encoding="ascii")


def _make_update_zip(path, version: str, files: tuple[str, ...] = _BUNDLE_FILES) -> None:
    import zipfile

    with zipfile.ZipFile(path, "w") as zf:
        for rel in files:
            zf.writestr(f"PlaudTools/{rel}", version)


def _run_update_ps1(tmp_path, install, zip_path):  # type: ignore[no-untyped-def]
    import os

    temp = tmp_path / "temp"
    temp.mkdir(exist_ok=True)
    exited = subprocess.Popen([sys.executable, "-c", "pass"])
    exited.wait()  # its PID stands in for a tray that has already quit
    env = {**os.environ, "TEMP": str(temp), "TMP": str(temp)}
    result = subprocess.run(
        [
            "powershell",
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(scripts_dir() / "update.ps1"),
            "-TrayPid",
            str(exited.pid),
            "-InstallDir",
            str(install),
            "-ZipPath",
            str(zip_path),
            "-ExtractDir",
            str(install.parent),
            "-NewVersion",
            "2.0.0",
        ],
        capture_output=True,
        text=True,
        timeout=120,
        env=env,
    )
    return result, temp


_needs_ps51 = pytest.mark.skipif(
    sys.platform != "win32" or shutil.which("powershell") is None,
    reason="Windows PowerShell 5.1 not available",
)


@_needs_ps51
def test_update_ps1_swaps_in_new_tree_and_drops_removed_files(tmp_path):
    install = tmp_path / "Programs" / "PlaudTools"
    _make_install(install, "1.0.0", extra=("_internal/old_dependency.dll", ".autostart_disabled"))
    zip_path = tmp_path / "plaud_update_1.zip"
    _make_update_zip(zip_path, "2.0.0")

    result, temp = _run_update_ps1(tmp_path, install, zip_path)

    assert result.returncode == 0, result.stdout + result.stderr
    assert (install / "PlaudTools.exe").read_text(encoding="ascii") == "2.0.0"
    assert not (install / "_internal" / "old_dependency.dll").exists()
    assert (install / ".autostart_disabled").exists(), "autostart opt-out must survive the update"
    assert not (tmp_path / "Programs" / "PlaudTools.old").exists()
    assert not (tmp_path / "Programs" / "PlaudTools.staging").exists()
    assert (temp / "plaud_just_updated.txt").read_bytes() == b"2.0.0"
    assert not (temp / "plaud_update_failed.txt").exists()


@_needs_ps51
def test_update_ps1_incomplete_package_leaves_live_install_untouched(tmp_path):
    install = tmp_path / "Programs" / "PlaudTools"
    _make_install(install, "1.0.0")
    zip_path = tmp_path / "plaud_update_1.zip"
    _make_update_zip(zip_path, "2.0.0", files=("PlaudTools.exe",))  # mcp/cli missing

    _result, temp = _run_update_ps1(tmp_path, install, zip_path)

    assert (install / "mcp" / "plaud-mcp.exe").read_text(encoding="ascii") == "1.0.0"
    assert (install / "PlaudTools.exe").read_text(encoding="ascii") == "1.0.0"
    assert not (tmp_path / "Programs" / "PlaudTools.staging").exists()
    assert "incomplete" in (temp / "plaud_update_failed.txt").read_text(encoding="utf-8")
    assert not (temp / "plaud_just_updated.txt").exists()


def _run_uninstall_ps1(tmp_path, install, log_dir, dispatcher):  # type: ignore[no-untyped-def]
    exited = subprocess.Popen([sys.executable, "-c", "pass"])
    exited.wait()
    return subprocess.run(
        [
            "powershell",
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(scripts_dir() / "uninstall.ps1"),
            "-TrayPid",
            str(exited.pid),
            "-InstallDir",
            str(install),
            "-LogDir",
            str(log_dir),
            "-DispatcherPath",
            str(dispatcher),
        ],
        capture_output=True,
        text=True,
        timeout=120,
    )


@_needs_ps51
def test_uninstall_ps1_deletes_install_and_logs_but_keeps_credentials(tmp_path):
    install = tmp_path / "Programs" / "PlaudTools"
    _make_install(install, "1.0.0")
    data = tmp_path / "PlaudTools"
    data.mkdir()
    for name in ("tray.log", "tray.log.1", "mcp.log", "session.json", "session.dat"):
        (data / name).write_text("x", encoding="ascii")
    dispatcher = tmp_path / "plaud_uninstall_1.ps1"
    dispatcher.write_text("# dispatcher", encoding="ascii")

    result = _run_uninstall_ps1(tmp_path, install, data, dispatcher)

    assert result.returncode == 0, result.stdout + result.stderr
    assert not install.exists()
    assert sorted(p.name for p in data.iterdir()) == ["session.dat", "session.json"]
    assert not dispatcher.exists()


@_needs_ps51
def test_uninstall_ps1_refuses_to_delete_a_folder_without_plaudtools_exe(tmp_path):
    not_ours = tmp_path / "Downloads"
    not_ours.mkdir()
    (not_ours / "important.docx").write_text("x", encoding="ascii")

    _run_uninstall_ps1(tmp_path, not_ours, tmp_path / "none", tmp_path / "none.ps1")

    assert (not_ours / "important.docx").exists()
