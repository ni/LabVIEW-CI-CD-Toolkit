#!/usr/bin/env python3
"""Exercise Windows VIPM fallback accounting without LabVIEW or VIPM."""

import shutil
import subprocess
import unittest
from pathlib import Path


INSTALLER = Path(__file__).parent / 'vipm' / 'install-vipc.ps1'
POWERSHELL = shutil.which('powershell.exe') or shutil.which('pwsh')


@unittest.skipUnless(POWERSHELL, 'PowerShell is required')
class VipmInstallTests(unittest.TestCase):
    def run_script(self, body, expected_exit=0):
        installer = str(INSTALLER).replace("'", "''")
        code = f"""
$ErrorActionPreference = 'Stop'
$errors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile(
    '{installer}', [ref]$null, [ref]$errors)
if ($errors.Count) {{ throw ($errors | Out-String) }}
$ast.FindAll({{
    param($node)
    $node -is [System.Management.Automation.Language.FunctionDefinitionAst]
}}, $true) | ForEach-Object {{ . ([scriptblock]::Create($_.Extent.Text)) }}
$VipmExe = 'Mock-Vipm'
$GlobalFlags = @('--labview-version', '2026', '--labview-bitness', '64')
$script:VipmEngineDead = $false
function Mock-Vipm {{
    param([Parameter(ValueFromRemainingArguments = $true)] [string[]] $Arguments)
    if (($Arguments -join ' ') -ne '--labview-version 2026 --labview-bitness 64 list --installed') {{
        throw "Unexpected inventory arguments: $Arguments"
    }}
    $global:LASTEXITCODE = $script:InventoryExit
    $script:Inventory
}}
{body}
"""
        result = subprocess.run(
            [POWERSHELL, '-NoLogo', '-NoProfile', '-NonInteractive', '-Command', code],
            capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, expected_exit, result.stdout + result.stderr)
        return result.stdout

    def fallback(self, inventory, root_exit=0, inventory_exit=0, batch_exit=3,
                 required_phase=False, expected_exit=0):
        install = """
$result = Install-VipmSpecs @('jki_lib_caraya@1.4.4.148')
Write-Output "RESULT=$result"
"""
        if required_phase:
            install = """
$requiredSpecs = @('jki_lib_caraya@1.4.4.148')
$applyFailed = $false
$requiredPhase = $ast.Find({
    param($node)
    $node -is [System.Management.Automation.Language.IfStatementAst] -and
    $node.Extent.Text.StartsWith('if ($requiredSpecs.Count -gt 0) {')
}, $true)
if (-not $requiredPhase) { throw 'Required install phase not found' }
. ([scriptblock]::Create($requiredPhase.Extent.Text))
Write-Output "RESULT=$(-not $applyFailed)"
$failureGate = $ast.Find({
    param($node)
    $node -is [System.Management.Automation.Language.IfStatementAst] -and
    $node.Extent.Text.StartsWith('if ($applyFailed) {')
}, $true)
if (-not $failureGate) { throw 'Required failure gate not found' }
$Env:VIPM_ALLOW_MISSING_PACKAGES = ''
. ([scriptblock]::Create($failureGate.Extent.Text))
"""
        return self.run_script(f"""
$script:Inventory = @('{inventory}')
$script:InventoryExit = {inventory_exit}
function Get-LocalVipFilesForSpecs {{
    @('/packages/legacy_palette-1.1-1.ogp', '/packages/caraya-1.4.4.148.vip')
}}
function Invoke-VipmInstall {{
    param([Parameter(ValueFromRemainingArguments = $true)] [string[]] $Targets)
    if ($Targets.Count -gt 1) {{ return {batch_exit} }}
    if ($Targets[0] -eq '/packages/legacy_palette-1.1-1.ogp') {{
        Write-Warning 'Failed to read package metadata: Missing Name in Package section'
        return 8
    }}
    if ($Targets[0] -eq '/packages/caraya-1.4.4.148.vip') {{ return {root_exit} }}
    return 3
}}
{install}
""", expected_exit=expected_exit)

    def test_recovered_required_root_does_not_latch_transitive_file_failure(self):
        output = self.fallback('Caraya (jki_lib_caraya v1.4.4.148)')
        self.assertIn('RESULT=True', output)
        self.assertIn('Missing Name in Package section', output)

    def test_unrecovered_required_root_stays_failed(self):
        output = self.fallback('Legacy palette (legacy_palette v1.1)', root_exit=8)
        self.assertIn('RESULT=False', output)

    def test_wrong_installed_version_stays_failed(self):
        output = self.fallback('Caraya (jki_lib_caraya v1.4.3.100)')
        self.assertIn('RESULT=False', output)

    def test_failed_inventory_cannot_confirm_recovery(self):
        output = self.fallback('Caraya (jki_lib_caraya v1.4.4.148)', inventory_exit=8)
        self.assertIn('RESULT=False', output)

    def test_successful_local_batch_must_still_contain_requested_root(self):
        output = self.fallback('Legacy palette (legacy_palette v1.1)', batch_exit=0)
        self.assertIn('RESULT=False', output)

    def test_recovered_required_phase_does_not_fail_bake(self):
        output = self.fallback('Caraya (jki_lib_caraya v1.4.4.148)', required_phase=True)
        self.assertIn('REQUIRED unit-test essentials installed.', output)
        self.assertIn('RESULT=True', output)

    def test_unrecovered_required_phase_fails_bake(self):
        output = self.fallback('Legacy palette (legacy_palette v1.1)', root_exit=8,
                               required_phase=True, expected_exit=1)
        self.assertIn('RESULT=False', output)

    def verify(self, inventory, specs):
        return self.run_script(f"""
$script:InventoryExit = 0
$script:Inventory = @({','.join(repr(line) for line in inventory)})
$result = Test-VipmSpecsInstalled @({','.join(repr(spec) for spec in specs)})
Write-Output "RESULT=$result"
""")

    def test_supported_inventory_formats(self):
        for line in ('Caraya (jki_lib_caraya v1.4.4.148)',
                     'jki_lib_caraya 1.4.4.148', 'jki_lib_caraya-1.4.4.148'):
            with self.subTest(line=line):
                self.assertIn('RESULT=True', self.verify([line], ['jki_lib_caraya@1.4.4.148']))

    def test_every_requested_root_must_be_present(self):
        output = self.verify(['Caraya (jki_lib_caraya v1.4.4.148)'],
                             ['jki_lib_caraya@1.4.4.148', 'ni_lib_utf_junit_report@1.0.1.43'])
        self.assertIn('RESULT=False', output)

    def test_alias_unpinned_minimum_and_openg_release_specs(self):
        output = self.verify(
            ['VI Tester (jki_labs_tool_vi_tester v1.1.2.164)',
             'Palette (jki_rsc_toolkits_palette v1.1-1)'],
            ['jki_vi_tester', 'jki_labs_tool_vi_tester>=1.1.0',
             'jki_rsc_toolkits_palette@1.1'])
        self.assertIn('RESULT=True', output)

    def test_minimum_and_explicit_release_versions_are_not_ignored(self):
        for spec in ('jki_rsc_toolkits_palette>=1.2', 'jki_rsc_toolkits_palette@1.1-2'):
            with self.subTest(spec=spec):
                output = self.verify(['Palette (jki_rsc_toolkits_palette v1.1-1)'], [spec])
                self.assertIn('RESULT=False', output)

    def test_diagnostics_and_empty_inventory_cannot_confirm_recovery(self):
        for lines in ([], ['Using LabVIEW 2026 (64-bit)', 'Found 0 packages:'],
                      ['not_jki_lib_caraya 1.4.4.148']):
            with self.subTest(lines=lines):
                self.assertIn('RESULT=False', self.verify(lines, ['jki_lib_caraya@1.4.4.148']))

    def test_dead_engine_does_not_attempt_inventory_recovery(self):
        output = self.run_script("""
$script:VipmEngineDead = $true
function Invoke-VipmInstall { return 124 }
function Mock-Vipm { throw 'Inventory must not run against a dead engine' }
$result = Install-VipmSpecs @('jki_lib_caraya@1.4.4.148')
Write-Output "RESULT=$result"
""")
        self.assertIn('RESULT=False', output)

    def test_inventory_preserves_legacy_cli_active_target_fallback(self):
        output = self.run_script("""
function Mock-Vipm {
    param([Parameter(ValueFromRemainingArguments = $true)] [string[]] $Arguments)
    if ($Arguments[0] -eq '--labview-version') {
        $global:LASTEXITCODE = 2
        return 'unexpected argument --labview-version'
    }
    if (($Arguments -join ' ') -ne 'list --installed') { throw 'Unexpected command' }
    $global:LASTEXITCODE = 0
    'Caraya (jki_lib_caraya v1.4.4.148)'
}
$result = Test-VipmSpecsInstalled @('jki_lib_caraya@1.4.4.148')
Write-Output "RESULT=$result"
""")
        self.assertIn('RESULT=True', output)
        self.assertIn('retrying bare form against active target', output)

    def test_project_failure_is_required_but_tooling_failure_is_optional(self):
        for name, required, optional in (('Dependencies.vipc', True, False),
                                         ('ci-tooling.vipc', False, True)):
            with self.subTest(name=name):
                output = self.run_script(f"""
$applyFailed = $false
$script:bestEffortFailed = $false
$script:LastVipmOutput = ''
$vipcFiles = @([pscustomobject]@{{ Name = '{name}'; FullName = '{name}' }})
function Invoke-VipmInstall {{ return 3 }}
function Get-VipcPackageSpecs {{ @('jki_lib_caraya@1.4.4.148') }}
function Install-VipmSpecs {{ return $false }}
$vipcLoop = $ast.Find({{
    param($node)
    $node -is [System.Management.Automation.Language.ForEachStatementAst] -and
    $node.Extent.Text.StartsWith('foreach ($vipc in $vipcFiles) {{')
}}, $true)
if (-not $vipcLoop) {{ throw 'VIPC install loop not found' }}
. ([scriptblock]::Create($vipcLoop.Extent.Text))
Write-Output "REQUIRED=$applyFailed OPTIONAL=$script:bestEffortFailed"
""")
                self.assertIn(f'REQUIRED={required} OPTIONAL={optional}', output)

    def test_normalized_exact_version_resolves_public_packaging_release(self):
        output = self.run_script("""
$package = [pscustomobject]@{
    Name = 'jki_rsc_toolkits_palette'; Version = '1.1-1'
    VersionKey = Get-NumericVersionKey '1.1-1'
}
$request = Split-VipmPackageSpec 'jki_rsc_toolkits_palette@1.1'
$selected = Select-PublicVipmPackage $request @($package)
if ($selected) { Write-Output "SELECTED=$($selected.Version)" }
""")
        self.assertIn('SELECTED=1.1-1', output)

    def test_skipped_transitive_dependency_cannot_satisfy_explicit_root(self):
        output = self.run_script("""
$VipcDir = ''
$env:TEMP = [System.IO.Path]::GetTempPath()
function Get-PublicVipmRepositoryPackages {
    [pscustomobject]@{
        Id = 'parent-1.0'; Name = 'parent'; Version = '1.0'
        VersionKey = Get-NumericVersionKey '1.0'
        Dependencies = 'missing_palette = 1.1'
    }
}
function Get-LocalVipFileIndex { @() }
function Save-PublicVipmPackage { throw 'No download before every root resolves' }
$caught = $false
try { $null = Get-LocalVipFilesForSpecs @('parent@1.0', 'missing_palette@1.1') }
catch {
    $caught = $true
    Write-Output "ERROR=$($_.Exception.Message)"
}
Write-Output "CAUGHT=$caught"
""")
        self.assertIn("Package 'missing_palette' version '1.1' was not found", output)

    def test_public_and_local_selection_preserve_exact_and_minimum_pins(self):
        output = self.run_script("""
$packages = @('1.0-1', '1.1-1', '1.1-2', '1.2-1') | ForEach-Object {
    [pscustomobject]@{
        Name = 'palette'; Version = $_; VersionKey = Get-NumericVersionKey $_
    }
}
foreach ($selector in @('Select-PublicVipmPackage', 'Select-LocalVipPackage')) {
    foreach ($case in @(
        @('palette@1.1', '1.1-2'), @('palette@1.1-1', '1.1-1'),
        @('palette>=1.1-2', '1.2-1'), @('palette', '1.2-1'),
        @('palette@1.10', ''), @('palette@1.1-3', ''), @('palette>=1.3', '')
    )) {
        $selected = & $selector (Split-VipmPackageSpec $case[0]) $packages
        $actual = if ($selected) { $selected.Version } else { '' }
        if ($actual -ne $case[1]) { throw "$selector $($case[0]): got '$actual', expected '$($case[1])'" }
    }
}
Write-Output 'PIN_MATRIX=passed'
""")
        self.assertIn('PIN_MATRIX=passed', output)

    def test_resolved_release_dependency_is_downloaded_once_and_satisfies_root(self):
        output = self.run_script("""
$VipcDir = ''
$env:TEMP = [System.IO.Path]::GetTempPath()
function Get-PublicVipmRepositoryPackages {
    [pscustomobject]@{
        Id = 'parent-1.0'; Name = 'parent'; Version = '1.0'
        VersionKey = Get-NumericVersionKey '1.0'
        Dependencies = 'palette >= 1.1'
    }
    [pscustomobject]@{
        Id = 'palette-1.1-1'; Name = 'palette'; Version = '1.1-1'
        VersionKey = Get-NumericVersionKey '1.1-1'
        Dependencies = ''
    }
}
function Get-LocalVipFileIndex { @() }
function Save-PublicVipmPackage { param($Package, $OutDir); "/packages/$($Package.Id).ogp" }
$files = @(Get-LocalVipFilesForSpecs @('parent@1.0', 'palette@1.1'))
Write-Output "FILES=$($files -join ',')"
""")
        self.assertIn('FILES=/packages/palette-1.1-1.ogp,/packages/parent-1.0.ogp', output)

    def test_missing_transitive_only_keeps_existing_optional_policy(self):
        output = self.run_script("""
$VipcDir = ''
$env:TEMP = [System.IO.Path]::GetTempPath()
function Get-PublicVipmRepositoryPackages {
    [pscustomobject]@{
        Id = 'parent-1.0'; Name = 'parent'; Version = '1.0'
        VersionKey = Get-NumericVersionKey '1.0'
        Dependencies = 'bundled_component = 1.1'
    }
}
function Get-LocalVipFileIndex { @() }
function Save-PublicVipmPackage { param($Package, $OutDir); "/packages/$($Package.Id).vip" }
$files = @(Get-LocalVipFilesForSpecs @('parent@1.0'))
Write-Output "FILES=$($files -join ',')"
""")
        self.assertIn("Skipping dependency 'bundled_component'", output)
        self.assertIn('FILES=/packages/parent-1.0.vip', output)

    def test_legacy_release_still_requires_installed_root_evidence(self):
        for inventory, expected in ((['Palette (palette v1.1-1)'], True),
                                    (['Palette (palette v1.0-1)'], False), ([], False)):
            with self.subTest(inventory=inventory):
                output = self.verify(inventory, ['palette@1.1'])
                self.assertIn(f'RESULT={expected}', output)


if __name__ == '__main__':
    unittest.main()
