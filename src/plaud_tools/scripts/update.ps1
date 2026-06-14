<#
.SYNOPSIS
    In-app update helper for Plaud Tools.

.DESCRIPTION
    Waits for the tray process to exit, shuts down scoped plaud-mcp processes
    (retrying against external supervisors like Claude Desktop that respawn
    them), extracts the update zip into the install directory, and restarts
    the tray.

    All output is captured to a transcript log at
    $env:TEMP\plaud_update_<TrayPid>.log so failed runs are diagnosable.

    On unrecoverable failure (plaud-mcp keeps respawning, locked files in the
    install dir, etc.) the script writes a JSON sentinel at
    $env:TEMP\plaud_update_failed.txt containing the reason and the log path.
    The tray reads this on next launch and surfaces the failure to the user.

    The tray is restarted in a `finally` block, so the user is never stranded
    without a tray icon — even when the update itself fails.

.PARAMETER TrayPid
    PID of the running PlaudTools.exe (tray app) to wait for.

.PARAMETER InstallDir
    Absolute path to the PlaudTools install directory.

.PARAMETER ZipPath
    Absolute path to the downloaded PlaudTools.zip update archive.

.PARAMETER ExtractDir
    Hint for the extraction directory. Overridden at runtime based on the zip
    layout, but accepted as a backstop for callers that still pass it.

.PARAMETER DispatcherPath
    Optional path to the %TEMP% dispatcher PS1 that invoked this script. Deleted
    after a successful run so %TEMP% does not accumulate stale .ps1 files. The
    bundled update.ps1 itself is NEVER deleted — earlier versions self-deleted
    it, which broke subsequent in-app updates.

.PARAMETER NewVersion
    The version being installed (e.g. "0.3.3"). Used to (a) prune stale
    plaud_tools-*.dist-info directories left behind by the overlay extraction
    so importlib.metadata resolves the NEW version, and (b) write the
    plaud_just_updated.txt success sentinel only AFTER a successful extraction.
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

    [string]$DispatcherPath = "",

    [string]$SentinelPath = "",

    [string]$NewVersion = ""
)

Set-StrictMode -Off
$ErrorActionPreference = 'Continue'

# ---------------------------------------------------------------------------
# Diagnostics — transcript log + structured failure sentinel
# ---------------------------------------------------------------------------

$logPath        = Join-Path $env:TEMP "plaud_update_$TrayPid.log"
$failSentinel   = Join-Path $env:TEMP "plaud_update_failed.txt"
$successSentinel = Join-Path $env:TEMP "plaud_just_updated.txt"

# Heartbeat: written before Start-Transcript so we can tell whether the script
# reached PowerShell at all (vs. PowerShell crashing before running any code).
Set-Content -Path "$env:TEMP\plaud_update_$TrayPid.alive.txt" `
    -Value "update.ps1 reached at $(Get-Date -Format 'o')" `
    -Encoding UTF8 -ErrorAction SilentlyContinue

# Wipe any stale failure sentinel from a previous run so we never surface an
# old failure on top of a successful update.
Remove-Item $failSentinel -ErrorAction SilentlyContinue

try {
    Start-Transcript -Path $logPath -Force | Out-Null
} catch {
    # Transcript is best-effort; continue without it.
}

function Write-FailureSentinel {
    param([string]$Reason)
    try {
        $payload = [ordered]@{
            reason   = $Reason
            log      = $logPath
            time     = (Get-Date).ToString('o')
            tray_pid = $TrayPid
        } | ConvertTo-Json -Compress
        Set-Content -Path $failSentinel -Value $payload -Encoding UTF8 -ErrorAction Stop
    } catch {
        # Best effort — the reason is still in the transcript log.
    }
    # A failed update must not leave the success sentinel behind, otherwise the
    # restarted (still-old) tray would falsely announce a successful upgrade.
    Remove-Item $successSentinel -ErrorAction SilentlyContinue
}

