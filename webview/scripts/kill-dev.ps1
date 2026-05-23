# Terminates stale dev processes for the wows-model-export webview pipeline.
#
# Why this exists: `npm run dev` chains `concurrently` -> `cmd /c vite` -> `node vite.js`
# and `uvicorn --reload` spawns a reloader parent + worker child. On Windows, Ctrl+C
# only reaches the foreground process group, so the deeper grandchildren (esbuild,
# uvicorn worker) get orphaned and keep serving stale code on ports 5173 / 5180.
#
# Usage:
#   pwsh -File webview/scripts/kill-dev.ps1
#   pwsh -File webview/scripts/kill-dev.ps1 -DryRun
#
# Selection happens in four passes, unioned:
#   1. Port-based: anything LISTENING on 5173 / 5180 (catches uvicorn
#      `multiprocessing.spawn` workers whose command line is just the
#      generic bootstrap stub — no `wows-webview-serve` substring left).
#   2. Command-line pattern: webview/, wows-webview-serve, vite, etc.
#   3. Descendant walk: every child of every seed PID, recursively. This
#      reaches workers whose direct parent is the listening (often dead)
#      PID — `ParentProcessId` keeps pointing at the dead parent, so the
#      walk still works.
#   4. Orphan zombies (-IncludeOrphans): every `multiprocessing.spawn`
#      worker whose `parent_pid=<X>` in the cmdline refers to a PID
#      that no longer exists. These accumulate across `npm run dev`
#      sessions — uvicorn's reloader dies via Ctrl+C but the worker
#      survives, holds no port, and is invisible to passes 1-3 on
#      subsequent runs. OFF by default because frida-mcp / ghidra-mcp
#      / other Python multiprocessing tools leave identical-looking
#      orphans and we can't distinguish them by cmdline alone.

[CmdletBinding()]
param(
    [switch]$DryRun,
    [switch]$IncludeOrphans
)

$selfPid = $PID
$devPorts = @(5173, 5180)
$patterns = @(
    'wows-webview-serve',
    'wows-model-export[\\/]webview[\\/]node_modules',
    'webview[\\/]node_modules[\\/](\.bin[\\/])?(vite|concurrently|esbuild)',
    'webview[\\/]node_modules[\\/]@esbuild',
    '[\\/]wows-model-export[\\/].*[\\/]vite\.js'
)
$pattern = '(' + ($patterns -join '|') + ')'

function Get-DevTargets {
    param(
        [int[]]$Ports,
        [string]$CmdPattern,
        [int]$Self,
        [switch]$WithOrphans,
        [ref]$OrphanCount
    )

    $allProcs = Get-CimInstance Win32_Process
    $procByPid = @{}
    foreach ($p in $allProcs) { $procByPid[[int]$p.ProcessId] = $p }

    # Pass 1: port-based seed. Listener may itself be dead (uvicorn
    # reloader exited but worker inherited the socket); we still seed
    # its PID so the descendant walk picks the worker up.
    $seed = New-Object System.Collections.Generic.HashSet[int]
    foreach ($port in $Ports) {
        $conns = Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue
        foreach ($c in $conns) {
            $owner = [int]$c.OwningProcess
            if ($owner -gt 0 -and $owner -ne $Self) { [void]$seed.Add($owner) }
        }
    }

    # Pass 2: command-line pattern.
    foreach ($p in $allProcs) {
        if ($p.ProcessId -eq $Self) { continue }
        if (-not $p.CommandLine) { continue }
        if ($p.CommandLine -match $CmdPattern) { [void]$seed.Add([int]$p.ProcessId) }
    }

    # Pass 3: walk descendants of every seed PID.
    $queue = New-Object System.Collections.Queue
    foreach ($s in $seed) { $queue.Enqueue($s) }
    while ($queue.Count -gt 0) {
        $parent = [int]$queue.Dequeue()
        foreach ($p in $allProcs) {
            if ([int]$p.ParentProcessId -ne $parent) { continue }
            $childPid = [int]$p.ProcessId
            if ($childPid -eq $Self) { continue }
            if ($seed.Add($childPid)) { $queue.Enqueue($childPid) }
        }
    }

    # Pass 4: dead-parent orphans. Count them either way; only include
    # in $seed when -IncludeOrphans is set.
    $orphanPids = @()
    foreach ($p in $allProcs) {
        if ($p.ProcessId -eq $Self) { continue }
        if (-not $p.CommandLine) { continue }
        if ($p.CommandLine -notmatch 'multiprocessing\.spawn') { continue }
        if ($p.CommandLine -notmatch 'parent_pid=(\d+)') { continue }
        $parentPid = [int]$Matches[1]
        if ($procByPid.ContainsKey($parentPid)) { continue }
        if ($seed.Contains([int]$p.ProcessId)) { continue }
        $orphanPids += [int]$p.ProcessId
    }
    if ($OrphanCount) { $OrphanCount.Value = $orphanPids.Count }
    if ($WithOrphans) {
        foreach ($op in $orphanPids) { [void]$seed.Add($op) }
    }

    $out = @()
    foreach ($s in $seed) {
        if ($procByPid.ContainsKey($s)) { $out += $procByPid[$s] }
    }
    return ,$out
}

