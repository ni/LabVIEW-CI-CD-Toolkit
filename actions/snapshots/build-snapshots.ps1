<#
.SYNOPSIS
    Orchestrates content-addressed VI snapshot rendering across one or more
    commits using a single long-lived LabVIEW container.

.DESCRIPTION
    Snapshots are content-addressed by git blob SHA, so each unique VI *content*
    is rendered exactly once — ever. Unchanged VIs are reused across commits with
    no re-rendering and no duplication.

    A single container is started and kept warm for the whole job; each commit is
    rendered via `docker exec` (no per-VI or per-commit container churn). For each
    commit:
      1. a detached git worktree of that commit is created (so the VI and all its
         dependencies are present on disk),
      2. only VIs whose blob isn't already rendered are sent to the container,
      3. the commit's manifest.json is written, mapping every VI to its by-blob
         snapshot.

    Backfill mode walks history oldest -> newest. It is resumable: already-rendered
    blobs (seeded from the deployed gallery) are skipped instantly, and a wall-clock
    -TimeBudgetMinutes lets a large backfill span several runs.

.PARAMETER Mode
    'head'     — render the single target commit (default; used on push).
    'backfill' — walk all VI-touching commits oldest -> newest.

.PARAMETER WorkspaceRoot
    The main checkout (contains .git and .github). Default: current directory.

.PARAMETER TargetSha
    head mode: commit to render. Default: current HEAD.

.PARAMETER Shas
    Exact space-separated commit SHAs in processing order. Overrides Mode and MaxCommits.

.PARAMETER OutDir
    Staging dir to publish (…/ci-out/vi-snapshots). Default under WorkspaceRoot.

.PARAMETER ExistingByBlobDir
    Path to the already-deployed by-blob store (gh-pages vi-snapshots/by-blob),
    used to skip work that's already been rendered.

.PARAMETER Image
    LabVIEW container image. Default: nationalinstruments/labview:latest-windows.

.PARAMETER MaxCommits
    backfill: cap to the most recent N VI-touching commits (0 = all).

.PARAMETER TimeBudgetMinutes
    backfill: stop launching new renders after this many minutes (resume next run).
#>
param(
    [ValidateSet('head', 'backfill')]
    [string]$Mode              = 'head',
    [string]$WorkspaceRoot     = (Get-Location).Path,
    [string]$TargetSha         = '',
    [string]$Shas              = '',
    [string]$OutDir            = '',
    [string]$ExistingByBlobDir = '',
    [string]$Image             = 'nationalinstruments/labview:latest-windows',
    [int]   $MaxCommits        = 0,
    [int]   $MaxVIs            = 0,
    [int]   $TimeBudgetMinutes = 300,
    # Directory holding render-snapshots.ps1 + build-gallery.py. Defaults to the
    # in-repo location; a composite action passes its own bundled directory so the
    # consumer repo needs no copy of these scripts.
    [string]$OpsDir            = '',
    # Directory holding the VI Browser page assets (vi-browser.html, vi-interactive.html).
    [string]$PagesDir          = '',
    # VI Browser 2.0 (position-aware frames JSON) emission: 'true' (default) emits
    # the frames JSON next to each HTML when the PrintToImagesJson op is present;
    # 'false' renders HTML only (VI Browser config: positionAware excludes Windows).
    [string]$EmitFramesJson    = 'true'
)

# NOTE: 'Continue' (not 'Stop') is deliberate. This orchestrator drives native
# commands (git, docker) that legitimately write progress to stderr — e.g.
# `git worktree add` prints "Preparing worktree (detached HEAD ...)". Under
# Windows PowerShell 5.1 with $ErrorActionPreference='Stop', that informational
# stderr is turned into a terminating NativeCommandError (even with 2>$null),
# which previously aborted the whole run. Every native call below judges success
# explicitly via $LASTEXITCODE (or an explicit `throw`), so 'Continue' is safe.
$ErrorActionPreference = 'Continue'
$ProgressPreference    = 'SilentlyContinue'

