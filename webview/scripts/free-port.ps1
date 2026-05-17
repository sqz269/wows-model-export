#!/usr/bin/env pwsh
# Free a TCP port by killing whatever process is listening on it.
# Defaults to 5180 (the wows-webview-serve backend port from package.json dev:backend).
#
# Also hunts down orphaned multiprocessing workers (uvicorn --reload spawns a child
# via python multiprocessing; killing the parent leaves the worker alive and still
# bound to the socket, but Windows keeps reporting the dead parent's pid as owner).
#
# Usage:
#   pwsh webview/scripts/free-port.ps1                  # frees 5180
#   pwsh webview/scripts/free-port.ps1 -Port 5173       # frees the vite dev port
#   pwsh webview/scripts/free-port.ps1 -Port 5180 -DryRun

[CmdletBinding()]
param(
    [int]$Port = 5180,
    [switch]$DryRun
)

$ErrorActionPreference = 'Stop'

function Stop-PidSafely {
    param([int]$TargetPid, [string]$Reason, [switch]$DryRun)
    try {
        $proc = Get-Process -Id $TargetPid -ErrorAction Stop
    } catch {
        return $false
    }
    $label = "pid=$TargetPid name=$($proc.ProcessName)"
    if ($DryRun) {
        Write-Output "would kill $label ($Reason)"
        return $true
    }
    try {
        Stop-Process -Id $TargetPid -Force -ErrorAction Stop
        Write-Output "killed $label ($Reason)"
        return $true
    } catch {
        Write-Output "fail  $label : $($_.Exception.Message)"
        return $false
    }
}

# Hunts python multiprocessing children whose `parent_pid=<N>` argv matches any pid
# in $DeadParentPids. These orphans inherit the listening socket from their dead parent.
function Find-OrphanedMpChildren {
    param([int[]]$DeadParentPids)
    if (-not $DeadParentPids -or $DeadParentPids.Count -eq 0) { return @() }
    $pattern = 'parent_pid=(' + (($DeadParentPids | ForEach-Object { [regex]::Escape("$_") }) -join '|') + ')\b'
    Get-CimInstance Win32_Process -Filter "name='python.exe' or name='pythonw.exe'" |
        Where-Object { $_.CommandLine -and $_.CommandLine -match $pattern } |
        Select-Object -ExpandProperty ProcessId
}

$listeners = Get-NetTCPConnection -State Listen -LocalPort $Port -ErrorAction SilentlyContinue
if (-not $listeners) {
    Write-Output "port $Port is free"
    exit 0
}

$ownerPids = @($listeners | Select-Object -ExpandProperty OwningProcess -Unique)
$killed = 0
$deadOwners = @()

foreach ($procId in $ownerPids) {
    if (-not (Get-Process -Id $procId -ErrorAction SilentlyContinue)) {
        Write-Output "skip  pid=$procId (already gone — checking for orphaned workers)"
        $deadOwners += $procId
        continue
    }
    if (Stop-PidSafely -TargetPid $procId -Reason "port $Port" -DryRun:$DryRun) {
        if (-not $DryRun) { $killed++ }
        $deadOwners += $procId
    }
}

# Sweep orphaned multiprocessing workers whose parent_pid matches any killed/dead owner.
if (-not $DryRun -and $deadOwners.Count -gt 0) {
    $orphans = @(Find-OrphanedMpChildren -DeadParentPids $deadOwners)
    foreach ($orphanPid in $orphans) {
        if (Stop-PidSafely -TargetPid $orphanPid -Reason "orphaned mp worker of dead parent") {
            $killed++
        }
    }
}

if (-not $DryRun -and ($killed -gt 0 -or $deadOwners.Count -gt 0)) {
    Start-Sleep -Milliseconds 300
    $still = Get-NetTCPConnection -State Listen -LocalPort $Port -ErrorAction SilentlyContinue
    if ($still) {
        $livePids = @($still | Where-Object { Get-Process -Id $_.OwningProcess -ErrorAction SilentlyContinue } | Select-Object -ExpandProperty OwningProcess -Unique)
        if ($livePids.Count -gt 0) {
            Write-Output "warn  port $Port still bound by live pid(s): $($livePids -join ', ')"
            exit 1
        }
        Write-Output "note  port $Port shows stale Listen entry (owner dead) — will clear shortly"
    } else {
        Write-Output "port $Port is free"
    }
}
