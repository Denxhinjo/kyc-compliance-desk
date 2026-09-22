<#
.SYNOPSIS
    The real queue: two workers claiming with FOR UPDATE SKIP LOCKED.

.EXAMPLE
    .\scripts\demo-correct.ps1

.NOTES
    Identical to demo-naive.ps1 except for one flag on the worker. Resets the
    jobs table, enqueues 200 jobs, runs two workers and prints the duplicate
    count. Expect zero.
#>
param([int]$Count = 200)
& (Join-Path $PSScriptRoot "_queue-demo.ps1") -Count $Count
