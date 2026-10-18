<#
.SYNOPSIS
    Run the configured LabVIEW unit-test frameworks headlessly and emit one JUnit
    XML file per tool into -ResultsDir. Runs INSIDE the LabVIEW CI container
    (same environment as run-vi-analyzer.ps1). Its JUnit output is consumed by
    build-unittest-report.py, which merges every *.xml into one friendly report.

.DESCRIPTION
    Reads `config.unitTests.tools[]` from .github/labview-ci.yml. For each enabled
    tool it resolves the configured test locations (each a directory to recurse or
    a glob / file-extension pattern) and invokes that tool's headless runner via
    g-cli (already baked into the CI image), writing JUnit XML.

    Caraya is the reference implementation, driven via g-cli. NI UTF runs through the
    built-in LabVIEWCLI RunUnitTests operation (see Invoke-UtfTests); LUnit (Astemes)
    runs through the native LabVIEWCLI "LUnit" operation the same way (see
    Invoke-LUnitTests); JKI VI Tester is scaffolded with the same contract. The exact
    command for each tool is a per-tool
    template that can be overridden from the config (`command:` key) so the precise
    invocation can be corrected on a real worker without editing this script.

    HEADLESS: LabVIEW must run -Headless in LabVIEW 2026+ Windows containers (same
    constraint run-vi-analyzer.ps1 documents) or VI Server fails with -350000. g-cli
    launches LabVIEW the same way, so we pass the LabVIEW path/version through.

.PARAMETER WorkspaceRoot
    Absolute path to the checked-out project inside the container. Default C:\workspace.

.PARAMETER ResultsDir
    Directory to write the per-tool JUnit XML into. build-unittest-report.py reads
    every *.xml here and infers the tool from the file name (caraya*.xml -> Caraya,
    vitester*/vi-tester* -> VI Tester, utf*/unit-test* -> UTF).

.PARAMETER ConfigPath
    Path to labview-ci.yml. Default: <WorkspaceRoot>\.github\labview-ci.yml. Ignored
    when -Framework is set.

.PARAMETER Framework
    When set, bypasses ConfigPath/labview-ci.yml entirely and runs exactly ONE tool:
    'caraya', 'vi-tester', 'utf', 'lunit', or an arbitrary id paired with -Command.
    Used by actions/unit-tests (the step-level composite action), which takes the
    framework as an action input instead of a repo-committed config file.

.PARAMETER TestDir
    -Framework only: a single test location (directory, glob, or .lvproj), same
    contract as a config tool's `locations[0]`. Empty = whole project/workspace.

.PARAMETER Command
    -Framework only: overrides that tool's default command template (tokens
    {ver}{dir}{out}{lv}, or {cli}{proj} for utf/lunit). Empty = built-in default.
#>
param(
    [string]$WorkspaceRoot  = 'C:\workspace',
    [string]$ResultsDir     = 'C:\workspace\ci-out\unit-tests\results',
    [string]$ConfigPath     = '',
    [string]$LabVIEWVersion = '2026',
    [string]$LabVIEWPath    = '',
    [string]$Framework      = '',
    [string]$TestDir        = '',
    [string]$Command        = ''
)

$ErrorActionPreference = 'Stop'
$ProgressPreference    = 'SilentlyContinue'

if (-not $ConfigPath) { $ConfigPath = Join-Path $WorkspaceRoot '.github\labview-ci.yml' }
New-Item -ItemType Directory -Force -Path $ResultsDir | Out-Null

# Tracks tools that were configured + attempted but could not run because the
# selected container lacks the required tooling (e.g. the NI Unit Test Framework
# toolkit is not installed). Serialized to <ResultsDir>\_tooling.json so
# build-unittest-report.py can render the shared "container is missing this
# dependency" banner on the report.
$Script:ToolingIssues = @()
function Add-ToolingIssue([string]$tool, [string]$name, [string]$kind, [string]$detail) {
    $Script:ToolingIssues += [pscustomobject]@{ tool = $tool; name = $name; kind = $kind; detail = $detail }
}

