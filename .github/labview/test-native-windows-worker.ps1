param(
    [string] $Root = (Split-Path -Parent (Split-Path -Parent $PSScriptRoot)),
    [string] $Python = 'python',
    [string] $OutputDirectory = '',
    [switch] $FixtureOnly
)

$ErrorActionPreference = 'Stop'
$PSNativeCommandUseErrorActionPreference = $false
$Root = (Resolve-Path -LiteralPath $Root).Path
if (-not $OutputDirectory) {
    $OutputDirectory = Join-Path $Root $(if ($FixtureOnly) { 'ci-out/native-windows-fixture' } else { 'ci-out/native-windows' })
}
$OutputDirectory = [IO.Path]::GetFullPath($OutputDirectory)
New-Item -ItemType Directory -Force -Path $OutputDirectory | Out-Null
$scratchParent = if ($env:RUNNER_TEMP) { $env:RUNNER_TEMP } else { [IO.Path]::GetTempPath() }
$context = Join-Path $scratchParent ('lvci-native-windows-' + [guid]::NewGuid().ToString('N'))
$containerNames = @()

function Invoke-Checked {
    param([string] $Executable, [string[]] $Arguments, [string] $Log)
    if ($Log) {
        & $Executable @Arguments 2>&1 | Tee-Object -FilePath $Log | Out-Host
    } else {
        & $Executable @Arguments | Out-Host
    }
    $commandExit = $LASTEXITCODE
    if ($commandExit -ne 0) { throw "$Executable failed with exit $commandExit : $($Arguments -join ' ')" }
}

function Assert-RequiredInventory {
    param([string] $Path, [string[]] $Specs)
    $inventory = Get-Content -LiteralPath $Path -Raw | ConvertFrom-Json
    if ($inventory -isnot [pscustomobject] -or $inventory.schema -cne 1 -or
        $inventory.platform -cne 'windows' -or $inventory.source -cne 'vipm list --installed' -or
        $inventory.labview_version -cne '2026' -or $inventory.labview_bitness -cne '64' -or
        $inventory.status -cne 'observed' -or $inventory.target_verified -isnot [bool] -or
        -not $inventory.target_verified -or $inventory.required_project -isnot [bool] -or
        -not $inventory.required_project -or -not $inventory.packages.Count) {
        throw 'Required observed Windows 2026/64 project inventory is missing or invalid.'
    }
    foreach ($spec in $Specs) {
        $request = Split-VipmPackageSpec $spec
        if (-not $request.Version -or $request.Minimum) { throw "Fixture must use exact pins: $spec" }
        $matches = @($inventory.packages | Where-Object {
            $_.name -ieq $request.Name -and $_.version -ceq $request.Version
        })
        if ($matches.Count -ne 1) { throw "Required exact pin not observed: $spec" }
    }
}

