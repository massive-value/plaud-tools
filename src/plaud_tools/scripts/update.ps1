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

# Probe the zip and return the correct extraction destination.
#
# Known shapes:
#   A) Single top-level directory (e.g. PlaudTools\...): extract to parent of
#      $InstallDir so files land at Programs\PlaudTools\ not Programs\PlaudTools\PlaudTools\.
#   B) Files at root of zip (flat layout): extract directly to $InstallDir.
#
# Returns the extraction destination path as a string.
function Get-ZipExtractDestination {
    param([string]$ZipPath, [string]$InstallDir)

    Add-Type -AssemblyName System.IO.Compression.FileSystem
    $zip = [System.IO.Compression.ZipFile]::OpenRead($ZipPath)
    try {
        # Collect distinct top-level names (first path segment of every non-empty entry).
        $topLevel = @{}
        foreach ($entry in $zip.Entries) {
            $name = $entry.FullName.TrimStart('/', '\')
            if (-not $name) { continue }
            $seg = ($name -split '[/\\]')[0]
            if ($seg) { $topLevel[$seg] = 1 }
        }

        $roots = @($topLevel.Keys)

        if ($roots.Count -eq 1) {
            # Shape A: single top-level folder.  Verify it is a directory (has children).
            $prefix = $roots[0] + '/'
            $hasChildren = $zip.Entries | Where-Object { $_.FullName -ne $prefix -and $_.FullName.StartsWith($prefix) }
            if ($hasChildren) {
                # Extract to parent — the folder inside the zip becomes $InstallDir.
                return (Split-Path $InstallDir -Parent)
            }
        }

        # Shape B (flat, or unknown multi-root): extract directly into $InstallDir.
        return $InstallDir
    } finally {
        $zip.Dispose()
    }
}

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

# Probe the zip layout so we extract to the right destination regardless of
# whether the zip has a top-level PlaudTools\ folder (shape A) or ships
# files at the root (shape B).  This overrides the $ExtractDir passed by the
# caller so the in-app update path is as robust as the install.ps1 path.
$ExtractDir = Get-ZipExtractDestination -ZipPath $ZipPath -InstallDir $InstallDir

# Extract the update archive.
if (-not (Test-Path $ExtractDir)) {
    New-Item -ItemType Directory -Path $ExtractDir | Out-Null
}
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
