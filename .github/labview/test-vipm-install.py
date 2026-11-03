#!/usr/bin/env python3
"""Exercise Windows VIPM bootstrap and fallback accounting without LabVIEW/VIPM."""

import json
import os
import re
import shutil
import subprocess
import tempfile
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
$Env:TEMP = [System.IO.Path]::GetTempPath()
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
        environment = {key: value for key, value in os.environ.items()
                       if key.casefold() != 'psmodulepath'}
        result = subprocess.run(
            [POWERSHELL, '-NoLogo', '-NoProfile', '-NonInteractive', '-Command', code],
            capture_output=True, text=True, timeout=30, cwd=INSTALLER.parents[3], env=environment)
        self.assertEqual(result.returncode, expected_exit, result.stdout + result.stderr)
        return result.stdout

    def bootstrap(self, cli='26.3.0', desktop='2026.3.0.3954', after_cli='26.3.2',
                  after_desktop='2026.3.2.4056', installer='26.3.4056',
                  url='https://example.test/custom.exe', exit_code=0,
                  failure='', expected_exit=0):
        def version(value):
            return f"[version]'{value}'" if value else '$null'

        return self.run_script(f"""
$Env:VIPM_ALLOW_MISSING_PACKAGES = '1'
$script:Before = [pscustomobject]@{{
    CliPath = $(if ({version(cli)}) {{ '/old/vipm.exe' }}); CliVersion = {version(cli)}
    DesktopPath = $(if ({version(desktop)}) {{ '/old/VI Package Manager.exe' }}); DesktopVersion = {version(desktop)}
}}
$script:After = [pscustomobject]@{{
    CliPath = $(if ({version(after_cli)}) {{ '/selected/vipm.exe' }}); CliVersion = {version(after_cli)}
    DesktopPath = $(if ({version(after_desktop)}) {{ '/selected/VI Package Manager.exe' }}); DesktopVersion = {version(after_desktop)}
}}
$script:RuntimeCalls = 0
$script:DownloadCalls = 0
$script:InstallCalls = 0
$script:InstallerFile = $null
function Get-VipmRuntime {{
    $script:RuntimeCalls++
    if ($script:RuntimeCalls -eq 1) {{ return $script:Before }}
    return $script:After
}}
function Invoke-WebRequest {{
    param($Uri, $OutFile, [switch]$UseBasicParsing, $TimeoutSec, $ErrorAction)
    if ($Uri.AbsoluteUri -ne '{url}' -or $TimeoutSec -ne 300) {{ throw 'Wrong URL/download bound' }}
    $script:DownloadCalls++
    $script:InstallerFile = $OutFile
    Set-Content -LiteralPath $OutFile -Value 'mock vendor installer'
    if ('{failure}' -eq 'download') {{ throw 'unreachable custom URL' }}
}}
function Assert-VipmPublisher {{
    param($Path)
    if ($Path -ne $script:InstallerFile) {{ throw 'Wrong signature path' }}
    if ('{failure}' -eq 'signature') {{ throw 'untrusted installer signature' }}
}}
function Get-Item {{
    param($LiteralPath, $ErrorAction)
    [pscustomobject]@{{ VersionInfo = [pscustomobject]@{{
        ProductName = 'VIPM 2026 Q3 f2'; FileVersion = '{installer}'
    }} }}
}}
function Invoke-VipmBootstrapProcess {{
    param($Path, $Arguments, $TimeoutSeconds)
    if ($Path -ne $script:InstallerFile -or $TimeoutSeconds -ne 1200 -or
        ($Arguments -join ' ') -ne '/exenoui /qn /norestart REBOOT=ReallySuppress') {{
        throw 'Wrong installer invocation'
    }}
    $script:InstallCalls++
    if ('{failure}' -eq 'timeout') {{ throw 'installer timed out' }}
    [pscustomobject]@{{ ExitCode = {exit_code}; Output = 'installer diagnostics' }}
}}
$failed = $false
try {{
    $runtime = Initialize-VipmRuntime -InstallerUrl '{url}'
    Write-Output "SELECTED=$($runtime.CliPath)|$($runtime.DesktopPath)"
}} catch {{
    $failed = $true
    Write-Output "FAILURE=$($_.Exception.Message)"
}} finally {{
    if ($script:InstallerFile -and (Test-Path -LiteralPath $script:InstallerFile)) {{
        throw 'Temporary installer was not cleaned up'
    }}
    Write-Output "CALLS=$script:RuntimeCalls/$script:DownloadCalls/$script:InstallCalls"
}}
if ($failed) {{ exit 1 }}
""", expected_exit=expected_exit)

    def test_bootstrap_upgrades_old_or_missing_executables_and_reselects(self):
        for cli, desktop in (
                ('26.3.0', '2026.3.0.3954'), (None, None),
                ('26.3.2', None), (None, '2026.3.2.4056'),
                ('26.3.0', '2026.3.2.4056'), ('26.3.2', '2026.3.0.3954')):
            with self.subTest(cli=cli, desktop=desktop):
                output = self.bootstrap(cli=cli, desktop=desktop)
                self.assertIn('SELECTED=/selected/vipm.exe|/selected/VI Package Manager.exe', output)
                self.assertIn('CALLS=2/1/1', output)

    def test_bootstrap_reuses_supported_pair_without_download_or_downgrade(self):
        for cli, desktop in (('26.3.2', '2026.3.2.4056'), ('26.4.0', '2026.4.0.5000')):
            with self.subTest(cli=cli, desktop=desktop):
                output = self.bootstrap(cli=cli, desktop=desktop)
                self.assertIn('CALLS=1/0/0', output)
                self.assertIn('SELECTED=/old/vipm.exe|/old/VI Package Manager.exe', output)

    def test_bootstrap_rejects_mixed_newer_pair_before_possible_downgrade(self):
        for cli, desktop in (('26.3.3', None), (None, '2026.3.2.4057')):
            with self.subTest(cli=cli, desktop=desktop):
                output = self.bootstrap(cli=cli, desktop=desktop, expected_exit=1)
                self.assertIn('refusing a possible downgrade', output)
                self.assertIn('CALLS=1/0/0', output)

    def test_bootstrap_custom_url_is_authoritative(self):
        output = self.bootstrap(url='https://custom.example.test/pinned.exe')
        self.assertIn('CALLS=2/1/1', output)

    def test_bootstrap_rejects_invalid_or_insecure_custom_url(self):
        for url in ('http://custom.example.test/pinned.exe', 'not-a-url',
                    'https://user:password@custom.example.test/pinned.exe'):
            with self.subTest(url=url):
                output = self.bootstrap(url=url, expected_exit=1)
                self.assertIn('absolute HTTPS URL', output)
                self.assertIn('CALLS=1/0/0', output)

    def test_bootstrap_unreachable_or_untrusted_custom_url_has_no_fallback(self):
        for failure, message in (('download', 'unreachable custom URL'),
                                 ('signature', 'untrusted installer signature')):
            with self.subTest(failure=failure):
                output = self.bootstrap(failure=failure, expected_exit=1)
                self.assertIn(message, output)
                self.assertIn('CALLS=1/1/0', output)

    def test_bootstrap_rejects_old_or_unverifiable_installer_before_execution(self):
        for version in ('26.3.3954', '', 'unknown'):
            with self.subTest(version=version):
                output = self.bootstrap(installer=version, expected_exit=1)
                self.assertIn('unsupported/unverifiable identity or version', output)
                self.assertIn('CALLS=1/1/0', output)

    def test_bootstrap_failed_or_reboot_requiring_installer_stays_failed(self):
        for code in (1603, 1641, 3010):
            with self.subTest(code=code):
                output = self.bootstrap(exit_code=code, expected_exit=1)
                self.assertIn(f'exit {code}', output)
                self.assertIn('CALLS=1/1/1', output)

    def test_bootstrap_installer_timeout_stays_failed(self):
        output = self.bootstrap(failure='timeout', expected_exit=1)
        self.assertIn('installer timed out', output)
        self.assertIn('CALLS=1/1/1', output)

    def test_bootstrap_success_exit_without_supported_selected_pair_fails(self):
        for cli, desktop in (('26.3.1', '2026.3.2.4056'),
                             ('26.3.2', '2026.3.1.3954'),
                             (None, '2026.3.2.4056'), ('26.3.2', None)):
            with self.subTest(cli=cli, desktop=desktop):
                output = self.bootstrap(after_cli=cli, after_desktop=desktop, expected_exit=1)
                self.assertIn('did not produce a supported selected CLI/Desktop pair', output)
                self.assertIn('CALLS=2/1/1', output)

    def test_runtime_measures_both_selected_paths(self):
        output = self.run_script("""
function Find-VipmExecutable {
    param($Kind)
    if ($Kind -eq 'CLI') { return '/selected/vipm.exe' }
    return '/selected/VI Package Manager.exe'
}
function Assert-VipmPublisher { param($Path); Write-Output "SIGNATURE=$Path" | Out-Host }
function Invoke-VipmBootstrapProcess {
    param($Path, $Arguments, $TimeoutSeconds)
    if ($Path -ne '/selected/vipm.exe' -or ($Arguments -join ' ') -ne '--version' -or $TimeoutSeconds -ne 30) {
        throw 'Wrong selected CLI version invocation'
    }
    [pscustomobject]@{ ExitCode = 0; Output = 'vipm 26.3.2' }
}
function Get-Item {
    param($LiteralPath, $ErrorAction)
    if ($LiteralPath -ne '/selected/VI Package Manager.exe') { throw 'Wrong selected Desktop identity' }
    [pscustomobject]@{ VersionInfo = [pscustomobject]@{
        ProductName = 'VI Package Manager'; FileVersion = '2026.3.2.4056'
    } }
}
$runtime = Get-VipmRuntime
if (-not (Test-VipmRuntimeSupported $runtime)) { throw 'Expected supported runtime' }
Write-Output "VERSIONS=$($runtime.CliVersion)|$($runtime.DesktopVersion)"
""")
        self.assertIn('VERSIONS=26.3.2|2026.3.2.4056', output)
        self.assertIn('SIGNATURE=/selected/vipm.exe', output)
        self.assertIn('SIGNATURE=/selected/VI Package Manager.exe', output)

    def test_runtime_rejects_wrong_cli_version_output_or_failed_command(self):
        for result in ("ExitCode = 0; Output = 'not vipm 26.3.2'",
                       "ExitCode = 3; Output = 'vipm 26.3.2'",
                       "ExitCode = 0; Output = ''"):
            with self.subTest(result=result):
                output = self.run_script("""
function Find-VipmExecutable { param($Kind); if ($Kind -eq 'CLI') { '/wrong/vipm.exe' } }
function Assert-VipmPublisher {}
function Invoke-VipmBootstrapProcess { [pscustomobject]@{""" + result + """} }
try { Get-VipmRuntime; throw 'Unexpected successful version check' }
catch { if ($_.Exception.Message -notlike 'Cannot verify selected VIPM CLI*') { throw }; $_.Exception.Message }
""")
                self.assertIn('Cannot verify selected VIPM CLI', output)

    def test_runtime_rejects_unverifiable_desktop_identity(self):
        for name, version in (('VI Package Manager', ''), ('Other Product', '2026.3.2.4056')):
            with self.subTest(name=name, version=version):
                output = self.run_script(f"""
function Find-VipmExecutable {{ param($Kind); if ($Kind -eq 'Desktop') {{ '/wrong/Desktop.exe' }} }}
function Assert-VipmPublisher {{}}
function Get-Item {{
    [pscustomobject]@{{ VersionInfo = [pscustomobject]@{{ ProductName = '{name}'; FileVersion = '{version}' }} }}
}}
try {{ Get-VipmRuntime; throw 'Unexpected success' }}
catch {{ if ($_.Exception.Message -notlike 'Cannot verify selected VIPM Desktop*') {{ throw }}; $_.Exception.Message }}
""")
                self.assertIn('Cannot verify selected VIPM Desktop', output)

    def test_discovery_keeps_preferred_stale_path_visible(self):
        output = self.run_script("""
function Test-Path {
    param($LiteralPath, $PathType)
    $LiteralPath -in @('C:\\Program Files\\JKI\\VIPM\\vipm.exe', 'C:\\Program Files\\JKI\\VI Package Manager\\support\\vipm.exe')
}
$selected = Find-VipmExecutable 'CLI'
if ($selected -ne 'C:\\Program Files\\JKI\\VIPM\\vipm.exe') { throw 'Preferred stale executable was silently bypassed' }
Write-Output "SELECTED=$selected"
""")
        self.assertIn(r'SELECTED=C:\Program Files\JKI\VIPM\vipm.exe', output)

    def test_publisher_requires_windows_trust_and_expected_signer(self):
        for status, signer, accepted in (('Valid', 'James Kring, Inc.', True),
                                         ('NotTrusted', 'James Kring, Inc.', False),
                                         ('HashMismatch', 'James Kring, Inc.', False),
                                         ('Valid', 'Other Publisher', False),
                                         ('NotSigned', '', False)):
            with self.subTest(status=status, signer=signer):
                output = self.run_script(f"""
function Get-AuthenticodeSignature {{
    $cert = [pscustomobject]@{{}}
    $cert | Add-Member ScriptMethod GetNameInfo {{ param($Type, $Issuer); '{signer}' }}
    [pscustomobject]@{{ Status = '{status}'; SignerCertificate = $cert }}
}}
try {{ Assert-VipmPublisher '/selected/binary.exe'; 'ACCEPTED=True' }}
catch {{ 'ACCEPTED=False'; $_.Exception.Message }}
""")
                self.assertIn(f'ACCEPTED={accepted}', output)

    def test_bootstrap_only_exits_before_all_package_and_labview_side_effects(self):
        installer = str(INSTALLER).replace("'", "''")
        output = self.run_script("""
$global:VersionFiles = @()
$global:VersionCalls = 0
$global:Signatures = @()
$global:CliPath = 'C:\\Program Files\\JKI\\VIPM\\vipm.exe'
$global:DesktopPath = 'C:\\Program Files\\JKI\\VI Package Manager\\VI Package Manager.exe'
function Test-Path {
    param($LiteralPath, $PathType)
    if ($LiteralPath -in @($global:CliPath, $global:DesktopPath)) { return $true }
    if ($LiteralPath -in $global:VersionFiles) {
        return Microsoft.PowerShell.Management\\Test-Path -LiteralPath $LiteralPath
    }
    if ($LiteralPath -like 'C:*') { 'UNEXPECTED filesystem discovery'; exit 99 }
    Microsoft.PowerShell.Management\\Test-Path -LiteralPath $LiteralPath
}
function Get-AuthenticodeSignature {
    param($LiteralPath, $ErrorAction)
    if ($LiteralPath -notin @($global:CliPath, $global:DesktopPath)) { 'UNEXPECTED installer signature'; exit 99 }
    $global:Signatures += $LiteralPath
    $cert = [pscustomobject]@{}
    $cert | Add-Member ScriptMethod GetNameInfo { param($Type, $Issuer); 'James Kring, Inc.' }
    [pscustomobject]@{ Status = 'Valid'; SignerCertificate = $cert }
}
function Get-Item {
    param($LiteralPath, $ErrorAction)
    if ($LiteralPath -ne $global:DesktopPath) { 'UNEXPECTED identity lookup'; exit 99 }
    [pscustomobject]@{ VersionInfo = [pscustomobject]@{
        ProductName = 'VI Package Manager'; FileVersion = '2026.3.2.4056'
    } }
}
function Start-Process {
    param($FilePath, $ArgumentList, [switch]$PassThru, $RedirectStandardOutput, $RedirectStandardError, $ErrorAction)
    if ($FilePath -ne $global:CliPath -or ($ArgumentList -join ' ') -ne '--version') {
        'UNEXPECTED LabVIEW/Desktop/installer launch'; exit 99
    }
    $global:VersionCalls++
    $global:VersionFiles = @($RedirectStandardOutput, $RedirectStandardError)
    Microsoft.PowerShell.Management\\Set-Content -LiteralPath $RedirectStandardOutput -Value 'vipm 26.3.2'
    Microsoft.PowerShell.Management\\Set-Content -LiteralPath $RedirectStandardError -Value ''
    $process = [pscustomobject]@{ ExitCode = 0 }
    $process | Add-Member ScriptMethod WaitForExit {
        param($Milliseconds)
        if ($Milliseconds -ne 30000) { 'UNEXPECTED CLI wait bound'; exit 99 }
        return $true
    }
    $process | Add-Member ScriptMethod Dispose {}
    return $process
}
function Remove-Item {
    param($LiteralPath, [switch]$Force, $ErrorAction)
    if ($LiteralPath -notin $global:VersionFiles) { 'UNEXPECTED environment/settings mutation'; exit 99 }
    Microsoft.PowerShell.Management\\Remove-Item -LiteralPath $LiteralPath -Force -ErrorAction Stop
}
function Get-CimInstance { 'UNEXPECTED memory preflight'; exit 99 }
function Get-ChildItem { 'UNEXPECTED VIPC/Git discovery'; exit 99 }
function Get-Process { 'UNEXPECTED engine lookup'; exit 99 }
function New-PublicRepoWorkdir { 'UNEXPECTED public Git workdir'; exit 99 }
function Set-Content { 'UNEXPECTED settings write'; exit 99 }
function New-Item { 'UNEXPECTED directory creation'; exit 99 }
function Invoke-WebRequest { 'UNEXPECTED installer download'; exit 99 }
$Env:LV_RTE_HEADLESS = 'sentinel'
$Env:VIPM_NONINTERACTIVE = 'unchanged'
& '""" + installer + """' -BootstrapOnly
if ($LASTEXITCODE -ne 0) { throw "Top-level bootstrap exited $LASTEXITCODE" }
if ($Env:LV_RTE_HEADLESS -ne 'sentinel' -or $Env:VIPM_NONINTERACTIVE -ne 'unchanged') {
    throw 'Top-level bootstrap changed the runtime environment'
}
if ($global:VersionCalls -ne 1 -or $global:Signatures.Count -ne 2) { throw 'Real bootstrap verification did not execute' }
foreach ($file in $global:VersionFiles) { if (Test-Path -LiteralPath $file) { throw 'Version output file leaked' } }
'TOP-LEVEL-SENTINELS=True'
""")
        self.assertIn('no LabVIEW, engine, Git context, or package operations requested', output)
        self.assertIn('TOP-LEVEL-SENTINELS=True', output)
        self.assertNotIn('UNEXPECTED', output)

    def test_running_engine_must_match_verified_selected_desktop(self):
        output = self.run_script("""
$script:VipmEngineExe = '/verified/Desktop.exe'
function Get-Process { [pscustomobject]@{ Path = '/stale/Desktop.exe' } }
function Start-Process { throw 'Must not launch another engine' }
try { Start-VipmEngineProcess; throw 'Wrong running engine accepted' }
catch { if ($_.Exception.Message -notlike 'Running VIPM Desktop*') { throw }; $_.Exception.Message }
""")
        self.assertIn('does not match verified selected executable', output)

    def test_bootstrap_native_process_records_output_exit_and_cleans_temp_files(self):
        with tempfile.TemporaryDirectory() as directory:
            script = Path(directory) / 'probe.ps1'
            script.write_text("Write-Output 'vipm 26.3.2'\nexit 7\n")
            escaped = str(script).replace("'", "''")
            pwsh = str(POWERSHELL).replace("'", "''")
            output = self.run_script(f"""
$result = Invoke-VipmBootstrapProcess -Path '{pwsh}' -Arguments @('-NoLogo', '-NoProfile', '-File', '{escaped}') -TimeoutSeconds 10
if ($result.ExitCode -ne 7 -or $result.Output -notmatch 'vipm 26.3.2') {{ throw "Wrong process result: $result" }}
'NATIVE=True'
""")
            self.assertIn('NATIVE=True', output)

    def test_bootstrap_process_timeout_stops_only_its_pid_tree_and_cleans_files(self):
        output = self.run_script("""
$script:Waits = 0
$script:Killed = $false
$script:Disposed = $false
$script:RetainedHandle = $false
$script:Files = @()
$script:FakeProcess = [pscustomobject]@{ Id = 73451; HasExited = $false }
$script:FakeProcess | Add-Member ScriptProperty Handle { $script:RetainedHandle = $true; 1 }
$script:FakeProcess | Add-Member ScriptMethod WaitForExit {
    param($Milliseconds)
    if (-not $script:RetainedHandle) { throw 'Process handle was not retained before waiting' }
    $script:Waits++
    if ($script:Waits -eq 1 -and $Milliseconds -ne 1000) { throw 'Wrong process timeout' }
    if ($script:Waits -eq 2 -and $Milliseconds -ne 5000) { throw 'Wrong cleanup timeout' }
    return ($script:Waits -gt 1)
}
$script:FakeProcess | Add-Member ScriptMethod Dispose { $script:Disposed = $true }
function Start-Process {
    param($FilePath, $ArgumentList, [switch]$PassThru, $RedirectStandardOutput, $RedirectStandardError, $ErrorAction)
    $script:Files = @($RedirectStandardOutput, $RedirectStandardError)
    foreach ($file in $script:Files) { Set-Content -LiteralPath $file -Value 'partial output' }
    return $script:FakeProcess
}
function taskkill.exe {
    param([Parameter(ValueFromRemainingArguments = $true)] $Arguments)
    if (($Arguments -join ' ') -ne '/PID 73451 /T /F') { throw 'Wrong process cleanup target' }
    $script:Killed = $true
    $script:FakeProcess.HasExited = $true
    $global:LASTEXITCODE = 0
}
try { Invoke-VipmBootstrapProcess -Path '/mock/installer.exe' -Arguments @('/qn') -TimeoutSeconds 1; throw 'Timeout accepted' }
catch { if ($_.Exception.Message -notlike '*timed out after 1s*') { throw } }
if (-not $script:Killed -or -not $script:Disposed -or $script:Waits -ne 2) { throw 'Missing bounded process cleanup' }
foreach ($file in $script:Files) { if (Test-Path -LiteralPath $file) { throw 'Output file leaked' } }
'TIMEOUT-CLEANUP=True'
""")
        self.assertIn('TIMEOUT-CLEANUP=True', output)

    def test_base_defaults_catalog_override_and_shared_bootstrap_are_wired(self):
        root = INSTALLER.parents[3]
        url = 'https://traffic.libsyn.com/secure/jkinc/vipm-26.3.4056-windows-setup.exe'
        catalog = json.loads((root / '.github/labview-ci/catalog.json').read_text())
        release = next(item for item in catalog['containers']['supported'] if item['id'] == '2026q3')
        self.assertEqual(release['vipmInstallerUrl'], url)
        self.assertIn(f"else {{ '{url}' }}", INSTALLER.read_text())
        dockerfile = (root / '.github/docker/labview-ci-base.Dockerfile').read_text()
        self.assertIn(f'ARG VIPM_INSTALLER_URL={url}', dockerfile)
        self.assertIn('COPY .github/labview/vipm/install-vipc.ps1 C:/ci-bootstrap/install-vipc.ps1', dockerfile)
        self.assertIn(r'RUN & C:\ci-bootstrap\install-vipc.ps1 -BootstrapOnly;', dockerfile)

    def test_both_base_hashes_match_and_change_when_bootstrap_bytes_change(self):
        root = INSTALLER.parents[3]
        snippets = []
        for workflow in ('build-labview-image.yml', 'publish-base-images.yml'):
            source = (root / '.github/workflows' / workflow).read_text()
            snippet = re.search(r'\$hashInput = .*?\$baseHash = [^\n]+', source, re.S).group()
            for name in ('windowsTag', 'nipmFeedUrl', 'vipmInstallerUrl'):
                snippet = snippet.replace("'${{ matrix." + name + " }}'", f'$rel.{name}')
            snippets.append(snippet)
        with tempfile.TemporaryDirectory() as directory:
            temp_root = Path(directory)
            for path in ('.github/docker/labview-ci-base.Dockerfile', '.github/labview/vipm/install-vipc.ps1'):
                target = temp_root / path
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes((root / path).read_bytes())
            escaped = str(temp_root).replace("'", "''")
            output = self.run_script(f"""
Set-Location '{escaped}'
$rel = [pscustomobject]@{{ windowsTag = 'ni:test'; nipmFeedUrl = 'https://ni.test'; vipmInstallerUrl = 'https://jki.test' }}
$first = & {{ {snippets[0]}; $baseHash }}
$second = & {{ {snippets[1]}; $baseHash }}
if ($first -cne $second) {{ throw 'Immutable base hashes disagree' }}
Add-Content -LiteralPath '.github/labview/vipm/install-vipc.ps1' -Value ''
$changedFirst = & {{ {snippets[0]}; $baseHash }}
$changedSecond = & {{ {snippets[1]}; $baseHash }}
foreach ($hash in @($first, $second, $changedFirst, $changedSecond)) {{
    if ($hash -cnotmatch '^[0-9a-f]{{12}}$') {{ throw "Invalid immutable identity: $hash" }}
}}
if ($changedFirst -cne $changedSecond) {{ throw 'Changed bootstrap produced inconsistent immutable identities' }}
if ($changedFirst -ceq $first -or $changedSecond -ceq $second) {{ throw 'Changed bootstrap reused an immutable identity' }}
'HASH-WIRING=True'
""")
            self.assertIn('HASH-WIRING=True', output)

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


if __name__ == '__main__':
    unittest.main()
