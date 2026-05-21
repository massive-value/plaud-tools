# Install plaud-tools (the unofficial tray bundle).
#
# Usage:
#   irm https://raw.githubusercontent.com/massive-value/plaud-tools/main/scripts/install.ps1 | iex
#
# This script does NOT handle upgrades. If PlaudTools is already installed,
# use the tray menu's built-in update flow to upgrade.

$ErrorActionPreference = 'Stop'
$ProgressPreference    = 'SilentlyContinue'   # suppress default Invoke-* progress noise

function Get-FileWithProgress {
    param([string]$Uri, [string]$OutFile, [string]$Label = 'Downloading')

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
                Write-Progress -Activity $Label -Status "${dlMb} MB / ${totalMb} MB" -PercentComplete $pct
            } else {
                Write-Progress -Activity $Label -Status "${dlMb} MB downloaded"
            }
        }
    } finally {
        $out.Close()
        $stream.Close()
        $resp.Close()
    }
    Write-Progress -Activity $Label -Completed
}

try {
    $installDir = Join-Path $env:LOCALAPPDATA 'Programs\PlaudTools'
    $exePath    = Join-Path $installDir 'PlaudTools.exe'
    $zipTemp    = Join-Path $env:TEMP 'PlaudTools.zip'

    # --- Guard: refuse to overwrite an existing install ---
    if (Test-Path $exePath) {
        Write-Host 'PlaudTools is already installed. Use the tray menu''s update flow to upgrade instead.' -ForegroundColor Yellow
        exit 1
    }

    # --- Step 1: resolve the latest release via GitHub API ---
    Write-Host '[1/4] Fetching latest release info...'
    $release = Invoke-RestMethod -Uri 'https://api.github.com/repos/massive-value/plaud-tools/releases/latest' -UseBasicParsing
    $asset   = $release.assets | Where-Object { $_.name -eq 'PlaudTools.zip' } | Select-Object -First 1

    if (-not $asset) {
        throw "Could not find PlaudTools.zip in the latest release assets. Check https://github.com/massive-value/plaud-tools/releases/latest"
    }

    Write-Host "    $($release.tag_name) — PlaudTools.zip ($([math]::Round($asset.size / 1MB, 1)) MB)"

    # --- Step 2: download the zip to temp ---
    Write-Host '[2/4] Downloading...'
    Get-FileWithProgress -Uri $asset.browser_download_url -OutFile $zipTemp -Label 'Downloading PlaudTools.zip'
    Write-Host '    Download complete.'

    # --- Step 3: extract to install directory ---
    # The zip contains a top-level PlaudTools\ folder, so extract to the
    # parent directory so the result lands at Programs\PlaudTools\.
    $extractDir = Split-Path $installDir -Parent
    Write-Host "[3/4] Extracting to $installDir ..."
    if (-not (Test-Path $extractDir)) {
        New-Item -ItemType Directory -Path $extractDir | Out-Null
    }
    Expand-Archive -Path $zipTemp -DestinationPath $extractDir -Force
    Remove-Item -Path $zipTemp -ErrorAction SilentlyContinue
    Write-Host '    Extraction complete.'

    # --- Step 4: launch the tray app ---
    Write-Host '[4/4] Launching PlaudTools...'
    if (-not (Test-Path $exePath)) {
        throw "PlaudTools.exe not found at '$exePath' after extraction. The zip layout may have changed."
    }
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