# Diagnostic: show where the NI Unit Test Framework toolkit landed and what this
# LabVIEW references. LabVIEW 2023+ loads toolkits from a VERSION-INDEPENDENT
# add-ons folder (C:\Program Files\NI\LVAddons); the UTF MSI deploys via NI's
# NIPaths resolver (logical path LVADDONSDIR64). This prints the add-ons folder
# state + LabVIEW registry so a -350053 'operation could not load' is traceable
# to whether the toolkit is actually visible to this LabVIEW.
function Show-UtfAddonsDiag([string]$LvPath) {
    Write-Host '===== UTF / version-independent add-ons diagnostic ====='
    $roots = @('C:\Program Files\NI\LVAddons',
               'C:\Program Files (x86)\NI\LVAddons',
               'C:\Program Files\National Instruments\Shared\LabVIEW Addons')
    foreach ($r in $roots) {
        if (Test-Path -LiteralPath $r) {
            Write-Host "ADDONS ROOT: $r"
            Get-ChildItem -LiteralPath $r -Recurse -Depth 2 -ErrorAction SilentlyContinue |
                Select-Object -First 80 | ForEach-Object { Write-Host "  $($_.FullName)" }
        } else {
            Write-Host "absent: $r"
        }
    }
    # Dump the add-on manifests. LabVIEW 2023+ loads an LVAddon only if its
    # lvaddoninfo.json declares compatibility with the running LabVIEW version.
    # Compare UTF (RunUnitTests fails) against viawin (VI Analyzer, which works)
    # to reveal the version gating that keeps UTF from loading on LabVIEW 2026.
    foreach ($mf in @('C:\Program Files\NI\LVAddons\utf64\1\lvaddoninfo.json',
                      'C:\Program Files\NI\LVAddons\utf32\1\lvaddoninfo.json',
                      'C:\Program Files\NI\LVAddons\viawin\1\lvaddoninfo.json')) {
        if (Test-Path -LiteralPath $mf) {
            Write-Host "--- manifest: $mf ---"
            Get-Content -LiteralPath $mf -Raw -ErrorAction SilentlyContinue | ForEach-Object { Write-Host $_ }
        }
    }
    $lvRoot = if ($LvPath) { Split-Path -Parent $LvPath } else { '' }
    if ($lvRoot -and (Test-Path -LiteralPath $lvRoot)) {
        Write-Host "LabVIEW root: $lvRoot"
        foreach ($sub in @('vi.lib\addons','user.lib','resource\Framework\Providers','project','vi.lib\Unit Test Framework')) {
            $p = Join-Path $lvRoot $sub
            if (Test-Path -LiteralPath $p) {
                $utf = @(Get-ChildItem -LiteralPath $p -ErrorAction SilentlyContinue | Where-Object { $_.Name -match '(?i)utf|unit.?test' })
                Write-Host ("  {0}: {1} UTF-ish entr(ies) {2}" -f $sub, $utf.Count, (($utf | ForEach-Object { $_.Name }) -join ', '))
            }
        }
    }
    foreach ($rk in @('HKLM:\SOFTWARE\National Instruments\LabVIEW','HKLM:\SOFTWARE\WOW6432Node\National Instruments\LabVIEW')) {
        if (Test-Path -LiteralPath $rk) {
            Write-Host "REG $rk"
            $props = Get-ItemProperty -LiteralPath $rk -ErrorAction SilentlyContinue
            if ($props) { $props.PSObject.Properties | Where-Object { $_.Name -notmatch '^PS' } | ForEach-Object { Write-Host "    $($_.Name) = $($_.Value)" } }
            Get-ChildItem -LiteralPath $rk -ErrorAction SilentlyContinue | ForEach-Object { Write-Host "    subkey: $($_.PSChildName)" }
        }
    }
    # The -350053 error names the LabVIEW CLI "operation folder" as the place with
    # "missing or bad files". Dump it so we can see whether the RunUnitTests operation
    # is actually present for this LabVIEW (the UTF version-independent add-on is
    # supposed to register it); a missing/broken RunUnitTests operation here is the
    # real cause of -350053, independent of any VIPM package.
    Write-Host '----- LabVIEW CLI operation folders -----'
    foreach ($op in @('C:\Program Files (x86)\National Instruments\Shared\LabVIEW CLI\Operations',
                      'C:\Program Files\National Instruments\Shared\LabVIEW CLI\Operations')) {
        if (Test-Path -LiteralPath $op) {
            Write-Host "OPERATIONS ROOT: $op"
            Get-ChildItem -LiteralPath $op -Recurse -ErrorAction SilentlyContinue |
                Select-Object -First 200 | ForEach-Object { Write-Host "  $($_.FullName)" }
        } else {
            Write-Host "absent: $op"
        }
    }
    # Where does the UTF add-on keep its RunUnitTests CLI operation, if anywhere?
    foreach ($addon in @('C:\Program Files\NI\LVAddons\utf64\1','C:\Program Files\NI\LVAddons\utf32\1')) {
        if (Test-Path -LiteralPath $addon) {
            $hits = @(Get-ChildItem -LiteralPath $addon -Recurse -ErrorAction SilentlyContinue |
                Where-Object { $_.Name -match '(?i)run.?unit.?test|unit.?test.*\.(vi|lvclass)$|cli' })
            Write-Host ("UTF add-on '{0}': {1} CLI/RunUnitTests-ish file(s)" -f $addon, $hits.Count)
            $hits | Select-Object -First 50 | ForEach-Object { Write-Host "  $($_.FullName)" }
        }
    }
    Write-Host '===== end diagnostic ====='
}

