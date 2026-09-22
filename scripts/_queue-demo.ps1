<#
.SYNOPSIS
    Shared body of the two queue demos. Call demo-naive.ps1 or
    demo-correct.ps1 rather than this.

.DESCRIPTION
    One run, start to finish, in a single invocation:

      reset the jobs table -> enqueue N jobs -> start two workers ->
      wait for both -> print the duplicate count

    Worker stdout goes to a temp file, not the console. Two workers logging
    two hundred "job N done" lines would push the one number that matters off
    the top of the screen, which is the opposite of the point.

    The two demos differ by ONE FLAG passed to the worker: --naive swaps
    FOR UPDATE SKIP LOCKED for a claim with no locking at all. Same queue,
    same handler, same everything else.
#>
param(
    [switch]$Naive,
    [int]$Count = 200
)

$ErrorActionPreference = "Stop"

$root   = Split-Path -Parent $PSScriptRoot
$python = Join-Path $root "worker\.venv\Scripts\python.exe"
$helper = Join-Path $PSScriptRoot "_demo_queue.py"
$worker = Join-Path $root "worker\main.py"
$rule   = "=" * 66

if (-not (Test-Path $python)) {
    Write-Host "Cannot find the worker venv at $python" -ForegroundColor Red
    Write-Host "Create it with: cd worker; python -m venv .venv; .venv\Scripts\pip install -r requirements.txt"
    exit 1
}

# --- header ---------------------------------------------------------------
# Clear the screen when there is a real console to clear. Redirected or piped
# hosts have no cursor and Clear-Host throws "The handle is invalid" there,
# which would make the script fail for the least interesting possible reason.
try { if ($Host.UI.RawUI.WindowSize.Width -gt 0) { Clear-Host } } catch { }

Write-Host ""
Write-Host $rule -ForegroundColor DarkGray
if ($Naive) {
    Write-Host "  NAIVE CLAIM - no row locking. THIS ONE IS BROKEN." -ForegroundColor Red
    Write-Host "  $Count jobs, 2 workers, claimed WITHOUT for update skip locked"
} else {
    Write-Host "  FOR UPDATE SKIP LOCKED - the real claim path." -ForegroundColor Green
    Write-Host "  $Count jobs, 2 workers, same code apart from the lock"
}
Write-Host $rule -ForegroundColor DarkGray
Write-Host ""

# --- prepare --------------------------------------------------------------
& $python $helper reset
$watermark = (& $python $helper watermark).Trim()
& $python $helper enqueue --count $Count

# --- run two workers ------------------------------------------------------
$log = Join-Path $env:TEMP "kyc-queue-demo"
New-Item -ItemType Directory -Force -Path $log | Out-Null

$workerArgs = @($worker, "--drain")
if ($Naive) { $workerArgs += "--naive" }

$processes = @()
foreach ($name in @("worker-A", "worker-B")) {
    # Set before each spawn: the child inherits the environment as it stands
    # at that moment, which is how the two get different identities.
    $env:WORKER_ID = $name
    $processes += Start-Process -FilePath $python -ArgumentList $workerArgs `
        -NoNewWindow -PassThru `
        -RedirectStandardOutput (Join-Path $log "$name.out") `
        -RedirectStandardError  (Join-Path $log "$name.err")
}
Remove-Item Env:\WORKER_ID -ErrorAction SilentlyContinue

Write-Host "  two workers running..." -ForegroundColor DarkGray
$processes | Wait-Process -Timeout 180
Write-Host ""

# --- the result -----------------------------------------------------------
& $python $helper report --since $watermark --count $Count
Write-Host ""
