# Install plaud-tools (the unofficial tray bundle).
#
# Usage:
#   irm https://raw.githubusercontent.com/massive-value/plaud-tools/main/scripts/install.ps1 | iex

$ErrorActionPreference = 'Stop'

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
        if ($installedVersion -eq $latestVersion) {
            Write-Host ''
            Write-Host "PlaudTools v$installedVersion is already installed and up to date." -ForegroundColor Green
            exit 0
        } else {
            Write-Host ''
            Write-Host "PlaudTools v$installedVersion is installed; v$latestVersion is available." -ForegroundColor Yellow
            Write-Host 'Open PlaudTools from the system tray and click Check for Updates to upgrade.' -ForegroundColor Yellow
            exit 1
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
    # The zip contains a top-level PlaudTools\ folder, so extract to the
    # parent so the result lands at Programs\PlaudTools\ not Programs\PlaudTools\PlaudTools\.
    $extractDir = Split-Path $installDir -Parent
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
