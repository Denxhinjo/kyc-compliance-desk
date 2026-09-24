# Build design-rationale.pdf.
#
# Requirements: any TeX distribution that provides pdflatex. On Windows:
#     winget install MiKTeX.MiKTeX
# Every package this document uses (fontenc, inputenc, lmodern, geometry,
# microtype, titlesec) ships with a basic install, so there is nothing to
# fetch beyond the distribution itself.
#
# Usage, from anywhere:
#     pwsh -File docs/build.ps1
#
# Two passes: the first writes the .aux, the second resolves against it.
# This document has no cross-references today, but a second pass costs a
# second and means adding one later cannot produce a silently stale PDF.

$ErrorActionPreference = 'Stop'
$doc = 'design-rationale'
Push-Location $PSScriptRoot

try {
    if (-not (Get-Command pdflatex -ErrorAction SilentlyContinue)) {
        throw "pdflatex not found on PATH. Install a TeX distribution (winget install MiKTeX.MiKTeX) and reopen the shell."
    }

    foreach ($pass in 1, 2) {
        Write-Host "pass $pass of 2..."
        # -halt-on-error so a broken document fails here rather than
        # producing a PDF missing whatever came after the error.
        pdflatex -interaction=nonstopmode -halt-on-error "$doc.tex" | Out-Null
        if ($LASTEXITCODE -ne 0) {
            throw "pdflatex failed on pass $pass. See $doc.log."
        }
    }

    # Warn rather than fail: a stretched line is a blemish, not a defect.
    $bad = Select-String -Path "$doc.log" -Pattern 'Overfull|Underfull' -ErrorAction SilentlyContinue
    if ($bad) {
        Write-Warning "$($bad.Count) overfull/underfull box(es) - check $doc.log"
    }

    $pages = (Select-String -Path "$doc.log" -Pattern 'Output written.*\((\d+) pages').Matches.Groups[1].Value
    Write-Host "built $doc.pdf - $pages pages" -ForegroundColor Green

    Remove-Item "$doc.aux", "$doc.log", "$doc.out" -ErrorAction SilentlyContinue
}
finally {
    Pop-Location
}
