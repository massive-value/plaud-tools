# Install plaud-tools (the unofficial tray bundle).
#
# Usage:
#   irm https://raw.githubusercontent.com/massive-value/plaud-tools/main/scripts/install.ps1 | iex
#
# Options:
#   -Force   - remove any existing install (after shutting down tray + MCP) and reinstall.
#   -Repair  - alias for -Force; use when files are missing or quarantined.
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
                # Extract to parent - the folder inside the zip becomes $InstallDir.
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

    Write-Host "    Latest: v$latestVersion - PlaudTools.zip ($([math]::Round($asset.size / 1MB, 1)) MB)"

    # Strip any pre-release suffix (e.g. "0.3.0-rc1" -> "0.3.0") before casting
    # to [version] so that numeric comparison is always used and a pre-release tag
    # is never ranked above or equal to the same numeric release.
    function Get-NumericVersion {
        param([string]$v)
        # Remove leading 'v', then strip everything from the first '-' onward.
        $numeric = $v.TrimStart('v') -replace '-.*$', ''
        return [version]$numeric
    }

    # --- Guard: handle existing installs ---
    if (Test-Path $exePath) {
        $installedVersion = (Get-Item $exePath).VersionInfo.FileVersion.Trim()
        $installedVerNum  = Get-NumericVersion $installedVersion
        $latestVerNum     = Get-NumericVersion $latestVersion
        if ($installedVerNum -eq $latestVerNum -and -not $Force) {
            Write-Host ''
            Write-Host "PlaudTools v$installedVersion is already installed and up to date." -ForegroundColor Green
            Write-Host ''
            Write-Host 'Press Enter to close...' -ForegroundColor Gray
            try { Read-Host } catch { }
            exit 0
        } elseif ($installedVerNum -gt $latestVerNum -and -not $Force) {
            # Installed build is ahead of the latest published release (e.g. a
            # dev/pre-release build). Without this branch, control fell into
            # the -Force/-Repair wipe branch below and silently DOWNGRADED the
            # user to the older published release (#159).
            Write-Host ''
            Write-Host "PlaudTools v$installedVersion (installed) is newer than the latest published release v$latestVersion." -ForegroundColor Yellow
            Write-Host 'Nothing to do. Re-run with -Force if you want to reinstall the published release anyway.' -ForegroundColor Yellow
            Write-Host ''
            Write-Host 'Press Enter to close...' -ForegroundColor Gray
            try { Read-Host } catch { }
            exit 0
        } elseif ($latestVerNum -gt $installedVerNum -and -not $Force) {
            Write-Host ''
            Write-Host "PlaudTools v$installedVersion is installed; v$latestVersion is available." -ForegroundColor Yellow
            Write-Host 'Open PlaudTools from the system tray and click Check for Updates to upgrade.' -ForegroundColor Yellow
            Write-Host 'If the in-app updater does not work (older installs), re-run this installer with -Repair.' -ForegroundColor Yellow
            Write-Host ''
            Write-Host 'Press Enter to close...' -ForegroundColor Gray
            try { Read-Host } catch { }
            exit 1
        } else {
            # -Force/-Repair: shut down running processes then wipe the install dir.
            $switchName = if ($Repair) { '-Repair' } else { '-Force' }
            Write-Host ''
            Write-Host "$switchName specified - shutting down PlaudTools processes..." -ForegroundColor Yellow

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
        Write-Host 'Found an incomplete installation (directory present, exe missing) - cleaning up...' -ForegroundColor Yellow
        Remove-Item $installDir -Recurse -Force
    }

    # --- Step 2: download the zip to temp ---
    Write-Host '[2/4] Downloading...'
    Get-FileWithProgress -Uri $asset.browser_download_url -OutFile $zipTemp
    Write-Host '    Download complete.'

    # --- Step 2b: verify SHA256 checksum (unconditionally fail-closed) ---
    #
    # The SHA256SUMS asset (format: "<hex>  PlaudTools.zip", standard sha256sum
    # two-space format) is published alongside PlaudTools.zip on every release
    # from v0.3.0 onward.
    #
    # Verification is FAIL CLOSED (#113): a hash mismatch aborts the install, and
    # so does an absent SHA256SUMS asset - an absent asset means a malformed
    # release or a tampered asset list, and the download cannot be trusted.
    $sumsAsset = $release.assets | Where-Object { $_.name -eq 'SHA256SUMS' } | Select-Object -First 1
    if (-not $sumsAsset) {
        throw 'SHA256SUMS asset not found for this release; the download''s integrity cannot be verified. Aborting install. If this persists, report it at https://github.com/massive-value/plaud-tools/issues'
    }
    Write-Host '    Verifying SHA256 checksum...'
    $sumsTemp = Join-Path $env:TEMP 'PlaudTools.SHA256SUMS'
    try {
        Invoke-RestMethod -Uri $sumsAsset.browser_download_url -OutFile $sumsTemp -UseBasicParsing
        $sumsContent = Get-Content $sumsTemp -Encoding UTF8 -Raw
        # Parse first token from the two-space format: "<hex>  <filename>"
        $expectedHash = ($sumsContent.Trim() -split '\s+')[0].ToUpper()
        $actualHash   = (Get-FileHash -Path $zipTemp -Algorithm SHA256).Hash.ToUpper()
        if ($actualHash -ne $expectedHash) {
            throw (
                "SHA256 mismatch - the downloaded zip may be corrupt or tampered.`n" +
                "  Expected: $expectedHash`n" +
                "  Actual:   $actualHash`n" +
                'Please retry; if the mismatch persists report it at https://github.com/massive-value/plaud-tools/issues'
            )
        }
        Write-Host '    Checksum verified.'
    } finally {
        Remove-Item -Path $sumsTemp -ErrorAction SilentlyContinue
    }

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
    #
    # PATH / PowerShell-completions / autostart setup used to happen here as
    # its own step (a second-language reimplementation of
    # plaud_tools.tray.setup._setup_cli_path / _setup_ps_completions /
    # _set_autostart). Wave 3 hardened the tray's own auto-heal pass
    # (_run_verify_env / _auto_repair_env in tray/background.py), which now
    # runs unconditionally on every launch -- including this first one -- and
    # silently restores anything missing. Deleting the installer step (Wave 5,
    # 2026-07-06 audit, S7.6) removes the duplicate PowerShell implementation
    # entirely; first launch below configures everything instead.
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
    Write-Host 'Open a new terminal for PATH changes to take effect.'

    if ($Force) {
        Write-Host ''
        Write-Host 'NOTE: The MCP server was replaced. Restart any coding agents (Claude Code,' -ForegroundColor Cyan
        Write-Host 'Cursor, Copilot, etc.) so they pick up the new plaud-mcp process.' -ForegroundColor Cyan
    }

} catch {
    Write-Host ''
    Write-Host "Installation failed: $_" -ForegroundColor Red
    Write-Host 'Please report this at https://github.com/massive-value/plaud-tools/issues'
    Write-Host ''
    Write-Host 'Press Enter to close...' -ForegroundColor Gray
    try { Read-Host } catch { }
    exit 1
}