# -- Resolve LabVIEW / LabVIEWCLI / g-cli (mirror run-vi-analyzer.ps1) ----------
function Resolve-LabVIEWPath([string]$PreferredPath) {
    if ($PreferredPath -and (Test-Path $PreferredPath)) { return $PreferredPath }
    $candidates = @(Get-ChildItem 'C:\Program Files\National Instruments' -Directory -Filter 'LabVIEW *' -ErrorAction SilentlyContinue |
        Sort-Object Name -Descending |
        ForEach-Object { Join-Path $_.FullName 'LabVIEW.exe' } |
        Where-Object { Test-Path $_ })
    if ($candidates.Count -gt 0) { return $candidates[0] }
    throw "LabVIEW.exe not found (preferred '$PreferredPath')."
}

function Resolve-Cmd([string[]]$names) {
    foreach ($n in $names) {
        $c = Get-Command $n -ErrorAction SilentlyContinue
        if ($c -and $c.Source) { return $c.Source }
    }
    return $null
}

function Sync-PathFromRegistry {
    # VIPM-installed CLIs (e.g. g-cli) add their directory to the MACHINE PATH in the
    # registry at install time, but a Windows container's process PATH is baked from
    # the image ENV layer and does NOT pick that up (the g-cli docs note you must
    # "restart any terminals or build agents after install"). Re-read the persisted
    # PATH from the registry and merge in anything missing so Get-Command can see a
    # freshly-baked g-cli without an image rebuild. Best-effort: never aborts the run.
    try {
        $machine = (Get-ItemProperty -Path 'HKLM:\SYSTEM\CurrentControlSet\Control\Session Manager\Environment' -Name 'Path' -ErrorAction SilentlyContinue).Path
        $user    = (Get-ItemProperty -Path 'HKCU:\Environment' -Name 'Path' -ErrorAction SilentlyContinue).Path
        $current = @($env:Path -split ';')
        foreach ($raw in @($machine, $user)) {
            if (-not $raw) { continue }
            foreach ($entry in ([System.Environment]::ExpandEnvironmentVariables($raw) -split ';')) {
                $e = $entry.Trim()
                if ($e -and ($current -notcontains $e)) { $env:Path = $env:Path.TrimEnd(';') + ';' + $e; $current += $e }
            }
        }
    } catch { Write-Host "  (PATH refresh from registry skipped: $($_.Exception.Message))" }
}

function Resolve-LabVIEWCLI([string]$LabVIEWExePath) {
    $cli = Get-Command 'LabVIEWCLI.exe' -ErrorAction SilentlyContinue
    if ($null -eq $cli) { $cli = Get-Command 'LabVIEWCLI' -ErrorAction SilentlyContinue }
    if ($null -ne $cli -and $cli.Source) { return $cli.Source }
    if ($LabVIEWExePath) {
        $candidate = Join-Path (Split-Path $LabVIEWExePath) 'LabVIEWCLI.exe'
        if (Test-Path -LiteralPath $candidate) { return $candidate }
    }
    return $null
}

function Resolve-LabVIEWPort([string]$LabVIEWExePath) {
    $ini = Join-Path (Split-Path -Parent $LabVIEWExePath) 'LabVIEW.ini'
    if (Test-Path -LiteralPath $ini) {
        $m = Select-String -LiteralPath $ini -Pattern '^server\.tcp\.port\s*=\s*(\d+)\s*$' -ErrorAction SilentlyContinue | Select-Object -First 1
        if ($m -and $m.Matches.Count -gt 0) { return [int]$m.Matches[0].Groups[1].Value }
    }
    return 3363
}

$LabVIEWPath = Resolve-LabVIEWPath $LabVIEWPath
Sync-PathFromRegistry
$GCli        = Resolve-Cmd @('g-cli', 'g-cli.exe')
$CliExe      = Resolve-LabVIEWCLI $LabVIEWPath
$LabVIEWPort = Resolve-LabVIEWPort $LabVIEWPath

Write-Host "=== Unit Tests (Windows) ==="
Write-Host "  Workspace : $WorkspaceRoot"
Write-Host "  Results   : $ResultsDir"
Write-Host "  LabVIEW   : $LabVIEWPath  (v$LabVIEWVersion)"
Write-Host "  VI Server : $LabVIEWPort"
Write-Host "  LabVIEWCLI: $(if ($CliExe) { $CliExe } else { '<not found>' })"
Write-Host "  g-cli     : $(if ($GCli) { $GCli } else { '<not found on PATH>' })"
Write-Host "  Config    : $ConfigPath"
Write-Host ""

