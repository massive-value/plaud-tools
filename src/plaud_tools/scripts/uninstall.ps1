<#
.SYNOPSIS
    Uninstall helper for Plaud Tools.

.DESCRIPTION
    Waits for the tray process to exit, shuts down scoped plaud-mcp processes,
    deletes the install directory, and optionally removes log directories.

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

# Shut down plaud-mcp processes scoped to the install directory.
$installDir = $InstallDir.TrimEnd('\').TrimEnd('/')
$mcpProcs = Get-Process -Name 'plaud-mcp' -ErrorAction SilentlyContinue | Where-Object {
    $_.Path -and $_.Path.ToLower().StartsWith($installDir.ToLower())
}
if ($mcpProcs) {
    foreach ($p in $mcpProcs) { $p.CloseMainWindow() | Out-Null }
    $deadline = (Get-Date).AddSeconds(3)
    while ($mcpProcs | Where-Object { !$_.HasExited }) {
        if ((Get-Date) -gt $deadline) { break }
        Start-Sleep -Milliseconds 100
    }
    $mcpProcs | Where-Object { !$_.HasExited } | Stop-Process -Force -ErrorAction SilentlyContinue
    # Poll until fully exited
    $exitDeadline = (Get-Date).AddSeconds(2)
    while (($mcpProcs | Where-Object { !$_.HasExited }) -and (Get-Date) -lt $exitDeadline) {
        Start-Sleep -Milliseconds 100
    }
}

# Delete the install directory.
Remove-Item -Path $InstallDir -Recurse -Force -ErrorAction SilentlyContinue

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
