<#
.SYNOPSIS
    In-app update helper for Plaud Tools.

.DESCRIPTION
    Waits for the tray process to exit, shuts down scoped plaud-mcp processes
    (retrying against external supervisors like Claude Desktop that respawn
    them), then installs the update with a staged swap:

      1. extract the zip into a sibling staging dir (<InstallDir>.staging)
      2. verify the staged tree has the expected executables
      3. rename the live install to <InstallDir>.old
      4. rename the staged tree to <InstallDir>
      5. delete <InstallDir>.old

    Any failure before step 4 completes rolls back to the untouched old
    install. Because the new version lands in a fresh directory, files that
    were removed from the bundle (old dependencies, stale dist-info) do not
    survive the update.

    All output is captured to a transcript log at
    $env:TEMP\plaud_update_<TrayPid>.log so failed runs are diagnosable.

    On unrecoverable failure (plaud-mcp keeps respawning, locked files in the
    install dir, etc.) the script writes a JSON sentinel at
    $env:TEMP\plaud_update_failed.txt containing the reason and the log path.
    The tray reads this on next launch and surfaces the failure to the user.

    The tray is restarted in a `finally` block, so the user is never stranded
    without a tray icon - even when the update itself fails.

.PARAMETER TrayPid
    PID of the running PlaudTools.exe (tray app) to wait for.

.PARAMETER InstallDir
    Absolute path to the PlaudTools install directory.

.PARAMETER ZipPath
    Absolute path to the downloaded PlaudTools.zip update archive.

.PARAMETER ExtractDir
    Legacy hint, accepted so older dispatchers still bind. Unused: the zip is
    always extracted into the staging dir.

.PARAMETER DispatcherPath
    Optional path to the %TEMP% dispatcher PS1 that invoked this script. Deleted
    after a successful run so %TEMP% does not accumulate stale .ps1 files. The
    bundled update.ps1 itself is NEVER deleted - earlier versions self-deleted
    it, which broke subsequent in-app updates.

.PARAMETER NewVersion
    The version being installed (e.g. "0.3.3"). Written to the
    plaud_just_updated.txt success sentinel only AFTER a successful swap.

.PARAMETER TrayExitTimeoutSec
    How long to wait for the tray to exit before giving up without installing
    anything (default 60).
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

    [string]$NewVersion = "",

    [int]$TrayExitTimeoutSec = 60
)

Set-StrictMode -Off
$ErrorActionPreference = 'Continue'

# ---------------------------------------------------------------------------
# Diagnostics - transcript log + structured failure sentinel
# ---------------------------------------------------------------------------

$logPath        = Join-Path $env:TEMP "plaud_update_$TrayPid.log"
$failSentinel   = Join-Path $env:TEMP "plaud_update_failed.txt"
$successSentinel = Join-Path $env:TEMP "plaud_just_updated.txt"

# Write text as UTF-8 WITHOUT a byte-order mark.
#
# Windows PowerShell 5.1 (what the updater launches) treats `-Encoding UTF8` as
# "UTF-8 WITH BOM" and prepends EF BB BF. The tray reads plaud_just_updated.txt
# and compares its contents to the running version; the leading U+FEFF made an
# otherwise-correct "0.4.0" mismatch "0.4.0", so a successful update was falsely
# reported as "did not complete". It also broke json.loads of the failure
# sentinel. .NET's UTF8Encoding($false) writes BOM-less UTF-8 on every PS
# version. Errors are swallowed to preserve the prior best-effort semantics.
function Write-NoBom {
    param([string]$Path, [string]$Value)
    try {
        [System.IO.File]::WriteAllText($Path, $Value, (New-Object System.Text.UTF8Encoding($false)))
    } catch {
        # Best effort - the equivalent information is still in the transcript log.
    }
}

