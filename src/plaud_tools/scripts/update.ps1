<#
.SYNOPSIS
    In-app update helper for Plaud Tools.

.DESCRIPTION
    Waits for the tray process to exit, shuts down scoped plaud-mcp processes,
    extracts the update zip, restarts the tray, and cleans up.

.PARAMETER TrayPid
    PID of the running PlaudTools.exe (tray app) to wait for.

.PARAMETER InstallDir
    Absolute path to the PlaudTools install directory (e.g. C:\Programs\PlaudTools).

.PARAMETER ZipPath
    Absolute path to the downloaded PlaudTools.zip update archive.

.PARAMETER ExtractDir
    Directory to extract the zip into (typically the parent of InstallDir so the
    top-level PlaudTools\ folder inside the zip lands correctly).

.PARAMETER SentinelPath
    Optional path to a sentinel file written by the tray before it exits that
    contains the new version string.  The script does not create this file; it
    is already written by the Python side before Popen is called.
#>
param(
    [Parameter(Mandatory)]
    [int]$TrayPid,

    [Parameter(Mandatory)]
    [string]$InstallDir,

    [Parameter(Mandatory)]
    [string]$ZipPath,

    [Parameter(Mandatory)]
    [string]$ExtractDir,

    [string]$SentinelPath = ""
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

# Extract the update archive.
$ProgressPreference = 'SilentlyContinue'
Expand-Archive -Path $ZipPath -DestinationPath $ExtractDir -Force
Remove-Item -Path $ZipPath -ErrorAction SilentlyContinue

# Restart the tray.
$trayExe = Join-Path $InstallDir 'PlaudTools.exe'
if (Test-Path $trayExe) {
    Start-Process $trayExe
}

# Self-destruct.
Remove-Item $MyInvocation.MyCommand.Path -ErrorAction SilentlyContinue