# -- Minimal reader for config.unitTests.tools[] -------------------------------
# Repo convention is regex/state-machine YAML parsing (see the awk parsers in the
# container workflows). We only need this fixed, shallow shape:
#   config:
#     unitTests:
#       tools:
#         caraya:
#           enabled: true
#           command: "<optional g-cli override with {ver} {dir} {out} tokens>"
#           locations:
#             - "tests/"
#             - "**/*.vi"
#         vi-tester:
#           enabled: false
#           locations: []
# Only tools with `enabled: true` are returned. Blank/empty locations = whole project.
function Read-UnitTestTools([string]$path) {
    $tools = @()
    if (-not (Test-Path $path)) { return ,$tools }
    $inUT = $false; $inTools = $false; $inLoc = $false
    $cur = $null
    foreach ($raw in (Get-Content -LiteralPath $path)) {
        $line = ($raw -replace '\t', '    ')
        if ($line -match '^\s*unitTests:\s*$') { $inUT = $true; $inTools = $false; $inLoc = $false; $cur = $null; continue }
        if (-not $inUT) { continue }
        if ($line -match '^\s{2}tools:\s*$')   { $inTools = $true; continue }

        # A tool entry is a map key under tools:, e.g. "    caraya:" (4-space indent).
        $m = [regex]::Match($line, '^\s{4}([A-Za-z0-9_.-]+):\s*$')
        if ($inTools -and $m.Success) {
            if ($cur -and $cur.enabled) { $tools += $cur }
            $cur = [ordered]@{ tool = $m.Groups[1].Value.ToLower(); enabled = $false; command = ''; locations = @() }
            $inLoc = $false
            continue
        }
        if ($null -ne $cur) {
            if ($line -match '^\s{6}enabled:\s*(true|false)\s*$') { $cur.enabled = ($Matches[1] -eq 'true'); $inLoc = $false; continue }
            $cm = [regex]::Match($line, '^\s{6}command:\s*"?(.+?)"?\s*$')
            if ($cm.Success) { $cur.command = $cm.Groups[1].Value; $inLoc = $false; continue }
            if ($line -match '^\s{6}locations:\s*\[\s*\]\s*$') { $cur.locations = @(); $inLoc = $false; continue }
            if ($line -match '^\s{6}locations:\s*$') { $inLoc = $true; continue }
            if ($inLoc) {
                $lm = [regex]::Match($line, '^\s{8}-\s*"?([^"]+?)"?\s*$')
                if ($lm.Success) { $cur.locations += $lm.Groups[1].Value; continue }
            }
        }
        # A top-level key (column 0) ends the unitTests block.
        if ($line -match '^\S') {
            if ($cur -and $cur.enabled) { $tools += $cur }
            $cur = $null; $inUT = $false; $inTools = $false; $inLoc = $false
        }
    }
    if ($cur -and $cur.enabled) { $tools += $cur }
    return ,$tools
}

