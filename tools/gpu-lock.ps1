# Machine-wide GPU lock for the agents sharing this box. It replaces announcing and holding GPU runs
# by message: every GPU run (captures, test runs, a live editor session) takes the lock first and
# releases it after. The harness takes it for every job submitted with --gpu.
#
#   powershell -File tools/gpu-lock.ps1 acquire <Name> "<what>"   # waits its TURN (FIFO)
#   powershell -File tools/gpu-lock.ps1 release <Name>
#   powershell -File tools/gpu-lock.ps1 status                    # holder + queue
#
# Creation is atomic (CreateNew), so two agents can never both hold it. A lock older than 3 hours is
# reported as stale but NOT broken automatically -- a hard reset leaves one behind, and breaking a
# live one is the overlap this exists to prevent. Break it by hand after checking tasklist.
#
# FIFO: waiters used to RACE every 15 s poll, so one agent lost the lock three times in
# twenty minutes and an announced reservation was sniped by a waiter that happened to poll first.
# A waiter now drops a TICKET in gpu-queue/ (named by its arrival time and its process id) and only
# tries the lock while its ticket is the oldest LIVE one. A ticket whose process is gone -- a reaped
# or killed waiter -- is pruned, so a dead waiter can never block the queue. Every caller must use the
# SAME copy of this script (and the same HARNESS_LOCK_DIR), or two queues exist and they can snipe.
param(
    [Parameter(Mandatory = $true, Position = 0)][ValidateSet('acquire', 'release', 'status')] [string]$Action,
    [Parameter(Position = 1)][string]$Name = '',
    [Parameter(Position = 2)][string]$What = ''
)
$ErrorActionPreference = 'Stop'
$dir = if ($env:HARNESS_LOCK_DIR) { $env:HARNESS_LOCK_DIR } else { Join-Path $env:LOCALAPPDATA 'AgentHarness' }
$lock = Join-Path $dir 'gpu.lock'
$queue = Join-Path $dir 'gpu-queue'
New-Item -ItemType Directory -Force $dir, $queue | Out-Null

function Show-Lock {
    if (Test-Path $lock) {
        $age = (Get-Date) - (Get-Item $lock).LastWriteTime
        $body = Get-Content $lock -Raw
        $stale = if ($age.TotalHours -gt 3) { ' (STALE: over 3 h; check tasklist before breaking)' } else { '' }
        "HELD for {0:N0} min: {1}{2}" -f $age.TotalMinutes, $body.Trim(), $stale
    } else { 'FREE' }
}

# Live tickets, oldest first. A ticket is "<utc ticks>-<pid>.ticket" containing "<Name> | <what>".
function Get-Queue {
    $live = @()
    foreach ($t in (Get-ChildItem $queue -Filter '*.ticket' -ErrorAction SilentlyContinue | Sort-Object Name)) {
        $procId = [int](($t.BaseName -split '-')[1])
        if (Get-Process -Id $procId -ErrorAction SilentlyContinue) { $live += $t }
        else { Remove-Item $t.FullName -Force -ErrorAction SilentlyContinue }   # reaped or killed waiter
    }
    , $live
}

switch ($Action) {
    'status' {
        Show-Lock
        $i = 1
        foreach ($t in (Get-Queue)) {
            $waited = (Get-Date) - $t.CreationTime
            "  queue {0}: {1} (waiting {2:N0} min)" -f $i, (Get-Content $t.FullName -Raw).Trim(), $waited.TotalMinutes
            $i++
        }
    }
    'acquire' {
        if (-not $Name) { throw 'acquire needs a name' }
        $ticket = Join-Path $queue ("{0:D20}-{1}.ticket" -f [DateTime]::UtcNow.Ticks, $PID)
        Set-Content -Path $ticket -Value "$Name | $What" -Encoding UTF8
        $announced = $false
        try {
            while ($true) {
                $q = Get-Queue
                if ($q.Count -eq 0 -or $q[0].FullName -eq $ticket) {
                    try {
                        $fs = [System.IO.File]::Open($lock, 'CreateNew', 'Write', 'None')
                        $bytes = [Text.Encoding]::UTF8.GetBytes("$Name | $What | $(Get-Date -Format s)")
                        $fs.Write($bytes, 0, $bytes.Length); $fs.Close()
                        "ACQUIRED by $Name"; break
                    } catch [System.IO.IOException] { }
                }
                if (-not $announced) {
                    $pos = [Array]::IndexOf(@($q | ForEach-Object { $_.FullName }), $ticket) + 1
                    "WAITING (queue position $pos): $(Show-Lock)"; $announced = $true
                }
                Start-Sleep -Seconds 5
            }
        } finally {
            Remove-Item $ticket -Force -ErrorAction SilentlyContinue
        }
    }
    'release' {
        if (-not (Test-Path $lock)) { 'already free'; break }
        $owner = ((Get-Content $lock -Raw) -split '\|')[0].Trim()
        if ($owner -ne $Name) { throw "lock is held by '$owner', not '$Name'; not released" }
        Remove-Item $lock -Force
        "RELEASED by $Name"
    }
}