# ── Paths ────────────────────────────────────────────────────────────────────
$WorkspaceRoot = (Resolve-Path $WorkspaceRoot).Path
if ($OutDir -eq '') { $OutDir = Join-Path $WorkspaceRoot 'ci-out\vi-snapshots' }
$ByBlobDir = Join-Path $OutDir 'by-blob'
if ($OpsDir   -eq '') { $OpsDir   = Join-Path $WorkspaceRoot '.github\labview' }
if ($PagesDir -eq '') { $PagesDir = Join-Path $WorkspaceRoot '.github\pages' }
$OpsHost   = $OpsDir

$TempRoot = if ($env:RUNNER_TEMP) { $env:RUNNER_TEMP } else { [System.IO.Path]::GetTempPath() }
$WorkTreesHost = Join-Path $TempRoot 'lvci-wt'

New-Item -ItemType Directory -Force -Path $OutDir, $ByBlobDir, $WorkTreesHost | Out-Null

$ContainerName = "lvci-snap-$([System.Guid]::NewGuid().ToString('N').Substring(0,8))"

function Resolve-Python {
    foreach ($c in 'python3', 'python') {
        $cmd = Get-Command $c -ErrorAction SilentlyContinue
        if ($cmd) { return $cmd.Source }
    }
    throw 'Python not found on PATH (need python3/python for build-gallery.py).'
}
$Python = Resolve-Python

function Get-VimapForSha([string]$Sha) {
    # Returns @( @{Blob=..; Rel=..}, ... ) for *.vi/*.ctl excluding CI/build dirs.
    $out = & git -C $WorkspaceRoot -c core.quotePath=false ls-tree -r $Sha
    $list = New-Object System.Collections.Generic.List[object]
    foreach ($line in $out) {
        $tab = $line.IndexOf("`t")
        if ($tab -lt 0) { continue }
        $meta = $line.Substring(0, $tab)
        $path = $line.Substring($tab + 1)
        if ($path -notmatch '\.(vi|ctl)$') { continue }
        if ($path -match '^(\.github|ci-out|build)/') { continue }
        $blob = ($meta -split '\s+')[2]
        $list.Add(@{ Blob = $blob; Rel = $path })
    }
    return $list
}

# ── Seed the set of already-rendered blobs ───────────────────────────────────
$Rendered = New-Object System.Collections.Generic.HashSet[string]
foreach ($dir in @($ExistingByBlobDir, $ByBlobDir)) {
    if ($dir -and (Test-Path $dir)) {
        Get-ChildItem $dir -Recurse -Filter '*.html' -ErrorAction SilentlyContinue |
            ForEach-Object { [void]$Rendered.Add($_.BaseName) }
    }
}
Write-Host "Seeded $($Rendered.Count) already-rendered blob(s)."

# ── Determine commit list ────────────────────────────────────────────────────
if (-not [string]::IsNullOrWhiteSpace($Shas)) {
    $Commits = @($Shas -split '\s+' | Where-Object { $_ } | Select-Object -Unique)
    foreach ($commit in $Commits) {
        if ($commit -notmatch '^[0-9a-fA-F]{40}$') { throw "Invalid snapshot commit SHA: $commit" }
        & git -C $WorkspaceRoot cat-file -e "${commit}^{commit}"
        if ($LASTEXITCODE -ne 0) { throw "Snapshot commit not found: $commit" }
    }
}
elseif ($Mode -eq 'head') {
    if ($TargetSha -eq '') { $TargetSha = (& git -C $WorkspaceRoot rev-parse HEAD).Trim() }
    $Commits = @($TargetSha)
}
else {
    $Commits = @(& git -C $WorkspaceRoot log --reverse --format='%H' -- '*.vi' '*.ctl')
    $head = (& git -C $WorkspaceRoot rev-parse HEAD).Trim()
    if ($Commits -notcontains $head) { $Commits += $head }
    if ($MaxCommits -gt 0 -and $Commits.Count -gt $MaxCommits) {
        $Commits = $Commits[($Commits.Count - $MaxCommits)..($Commits.Count - 1)]
    }
}
Write-Host "Mode=$Mode - processing $($Commits.Count) commit(s)."

