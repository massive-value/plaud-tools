# Install plaud-tools (the unofficial tray bundle).
#
# Usage:
#   irm https://raw.githubusercontent.com/massive-value/plaud-tools/main/scripts/install.ps1 | iex
#
# This script does NOT handle upgrades. If PlaudTools is already installed,
# use the tray menu's built-in update flow to upgrade.

$ErrorActionPreference = 'Stop'

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
    Write-Host '[1/4] Fetching latest release info from GitHub...'
    $release  = Invoke-RestMethod -Uri 'https://api.github.com/repos/massive-value/plaud-tools/releases/latest' -UseBasicParsing
    $asset    = $release.assets | Where-Object { $_.name -eq 'PlaudTools.zip' } | Select-Object -First 1

    if (-not $asset) {
        throw "Could not find PlaudTools.zip in the latest release assets. Check https://github.com/massive-value/plaud-tools/releases/latest"
    }

    $downloadUrl = $asset.browser_download_url
    Write-Host "    Found: $($release.tag_name) — $downloadUrl"

    # --- Step 2: download the zip to temp ---
    Write-Host '[2/4] Downloading PlaudTools.zip...'
    Invoke-WebRequest -Uri $downloadUrl -OutFile $zipTemp -UseBasicParsing
    Write-Host "    Saved to: $zipTemp"

    # --- Step 3: extract to install directory ---
    Write-Host "[3/4] Extracting to $installDir ..."
    if (-not (Test-Path $installDir)) {
        New-Item -ItemType Directory -Path $installDir | Out-Null
    }
    Expand-Archive -Path $zipTemp -DestinationPath $installDir
    Write-Host '    Extraction complete.'

    # --- Cleanup: remove temp zip ---
    Remove-Item -Path $zipTemp -ErrorAction SilentlyContinue

    # --- Step 4: launch the tray app ---
    Write-Host '[4/4] Launching PlaudTools...'
    if (-not (Test-Path $exePath)) {
        throw "PlaudTools.exe not found at '$exePath' after extraction. The zip layout may have changed."
    }
    Start-Process -FilePath $exePath

    Write-Host ''
    Write-Host 'PlaudTools installed successfully!' -ForegroundColor Green
    Write-Host "Location: $installDir"
    Write-Host 'Open a new PowerShell or cmd window after first launch for PATH changes to take effect.'

} catch {
    Write-Host ''
    Write-Host "Installation failed: $_" -ForegroundColor Red
    Write-Host 'Please report this at https://github.com/massive-value/plaud-tools/issues'
    exit 1
}
