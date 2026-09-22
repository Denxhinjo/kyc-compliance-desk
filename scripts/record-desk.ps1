<#
.SYNOPSIS
    Record a scripted GIF of the review desk, plus the case-study stills.

.DESCRIPTION
    Repeatable: run it again after a UI change and the GIF regenerates.

    ISOLATION — this is the part worth reading.

    The flow ends by DECIDING a case, and a decision writes to audit_events,
    which refuses DELETE. So there is no rolling it back: a decision made
    against the demo database is permanent, shows in that case's timeline for
    ever, and quietly eats one of the pending cases the desk exists to show.

    So the whole thing runs against a throwaway database (kyc_gif) served by a
    second Next.js process on port 3002, built into its own directory so the
    dev server's .next is untouched. Your demo data is never opened.

    Everything is torn down at the end except the database, which is left in
    place so a re-run skips the seed. Drop it with:
        docker compose exec -T db psql -U kyc -d postgres -c "drop database kyc_gif"

.EXAMPLE
    .\scripts\record-desk.ps1
    .\scripts\record-desk.ps1 -Reseed      # rebuild the throwaway data first
#>
param(
    [switch]$Reseed,
    [int]$Port = 3002,
    [string]$GifName = "review-desk.gif",
    [switch]$KeepVideo,
    # Size levers, in order of how much they cost you visually.
    #   -Fps 6      cheapest. The only motion is a cursor glide and a page load.
    #   -Width 1000 next cheapest. Still legible in a README.
    #   -Colors 96  last resort; the palette is already tight.
    # Measured on a 22s clip: 8fps/1200/128 = 3.1 MB, 6fps/1000/128 = ~1.7 MB.
    [int]$Fps = 8,
    [int]$Width = 1200,
    [int]$Colors = 128,
    # A thin queue films badly. 260 produced two pending cases; this fills it.
    [int]$SeedCount = 600
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$python = Join-Path $root "worker\.venv\Scripts\python.exe"
$media = Join-Path $root "docs\media"
$distDir = ".next-gif"
$dbName = "kyc_gif"

function Step($text) { Write-Host "  $text" -ForegroundColor DarkGray }

Write-Host ""
Write-Host "Recording the review desk" -ForegroundColor Cyan
Write-Host "  isolated: database $dbName, port $Port, distDir $distDir" -ForegroundColor DarkGray
Write-Host ""

# --- the throwaway database ------------------------------------------------
$setup = Join-Path $PSScriptRoot "_gif_setup.py"
$baseUrl = (& $python $setup url).Trim()
$exists  = (& $python $setup exists).Trim()

if ($exists -eq "no" -or $Reseed) {
    Step "building the throwaway database (drop, migrate, seed)"
    & $python $setup build --count $SeedCount
    if ($LASTEXITCODE -ne 0) { throw "could not build $dbName" }
} else {
    Step "reusing $dbName (pass -Reseed to rebuild it)"
}

# --- pick a case that actually shows a sanctions match ---------------------
$picked = (& $python $setup pick).Trim()
if ($LASTEXITCODE -ne 0 -or -not $picked) {
    Write-Host "No flagged case in $dbName. Re-run with -Reseed." -ForegroundColor Red
    exit 1
}
$case = $picked | ConvertFrom-Json
Step "case: $($case.name)  (score $($case.score))"

# --- playwright ------------------------------------------------------------
if (-not (Test-Path (Join-Path $PSScriptRoot "node_modules"))) {
    Step "installing playwright-core (scripts/ only, drives installed Edge)"
    Push-Location $PSScriptRoot
    npm install --silent | Out-Null
    Pop-Location
}

# --- build and start the isolated server -----------------------------------
Step "building the web app into $distDir"
Push-Location (Join-Path $root "web")
# `next build` rewrites tsconfig.json to add "<distDir>/types/**/*.ts" to its
# include list. Harmless in itself, but tsconfig.json is a tracked file and a
# recording script has no business leaving a diff behind — the first run of
# this committed a stray ".next-gif/types/**/*.ts" line. Snapshot it here,
# restore it in the finally block below.
$tsconfig = Join-Path $root "web/tsconfig.json"
$tsconfigBefore = Get-Content $tsconfig -Raw
$env:NEXT_DIST_DIR = $distDir
$env:DATABASE_URL = $baseUrl
npm run build 2>&1 | Out-Null

Step "starting it on port $Port"
$env:NODE_ENV = "production"
# npx.cmd, not npx: Start-Process needs a real executable and the extensionless
# shim is a shell script, which Windows rejects as "not a valid Win32
# application".
$server = Start-Process -FilePath "npx.cmd" `
    -ArgumentList @("next", "start", "-p", "$Port") `
    -NoNewWindow -PassThru `
    -RedirectStandardOutput (Join-Path $env:TEMP "kyc-gif-server.out") `
    -RedirectStandardError  (Join-Path $env:TEMP "kyc-gif-server.err")
Pop-Location

try {
    $up = $false
    foreach ($i in 1..60) {
        try {
            Invoke-WebRequest "http://localhost:$Port/" -UseBasicParsing -TimeoutSec 2 | Out-Null
            $up = $true; break
        } catch { Start-Sleep -Seconds 1 }
    }
    if (-not $up) { throw "server did not start on port $Port" }

    Step "driving the browser"
    $env:GIF_BASE_URL = "http://localhost:$Port"
    $env:GIF_CASE_ID = $case.id
    $env:GIF_CASE_NAME = $case.name
    $env:GIF_OUT = $media
    node (Join-Path $PSScriptRoot "record-desk.mjs")
}
finally {
    if ($server -and -not $server.HasExited) { Stop-Process -Id $server.Id -Force }
    Remove-Item Env:\NEXT_DIST_DIR, Env:\DATABASE_URL, Env:\NODE_ENV -ErrorAction SilentlyContinue
    if ($tsconfigBefore) { Set-Content -Path $tsconfig -Value $tsconfigBefore -NoNewline }
    Remove-Item (Join-Path $root "web/$distDir") -Recurse -Force -ErrorAction SilentlyContinue
}

# --- video -> GIF ----------------------------------------------------------
$webm = Get-ChildItem (Join-Path $media "_video") -Filter *.webm |
        Sort-Object LastWriteTime -Descending | Select-Object -First 1
if (-not $webm) { throw "playwright produced no video" }

$gif = Join-Path $media $GifName
$palette = Join-Path $env:TEMP "kyc-palette.png"

Step "converting to GIF"
# Two passes. palettegen builds a 256-colour table from the WHOLE clip, then
# paletteuse maps against it with dithering — without this a dark UI turns
# into the banded, dirty-looking GIFs of 1998.
# dither=none is the single biggest lever, and counter-intuitive. Bayer
# dithering scatters per-pixel noise that changes on every frame, which defeats
# GIF's inter-frame compression entirely — it cost 0.7 MB here. This interface
# is flat panels and text, not a photograph, so it quantises cleanly with no
# dithering at all and looks better for it.
#
# 8 fps because the only motion is a cursor glide and a page load. Measured:
# 12 fps with 192 colours and bayer was 3.67 MB; this is under 2.
& ffmpeg -y -loglevel error -i $webm.FullName `
    -vf "fps=$Fps,scale=${Width}:-1:flags=lanczos,palettegen=max_colors=${Colors}:stats_mode=diff" `
    $palette
& ffmpeg -y -loglevel error -i $webm.FullName -i $palette `
    -lavfi "fps=$Fps,scale=${Width}:-1:flags=lanczos[x];[x][1:v]paletteuse=dither=none:diff_mode=rectangle" `
    -loop 0 $gif

if (-not $KeepVideo) {
    Remove-Item (Join-Path $media "_video") -Recurse -Force -ErrorAction SilentlyContinue
}
Remove-Item $palette -ErrorAction SilentlyContinue

$sizeMb = [math]::Round((Get-Item $gif).Length / 1MB, 2)
Write-Host ""
$colour = if ($sizeMb -le 2) { "Green" } else { "Yellow" }
Write-Host "  $GifName  ${sizeMb} MB  (${Fps} fps, ${Width}px)" -ForegroundColor $colour
if ($sizeMb -gt 2) {
    Write-Host "  over 2 MB - try: -Fps 6 -Width 1000" -ForegroundColor Yellow
}
Get-ChildItem $media -Filter *.png | ForEach-Object {
    Write-Host ("  {0}  {1} KB" -f $_.Name, [math]::Round($_.Length / 1KB))
}
Write-Host ""
Write-Host "  Your demo database was not opened." -ForegroundColor DarkGray
Write-Host ""
