<#
.SYNOPSIS
    Bootstrap the LabVIEW CI installer (PowerShell / Windows entry point).

.DESCRIPTION
    Fetches the tooling (unless run from a checkout) and hands off to install.py,
    which does the actual catalog-driven copy. This wrapper only locates Python,
    acquires the source, and forwards your flags.

.EXAMPLE
    From the root of the repo you want to add CI to:

    & ([scriptblock]::Create((irm https://raw.githubusercontent.com/ni/LabVIEW-CI-CD-Toolkit/main/.github/labview-ci/install.ps1))) `
        --activities masscompile,vi-analyzer,vidiff,dashboard --os windows,linux --labview-version 2026

.NOTES
    All flags are forwarded to install.py (run with --help to see them).
    Bootstrap-only flags handled here:
    --source-host github|gitlab  distribution to fetch from (default github)
    --source-repo OWNER/NAME     tooling repo to fetch from (default below)
    --source-ref  REF            branch/tag/sha of the tooling repo (default main)
    --source-gitlab-url URL      GitLab host for --source-host gitlab (default https://gitlab.com)
    --source      DIR            use a local tooling checkout instead of fetching
    Requires Python 3 and `tar` (built into Windows 10+).
#>
[CmdletBinding()]
param([Parameter(ValueFromRemainingArguments = $true)][string[]]$Rest)

$ErrorActionPreference = 'Stop'

$SourceHost      = 'github'
$SourceRepo      = 'ni/LabVIEW-CI-CD-Toolkit'
$SourceRef       = 'main'
$SourceGitLabUrl = 'https://gitlab.com'
$SrcDir          = $null
$ExplicitRepo    = $false
$ExplicitRef     = $false
$ExplicitSource  = $false
$IsUpdate        = $false
$Pass            = @()

for ($i = 0; $i -lt $Rest.Count; $i++) {
    switch ($Rest[$i]) {
        '--source-host'       { $SourceHost = $Rest[++$i]; $ExplicitSource = $true }
        '--source-repo'       { $SourceRepo = $Rest[++$i]; $ExplicitRepo = $true; $ExplicitSource = $true }
        '--source-ref'        { $SourceRef = $Rest[++$i]; $ExplicitRef = $true; $ExplicitSource = $true }
        '--source-gitlab-url' { $SourceGitLabUrl = $Rest[++$i]; $ExplicitSource = $true }
        '--source'            { $SrcDir = $Rest[++$i]; $ExplicitSource = $true }
        '--update'            { $IsUpdate = $true; $Pass += $Rest[$i] }
        default               { $Pass += $Rest[$i] }
    }
}

$py = Get-Command python3 -ErrorAction SilentlyContinue
if (-not $py) { $py = Get-Command python -ErrorAction SilentlyContinue }
if (-not $py) { throw 'Python 3 is required but was not found on PATH.' }

$target = $PWD.Path

function Get-ManifestDistribution([string]$Manifest) {
    $values = @{}
    $inSource = $false
    $inDistribution = $false
    foreach ($line in Get-Content -LiteralPath $Manifest) {
        if ($line -match '^source:\s*$') {
            $inSource = $true
            $inDistribution = $false
            continue
        }
        if ($line -and -not [char]::IsWhiteSpace($line[0])) {
            $inSource = $false
            $inDistribution = $false
            continue
        }
        if ($inSource -and $line -match '^\s{2}distribution:\s*$') {
            $inDistribution = $true
            continue
        }
        if ($inDistribution -and $line -match '^\s{4}(host|repo|ref|url):\s*(\S.*?)\s*$') {
            $values[$Matches[1]] = $Matches[2].Trim().Trim('"').Trim("'")
        }
    }
    return $values
}

if (-not $SrcDir -and $IsUpdate -and -not $ExplicitSource) {
    $manifest = Join-Path $target '.github/labview-ci.yml'
    if (Test-Path $manifest) {
        $stored = Get-ManifestDistribution $manifest
        if ($stored['host']) { $SourceHost = $stored['host'] }
        if ($stored['repo']) { $SourceRepo = $stored['repo'] }
        if ($stored['ref']) { $SourceRef = $stored['ref'] }
        if ($stored['url']) { $SourceGitLabUrl = $stored['url'] }
    }
}

if ($SourceHost -notin @('github', 'gitlab')) {
    throw '--source-host must be github or gitlab.'
}
if ($SourceHost -eq 'gitlab' -and -not $ExplicitRepo -and $SourceRepo -eq 'ni/LabVIEW-CI-CD-Toolkit') {
    $SourceRepo = 'elijah286/ci-for-labview'
}

function Get-SourcePointerUrl {
    if ($SourceHost -eq 'github') {
        return "https://raw.githubusercontent.com/$SourceRepo/$SourceRef/.github/labview-ci/source.json"
    }
    $project = [uri]::EscapeDataString($SourceRepo)
    $file = [uri]::EscapeDataString('.github/labview-ci/source.json')
    $ref = [uri]::EscapeDataString($SourceRef)
    return "$($SourceGitLabUrl.TrimEnd('/'))/api/v4/projects/$project/repository/files/$file/raw?ref=$ref"
}

function Get-SourcePointerDistribution([string]$Content) {
    try {
        $pointer = $Content | ConvertFrom-Json
        $entry = $null
        if ($pointer.distributions) {
            $entry = $pointer.distributions.PSObject.Properties[$SourceHost].Value
        }
        if (-not $entry -and $SourceHost -eq 'github') { $entry = $pointer }
        if (-not $entry) { return $null }
        return [pscustomobject]@{
            Repo = "$($entry.repo)"
            Ref = "$($entry.ref)"
            Url = "$($entry.url)"
        }
    }
    catch {
        return $null
    }
}

if (-not $SrcDir) {
    if ((Test-Path '.github/labview-ci/install.py') -and -not $IsUpdate) {
        $SrcDir = $PWD.Path
    }
    else {
        # Relocation pointer: if the source repo names a different official home in
        # .github/labview-ci/source.json, follow it (unless --source-repo was given)
        # so installs land on the current repo. install.py records the FETCHED
        # catalog's source.repo, so the new client polls the new home from then on.
        if (-not $ExplicitRepo) {
            try {
                $moved = Get-SourcePointerDistribution ((Invoke-WebRequest -Uri (Get-SourcePointerUrl) -UseBasicParsing -ErrorAction Stop).Content)
                if ($moved) {
                    if ($moved.Repo -and ($moved.Repo.ToLower() -ne $SourceRepo.ToLower())) {
                        Write-Host "LabVIEW CI tooling has moved to $($moved.Repo); installing from there ..."
                        $SourceRepo = $moved.Repo
                    }
                    if ($moved.Ref -and -not $ExplicitRef) { $SourceRef = $moved.Ref }
                    if ($moved.Url -and $SourceHost -eq 'gitlab') { $SourceGitLabUrl = $moved.Url }
                }
            }
            catch { }
        }
        $tmp = Join-Path ([System.IO.Path]::GetTempPath()) ('lvci-' + [guid]::NewGuid().ToString('N').Substring(0, 8))
        New-Item -ItemType Directory -Force -Path $tmp | Out-Null
        $archive = Join-Path $tmp 'tooling.tar.gz'
        if ($SourceHost -eq 'github') {
            # Bare ref form so --source-ref accepts a branch, a release tag (e.g. v1.2.0),
            # or a commit SHA; codeload resolves all three.
            $url = "https://codeload.github.com/$SourceRepo/tar.gz/$SourceRef"
        }
        else {
            $project = [uri]::EscapeDataString($SourceRepo)
            $ref = [uri]::EscapeDataString($SourceRef)
            $url = "$($SourceGitLabUrl.TrimEnd('/'))/api/v4/projects/$project/repository/archive.tar.gz?sha=$ref"
        }
        Write-Host "Fetching LabVIEW CI tooling from $SourceHost`:$SourceRepo@$SourceRef ..."
        Invoke-WebRequest -Uri $url -OutFile $archive -UseBasicParsing
        tar -xzf $archive -C $tmp
        $SrcDir = (Get-ChildItem $tmp -Directory | Select-Object -First 1).FullName
    }
}

$installer = Join-Path $SrcDir '.github/labview-ci/install.py'
if (-not (Test-Path $installer)) {
    throw "Tooling not found under $SrcDir (.github/labview-ci/install.py missing)."
}

$SourceDistributionUrl = if ($SourceHost -eq 'gitlab') { $SourceGitLabUrl } else { 'https://github.com' }

& $py.Source $installer --source $SrcDir --target $target `
    --source-distribution $SourceHost `
    --source-distribution-repo $SourceRepo `
    --source-distribution-ref $SourceRef `
    --source-distribution-url $SourceDistributionUrl @Pass
exit $LASTEXITCODE
