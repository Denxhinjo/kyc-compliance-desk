<#
.SYNOPSIS
    The broken queue: two workers claiming without a lock.

.EXAMPLE
    .\scripts\demo-naive.ps1

.NOTES
    Resets the jobs table, enqueues 200 jobs, runs two workers against them
    and prints how many were processed twice. Expect a non-zero duplicate
    count — that is the whole point of the take.
#>
param([int]$Count = 200)
& (Join-Path $PSScriptRoot "_queue-demo.ps1") -Naive -Count $Count