# ── Start the long-lived container ───────────────────────────────────────────
& docker pull $Image | Out-Null
Write-Host "Starting container $ContainerName ..."
& docker run -d --name $ContainerName `
    -v "${OpsHost}:C:\ops" `
    -v "${WorkTreesHost}:C:\wt" `
    -v "${OutDir}:C:\out" `
    $Image powershell -NoProfile -Command "while (`$true) { Start-Sleep -Seconds 3600 }" | Out-Null
if ($LASTEXITCODE -ne 0) { throw "Failed to start container." }

# Probe that host dirs created after start are visible inside the container.
$probe = Join-Path $WorkTreesHost '.probe'
Set-Content -Path $probe -Value 'ok' -Encoding ascii
$probeSeen = (& docker exec $ContainerName powershell -NoProfile -Command "if (Test-Path 'C:\wt\.probe') { 'yes' } else { 'no' }").Trim()
Remove-Item $probe -Force -ErrorAction SilentlyContinue
if ($probeSeen -ne 'yes') {
    & docker rm -f $ContainerName | Out-Null
    throw "Live bind-mount probe failed (container cannot see new host files under C:\wt)."
}

$deadline = (Get-Date).AddMinutes($TimeBudgetMinutes)
$totalRendered = 0
$processed = 0

try {
    foreach ($sha in $Commits) {
        $vimap    = Get-VimapForSha $sha
        $worklist = @($vimap | Where-Object { -not $Rendered.Contains($_.Blob) })

        # Smoke-test cap: limit how many NEW VIs to render (0 = no limit). Lets a
        # validation run exercise the full pipeline quickly without rendering all VIs.
        if ($MaxVIs -gt 0 -and $worklist.Count -gt $MaxVIs) {
            Write-Host "MaxVIs=${MaxVIs}: capping worklist from $($worklist.Count) for $($sha.Substring(0,7))."
            $worklist = @($worklist[0..($MaxVIs - 1)])
        }
        if ($worklist.Count -gt 0 -and (Get-Date) -gt $deadline) {
            Write-Host "Time budget reached - stopping before $($sha.Substring(0,7)). Re-run backfill to resume."
            break
        }

        # Worktree of this commit (provides the VI files + their dependencies).
        $wtHost = Join-Path $WorkTreesHost $sha
        if (Test-Path $wtHost) { & git -C $WorkspaceRoot worktree remove --force $wtHost 2>$null | Out-Null }
        & git -C $WorkspaceRoot worktree add --detach $wtHost $sha 2>$null | Out-Null
        if ($LASTEXITCODE -ne 0) { Write-Warning "worktree add failed for $sha - skipping."; continue }

        try {
            # Write full vimap + worklist (consumed by build-gallery.py / render-snapshots.ps1).
            # Use UTF-8 without BOM so the TSV parses cleanly on both sides.
            $vimapFile    = Join-Path $OutDir   "vimap-$sha.tsv"
            $worklistFile = Join-Path $OutDir   "worklist-$sha.tsv"
            $utf8NoBom    = New-Object System.Text.UTF8Encoding($false)
            [System.IO.File]::WriteAllLines($vimapFile,    @($vimap    | ForEach-Object { "$($_.Blob)`t$($_.Rel)" }), $utf8NoBom)
            [System.IO.File]::WriteAllLines($worklistFile, @($worklist | ForEach-Object { "$($_.Blob)`t$($_.Rel)" }), $utf8NoBom)

            if ($worklist.Count -gt 0) {
                Write-Host "[$($sha.Substring(0,7))] rendering $($worklist.Count) new VI(s) of $($vimap.Count)..."
                & docker exec $ContainerName powershell -NoProfile -ExecutionPolicy Bypass `
                    -File 'C:\ops\render-snapshots.ps1' `
                    -WorkspaceRoot "C:\wt\$sha" `
                    -OpsDir        'C:\ops' `
                    -OutByBlobDir  'C:\out\by-blob' `
                    -WorkListPath  "C:\out\worklist-$sha.tsv" `
                    -EmitFramesJson $EmitFramesJson
                if ($LASTEXITCODE -ne 0) { Write-Warning "render exec returned $LASTEXITCODE for $sha (continuing)." }
                foreach ($e in $worklist) { [void]$Rendered.Add($e.Blob) }
                $totalRendered += $worklist.Count
            }
            else {
                Write-Host "[$($sha.Substring(0,7))] all $($vimap.Count) VI(s) already rendered - manifest only."
            }

            # Build this commit's manifest + update commits.json.
            $msg  = (& git -C $WorkspaceRoot show -s --format='%s'  $sha).Trim()
            $auth = (& git -C $WorkspaceRoot show -s --format='%an' $sha).Trim()
            $date = (& git -C $WorkspaceRoot show -s --format='%aI' $sha).Trim()
            & $Python (Join-Path $OpsHost 'build-gallery.py') `
                --vimap        $vimapFile `
                --commit-sha   $sha `
                --commit-msg   $msg `
                --author       $auth `
                --date         $date `
                --output-dir   (Join-Path $OutDir $sha) `
                --commits-file (Join-Path $OutDir 'commits.json')
            if ($LASTEXITCODE -ne 0) { Write-Warning "build-gallery.py failed for $sha." }

            Remove-Item $worklistFile, $vimapFile -Force -ErrorAction SilentlyContinue
            $processed++
        }
        finally {
            & git -C $WorkspaceRoot worktree remove --force $wtHost 2>$null | Out-Null
        }
    }
}
finally {
    & docker rm -f $ContainerName 2>$null | Out-Null
    & git -C $WorkspaceRoot worktree prune 2>$null | Out-Null
}

# ── Publish the browser pages alongside the gallery data ─────────────────────
Copy-Item (Join-Path $PagesDir 'vi-browser.html')     (Join-Path $OutDir 'index.html')          -Force
Copy-Item (Join-Path $PagesDir 'vi-interactive.html') (Join-Path $OutDir 'vi-interactive.html') -Force
# vi-render.js drives the in-place block-diagram renderer (loaded by both pages).
# Tolerate its absence so older page bundles without it still deploy cleanly.
$ViRenderSrc = Join-Path $PagesDir 'vi-render.js'
if (Test-Path $ViRenderSrc) { Copy-Item $ViRenderSrc (Join-Path $OutDir 'vi-render.js') -Force }

# -- Emit a flat index of every rendered blob (content-addressed) -------------
# The dashboard reads this single file to know which of each revision's VIs have
# a snapshot (snapshots are keyed by git blob SHA), so the Snapshots column and
# the per-revision glyph reflect the real gallery without crawling by-blob/. The
# set is every blob that now has a by-blob HTML (already-deployed seed + rendered
# this run); it deploys alongside commits.json under vi-snapshots/.
$BlobsIndex = Join-Path $OutDir 'blobs.json'
try {
    $sortedBlobs = @($Rendered) | Sort-Object
    if ($sortedBlobs.Count -eq 0) {
        $blobsJson = '[]'
    } else {
        # Build the array literal by hand: ConvertTo-Json renders a single-element
        # set as a bare string, not a JSON array. Blob SHAs are hex - no escaping.
        $blobsJson = '[' + (($sortedBlobs | ForEach-Object { '"' + $_ + '"' }) -join ',') + ']'
    }
    [System.IO.File]::WriteAllText($BlobsIndex, $blobsJson, (New-Object System.Text.UTF8Encoding($false)))
    Write-Host "Wrote blobs.json with $($sortedBlobs.Count) rendered blob(s)."
} catch {
    Write-Warning "Could not write blobs.json: $_"
}

Write-Host ""
Write-Host "=== Snapshots done: $processed commit(s) processed, $totalRendered VI(s) rendered this run ==="

# Reaching here means the orchestrator ran to completion. Return success explicitly:
# under $ErrorActionPreference='Continue' a benign non-zero $LASTEXITCODE from a
# native cleanup call could otherwise mark the step as failed. The workflow's
# "Verify gallery output" step independently gates on real output (commits.json + by-blob).
exit 0
