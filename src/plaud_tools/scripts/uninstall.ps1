<#
.SYNOPSIS
    Uninstall helper for Plaud Tools.

.DESCRIPTION
    Waits for the tray process to exit, shuts down all processes running from
    the install directory (plaud-mcp, ffmpeg, etc.), deletes the install
    directory, and optionally removes log directories.

.PARAMETER TrayPid
    PID of the running PlaudTools.exe (tray app) to wait for.

.PARAMETER InstallDir
    Absolute path to the PlaudTools install directory (e.g. C:\Programs\PlaudTools).
    This directory is deleted after cleanup.

.PARAMETER LogDirs
    Optional semicolon-separated list of log directories to delete.
    Example: "C:\Users\foo\AppData\Local\PlaudTools;C:\Users\foo\AppData\Local\Plaud"
#>
param(
    [Parameter(Mandatory)]
    [int]$TrayPid,

    [Parameter(Mandatory)]
    [string]$InstallDir,

    [string]$LogDirs = ""
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

# Wait for the tray process to exit.
while (Get-Process -Id $TrayPid -ErrorAction SilentlyContinue) {
    Start-Sleep -Seconds 1
}
# Brief pause so Windows can release file handles on the PyInstaller bundle DLLs.
Start-Sleep -Seconds 2

# Shut down ALL processes running from the install directory (plaud-mcp, ffmpeg,
# any future executables), retrying if a supervisor respawns them.
Stop-ScopedProcesses -InstallDir $InstallDir | Out-Null

# Delete the install directory with retries in case file handles are still held.
$maxAttempts = 5
for ($attempt = 1; $attempt -le $maxAttempts; $attempt++) {
    Remove-Item -Path $InstallDir -Recurse -Force -ErrorAction SilentlyContinue
    if (-not (Test-Path $InstallDir)) { break }
    if ($attempt -lt $maxAttempts) { Start-Sleep -Seconds 2 }
}

# Optionally delete log directories.
if ($LogDirs -ne "") {
    foreach ($dir in ($LogDirs -split ';')) {
        $dir = $dir.Trim()
        if ($dir -ne "") {
            Remove-Item -Path $dir -Recurse -Force -ErrorAction SilentlyContinue
        }
    }
}

# Self-destruct.
Remove-Item $MyInvocation.MyCommand.Path -ErrorAction SilentlyContinue
