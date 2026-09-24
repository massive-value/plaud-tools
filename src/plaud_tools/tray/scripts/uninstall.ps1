<#
.SYNOPSIS
    Uninstall helper for Plaud Tools.

.DESCRIPTION
    Waits for the tray process to exit, shuts down all processes running from
    the install directory (plaud-mcp, ffmpeg, etc.), deletes the install
    directory, and optionally deletes the tray/MCP log files.

.PARAMETER TrayPid
    PID of the running PlaudTools.exe (tray app) to wait for.

.PARAMETER InstallDir
    Absolute path to the PlaudTools install directory (e.g. C:\Programs\PlaudTools).
    Deleted after cleanup, but only if it contains PlaudTools.exe.

.PARAMETER LogDir
    Optional data directory (e.g. C:\Users\foo\AppData\Local\PlaudTools).
    Only tray.log* and mcp.log* inside it are deleted. The directory itself and
    the session files next to the logs (session.json, session.dat) are kept;
    the tray deletes those only when "Delete session / credentials" is checked.

.PARAMETER DispatcherPath
    Optional path of the %TEMP% dispatcher that invoked this script. Deleted at
    the end so %TEMP% does not accumulate stale .ps1 files.

.PARAMETER TrayExitTimeoutSec
    How long to wait for the tray to exit before giving up (default 60).
#>
param(
    [Parameter(Mandatory)]
    [int]$TrayPid,

    [Parameter(Mandatory)]
    [string]$InstallDir,

    [string]$LogDir = "",

    [string]$DispatcherPath = "",

    [int]$TrayExitTimeoutSec = 60
)

Set-StrictMode -Off
$ErrorActionPreference = 'Continue'

# ---------------------------------------------------------------------------
# Stop ALL processes whose Path is under $InstallDir (plaud-mcp, ffmpeg, any
# other child process), retrying against a supervisor that respawns them.
#
# A single kill pass is not enough: if Claude Desktop (or any other MCP
# client) has plaud-mcp registered, killing it once just causes the client to
# relaunch it almost immediately, and the respawned exe re-locks the very
# DLLs Remove-Item is about to delete -- a first kill followed by an
# unretried delete can therefore leave a partially-deleted install directory
# with Claude Desktop still running (#156). Mirrors update.ps1's
# Stop-PlaudMcpScoped, generalized to the install-dir scope this script uses.
# ---------------------------------------------------------------------------

function Stop-ScopedProcesses {
    param(
        [string]$InstallDir,
        [int]$MaxAttempts = 8,
        [int]$StableMs = 500
    )

    $scope = $InstallDir.TrimEnd('\').TrimEnd('/').ToLower() + '\'

    $findProcs = {
        Get-Process -ErrorAction SilentlyContinue | Where-Object {
            $_.Path -and $_.Path.ToLower().StartsWith($scope)
        }
    }

    for ($attempt = 1; $attempt -le $MaxAttempts; $attempt++) {
        $procs = & $findProcs
        if (-not $procs) {
            # Nothing alive - wait $StableMs to make sure nobody respawns it.
            Start-Sleep -Milliseconds $StableMs
            if (-not (& $findProcs)) {
                return $true
            }
            continue
        }

        foreach ($p in $procs) {
            try { $p.CloseMainWindow() | Out-Null } catch {}
        }
        Start-Sleep -Milliseconds 150
        $procs = & $findProcs
        if ($procs) {
            $procs | Stop-Process -Force -ErrorAction SilentlyContinue
        }
        Start-Sleep -Milliseconds 200
    }

    return $false
}

# ---------------------------------------------------------------------------
# Delete only the tray and MCP log files in $Dir. Keep this list in sync with
# _LOG_FILE_GLOBS in tray/uninstaller.py (a test enforces it). Never deletes
# the directory itself: session.json / session.dat live next to the logs.
# ---------------------------------------------------------------------------

function Remove-PlaudLogFiles {
    param([string]$Dir)
    if (-not $Dir -or -not (Test-Path -LiteralPath $Dir)) { return }
    foreach ($pattern in @('tray.log*', 'mcp.log*')) {
        Get-ChildItem -LiteralPath $Dir -Filter $pattern -File -ErrorAction SilentlyContinue |
            Remove-Item -Force -ErrorAction SilentlyContinue
    }
}

# Wait for the tray process to exit, but not forever: if it never exits the
# install dir stays locked, so give up instead of hanging in the background.
$deadline = (Get-Date).AddSeconds($TrayExitTimeoutSec)
while ((Get-Process -Id $TrayPid -ErrorAction SilentlyContinue) -and ((Get-Date) -lt $deadline)) {
    Start-Sleep -Seconds 1
}
$trayExited = -not (Get-Process -Id $TrayPid -ErrorAction SilentlyContinue)

if ($trayExited) {
    # Brief pause so Windows can release file handles on the PyInstaller bundle DLLs.
    Start-Sleep -Seconds 2

    # Shut down ALL processes running from the install directory (plaud-mcp,
    # ffmpeg, any future executables), retrying if a supervisor respawns them.
    Stop-ScopedProcesses -InstallDir $InstallDir | Out-Null

    # Only delete a directory that really is a PlaudTools install, so a wrong
    # InstallDir can never take an unrelated folder with it.
    $isPlaudInstall = $InstallDir -and (Test-Path -LiteralPath (Join-Path $InstallDir 'PlaudTools.exe'))
    if ($isPlaudInstall) {
        # Retry in case file handles are still held.
        $maxAttempts = 5
        for ($attempt = 1; $attempt -le $maxAttempts; $attempt++) {
            Remove-Item -LiteralPath $InstallDir -Recurse -Force -ErrorAction SilentlyContinue
            if (-not (Test-Path -LiteralPath $InstallDir)) { break }
            if ($attempt -lt $maxAttempts) { Start-Sleep -Seconds 2 }
        }
    }

    # The tray and plaud-mcp are gone now, so their logs are unlocked.
    Remove-PlaudLogFiles -Dir $LogDir
}

# Delete the %TEMP% dispatcher that launched us. The bundled uninstall.ps1
# goes away with the install directory.
if ($DispatcherPath -and (Test-Path -LiteralPath $DispatcherPath)) {
    Remove-Item -LiteralPath $DispatcherPath -Force -ErrorAction SilentlyContinue
}