# -- Resolve a tool's locations (dir OR glob/extension) to scan-root directories -
function Resolve-TestRoots([string[]]$locations) {
    $roots = New-Object System.Collections.Generic.List[string]
    foreach ($loc in $locations) {
        if (-not $loc) { continue }
        $rel  = ($loc -replace '/', '\')
        $full = Join-Path $WorkspaceRoot $rel
        if (Test-Path -LiteralPath $full -PathType Container) {
            $roots.Add((Resolve-Path -LiteralPath $full).Path); continue
        }
        # Glob / extension: expand and take each match's parent directory.
        try {
            $matches = Get-ChildItem -Path (Join-Path $WorkspaceRoot $rel) -Recurse -File -ErrorAction SilentlyContinue
            foreach ($f in $matches) { $roots.Add($f.DirectoryName) }
        } catch { }
    }
    return ($roots | Sort-Object -Unique)
}

# -- Per-tool default g-cli command templates ---------------------------------
# Tokens: {ver}=LabVIEW year, {dir}=a resolved test-root directory, {out}=JUnit
# output path, {lv}=LabVIEW.exe path. A `command:` in the tool's config overrides
# the default. THESE DEFAULTS ARE BEST-EFFORT and must be confirmed on a real
# worker (the exact g-cli plugin name + flags per tool); the override key exists
# so that confirmation needs no script change.
$DEFAULT_CMD = @{
    # Caraya CLI g-cli extension (lvos_lib_caraya_cli_extension): run every Caraya
    # test under a directory and export JUnit. Reference implementation.
    'caraya'    = 'g-cli --lv-ver {ver} -- caraya -- --directory "{dir}" --junit "{out}"'
    # JKI VI Tester via sas_workshops_lib_vitester_for_g_cli (scaffold).
    'vi-tester' = 'g-cli --lv-ver {ver} -- vitester -- --directory "{dir}" --junit "{out}"'
}

# NI Unit Test Framework runs the .lvtest files of a PROJECT (not a flat directory of
# test VIs), so UTF has its own runner (Invoke-UtfTests) rather than the generic
# {dir} template. The default uses the FIRST-PARTY LabVIEWCLI RunUnitTests operation
# (built into the LabVIEW CLI in the container): it runs every unit test in the
# project and writes a JUnit report to -JUnitReportPath. -Headless is required for
# LabVIEW 2026 Windows containers (mirrors run-vi-analyzer / RunVIAnalyzer).
# Tokens: {cli}=LabVIEWCLI, {lv}=LabVIEW.exe, {proj}=.lvproj path, {out}=JUnit output
# path, {ver}=LabVIEW year. Override per tool with the config `command:` key.
$UTF_DEFAULT_CMD = '"{cli}" -LogToConsole TRUE -OperationName RunUnitTests -ProjectPath "{proj}" -JUnitReportPath "{out}" -LabVIEWPath "{lv}" -Headless'

# LUnit (Astemes' xUnit-style framework) is driven the SAME WAY as NI UTF: through
# the native LabVIEW CLI, not g-cli. Its `astemes_lib_lunit_cli` package registers
# the "LUnit" operation, which discovers Test Case classes under -Path and writes a
# JUnit report when -ReportPath ends in .xml. Unlike UTF, -Path accepts a directory
# (or project/class/library), so LUnit resolves test-root DIRECTORIES like the
# g-cli tools rather than .lvproj files. -Headless is required on LabVIEW 2026
# Windows containers (same constraint as UTF / VI Analyzer). Tokens: {cli}=LabVIEWCLI,
# {lv}=LabVIEW.exe, {dir}=a resolved test-root directory, {out}=JUnit output path,
# {ver}=LabVIEW year. Override per tool with the config `command:` key.
$LUNIT_DEFAULT_CMD = '"{cli}" -LogToConsole TRUE -OperationName LUnit -Path "{dir}" -ReportPath "{out}" -LabVIEWPath "{lv}" -Headless'

function Invoke-Tool($tool, [int]$index) {
    $id   = $tool.tool
    $tmpl = if ($tool.command) { $tool.command } elseif ($DEFAULT_CMD.ContainsKey($id)) { $DEFAULT_CMD[$id] } else { '' }
    $locs = @($tool.locations | Where-Object { $_ -and $_.Trim() })
    # Empty locations means "the whole project" (the config page documents this).
    $roots = if ($locs.Count -gt 0) { Resolve-TestRoots $locs } else { @($WorkspaceRoot) }

    Write-Host "--- tool: $id ---"
    if ($roots.Count -eq 0) { Write-Warning "  no test locations resolved for '$id' (locations: $($tool.locations -join ', ')) - skipping."; return }
    if (-not $tmpl)         { Write-Warning "  no headless command known for '$id' yet (set config.unitTests.tools[].command to enable) - skipping."; return }
    if (-not $GCli -and $tmpl -match '(^|\s)g-cli(\s|$)') { Write-Warning "  g-cli not found on PATH; cannot run '$id'."; return }

    $i = 0
    foreach ($dir in $roots) {
        $out = Join-Path $ResultsDir ("{0}-{1}.xml" -f $id, ($index * 100 + $i))
        $cmd = $tmpl.Replace('{ver}', $LabVIEWVersion).Replace('{dir}', $dir).Replace('{out}', $out).Replace('{lv}', $LabVIEWPath)
        Write-Host "  [$id] $cmd"
        $prevEAP = $ErrorActionPreference; $ErrorActionPreference = 'Continue'
        try {
            & cmd.exe /c $cmd 2>&1 | Out-Host
            Write-Host ("  [$id] exit={0}" -f $LASTEXITCODE)
        } catch {
            Write-Warning "  [$id] runner error: $($_.Exception.Message)"
        }
        $ErrorActionPreference = $prevEAP
        if (Test-Path -LiteralPath $out) { Write-Host "  [$id] wrote $out" }
        else { Write-Warning "  [$id] produced no JUnit at $out (check the command/plugin for this tool)." }
        $i++
    }
}

# -- NI Unit Test Framework (UTF) ---------------------------------------------
# UTF tests live as .lvtest files inside a .lvproj, and RunUnitTests runs a whole
# PROJECT, so we must resolve the tool's locations to the owning .lvproj(s). This
# is deliberately robust to however a repo is laid out - a location may be:
#   * a .lvproj path            -> run that project;
#   * a directory that CONTAINS one or more test-bearing .lvproj -> run each;
#   * a directory of .lvtest files whose .lvproj lives ABOVE it  -> walk parents
#     up to the workspace root and run the nearest owning project(s);
#   * empty                     -> discover EVERY test-bearing .lvproj anywhere in
#     the repo and run them all.
# Only projects that actually reference UTF tests are kept, so we never launch
# LabVIEW for nothing, and CI tooling folders (.github, ci-out, build, .git) are
# always excluded.
function Test-ProjHasUtfTests([string]$projPath) {
    $txt = Get-Content -LiteralPath $projPath -Raw -ErrorAction SilentlyContinue
    return [bool]($txt -and ($txt -match 'Type="TestItem"' -or $txt -match '\.lvtest'))
}

function Resolve-UtfProjects([string[]]$locations) {
    $found  = New-Object System.Collections.Generic.List[string]
    $exclRe = '(?i)[\\/](\.github|ci-out|build|\.git)[\\/]'
    $wsFull = (Resolve-Path -LiteralPath $WorkspaceRoot).Path

    # No explicit locations => discover every test-bearing project in the repo.
    $locList = @($locations | Where-Object { $_ -and $_.Trim() })
    if ($locList.Count -eq 0) {
        Get-ChildItem -LiteralPath $wsFull -Recurse -File -Filter '*.lvproj' -ErrorAction SilentlyContinue |
            Where-Object { $_.FullName -notmatch $exclRe -and (Test-ProjHasUtfTests $_.FullName) } |
            ForEach-Object { $found.Add($_.FullName) }
        return ($found | Sort-Object -Unique)
    }

    foreach ($loc in $locList) {
        $full = Join-Path $WorkspaceRoot ($loc -replace '/', '\')

        # (a) the location is itself a .lvproj -> run it directly.
        if ((Test-Path -LiteralPath $full -PathType Leaf) -and ($full -match '\.lvproj$')) {
            $found.Add((Resolve-Path -LiteralPath $full).Path)
            continue
        }

        # (b) the location is a directory or glob -> resolve to concrete roots.
        foreach ($root in @(Resolve-TestRoots @($loc))) {
            if (-not (Test-Path -LiteralPath $root)) { continue }

            # (b1) downward: test-bearing .lvproj at or under the location.
            $downward = @(Get-ChildItem -LiteralPath $root -Recurse -File -Filter '*.lvproj' -ErrorAction SilentlyContinue |
                Where-Object { $_.FullName -notmatch $exclRe -and (Test-ProjHasUtfTests $_.FullName) })
            if ($downward.Count -gt 0) {
                $downward | ForEach-Object { $found.Add($_.FullName) }
                continue
            }

            # (b2) upward: the location holds .lvtest files but the owning .lvproj
            # lives above it. Walk parents up to the workspace root and take the
            # nearest ancestor that has a test-bearing project.
            $hasTests = @(Get-ChildItem -LiteralPath $root -Recurse -File -Filter '*.lvtest' -ErrorAction SilentlyContinue).Count -gt 0
            if (-not $hasTests) { continue }
            $dir = (Resolve-Path -LiteralPath $root).Path
            while ($dir) {
                $up = @(Get-ChildItem -LiteralPath $dir -File -Filter '*.lvproj' -ErrorAction SilentlyContinue |
                    Where-Object { $_.FullName -notmatch $exclRe -and (Test-ProjHasUtfTests $_.FullName) })
                if ($up.Count -gt 0) { $up | ForEach-Object { $found.Add($_.FullName) }; break }
                if ($dir -eq $wsFull) { break }
                $parent = Split-Path -Parent $dir
                if (-not $parent -or $parent -eq $dir -or $parent.Length -lt $wsFull.Length) { break }
                $dir = $parent
            }
        }
    }

    return ($found | Sort-Object -Unique)
}

function Invoke-UtfTests($tool, [int]$index) {
    $id = $tool.tool
    Write-Host "--- tool: $id (NI Unit Test Framework) ---"
    $projects = @(Resolve-UtfProjects $tool.locations)
    if ($projects.Count -eq 0) {
        Write-Warning "  no UTF project (.lvproj containing .lvtest) found for locations: $($tool.locations -join ', ') - skipping."
        return
    }
    if (-not $CliExe) { Write-Warning "  LabVIEWCLI not found; cannot run UTF."; return }

    Show-UtfAddonsDiag $LabVIEWPath

    $tmpl = if ($tool.command) { $tool.command } else { $UTF_DEFAULT_CMD }

    $i = 0
    foreach ($proj in $projects) {
        $out = Join-Path $ResultsDir ("utf-{0}.xml" -f ($index * 100 + $i))
        Write-Host "  [utf] project: $proj"

        # RunUnitTests writes the JUnit report directly to -JUnitReportPath ({out}).
        $cmd = $tmpl.Replace('{cli}', $CliExe).Replace('{lv}', $LabVIEWPath).Replace('{proj}', $proj).Replace('{out}', $out).Replace('{ver}', $LabVIEWVersion)
        Write-Host "  [utf] $cmd"

        $prevEAP = $ErrorActionPreference; $ErrorActionPreference = 'Continue'
        $cliOut = ''
        try {
            $cliOut = (& cmd.exe /c $cmd 2>&1 | Out-String)
            Write-Host $cliOut
            Write-Host ("  [utf] exit={0}" -f $LASTEXITCODE)
        } catch {
            Write-Warning "  [utf] runner error: $($_.Exception.Message)"
        }
        $ErrorActionPreference = $prevEAP

        if (Test-Path -LiteralPath $out) { Write-Host "  [utf] wrote $out" }
        else {
            Write-Warning "  [utf] produced no JUnit at $out (check the RunUnitTests output above; override with the tool's command: key)."
            # The LabVIEWCLI console error (e.g. -350053) is generic; the actual
            # detail (which VI is broken / which module is missing) is written to
            # the CLI's own session log. Echo it so failures are diagnosable.
            $m = [regex]::Match($cliOut, '(?i)started logging in file:\s*(.+\.log)')
            if ($m.Success) {
                $logPath = $m.Groups[1].Value.Trim()
                Write-Host "  [utf] --- LabVIEW CLI session log ($logPath) ---"
                if (Test-Path -LiteralPath $logPath) {
                    Get-Content -LiteralPath $logPath | ForEach-Object { Write-Host "  [utf-log] $_" }
                } else {
                    Write-Host "  [utf] (session log not found on disk)"
                }
                Write-Host "  [utf] --- end LabVIEW CLI session log ---"
            } else {
                Write-Host "  [utf] (no CLI session-log path found in output)"
            }
            # DIAGNOSTIC PROBE: re-run the SAME operation WITHOUT -JUnitReportPath. If it
            # then loads/succeeds, the -350053 is specific to the JUnit-report step (its
            # VIs), not the operation; if it still fails, the RunUnitTests operation cannot
            # load at all in this LabVIEW. Output-only; does not affect the report.
            $cmdNoJUnit = $tmpl.Replace('{cli}', $CliExe).Replace('{lv}', $LabVIEWPath).Replace('{proj}', $proj).Replace('{out}', $out).Replace('{ver}', $LabVIEWVersion)
            $cmdNoJUnit = $cmdNoJUnit -replace '\s*-JUnitReportPath\s+"[^"]*"', ''
            Write-Host "  [utf][diag] retry WITHOUT -JUnitReportPath:"
            Write-Host "  [utf][diag] $cmdNoJUnit"
            $prevEAP2 = $ErrorActionPreference; $ErrorActionPreference = 'Continue'
            try {
                $diagOut = (& cmd.exe /c $cmdNoJUnit 2>&1 | Out-String)
                Write-Host $diagOut
                Write-Host ("  [utf][diag] exit={0}" -f $LASTEXITCODE)
            } catch { Write-Warning "  [utf][diag] runner error: $($_.Exception.Message)" }
            $ErrorActionPreference = $prevEAP2
            # Record that UTF could not run, so the report shows the shared
            # "missing container tooling" banner. -350053 / "missing or bad files"
            # / "required modules or toolkits" => the UTF toolkit is absent.
            if (-not ($Script:ToolingIssues | Where-Object { $_.tool -eq 'utf' })) {
                $missingTooling = ($cliOut -match '350053' -or $cliOut -match 'missing or bad files' -or $cliOut -match 'required modules or toolkits')
                if ($missingTooling) {
                    Add-ToolingIssue 'utf' 'NI Unit Test Framework' 'missing-tooling' 'The NI Unit Test Framework toolkit is not installed in this container, so the LabVIEW CLI RunUnitTests operation could not load (error -350053).'
                } else {
                    Add-ToolingIssue 'utf' 'NI Unit Test Framework' 'error' 'The RunUnitTests operation produced no JUnit output.'
                }
            }
        }
        $i++
    }
}

# -- LUnit (Astemes) ----------------------------------------------------------
# LUnit tests are Test Case classes (.lvclass) discovered under a directory or
# project. We resolve the tool's locations to test-root DIRECTORIES (empty =
# whole project) and run the native LabVIEWCLI "LUnit" operation against each,
# writing one JUnit XML per root. Mirrors Invoke-UtfTests' diagnostics: it echoes
# the LabVIEW CLI session log on failure and records a missing-tooling finding so
# a worker without the astemes_lib_lunit_cli package surfaces the shared
# "missing container tooling" banner (instead of a bare "no tests found").
function Invoke-LUnitTests($tool, [int]$index) {
    $id = $tool.tool
    Write-Host "--- tool: $id (LUnit) ---"
    if (-not $CliExe) { Write-Warning "  LabVIEWCLI not found; cannot run LUnit."; return }

    $locs  = @($tool.locations | Where-Object { $_ -and $_.Trim() })
    $roots = if ($locs.Count -gt 0) { Resolve-TestRoots $locs } else { @($WorkspaceRoot) }
    if ($roots.Count -eq 0) {
        Write-Warning "  no test locations resolved for '$id' (locations: $($tool.locations -join ', ')) - skipping."
        return
    }

    $tmpl = if ($tool.command) { $tool.command } else { $LUNIT_DEFAULT_CMD }

    $i = 0
    foreach ($dir in $roots) {
        $out = Join-Path $ResultsDir ("lunit-{0}.xml" -f ($index * 100 + $i))
        Write-Host "  [lunit] path: $dir"

        $cmd = $tmpl.Replace('{cli}', $CliExe).Replace('{lv}', $LabVIEWPath).Replace('{dir}', $dir).Replace('{out}', $out).Replace('{ver}', $LabVIEWVersion)
        Write-Host "  [lunit] $cmd"

        $prevEAP = $ErrorActionPreference; $ErrorActionPreference = 'Continue'
        $cliOut = ''
        try {
            $cliOut = (& cmd.exe /c $cmd 2>&1 | Out-String)
            Write-Host $cliOut
            Write-Host ("  [lunit] exit={0}" -f $LASTEXITCODE)
        } catch {
            Write-Warning "  [lunit] runner error: $($_.Exception.Message)"
        }
        $ErrorActionPreference = $prevEAP

        if (Test-Path -LiteralPath $out) { Write-Host "  [lunit] wrote $out" }
        else {
            Write-Warning "  [lunit] produced no JUnit at $out (check the LUnit output above; override with the tool's command: key)."
            # Echo the LabVIEW CLI session log (the console error is generic; the
            # real detail lives in the CLI's own log), same as Invoke-UtfTests.
            $m = [regex]::Match($cliOut, '(?i)started logging in file:\s*(.+\.log)')
            if ($m.Success) {
                $logPath = $m.Groups[1].Value.Trim()
                Write-Host "  [lunit] --- LabVIEW CLI session log ($logPath) ---"
                if (Test-Path -LiteralPath $logPath) {
                    Get-Content -LiteralPath $logPath | ForEach-Object { Write-Host "  [lunit-log] $_" }
                } else {
                    Write-Host "  [lunit] (session log not found on disk)"
                }
                Write-Host "  [lunit] --- end LabVIEW CLI session log ---"
            } else {
                Write-Host "  [lunit] (no CLI session-log path found in output)"
            }
            # -350053 / "missing or bad files" / "required modules or toolkits" =>
            # the LUnit CLI add-on (astemes_lib_lunit_cli) is not installed in this
            # container, so the "LUnit" operation could not load.
            if (-not ($Script:ToolingIssues | Where-Object { $_.tool -eq 'lunit' })) {
                $missingTooling = ($cliOut -match '350053' -or $cliOut -match 'missing or bad files' -or $cliOut -match 'required modules or toolkits')
                if ($missingTooling) {
                    Add-ToolingIssue 'lunit' 'LUnit' 'missing-tooling' 'The LUnit CLI toolkit (astemes_lib_lunit_cli) is not installed in this container, so the LabVIEW CLI LUnit operation could not load (error -350053).'
                } else {
                    Add-ToolingIssue 'lunit' 'LUnit' 'error' 'The LUnit operation produced no JUnit output.'
                }
            }
        }
        $i++
    }
}

# -- Main ---------------------------------------------------------------------
# actions/unit-tests passes the framework selection as ENVIRONMENT VARIABLES
# (docker --env-file) rather than as -Framework/-TestDir/-Command arguments,
# because Windows PowerShell 5.1 mangles such values on a native command line: an
# empty one is dropped (breaking parameter binding) and embedded double quotes -
# which every default command template contains, e.g. --junit "{out}" - are
# stripped. An explicit parameter still wins, so the script stays directly
# invokable with -Framework for local runs and other callers.
if (-not $Framework -and $env:LVCI_FRAMEWORK) { $Framework = $env:LVCI_FRAMEWORK }
if (-not $TestDir   -and $env:LVCI_TEST_DIR)  { $TestDir   = $env:LVCI_TEST_DIR }
if (-not $Command   -and $env:LVCI_COMMAND)   { $Command   = $env:LVCI_COMMAND }

# -Framework runs a single tool straight from parameters (no config file / YAML
# parsing involved), which is what actions/unit-tests uses. Otherwise fall back
# to the repo's own config.unitTests.tools[] as before.
if ($Framework) {
    $tools = @([ordered]@{ tool = $Framework.ToLower(); enabled = $true; command = $Command; locations = @($TestDir) })
} else {
    $tools = Read-UnitTestTools $ConfigPath
}
if (-not $tools -or $tools.Count -eq 0) {
    Write-Warning "No config.unitTests.tools configured in $ConfigPath - nothing to run."
    Write-Host "Wrote 0 JUnit file(s) to $ResultsDir."
    exit 0
}

Write-Host ("Configured tools: {0}" -f (($tools | ForEach-Object { $_.tool }) -join ', '))
Write-Host ""

$idx = 0
foreach ($t in $tools) {
    if ($t.tool -eq 'utf') { Invoke-UtfTests $t $idx }
    elseif ($t.tool -eq 'lunit') { Invoke-LUnitTests $t $idx }
    else { Invoke-Tool $t $idx }
    $idx++; Write-Host ""
}

$xml = @(Get-ChildItem -Path $ResultsDir -Filter '*.xml' -File -ErrorAction SilentlyContinue)
Write-Host "=== Unit Tests finished: wrote $($xml.Count) JUnit file(s) to $ResultsDir ==="
# Persist any "container is missing this tooling" findings for the report builder.
$toolingPath = Join-Path $ResultsDir '_tooling.json'
if ($Script:ToolingIssues.Count -gt 0) {
    (@{ missing = @($Script:ToolingIssues) } | ConvertTo-Json -Depth 5) | Set-Content -LiteralPath $toolingPath -Encoding ascii
    Write-Host "Recorded $($Script:ToolingIssues.Count) missing-tooling finding(s) -> $toolingPath"
}
# Always exit 0: pass/fail is derived from the JUnit content by
# build-unittest-report.py (its summary.json drives the commit status), exactly
# like the Mass Compile report. A runner-level error surfaces as a missing/empty
# report, not a hard CI failure here.
exit 0