# ---------------------------------------------------------------------------
# Probe the zip and return the correct extraction destination.
#
#   A) Single top-level directory (PlaudTools\...): extract to parent of
#      $InstallDir so files land at Programs\PlaudTools\ not
#      Programs\PlaudTools\PlaudTools\.
#   B) Flat layout (files at root of zip): extract directly to $InstallDir.
# ---------------------------------------------------------------------------

function Get-ZipExtractDestination {
    param([string]$ZipPath, [string]$InstallDir)

    Add-Type -AssemblyName System.IO.Compression.FileSystem
    $zip = [System.IO.Compression.ZipFile]::OpenRead($ZipPath)
    try {
        $topLevel = @{}
        foreach ($entry in $zip.Entries) {
            $name = $entry.FullName.TrimStart('/', '\')
            if (-not $name) { continue }
            $seg = ($name -split '[/\\]')[0]
            if ($seg) { $topLevel[$seg] = 1 }
        }
        $roots = @($topLevel.Keys)
        if ($roots.Count -eq 1) {
            $prefix = $roots[0] + '/'
            $hasChildren = $zip.Entries | Where-Object {
                $_.FullName -ne $prefix -and $_.FullName.StartsWith($prefix)
            }
            if ($hasChildren) {
                return (Split-Path $InstallDir -Parent)
            }
        }
        return $InstallDir
    } finally {
        $zip.Dispose()
    }
}

# ---------------------------------------------------------------------------
# Remove orphaned plaud_tools-*.dist-info directories left behind by the
# overlay extraction (Expand-Archive -Force overwrites matching paths but never
# deletes files absent from the zip). If a previous version's dist-info
# survives next to the new one, importlib.metadata.version("plaud-tools")
# resolves the OLD version and the tray keeps reporting the pre-update version
# (and re-offering the same "update available"). Keep only $NewVersion's
# dist-info. No-op when $NewVersion is empty (older callers).
# ---------------------------------------------------------------------------

function Remove-StaleDistInfo {
    param([string]$InstallDir, [string]$NewVersion)

    if (-not $NewVersion) { return }
    $keep = "plaud_tools-$NewVersion.dist-info"
    $stale = Get-ChildItem -Path $InstallDir -Recurse -Directory `
        -Filter 'plaud_tools-*.dist-info' -ErrorAction SilentlyContinue |
        Where-Object { $_.Name -ne $keep }
    foreach ($d in $stale) {
        Write-Host "Removing stale dist-info: $($d.FullName)"
        Remove-Item -LiteralPath $d.FullName -Recurse -Force -ErrorAction SilentlyContinue
    }
}

# ---------------------------------------------------------------------------
# Stop ALL processes whose Path is under $InstallDir (plaud-mcp, ffmpeg, any
# other child processes), and confirm they stay dead. Returns $true when no
# scoped process has been alive for $StableMs milliseconds. Returns $false if
# a supervisor keeps respawning processes after $MaxAttempts attempts.
#
# This is the bug the v0.2.0 → 0.2.1 update path hit: when Claude Desktop
# launches plaud-mcp, killing the process just causes Claude to relaunch it
# almost immediately, and the respawned exe keeps mcp\_internal\*.dll locked,
# causing Expand-Archive to throw and the script to bail.
#
# Using path-based discovery (rather than name-based) also catches ffmpeg and
# any other child processes that plaud-mcp may have spawned — Stop-Process on
# the parent does NOT kill children on Windows.
# ---------------------------------------------------------------------------

function Stop-PlaudMcpScoped {
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
            # Nothing alive — wait $StableMs to make sure nobody respawns it.
            Start-Sleep -Milliseconds $StableMs
            if (-not (& $findProcs)) {
                Write-Host "All install-dir processes confirmed stopped (attempt $attempt)"
                return $true
            }
            continue
        }

        Write-Host "Attempt $attempt`: killing $($procs.Count) process(es): $(($procs | Select-Object -ExpandProperty Name) -join ', ')"
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

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

try {
    Write-Host "Plaud Tools updater starting at $(Get-Date -Format 'o')"
    Write-Host "  TrayPid        = $TrayPid"
    Write-Host "  InstallDir     = $InstallDir"
    Write-Host "  ZipPath        = $ZipPath"
    Write-Host "  ExtractDir     = $ExtractDir (hint; may be overridden)"
    Write-Host "  DispatcherPath = $DispatcherPath"

    # 1. Wait for the tray to exit.
    while (Get-Process -Id $TrayPid -ErrorAction SilentlyContinue) {
        Start-Sleep -Seconds 1
    }
    Write-Host "Tray PID $TrayPid has exited"

    # 2. Make sure scoped plaud-mcp is dead AND stays dead long enough to
    #    extract over its locked DLLs.
    if (-not (Stop-PlaudMcpScoped -InstallDir $InstallDir)) {
        $msg = "A process under $InstallDir keeps respawning (likely plaud-mcp being restarted by Claude Desktop). Close Claude Desktop (or any other MCP client that has Plaud Tools registered) and run the update again."
        Write-Host "FAIL: $msg"
        Write-FailureSentinel -Reason $msg
        throw $msg
    }

    # 3. Probe the zip layout and pick the right destination. This overrides
    #    the $ExtractDir hint from the caller so the in-app update path is as
    #    robust as the install.ps1 path.
    $destination = Get-ZipExtractDestination -ZipPath $ZipPath -InstallDir $InstallDir
    Write-Host "Extracting to $destination"

    if (-not (Test-Path $destination)) {
        New-Item -ItemType Directory -Path $destination | Out-Null
    }

    $ProgressPreference = 'SilentlyContinue'
    try {
        Expand-Archive -Path $ZipPath -DestinationPath $destination -Force -ErrorAction Stop
    } catch {
        $msg = "Could not extract update zip: $($_.Exception.Message)"
        Write-Host "FAIL: $msg"
        Write-FailureSentinel -Reason $msg
        throw
    }
    Write-Host "Extraction complete"

    # 4. Cleanup: remove the zip and the %TEMP% dispatcher. The bundled
    #    update.ps1 (this very script) is NOT deleted — earlier versions
    #    self-deleted via $MyInvocation.MyCommand.Path, which broke subsequent
    #    in-app updates because the script vanished after the first successful
    #    upgrade.
    Remove-Item $ZipPath -ErrorAction SilentlyContinue
    if ($DispatcherPath -and (Test-Path $DispatcherPath)) {
        Remove-Item $DispatcherPath -ErrorAction SilentlyContinue
    }

    # 5. Prune stale dist-info so the restarted tray resolves the NEW version.
    Remove-StaleDistInfo -InstallDir $InstallDir -NewVersion $NewVersion

    # 6. Write the success sentinel ONLY now that extraction has actually
    #    succeeded. (Earlier the tray pre-wrote this before launching the
    #    updater, so a silently-failed update — e.g. the updater process being
    #    killed before it ran — still left the sentinel behind and the old tray
    #    falsely announced success. The tray additionally verifies the running
    #    version matches before showing the success banner.)
    if ($NewVersion) {
        Set-Content -Path $successSentinel -Value $NewVersion -Encoding UTF8 -ErrorAction SilentlyContinue
    }

    Write-Host "Update succeeded"
}
catch {
    Write-Host "Updater aborted: $_"
    # Backstop: if the failure path that threw did not already write a
    # sentinel (e.g. an unexpected exception from Get-ZipExtractDestination or
    # the wait loop), record one here so the tray can still surface the
    # failure on the next launch.
    if (-not (Test-Path $failSentinel)) {
        Write-FailureSentinel -Reason "Updater aborted: $($_.Exception.Message)"
    }
}
finally {
    # 5. Always restart the tray so the user is not stranded after a failed
    #    update. If the new tray bundle is in place, we get the new version;
    #    if extraction failed, we get the old one back — better than nothing.
    $trayExe = Join-Path $InstallDir 'PlaudTools.exe'
    if (Test-Path $trayExe) {
        try {
            Start-Process $trayExe -ErrorAction Stop
            Write-Host "Tray restarted from $trayExe"
        } catch {
            Write-Host "Could not restart tray: $($_.Exception.Message)"
        }
    } else {
        Write-Host "Tray exe missing at $trayExe — cannot restart"
    }

    try { Stop-Transcript | Out-Null } catch {}
}
