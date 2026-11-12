param(
    [string] $Root = (Split-Path -Parent (Split-Path -Parent $PSScriptRoot)),
    [string] $Python = ''
)
$ErrorActionPreference = 'Stop'
$errors = $null
$installer = Join-Path $Root '.github/labview/vipm/install-vipc.ps1'
$ast = [System.Management.Automation.Language.Parser]::ParseFile($installer, [ref]$null, [ref]$errors)
if ($errors.Count) { throw ($errors | Out-String) }
$ast.FindAll({
    param($node)
    $node -is [System.Management.Automation.Language.FunctionDefinitionAst]
}, $true) | ForEach-Object { . ([scriptblock]::Create($_.Extent.Text)) }
$LabVIEWVersion = '2026'
$LabVIEWBitness = '64'
$GlobalFlags = @('--labview-version', '2026', '--labview-bitness', '64')
$VipmExe = 'Mock-Vipm'
$VipcDir = Join-Path ([System.IO.Path]::GetTempPath()) ([guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $VipcDir | Out-Null
function Mock-Vipm {
    param([Parameter(ValueFromRemainingArguments = $true)] [string[]] $Arguments)
    if (($Arguments -join ' ') -ne '--labview-version 2026 --labview-bitness 64 list --installed') {
        throw "Unexpected target arguments: $Arguments"
    }
    $global:LASTEXITCODE = $script:InventoryExit
    $script:Inventory
}
function Get-VipcPackageSpecs { 'package_id@1.1-1' }
try {
    $files = @([pscustomobject]@{ Name = 'ci-tooling-project.vipc'; FullName = 'project.vipc' })
    foreach ($case in @(
        @{ Banner = 'Using LabVIEW 2026 (64-bit)'; Package = 'package_id v1.1-1'; Exit = 0; Accepted = $true },
        @{ Banner = 'Using LabVIEW 2026 (64-bit)'; Package = 'Display Name (package_id v1.1-1)'; Exit = 0; Accepted = $true },
        @{ Banner = 'Using LabVIEW 2026 (64-bit)'; Package = 'package_id_extra v1.1-1'; Exit = 0; Accepted = $false },
        @{ Banner = 'Using LabVIEW 2026 (64-bit)'; Package = 'package_id v1.1-2'; Exit = 0; Accepted = $false },
        @{ Banner = 'Using LabVIEW 2025 (64-bit)'; Package = 'package_id v1.1-1'; Exit = 0; Accepted = $false },
        @{ Banner = 'Using LabVIEW 2026 (32-bit)'; Package = 'package_id v1.1-1'; Exit = 0; Accepted = $false },
        @{ Banner = ''; Package = 'package_id v1.1-1'; Exit = 0; Accepted = $false },
        @{ Banner = 'Using LabVIEW 2026 (64-bit)'; Package = ''; Exit = 0; Accepted = $false },
        @{ Banner = 'Using LabVIEW 2026 (64-bit)'; Package = 'package_id v1.1-1'; Exit = 2; Accepted = $false }
    )) {
        $script:InventoryExit = $case.Exit
        $script:Inventory = @($case.Banner, $case.Package)
        $Env:VIPM_ALLOW_MISSING_PACKAGES = '1'
        $accepted = $true
        try { Assert-FinalWindowsProjectInventory -VipcFiles $files -DragonSpecs @() }
        catch { $accepted = $false }
        if ($accepted -ne $case.Accepted) { throw "Incorrect inventory result: $($case | ConvertTo-Json -Compress)" }
        if ($accepted) {
            $observed = Get-Content (Join-Path $VipcDir 'observed-inventory.json') -Raw | ConvertFrom-Json
            if (-not $observed.target_verified -or $observed.packages[0].name -ne 'package_id' -or
                $observed.packages[0].version -ne '1.1-1') { throw 'Incorrect observed artifact' }
        }
    }
    'PASS: observed target-bound project inventory fixtures (9 cases)'
    $baseline = @([pscustomobject]@{ Name = 'ci-tooling.vipc'; FullName = 'baseline.vipc' })
    foreach ($version in @('1.1-1', '1.1-2', '')) {
        $script:InventoryExit = 0
        $script:Inventory = @('Using LabVIEW 2026 (64-bit)', "package_id v$version")
        $accepted = $true
        try { Assert-FinalWindowsProjectInventory -VipcFiles $baseline -DragonSpecs @() }
        catch { $accepted = $false }
        if ($accepted -ne ($version -eq '1.1-1')) { throw "Incomplete baseline accepted: '$version'" }
    }
    'PASS: tooling baseline requires final installed inventory (3 cases)'
    $script:InventoryExit = 8
    Assert-FinalWindowsProjectInventory -VipcFiles @() -DragonSpecs @()
    $unavailable = Get-Content (Join-Path $VipcDir 'observed-inventory.json') -Raw | ConvertFrom-Json
    if ($unavailable.status -ne 'unavailable' -or $unavailable.target_verified -or
        $unavailable.packages.Count -or $unavailable.required_project) { throw 'Baseline failure claimed packages' }
    'PASS: empty selection records unavailable observation without claiming packages'
    $workflow = Get-Content (Join-Path $Root '.github/workflows/build-labview-image.yml') -Raw
    $validation = [regex]::Match($workflow, '(?s)            if \(\$observed -isnot \[pscustomobject\].*?(?=            docker push \$immutable)')
    if (-not $validation.Success) { throw 'Candidate inventory validation block not found' }
    $validateInventory = [scriptblock]::Create($validation.Value)
    $ver = '2026'
    $validInventory = '{"schema":1,"platform":"windows","source":"vipm list --installed","labview_version":"2026","labview_bitness":"64","status":"observed","target_verified":true,"required_project":true,"raw":"package_id 1.1-1","packages":[{"name":"package_id","version":"1.1-1"}]}'
    $inventoryCases = @(
        @{ Field = ''; Value = $null; Accepted = $true },
        @{ Field = 'schema'; Value = '1'; Accepted = $false },
        @{ Field = 'schema'; Value = $true; Accepted = $false },
        @{ Field = 'schema'; Value = 1.0; Accepted = $false },
        @{ Field = 'source'; Value = 'declared'; Accepted = $false },
        @{ Field = 'source'; Value = @('vipm list --installed'); Accepted = $false },
        @{ Field = 'platform'; Value = 'linux'; Accepted = $false },
        @{ Field = 'platform'; Value = @('windows'); Accepted = $false },
        @{ Field = 'labview_version'; Value = 2026; Accepted = $false },
        @{ Field = 'labview_version'; Value = '2025'; Accepted = $false },
        @{ Field = 'labview_bitness'; Value = 64; Accepted = $false },
        @{ Field = 'labview_bitness'; Value = '32'; Accepted = $false },
        @{ Field = 'status'; Value = 'declared'; Accepted = $false },
        @{ Field = 'status'; Value = 'Observed'; Accepted = $false },
        @{ Field = 'status'; Value = @('observed'); Accepted = $false },
        @{ Field = 'required_project'; Value = 'false'; Accepted = $false },
        @{ Field = 'required_project'; Value = $null; Accepted = $false },
        @{ Field = 'target_verified'; Value = 'true'; Accepted = $false },
        @{ Field = 'target_verified'; Value = 1; Accepted = $false },
        @{ Field = 'target_verified'; Value = $false; Accepted = $false },
        @{ Field = 'raw'; Value = @('raw'); Accepted = $false },
        @{ Field = 'packages'; Value = $null; Accepted = $false },
        @{ Field = 'packages'; Value = [pscustomobject]@{ name = 'package_id'; version = '1.1-1' }; Accepted = $false },
        @{ Field = 'packages'; Value = @('package_id'); Accepted = $false },
        @{ Field = 'packages'; Value = @([pscustomobject]@{ name = 1; version = '1.1-1' }); Accepted = $false },
        @{ Field = 'packages'; Value = @([pscustomobject]@{ name = ' '; version = '1.1-1' }); Accepted = $false },
        @{ Field = 'packages'; Value = @([pscustomobject]@{ name = 'package_id'; version = $null }); Accepted = $false },
        @{ Field = 'packages'; Value = @([pscustomobject]@{ name = 'package_id'; version = ' ' }); Accepted = $false }
    )
    foreach ($case in $inventoryCases) {
        $observed = $validInventory | ConvertFrom-Json -NoEnumerate
        if ($case.Field) { $observed.($case.Field) = $case.Value }
        $accepted = $true
        try { & $validateInventory } catch { $accepted = $false }
        if ($accepted -ne $case.Accepted) { throw "Incorrect candidate inventory result: $($case | ConvertTo-Json -Compress -Depth 5)" }
    }
    foreach ($json in @('null', '[]', "[$validInventory]", 'true')) {
        $observed = $json | ConvertFrom-Json -NoEnumerate
        $rejected = $false
        try { & $validateInventory } catch { $rejected = $true }
        if (-not $rejected) { throw "Non-object inventory was accepted: $json" }
    }
    $observed = $unavailable
    & $validateInventory
    $observed.packages = @([pscustomobject]@{ name = 'invented'; version = '1.0' })
    $rejected = $false
    try { & $validateInventory } catch { $rejected = $true }
    if (-not $rejected) { throw 'Unavailable baseline claimed installed packages' }
    'PASS: strict candidate inventory fixtures (34 cases)'
    if ($workflow -notmatch "--dependency-sha '\$\{\{ inputs.dependency_sha \|\| github.sha \}\}'" -or
        $workflow -notmatch '--labview-bitness 64' -or $workflow -notmatch '@inventoryArgs') {
        throw 'Manifest target/provenance arguments are missing'
    }
    $inventoryArguments = [regex]::Match($workflow, '(?s)          \$inventoryArgs = @\(\).*?(?=          python )')
    if (-not $inventoryArguments.Success) { throw 'Optional observed inventory arguments not found' }
    $argumentBlock = [scriptblock]::Create($inventoryArguments.Value + "`n,`$inventoryArgs")
    Push-Location $VipcDir
    try {
        $arguments = & $argumentBlock
        if ($arguments.Count) { throw 'Missing observed inventory must not be inferred' }
        New-Item -ItemType Directory -Path 'ci-out/worker' -Force | Out-Null
        Copy-Item 'observed-inventory.json' 'ci-out/worker/observed-inventory.json'
        $arguments = & $argumentBlock
        if ($arguments.Count -ne 2 -or $arguments[0] -ne '--observed-inventory' -or
            $arguments[1] -ne 'ci-out/worker/observed-inventory.json') { throw 'Observed inventory arguments are incorrect' }
    } finally { Pop-Location }
    'PASS: optional manifest inventory wiring fixtures (2 cases)'
    $global:WorkerFixture = @{
        Current = @{}
        NewDigest = 'sha256:' + ('b' * 64)
        OldDigest = 'sha256:' + ('a' * 64)
        TagCalls = @()
        FailAlias = $true
    }
    function crane {
        param([string] $Operation, [string] $Source, [string] $Tag)
        $global:LASTEXITCODE = 0
        if ($Operation -eq 'digest') {
            if ($Source -eq 'ghcr.io/test/worker:immutable') { return $global:WorkerFixture.NewDigest }
            if ($global:WorkerFixture.Current.ContainsKey($Source)) { return $global:WorkerFixture.Current[$Source] }
            return $global:WorkerFixture.OldDigest
        }
        if ($Operation -ne 'tag') { throw 'Unexpected crane command' }
        $global:WorkerFixture.TagCalls += "$Source $Tag"
        if ($global:WorkerFixture.FailAlias -and $Tag -eq 'latest' -and $Source.EndsWith($global:WorkerFixture.NewDigest)) {
            $global:LASTEXITCODE = 1
            return
        }
        $global:WorkerFixture.Current["ghcr.io/test/worker:$Tag"] = ($Source -split '@')[-1]
    }
    $publisher = Join-Path $Root '.github/labview/vipm/publish-worker-tags.ps1'
    $failed = $false
    try { & $publisher -Immutable 'ghcr.io/test/worker:immutable' -References @('ghcr.io/test/worker:2026', 'ghcr.io/test/worker:latest') }
    catch {
        $failed = $true
        if ($_.Exception.Message -notlike 'Worker alias publication failed:*') { throw }
    }
    if (-not $failed -or $global:WorkerFixture.Current['ghcr.io/test/worker:2026'] -ne $global:WorkerFixture.OldDigest -or
        $global:WorkerFixture.Current['ghcr.io/test/worker:latest'] -ne $global:WorkerFixture.OldDigest) { throw 'Alias rollback failed' }
    $global:WorkerFixture.FailAlias = $false
    $global:WorkerFixture.TagCalls = @()
    & $publisher -Immutable 'ghcr.io/test/worker:immutable' -References @('ghcr.io/test/worker:immutable', 'ghcr.io/test/worker:2026-deps-date')
    if ($global:WorkerFixture.TagCalls.Count -ne 1 -or $global:WorkerFixture.TagCalls[0] -notlike '* 2026-deps-date') {
        throw 'New mode moved an active alias'
    }
    'PASS: alias rollback and new-mode publication fixtures'
} finally {
    Remove-Item -LiteralPath $VipcDir -Recurse -Force
}
if ($Python) {
    $Env:PATH = (Split-Path -Parent (Get-Process -Id $PID).Path) + [System.IO.Path]::PathSeparator + $Env:PATH
    Push-Location $Root
    try {
        & $Python (Join-Path $Root '.github/labview/test-vipm-install.py') -v
        if ($LASTEXITCODE -ne 0) { throw 'Windows VIPM regression suite failed' }
        & $Python (Join-Path $Root '.github/labview/validate-catalog-source-sync.py')
        if ($LASTEXITCODE -ne 0) { throw 'Catalog source synchronization gate failed' }
    } finally { Pop-Location }
}
