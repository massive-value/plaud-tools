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

# Wait for the tray process to exit.
while (Get-Process -Id $TrayPid -ErrorAction SilentlyContinue) {
    Start-Sleep -Seconds 1
}
# Brief pause so Windows can release file handles on the PyInstaller bundle DLLs.
Start-Sleep -Seconds 2

# Shut down ALL processes running from the install directory (plaud-mcp, ffmpeg,
# any future executables).  Filtering by path rather than by name means we
# don't miss child processes spawned by plaud-mcp (e.g. ffmpeg for audio work).
$installDir = $InstallDir.TrimEnd('\').TrimEnd('/')
$scopedProcs = Get-Process -ErrorAction SilentlyContinue | Where-Object {
    $_.Path -and $_.Path.ToLower().StartsWith($installDir.ToLower() + '\')
}
if ($scopedProcs) {
    foreach ($p in $scopedProcs) { $p.CloseMainWindow() | Out-Null }
    $deadline = (Get-Date).AddSeconds(5)
    while (($scopedProcs | Where-Object { !$_.HasExited }) -and (Get-Date) -lt $deadline) {
        Start-Sleep -Milliseconds 200
    }
    $scopedProcs | Where-Object { !$_.HasExited } | Stop-Process -Force -ErrorAction SilentlyContinue
    # Poll until all handles are fully released.
    $exitDeadline = (Get-Date).AddSeconds(3)
    while (($scopedProcs | Where-Object { !$_.HasExited }) -and (Get-Date) -lt $exitDeadline) {
        Start-Sleep -Milliseconds 200
    }
}

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
