# Install plaud-tools (the unofficial tray bundle).
#
# Usage:
#   irm https://raw.githubusercontent.com/massive-value/plaud-tools/main/scripts/install.ps1 | iex
#
# Options:
#   -Force   — remove any existing install (after shutting down tray + MCP) and reinstall.
#   -Repair  — alias for -Force; use when files are missing or quarantined.
#
# Example:
#   irm .../install.ps1 | iex                      # normal install
#   & ([scriptblock]::Create((irm .../install.ps1))) -Force    # wipe + reinstall
#   & ([scriptblock]::Create((irm .../install.ps1))) -Repair   # repair broken install

[CmdletBinding()]
param(
    [switch]$Force,
    [switch]$Repair
)

$ErrorActionPreference = 'Stop'

# -Repair is a user-friendly alias for -Force.
if ($Repair) { $Force = $true }

function Expand-ArchiveWithProgress {
    param([string]$Path, [string]$DestinationPath)

    Add-Type -AssemblyName System.IO.Compression.FileSystem
    $zip     = [System.IO.Compression.ZipFile]::OpenRead($Path)
    $total   = $zip.Entries.Count
    $done    = 0

    try {
        foreach ($entry in $zip.Entries) {
            $dest = Join-Path $DestinationPath $entry.FullName
            if ($entry.FullName.EndsWith('/') -or $entry.FullName.EndsWith('\')) {
                if (-not (Test-Path $dest)) { New-Item -ItemType Directory -Path $dest | Out-Null }
            } else {
                $dir = Split-Path $dest -Parent
                if (-not (Test-Path $dir)) { New-Item -ItemType Directory -Path $dir | Out-Null }
                [System.IO.Compression.ZipFileExtensions]::ExtractToFile($entry, $dest, $true)
            }
            $done++
            $pct    = [int]($done * 100 / $total)
            $filled = [int]($pct * 30 / 100)
            $bar    = ('=' * $filled).PadRight(30, '-')
            [Console]::Write("`r    [$bar] $pct%  $done / $total files  ")
        }
    } finally {
        $zip.Dispose()
    }
    [Console]::WriteLine()
}

function Get-FileWithProgress {
    param([string]$Uri, [string]$OutFile)

    $req = [System.Net.HttpWebRequest]::Create($Uri)
    $req.UserAgent = 'PlaudTools-Installer/1.0'
    $resp   = $req.GetResponse()
    $total  = $resp.ContentLength
    $stream = $resp.GetResponseStream()
    $out    = [System.IO.File]::Create($OutFile)
    $buf    = New-Object byte[] 65536
    $done   = 0L

    try {
        while (($n = $stream.Read($buf, 0, $buf.Length)) -gt 0) {
            $out.Write($buf, 0, $n)
            $done += $n
            $dlMb = [math]::Round($done / 1MB, 1)
            if ($total -gt 0) {
                $pct     = [int]($done * 100 / $total)
                $totalMb = [math]::Round($total / 1MB, 1)
                $filled  = [int]($pct * 30 / 100)
                $bar     = ('=' * $filled).PadRight(30, '-')
                [Console]::Write("`r    [$bar] $pct%  $dlMb / $totalMb MB  ")
            } else {
                [Console]::Write("`r    $dlMb MB downloaded  ")
            }
        }
    } finally {
        $out.Close()
        $stream.Close()
        $resp.Close()
    }
    [Console]::WriteLine()
}

# Probe the zip and return the correct extraction destination.
#
# Known shapes:
#   A) Single top-level directory (e.g. PlaudTools\...): extract to parent of
#      $installDir so files land at Programs\PlaudTools\ not Programs\PlaudTools\PlaudTools\.
#   B) Files at root of zip (flat layout): extract directly to $installDir.
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

try {
    $installDir = Join-Path $env:LOCALAPPDATA 'Programs\PlaudTools'
    $exePath    = Join-Path $installDir 'PlaudTools.exe'
    $zipTemp    = Join-Path $env:TEMP 'PlaudTools.zip'

    # --- Step 1: resolve the latest release (needed for version checks too) ---
    Write-Host '[1/4] Fetching latest release info...'
    $release       = Invoke-RestMethod -Uri 'https://api.github.com/repos/massive-value/plaud-tools/releases/latest' -UseBasicParsing
    $asset         = $release.assets | Where-Object { $_.name -eq 'PlaudTools.zip' } | Select-Object -First 1
    $latestVersion = $release.tag_name.TrimStart('v')

    if (-not $asset) {
        throw "Could not find PlaudTools.zip in the latest release assets. Check https://github.com/massive-value/plaud-tools/releases/latest"
    }

    Write-Host "    Latest: v$latestVersion — PlaudTools.zip ($([math]::Round($asset.size / 1MB, 1)) MB)"

    # --- Guard: handle existing installs ---
    if (Test-Path $exePath) {
        $installedVersion = (Get-Item $exePath).VersionInfo.FileVersion.Trim()
        if ($installedVersion -eq $latestVersion -and -not $Force) {
            Write-Host ''
            Write-Host "PlaudTools v$installedVersion is already installed and up to date." -ForegroundColor Green
            exit 0
        } elseif (-not $Force) {
            Write-Host ''
            Write-Host "PlaudTools v$installedVersion is installed; v$latestVersion is available." -ForegroundColor Yellow
            Write-Host 'Open PlaudTools from the system tray and click Check for Updates to upgrade.' -ForegroundColor Yellow
            exit 1
        } else {
            # -Force/-Repair: shut down running processes then wipe the install dir.
            $switchName = if ($Repair) { '-Repair' } else { '-Force' }
            Write-Host ''
            Write-Host "$switchName specified — shutting down PlaudTools processes..." -ForegroundColor Yellow

            # Gracefully stop any running tray process.
            $trayProcs = Get-Process -Name 'PlaudTools' -ErrorAction SilentlyContinue | Where-Object {
                $_.Path -and $_.Path.ToLower().StartsWith($installDir.ToLower())
            }
            if ($trayProcs) {
                foreach ($p in $trayProcs) { $p.CloseMainWindow() | Out-Null }
                $deadline = (Get-Date).AddSeconds(5)
                while (($trayProcs | Where-Object { !$_.HasExited }) -and (Get-Date) -lt $deadline) {
                    Start-Sleep -Milliseconds 200
                }
                $trayProcs | Where-Object { !$_.HasExited } | Stop-Process -Force -ErrorAction SilentlyContinue
            }

            # Gracefully stop any running MCP process.
            $mcpProcs = Get-Process -Name 'plaud-mcp' -ErrorAction SilentlyContinue | Where-Object {
                $_.Path -and $_.Path.ToLower().StartsWith($installDir.ToLower())
            }
            if ($mcpProcs) {
                foreach ($p in $mcpProcs) { $p.CloseMainWindow() | Out-Null }
                $deadline = (Get-Date).AddSeconds(3)
                while (($mcpProcs | Where-Object { !$_.HasExited }) -and (Get-Date) -lt $deadline) {
                    Start-Sleep -Milliseconds 100
                }
                $mcpProcs | Where-Object { !$_.HasExited } | Stop-Process -Force -ErrorAction SilentlyContinue
            }

            Write-Host "    Removing existing install at $installDir ..." -ForegroundColor Yellow
            Remove-Item $installDir -Recurse -Force
        }
    }

    # Broken/partial install: directory exists but exe is missing (e.g. Defender quarantine).
    if (Test-Path $installDir) {
        Write-Host ''
        Write-Host 'Found an incomplete installation (directory present, exe missing) — cleaning up...' -ForegroundColor Yellow
        Remove-Item $installDir -Recurse -Force
    }

    # --- Step 2: download the zip to temp ---
    Write-Host '[2/4] Downloading...'
    Get-FileWithProgress -Uri $asset.browser_download_url -OutFile $zipTemp
    Write-Host '    Download complete.'

    # --- Step 3: extract to install directory ---
    # Probe the zip layout so we extract to the right destination regardless of
    # whether the zip has a top-level PlaudTools\ folder (shape A) or ships
    # files at the root (shape B).
    $extractDir = Get-ZipExtractDestination -ZipPath $zipTemp -InstallDir $installDir
    Write-Host "[3/4] Extracting to $installDir ..."
    if (-not (Test-Path $extractDir)) {
        New-Item -ItemType Directory -Path $extractDir | Out-Null
    }
    Expand-ArchiveWithProgress -Path $zipTemp -DestinationPath $extractDir
    Remove-Item -Path $zipTemp -ErrorAction SilentlyContinue
    Write-Host '    Extraction complete.'

    # --- Step 4: launch the tray app ---
    Write-Host '[4/4] Launching PlaudTools...'
    if (-not (Test-Path $exePath)) {
        throw "PlaudTools.exe not found at '$exePath' after extraction. The zip layout may have changed."
    }
    # Sentinel so the tray knows to open its window on this first launch.
    [System.IO.File]::WriteAllText("$env:TEMP\plaud_just_installed.txt", '')
    Start-Process -FilePath $exePath

    Write-Host ''
    Write-Host 'PlaudTools installed successfully!' -ForegroundColor Green
    Write-Host "Location: $installDir"
    Write-Host 'Open a new terminal after first launch for PATH changes to take effect.'

} catch {
    Write-Host ''
    Write-Host "Installation failed: $_" -ForegroundColor Red
    Write-Host 'Please report this at https://github.com/massive-value/plaud-tools/issues'
    exit 1
}