try {
    Push-Location $Root
    try { $sha = (& git rev-parse HEAD).Trim(); $gitExit = $LASTEXITCODE } finally { Pop-Location }
    if ($gitExit -ne 0 -or $sha -notmatch '^[a-f0-9]{40}$') { throw 'Cannot identify exact candidate commit.' }
    $tag = 'lvci-native-windows:' + $sha
    $version = 'native-win-' + $sha.Substring(0, 12)
    New-Item -ItemType Directory -Force -Path "$context/.github/docker", "$context/.github/workflows", "$context/.fixture/.github", "$context/.fixture/example" | Out-Null
    Copy-Item -LiteralPath "$Root/.github/labview" -Destination "$context/.github/labview" -Recurse
    Copy-Item -LiteralPath "$Root/.github/docker/labview-ci.Dockerfile" -Destination "$context/.github/docker/labview-ci.Dockerfile"
    Copy-Item -LiteralPath "$Root/.github/workflows/build-labview-image.yml" -Destination "$context/.github/workflows/build-labview-image.yml"
    Copy-Item -LiteralPath "$Root/.github/workflows/run-antidoc-windows-container.yml" -Destination "$context/.github/workflows/run-antidoc-windows-container.yml"
    Copy-Item -LiteralPath "$Root/example/Dependencies.vipc", "$Root/example/Example.dragon" -Destination "$context/.fixture/example"
    Copy-Item -LiteralPath "$Root/.github/labview-ci.yml" -Destination "$OutputDirectory/source-labview-ci.yml"
    $fixture = @'
schemaVersion: 1
config:
  labviewVersion: "2026"
  os: [windows]
  container:
    vipc:
      - path: example/Dependencies.vipc
        monitor: true
        monitorOn: [windows]
    dragon:
      - path: example/Example.dragon
        monitor: true
        monitorOn: [windows]
'@
    $fixture | Set-Content -LiteralPath "$context/.fixture/.github/labview-ci.yml" -Encoding utf8NoBOM
    $fixture | Set-Content -LiteralPath "$OutputDirectory/fixture-labview-ci.yml" -Encoding utf8NoBOM
    $workflow = Get-Content -LiteralPath "$context/.github/workflows/build-labview-image.yml" -Raw
    $stage = [regex]::Match($workflow, '(?ms)^      - name: Stage repo VIPC(?: and Dragon)? files\r?\n.*?^          python -I - <<''PY''\r?\n(?<body>.*?)^          PY\s*$')
    if (-not $stage.Success) { throw 'Trusted production VIPC staging Python could not be located.' }
    $stageCode = ($stage.Groups['body'].Value -split '\r?\n' | ForEach-Object { $_ -replace '^          ', '' }) -join "`n"
    $stagePath = Join-Path $context 'production-stage.py'
    $stageCode | Set-Content -LiteralPath $stagePath -Encoding utf8NoBOM
    $savedEnvironment = @{}
    foreach ($key in @('DEPENDENCY_ROOT', 'DEFAULT_TAG', 'GITHUB_OUTPUT')) {
        $savedEnvironment[$key] = [Environment]::GetEnvironmentVariable($key)
    }
    Push-Location $context
    try {
        $env:DEPENDENCY_ROOT = '.fixture'
        $env:DEFAULT_TAG = '2026'
        $env:GITHUB_OUTPUT = Join-Path $OutputDirectory 'staging-output.txt'
        Invoke-Checked $Python @('-I', $stagePath) (Join-Path $OutputDirectory 'staging.log')
    } finally {
        Pop-Location
        foreach ($key in $savedEnvironment.Keys) { [Environment]::SetEnvironmentVariable($key, $savedEnvironment[$key]) }
    }
    $vipm = Join-Path $context '.github/labview/vipm'
    $selected = Get-Content -LiteralPath "$vipm/selected-stage.json" -Raw | ConvertFrom-Json
    if (@($selected.PSObject.Properties).Count -ne 2 -or
        $selected.'dependencies.vipc' -cne '.fixture/example/Dependencies.vipc' -or
        @(Get-ChildItem -LiteralPath $vipm -Filter '*.dragon' -Recurse).Count -ne 1 -or
        -not (Test-Path -LiteralPath "$vipm/ci-tooling.vipc")) {
        throw 'Staging must include the example VIPC, Dragon, Antidoc, and preserved tooling baseline.'
    }
    if ((Get-FileHash "$Root/example/Dependencies.vipc").Hash -ne (Get-FileHash "$vipm/Dependencies.vipc").Hash -or
        (Get-FileHash "$Root/.github/labview/vipm/ci-tooling.vipc").Hash -ne (Get-FileHash "$vipm/ci-tooling.vipc").Hash) {
        throw 'Staging changed the selected VIPC or tooling baseline.'
    }
    Copy-Item -LiteralPath "$vipm/selected-stage.json", "$vipm/dragon-plan.json", "$vipm/Dependencies.vipc", "$vipm/ci-tooling.vipc" -Destination $OutputDirectory
    $parseErrors = $null
    $installerAst = [System.Management.Automation.Language.Parser]::ParseFile("$vipm/install-vipc.ps1", [ref]$null, [ref]$parseErrors)
    if ($parseErrors.Count) { throw ($parseErrors | Out-String) }
    $functionNames = @('Get-VipcPackageSpecs', 'Split-VipmPackageSpec', 'Find-VipmExecutable',
        'Test-VipmSpecsInstalled', 'Test-VipmPackageVersion', 'Get-NumericVersionKey', 'Assert-FinalWindowsProjectInventory',
        'New-PublicRepoWorkdir', 'Get-WindowsDragonPlan', 'Resolve-WindowsSelectedSpecs')
    $functions = @($installerAst.FindAll({
        param($node)
        $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $node.Name -in $functionNames
    }, $true))
    if ($functions.Count -ne $functionNames.Count) { throw 'Required trusted installer functions were not found.' }
    foreach ($definition in $functions) { . ([scriptblock]::Create($definition.Extent.Text)) }
    $dragon = Get-WindowsDragonPlan -Directory $vipm -TargetYear '2026' -TargetBitness '64'
    $specs = @(@(Get-ChildItem $vipm -Filter '*.vipc' -File | ForEach-Object {
        Get-VipcPackageSpecs $_.FullName
    }) + @($dragon.Specs) | Sort-Object -Unique)
    if (-not $specs.Count) { throw 'Selected example VIPC has no required packages.' }
    $specs | Set-Content -LiteralPath "$OutputDirectory/required-pins.txt" -Encoding utf8NoBOM
    Write-Host ('Exact required project pins: ' + ($specs -join ', '))
    $sourcePaths = @('.github/docker/labview-ci.Dockerfile', '.github/workflows/build-labview-image.yml',
        '.github/labview-ci.yml', '.github/labview/dragon_deps.py', '.github/labview/build-worker-manifest.py',
        '.github/labview/test-native-windows-worker.ps1', '.github/labview/vipm/install-vipc.ps1',
        '.github/labview/vipm/ci-tooling.vipc', '.github/labview/antidoc/antidoc.vipc',
        'example/Dependencies.vipc', 'example/Example.dragon')
    $hashes = [ordered]@{}
    foreach ($path in $sourcePaths) { $hashes[$path] = (Get-FileHash -LiteralPath (Join-Path $Root $path) -Algorithm SHA256).Hash.ToLowerInvariant() }
    $evidence = [ordered]@{
        commit = $sha; mode = $(if ($FixtureOnly) { 'synthetic-fixture-only' } else { 'native-build' })
        selected_source = 'example/Dependencies.vipc'; selected_platforms = @('windows')
        dragons = @('example/Example.dragon'); tooling_baseline_preserved = $true; source_sha256 = $hashes
        fixture_config_sha256 = (Get-FileHash "$OutputDirectory/fixture-labview-ci.yml").Hash.ToLowerInvariant()
        staging_python_sha256 = (Get-FileHash $stagePath).Hash.ToLowerInvariant()
    }
    $evidence | ConvertTo-Json -Depth 8 | Set-Content "$OutputDirectory/provenance.json" -Encoding utf8NoBOM
    $inventoryPath = Join-Path $OutputDirectory 'observed-inventory.json'
    $baseImage = $env:WINDOWS_BASE_IMAGE
    $seedDigest = ''
    $runtimeCode = ($functions | ForEach-Object { $_.Extent.Text }) -join "`n"
    $runtimeCode += @'

$ErrorActionPreference = 'Stop'
$LabVIEWVersion = '2026'
$LabVIEWBitness = '64'
$GlobalFlags = @('--labview-version', '2026', '--labview-bitness', '64')
$VipcDir = 'C:\vipm'
$VipmExe = Find-VipmExecutable -Kind CLI
if (-not $VipmExe) { throw 'Installed VIPM CLI not found.' }
Write-Host "Runtime CLI: $VipmExe"
$env:VIPM_COMMUNITY_EDITION = 'true'
$env:VIPM_NONINTERACTIVE = '1'
$env:VIPM_ASSUME_YES = '1'
$env:NO_COLOR = '1'
foreach ($gitDir in @('C:\git\cmd', 'C:\Program Files\Git\cmd')) {
    if ((Test-Path (Join-Path $gitDir 'git.exe')) -and ($Env:Path -notlike "*$gitDir*")) {
        $Env:Path = "$gitDir;$Env:Path"
    }
}
if (-not (Get-Command git -ErrorAction SilentlyContinue)) { throw 'Real Git is required for runtime validation.' }
if ($env:VIPM_PUBLIC_REPO_URL -cne 'https://github.com/ni/LabVIEW-CI-CD-Toolkit.git' -or
    $env:VIPM_PUBLIC_REPO_COMMIT -notmatch '^[a-f0-9]{40}$') { throw 'Expected public source URL and exact candidate commit.' }
$previousLocation = Get-Location
$installWorkdir = $null
Remove-Item -LiteralPath "$VipcDir/observed-inventory.json" -Force -ErrorAction Stop
try {
    $installWorkdir = New-PublicRepoWorkdir $env:VIPM_PUBLIC_REPO_URL
    Set-Location $installWorkdir
    $head = & git rev-parse --verify HEAD 2>&1
    if ($LASTEXITCODE -ne 0) { throw 'Public repository bootstrap did not produce a genuine commit.' }
    $origin = & git remote get-url origin
    if ($LASTEXITCODE -ne 0 -or "$origin".Trim() -cne $env:VIPM_PUBLIC_REPO_URL) { throw 'Unexpected public origin.' }
    & git fetch --depth 1 origin $env:VIPM_PUBLIC_REPO_COMMIT
    if ($LASTEXITCODE -ne 0) { throw 'Cannot fetch the exact public candidate commit.' }
    & git checkout --detach FETCH_HEAD
    if ($LASTEXITCODE -ne 0) { throw 'Cannot check out the exact public candidate commit.' }
    $head = & git rev-parse --verify HEAD
    if ($LASTEXITCODE -ne 0 -or "$head".Trim() -cne $env:VIPM_PUBLIC_REPO_COMMIT) { throw 'Public context commit mismatch.' }
    if ((Get-FileHash 'example/Dependencies.vipc').Hash -cne (Get-FileHash "$VipcDir/Dependencies.vipc").Hash) {
        throw 'Public checkout does not contain the trusted staged dependency fixture.'
    }
    Write-Host "Runtime public context: $origin at $head"
    $dragon = Get-WindowsDragonPlan -Directory $VipcDir -TargetYear '2026' -TargetBitness '64'
    Assert-FinalWindowsProjectInventory -VipcFiles @(Get-ChildItem $VipcDir -Filter '*.vipc' -File) -DragonSpecs $dragon.Specs
} finally {
    $script:LastVipmInventoryRaw | Set-Content -LiteralPath "$VipcDir/runtime-inventory-raw.txt" -Encoding UTF8
    Set-Location $previousLocation
    if ($installWorkdir) { Remove-Item -LiteralPath $installWorkdir -Recurse -Force }
}
'@
    $runtimePath = Join-Path $OutputDirectory 'native-runtime.ps1'
    $runtimeCode | Set-Content -LiteralPath $runtimePath -Encoding utf8NoBOM
    $runtimeErrors = $null
    [void][System.Management.Automation.Language.Parser]::ParseFile($runtimePath, [ref]$null, [ref]$runtimeErrors)
    if ($runtimeErrors.Count) { throw ($runtimeErrors | Out-String) }
    $publicEnvironment = @('--env', 'VIPM_PUBLIC_REPO_URL=https://github.com/ni/LabVIEW-CI-CD-Toolkit.git',
        '--env', "VIPM_PUBLIC_REPO_COMMIT=$sha", '--env', 'CI=true', '--env', 'GITHUB_ACTIONS=true',
        '--env', 'GITHUB_SERVER_URL=https://github.com', '--env', 'GITHUB_REPOSITORY=ni/LabVIEW-CI-CD-Toolkit',
        '--env', 'GITHUB_REPOSITORY_OWNER=ni')
    if ($FixtureOnly) {
        $packages = @($specs | ForEach-Object {
            $request = Split-VipmPackageSpec $_
            if (-not $request.Version -or $request.Minimum) { throw "Expected exact fixture pin: $_" }
            [ordered]@{ name = $request.Name; version = $request.Version }
        })
        [ordered]@{
            schema = 1; platform = 'windows'; source = 'vipm list --installed'
            labview_version = '2026'; labview_bitness = '64'; status = 'observed'
            target_verified = $true; required_project = $true
            raw = 'SYNTHETIC LOCAL FIXTURE ONLY - not native build evidence'; packages = $packages
        } | ConvertTo-Json -Depth 8 | Set-Content $inventoryPath -Encoding utf8NoBOM
    } else {
        if (-not $IsWindows -or [Runtime.InteropServices.RuntimeInformation]::OSArchitecture.ToString() -ne 'X64') {
            throw 'Native validation requires Windows x64, using PowerShell 7 on windows-2022.'
        }
        $hostInfo = [ordered]@{
            os = Get-CimInstance Win32_OperatingSystem | Select-Object Caption, Version, OSArchitecture, TotalVisibleMemorySize, FreePhysicalMemory
            disks = @(Get-CimInstance Win32_LogicalDisk -Filter 'DriveType=3' | Select-Object DeviceID, Size, FreeSpace)
        }
        $hostInfo | ConvertTo-Json -Depth 5 | Tee-Object -FilePath "$OutputDirectory/host-before.json" | Out-Host
        if ((Get-PSDrive C).Free -lt 35GB -and $env:RUNNER_TOOL_CACHE -and $env:GITHUB_ACTIONS -eq 'true') {
            $toolCache = [IO.Path]::GetFullPath($env:RUNNER_TOOL_CACHE)
            $pythonPath = (Get-Command $Python -ErrorAction Stop).Source
            foreach ($tool in @('CodeQL', 'Java', 'Ruby', 'go', 'node', 'PyPy')) {
                $unused = Join-Path $toolCache $tool
                if ($pythonPath.StartsWith($unused, [StringComparison]::OrdinalIgnoreCase)) { continue }
                if (Test-Path -LiteralPath $unused) {
                    Write-Host "Freeing unused preinstalled tool cache: $unused"
                    Remove-Item -LiteralPath $unused -Recurse -Force
                }
            }
        }
        Get-PSDrive C | Select-Object Name, Used, Free | Out-String | Tee-Object -FilePath "$OutputDirectory/disk-after-cleanup.txt" | Out-Host
        & docker info --format '{{.OSType}}' 2>&1 | Out-Null
        if ($LASTEXITCODE -ne 0) {
            & "$Root/.github/labview/ensure-docker.ps1"
            $ErrorActionPreference = 'Stop'
        }
        $dockerOS = & docker info --format '{{.OSType}}'
        if ($LASTEXITCODE -ne 0 -or "$dockerOS".Trim() -cne 'windows') { throw 'Docker must be running in Windows container mode.' }
        Invoke-Checked docker @('info') "$OutputDirectory/docker-info.txt"
        if (-not $baseImage) {
            $defaultBase = [regex]::Match((Get-Content "$context/.github/docker/labview-ci.Dockerfile" -Raw), '(?m)^ARG LCWC_BASE_IMAGE=(\S+)')
            if (-not $defaultBase.Success) { throw 'Candidate Dockerfile does not declare LCWC_BASE_IMAGE.' }
            $baseImage = $defaultBase.Groups[1].Value
        }
        Invoke-Checked docker @('pull', $baseImage) "$OutputDirectory/seed-pull.log"
        $seedJson = & docker image inspect $baseImage
        if ($LASTEXITCODE -ne 0) { throw 'Seed inspect failed.' }
        $seedJson | Set-Content "$OutputDirectory/seed-inspect.json" -Encoding utf8NoBOM
        $seed = @($seedJson | ConvertFrom-Json)[0]
        $pinnedSeed = @($seed.RepoDigests)[0]
        if ($pinnedSeed -notmatch '@(sha256:[a-f0-9]{64})$' -or $seed.Os -cne 'windows' -or $seed.Architecture -cne 'amd64') {
            throw 'Seed must have an immutable registry digest and Windows amd64 platform.'
        }
        $seedDigest = $Matches[1]
        $evidence['seed_image'] = $baseImage
        $evidence['seed_digest'] = $seedDigest
        $evidence['pinned_seed'] = $pinnedSeed
        $evidence | ConvertTo-Json -Depth 8 | Set-Content "$OutputDirectory/provenance.json" -Encoding utf8NoBOM
        Invoke-Checked docker @('build', '--no-cache', '--memory', '8g', '--build-arg', "LCWC_BASE_IMAGE=$pinnedSeed",
            '--build-arg', "CI_WORKER_VERSION=$version", '--build-arg', 'LABVIEW_VERSION=2026',
            '--build-arg', 'LABVIEW_BITNESS=64', '--build-arg', 'LVCI_UNIT_TESTS_INSTALLED=true',
            '--build-arg', 'VIPM_PUBLIC_REPO_URL=https://github.com/ni/LabVIEW-CI-CD-Toolkit.git',
            '--tag', $tag, '--file', "$context/.github/docker/labview-ci.Dockerfile", $context) "$OutputDirectory/build.log"
        Invoke-Checked docker @('image', 'inspect', $tag) "$OutputDirectory/worker-inspect.json"
        $stopped = 'lvci-native-extract-' + [guid]::NewGuid().ToString('N')
        $containerNames += $stopped
        Invoke-Checked docker @('create', '--name', $stopped, $tag, 'cmd', '/c', 'exit', '0')
        Invoke-Checked docker @('cp', "${stopped}:C:/vipm/observed-inventory.json", $inventoryPath)
        Assert-RequiredInventory $inventoryPath $specs
        $runtime = 'lvci-native-runtime-' + [guid]::NewGuid().ToString('N')
        $containerNames += $runtime
        Invoke-Checked docker (@('create', '--name', $runtime, '--memory', '8g', '--entrypoint', 'powershell') +
            $publicEnvironment + @($tag, '-NoLogo', '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass', '-File', 'C:/vipm/native-runtime.ps1'))
        Invoke-Checked docker @('cp', $runtimePath, "${runtime}:C:/vipm/native-runtime.ps1")
        & docker start --attach $runtime 2>&1 | Tee-Object -FilePath "$OutputDirectory/runtime-vipm.log" | Out-Host
        $runtimeStartExit = $LASTEXITCODE
        $runtimeExit = & docker inspect --format '{{.State.ExitCode}}' $runtime
        if ($LASTEXITCODE -ne 0) { throw 'Cannot inspect native runtime exit code.' }
        Invoke-Checked docker @('cp', "${runtime}:C:/vipm/runtime-inventory-raw.txt", "$OutputDirectory/runtime-inventory-raw.txt")
        if ($runtimeStartExit -ne 0) { throw "Native runtime attach failed with exit $runtimeStartExit (container exit $runtimeExit)" }
        if ("$runtimeExit".Trim() -cne '0') { throw "Native runtime exited with $runtimeExit" }
        $runtimeInventory = Join-Path $OutputDirectory 'runtime-observed-inventory.json'
        Invoke-Checked docker @('cp', "${runtime}:C:/vipm/observed-inventory.json", $runtimeInventory)
        Assert-RequiredInventory $runtimeInventory $specs
    }
    Assert-RequiredInventory $inventoryPath $specs
    $manifestArguments = @('-I', "$context/.github/labview/build-worker-manifest.py", '--platform', 'windows',
        '--version', $version, '--labview-version', '2026', '--labview-bitness', '64',
        '--git-sha', $sha, '--dependency-sha', $sha, '--observed-inventory', $inventoryPath,
        '--worker-layer', 'repo-vipc-layer', '--image-ref', $tag, '--out-dir', $OutputDirectory,
        '--vipc', "$vipm/Dependencies.vipc", '--vipc', "$vipm/ci-tooling.vipc", '--vipc', "$vipm/antidoc.vipc")
    if ($baseImage) { $manifestArguments += @('--base-image', $baseImage, '--seed-image', $baseImage) }
    if ($seedDigest) { $manifestArguments += @('--base-digest', $seedDigest, '--seed-digest', $seedDigest) }
    if ($env:GITHUB_SERVER_URL -and $env:GITHUB_REPOSITORY -and $env:GITHUB_RUN_ID) {
        $manifestArguments += @('--run-url', "$env:GITHUB_SERVER_URL/$env:GITHUB_REPOSITORY/actions/runs/$env:GITHUB_RUN_ID")
    }
    Invoke-Checked $Python $manifestArguments "$OutputDirectory/manifest.log"
    $manifest = Get-Content "$OutputDirectory/manifest.json" -Raw | ConvertFrom-Json
    if ($manifest.schema -cne 3 -or $manifest.git_sha -cne $sha -or $manifest.dependency_sha -cne $sha) {
        throw 'Manifest must be schema 3 and bound to the exact candidate commit.'
    }
    if ($FixtureOnly) {
        $badPath = Join-Path $context 'bad-inventory.json'
        foreach ($field in @('status', 'target_verified', 'required_project', 'labview_version', 'labview_bitness', 'pin')) {
            $bad = Get-Content $inventoryPath -Raw | ConvertFrom-Json
            switch ($field) {
                'status' { $bad.status = 'unavailable' }
                'target_verified' { $bad.target_verified = $false }
                'required_project' { $bad.required_project = $false }
                'labview_version' { $bad.labview_version = '2025' }
                'labview_bitness' { $bad.labview_bitness = '32' }
                'pin' { $bad.packages[0].version = '0.0.0' }
            }
            $bad | ConvertTo-Json -Depth 8 | Set-Content $badPath -Encoding utf8NoBOM
            $rejected = $false
            try { Assert-RequiredInventory $badPath $specs } catch { $rejected = $true }
            if (-not $rejected) { throw "Invalid inventory accepted: $field" }
        }
        Write-Host 'PASS: production staging, exact VIPC pins, schema-3 producer, six negative inventory fixtures. No native build was run.'
    } else {
        Write-Host "PASS: candidate built locally as $tag; required build and runtime inventory verified. No image was published."
    }
} finally {
    foreach ($name in $containerNames) { & docker rm -f $name 2>&1 | Out-Host }
    if (Test-Path -LiteralPath $context) { Remove-Item -LiteralPath $context -Recurse -Force }
}