# Heartbeat: written before Start-Transcript so we can tell whether the script
# reached PowerShell at all (vs. PowerShell crashing before running any code).
Write-NoBom -Path "$env:TEMP\plaud_update_$TrayPid.alive.txt" `
    -Value "update.ps1 reached at $(Get-Date -Format 'o')"

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
        Write-NoBom -Path $failSentinel -Value $payload
    } catch {
        # Best effort - the reason is still in the transcript log.
    }
    # A failed update must not leave the success sentinel behind, otherwise the
    # restarted (still-old) tray would falsely announce a successful upgrade.
    Remove-Item $successSentinel -ErrorAction SilentlyContinue
}

# ---------------------------------------------------------------------------
# Find the install root inside the extracted staging dir.
#
#   A) Single top-level directory (PlaudTools\...): the root is that folder.
#   B) Flat layout (files at the root of the zip): the root is the staging dir.
#
# Returns $null when neither shape contains PlaudTools.exe.
# ---------------------------------------------------------------------------

function Get-StagedInstallRoot {
    param([string]$StagingDir)

    if (Test-Path -LiteralPath (Join-Path $StagingDir 'PlaudTools.exe')) {
        return $StagingDir
    }
    $children = @(Get-ChildItem -LiteralPath $StagingDir -Force -ErrorAction SilentlyContinue)
    if ($children.Count -eq 1 -and $children[0].PSIsContainer) {
        $candidate = $children[0].FullName
        if (Test-Path -LiteralPath (Join-Path $candidate 'PlaudTools.exe')) {
            return $candidate
        }
    }
    return $null
}

# Remove a directory tree, retrying briefly for handles that are still closing.
# Returns $true when the directory is gone.
function Remove-DirWithRetry {
    param([string]$Path, [int]$Attempts = 5)

    for ($i = 1; $i -le $Attempts; $i++) {
        if (-not (Test-Path -LiteralPath $Path)) { return $true }
        Remove-Item -LiteralPath $Path -Recurse -Force -ErrorAction SilentlyContinue
        if (-not (Test-Path -LiteralPath $Path)) { return $true }
        Start-Sleep -Milliseconds 500
    }
    return (-not (Test-Path -LiteralPath $Path))
}

# Rename a directory, retrying briefly (antivirus scanners and just-killed
# processes can hold handles for a moment). Throws on final failure.
function Move-DirWithRetry {
    param([string]$From, [string]$To, [int]$Attempts = 5)

    for ($i = 1; $i -le $Attempts; $i++) {
        try {
            Move-Item -LiteralPath $From -Destination $To -ErrorAction Stop
            return
        } catch {
            if ($i -eq $Attempts) { throw }
            Write-Host "Rename $From -> $To failed (attempt $i): $($_.Exception.Message)"
            Start-Sleep -Milliseconds 500
        }
    }
}

# ---------------------------------------------------------------------------
# Stop ALL processes whose Path is under $InstallDir (plaud-mcp, ffmpeg, any
# other child processes), and confirm they stay dead. Returns $true when no
# scoped process has been alive for $StableMs milliseconds. Returns $false if
# a supervisor keeps respawning processes after $MaxAttempts attempts.
#
# This is the bug the v0.2.0 -> 0.2.1 update path hit: when Claude Desktop
# launches plaud-mcp, killing the process just causes Claude to relaunch it
# almost immediately, and the respawned exe keeps mcp\_internal\*.dll locked,
# causing the install to fail.
#
# Using path-based discovery (rather than name-based) also catches ffmpeg and
# any other child processes that plaud-mcp may have spawned - Stop-Process on
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
            # Nothing alive - wait $StableMs to make sure nobody respawns it.
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

$liveDir    = $InstallDir.TrimEnd('\').TrimEnd('/')
$stagingDir = "$liveDir.staging"
$oldDir     = "$liveDir.old"
$restartTray = $true
$movedLiveAway = $false
$swapped = $false

try {
    Write-Host "Plaud Tools updater starting at $(Get-Date -Format 'o')"
    Write-Host "  TrayPid        = $TrayPid"
    Write-Host "  InstallDir     = $liveDir"
    Write-Host "  ZipPath        = $ZipPath"
    Write-Host "  StagingDir     = $stagingDir"
    Write-Host "  DispatcherPath = $DispatcherPath"

    # 1. Wait for the tray to exit, with a timeout. If it never exits, give up
    #    WITHOUT installing: installing later, after the tray already told the
    #    user the update failed, is how two updaters ended up racing.
    $deadline = (Get-Date).AddSeconds($TrayExitTimeoutSec)
    while (Get-Process -Id $TrayPid -ErrorAction SilentlyContinue) {
        if ((Get-Date) -ge $deadline) {
            # The tray is still running, so do not start a second copy.
            $restartTray = $false
            $msg = "The tray (PID $TrayPid) did not exit within $TrayExitTimeoutSec seconds, so the update was not installed. Please try again."
            Write-Host "FAIL: $msg"
            Write-FailureSentinel -Reason $msg
            throw $msg
        }
        Start-Sleep -Seconds 1
    }
    Write-Host "Tray PID $TrayPid has exited"

    # 2. Make sure scoped plaud-mcp is dead AND stays dead long enough to swap
    #    the install directory.
    if (-not (Stop-PlaudMcpScoped -InstallDir $liveDir)) {
        $msg = "A process under $liveDir keeps respawning (likely plaud-mcp being restarted by Claude Desktop). Close Claude Desktop (or any other MCP client that has Plaud Tools registered) and run the update again."
        Write-Host "FAIL: $msg"
        Write-FailureSentinel -Reason $msg
        throw $msg
    }

    # 3. Clear leftovers from an earlier interrupted run, then extract into
    #    the staging dir. The live install is not touched yet.
    foreach ($leftover in @($stagingDir, $oldDir)) {
        if (-not (Remove-DirWithRetry -Path $leftover)) {
            $msg = "Could not remove leftover folder $leftover from an earlier update. Delete it and try again."
            Write-Host "FAIL: $msg"
            Write-FailureSentinel -Reason $msg
            throw $msg
        }
    }

    Write-Host "Extracting to $stagingDir"
    $ProgressPreference = 'SilentlyContinue'
    try {
        Expand-Archive -Path $ZipPath -DestinationPath $stagingDir -Force -ErrorAction Stop
    } catch {
        $msg = "Could not extract update zip: $($_.Exception.Message)"
        Write-Host "FAIL: $msg"
        Write-FailureSentinel -Reason $msg
        throw
    }
    Write-Host "Extraction complete"

    # 4. Verify the staged tree before touching the live install.
    $newRoot = Get-StagedInstallRoot -StagingDir $stagingDir
    $missing = @()
    if (-not $newRoot) {
        $missing = @('PlaudTools.exe')
    } else {
        foreach ($rel in @('PlaudTools.exe', 'mcp\plaud-mcp.exe', 'cli\plaud-tools.exe')) {
            if (-not (Test-Path -LiteralPath (Join-Path $newRoot $rel))) { $missing += $rel }
        }
    }
    if ($missing.Count -gt 0) {
        $msg = "The update package is incomplete (missing: $($missing -join ', ')). Nothing was changed."
        Write-Host "FAIL: $msg"
        Write-FailureSentinel -Reason $msg
        throw $msg
    }
    Write-Host "Staged install verified at $newRoot"

    # Carry the user's autostart opt-out across the update (the tray writes
    # this marker into the install dir; the zip does not contain it).
    $optOut = Join-Path $liveDir '.autostart_disabled'
    if (Test-Path -LiteralPath $optOut) {
        Copy-Item -LiteralPath $optOut -Destination (Join-Path $newRoot '.autostart_disabled') -Force
    }

    # 5. Swap: live -> .old, staged -> live. Roll back on any failure.
    try {
        Move-DirWithRetry -From $liveDir -To $oldDir
    } catch {
        $msg = "Could not move the current install aside (a file is probably still in use): $($_.Exception.Message.TrimEnd('.')). Nothing was changed."
        Write-Host "FAIL: $msg"
        Write-FailureSentinel -Reason $msg
        throw
    }
    $movedLiveAway = $true
    Write-Host "Moved live install to $oldDir"

    try {
        Move-DirWithRetry -From $newRoot -To $liveDir
    } catch {
        $msg = "Could not move the new version into place: $($_.Exception.Message.TrimEnd('.')). The previous version was restored."
        Write-Host "FAIL: $msg"
        Write-FailureSentinel -Reason $msg
        throw
    }
    $swapped = $true
    Write-Host "New version moved into $liveDir"

    # 6. Cleanup: old install, staging remains, the zip and the %TEMP%
    #    dispatcher. The bundled update.ps1 is never deleted directly (it
    #    lives in the install tree; earlier versions self-deleted and broke
    #    later updates). Failing to delete .old is harmless: the next update
    #    clears it first.
    if (-not (Remove-DirWithRetry -Path $oldDir)) {
        Write-Host "Could not fully delete $oldDir; it will be removed by the next update"
    }
    Remove-DirWithRetry -Path $stagingDir | Out-Null
    Remove-Item $ZipPath -ErrorAction SilentlyContinue
    if ($DispatcherPath -and (Test-Path $DispatcherPath)) {
        Remove-Item $DispatcherPath -ErrorAction SilentlyContinue
    }

    # 7. Write the success sentinel ONLY now that the swap has actually
    #    succeeded. (Earlier the tray pre-wrote this before launching the
    #    updater, so a silently-failed update - e.g. the updater process being
    #    killed before it ran - still left the sentinel behind and the old tray
    #    falsely announced success. The tray additionally verifies the running
    #    version matches before showing the success banner.)
    if ($NewVersion) {
        Write-NoBom -Path $successSentinel -Value $NewVersion
    }

    Write-Host "Update succeeded"
}
catch {
    Write-Host "Updater aborted: $_"

    # Roll back: the live install was moved aside but the new one never made
    # it into place. Put the old one back so the user keeps a working tray.
    if ($movedLiveAway -and -not $swapped) {
        try {
            if (Test-Path -LiteralPath $liveDir) {
                Remove-DirWithRetry -Path $liveDir | Out-Null
            }
            Move-DirWithRetry -From $oldDir -To $liveDir -Attempts 10
            Write-Host "Rolled back: restored previous install from $oldDir"
        } catch {
            $msg = "The update failed and the previous version could not be restored automatically. It is saved at $oldDir. Reinstall Plaud Tools with the install script (-Repair)."
            Write-Host "FAIL: $msg ($($_.Exception.Message))"
            Write-FailureSentinel -Reason $msg
        }
    }
    Remove-DirWithRetry -Path $stagingDir | Out-Null

    # Backstop: if the failure path that threw did not already write a
    # sentinel (e.g. an unexpected exception), record one here so the tray can
    # still surface the failure on the next launch.
    if (-not (Test-Path $failSentinel)) {
        Write-FailureSentinel -Reason "Updater aborted: $($_.Exception.Message)"
    }
}
finally {
    # Always restart the tray so the user is not stranded after a failed
    # update. If the swap worked we get the new version; if it failed we get
    # the old one back - better than nothing. Skipped only when the old tray
    # never exited (it is still running).
    if ($restartTray) {
        $trayExe = Join-Path $liveDir 'PlaudTools.exe'
        if (Test-Path $trayExe) {
            try {
                Start-Process $trayExe -ErrorAction Stop
                Write-Host "Tray restarted from $trayExe"
            } catch {
                Write-Host "Could not restart tray: $($_.Exception.Message)"
            }
        } else {
            Write-Host "Tray exe missing at $trayExe - cannot restart"
        }
    }

    try { Stop-Transcript | Out-Null } catch {}
}