$orphanCount = 0
$targets = Get-DevTargets -Ports $devPorts -CmdPattern $pattern -Self $selfPid -WithOrphans:$IncludeOrphans -OrphanCount ([ref]$orphanCount)

if (-not $targets) {
    Write-Host "No matching dev processes found."
    if ($orphanCount -gt 0) {
        Write-Host "`n$orphanCount orphaned multiprocessing.spawn worker(s) detected (dead parent PID)."
        Write-Host "These are likely zombie uvicorn workers from prior 'npm run dev' sessions."
        Write-Host "Re-run with -IncludeOrphans to kill them:"
        Write-Host "  pwsh -File scripts/kill-dev.ps1 -IncludeOrphans"
        Write-Host "WARNING: this also catches orphans from frida-mcp / ghidra-mcp / other"
        Write-Host "Python tools; review with -DryRun -IncludeOrphans first."
    }
    return
}

# Sort children-before-parents so killing a parent doesn't auto-reap a child we
# haven't logged yet, and so uvicorn reloader children die before the reloader
# can respawn them.
$byPid = @{}
foreach ($t in $targets) { $byPid[[int]$t.ProcessId] = $t }

function Get-Depth([int]$thePid) {
    $depth = 0
    $cur = $byPid[$thePid]
    while ($cur -and $byPid.ContainsKey([int]$cur.ParentProcessId)) {
        $cur = $byPid[[int]$cur.ParentProcessId]
        $depth++
        if ($depth -gt 32) { break }
    }
    $depth
}

$ordered = $targets | Sort-Object @{Expression = { Get-Depth ([int]$_.ProcessId) }; Descending = $true}

$banner = "Found $($targets.Count) process(es)"
if ($IncludeOrphans -and $orphanCount -gt 0) {
    $banner += " (including $orphanCount dead-parent orphan(s))"
} elseif ($orphanCount -gt 0) {
    $banner += " (skipping $orphanCount dead-parent orphan(s); use -IncludeOrphans to nuke them)"
}
Write-Host "${banner}:"
foreach ($t in $ordered) {
    $cmd = $t.CommandLine
    if ($cmd.Length -gt 110) { $cmd = $cmd.Substring(0, 110) + '...' }
    Write-Host ("  [{0,6}] {1,-22} {2}" -f $t.ProcessId, $t.Name, $cmd)
}

if ($DryRun) {
    Write-Host "`nDry run - nothing killed."
    return
}

Write-Host ""
$killed = 0; $skipped = 0
foreach ($t in $ordered) {
    try {
        Stop-Process -Id $t.ProcessId -Force -ErrorAction Stop
        Write-Host ("killed {0,6} {1}" -f $t.ProcessId, $t.Name)
        $killed++
    } catch {
        Write-Host ("skip   {0,6} {1} ({2})" -f $t.ProcessId, $t.Name, $_.Exception.Message)
        $skipped++
    }
}

# Reloaders can respawn workers between our enumeration and kill; sweep once more.
Start-Sleep -Milliseconds 400
$leftover = Get-DevTargets -Ports $devPorts -CmdPattern $pattern -Self $selfPid -WithOrphans:$IncludeOrphans
if ($leftover) {
    Write-Host "`nRespawned children detected, sweeping again:"
    foreach ($t in $leftover) {
        try {
            Stop-Process -Id $t.ProcessId -Force -ErrorAction Stop
            Write-Host ("killed {0,6} {1}" -f $t.ProcessId, $t.Name)
            $killed++
        } catch {
            Write-Host ("skip   {0,6} {1}" -f $t.ProcessId, $t.Name)
            $skipped++
        }
    }
}

Write-Host "`nDone. killed=$killed skipped=$skipped"
