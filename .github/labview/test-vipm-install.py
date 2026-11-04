#!/usr/bin/env python3
"""Exercise Windows VIPM bootstrap and fallback accounting without LabVIEW/VIPM."""

import json
import importlib.util
import os
import re
import shutil
import subprocess
import tempfile
import unittest
import zipfile
from pathlib import Path


INSTALLER = Path(__file__).parent / 'vipm' / 'install-vipc.ps1'
POWERSHELL = shutil.which('powershell.exe') or shutil.which('pwsh')
PARSER_PATH = Path(__file__).parent / 'dragon_deps.py'
PARSER_SPEC = importlib.util.spec_from_file_location('dragon_deps', PARSER_PATH)
DRAGON = importlib.util.module_from_spec(PARSER_SPEC)
PARSER_SPEC.loader.exec_module(DRAGON)


class WindowsDragonPlanTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def source(self, text, name='Example.dragon'):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding='utf-8')
        return str(path)

    def test_string_and_inline_versions_create_hash_bound_exact_plan(self):
        import hashlib
        path = self.source('[project]\nlabview-version=2026\nvipc=""\n[vipm.dependencies]\n'
                           'oglib_boolean="6.0.0.9"\noglib_array={version="6.0.1.20"}\n')
        plan = DRAGON.windows_install_plan([path])
        self.assertEqual(plan['specs'], ['oglib_array@6.0.1.20', 'oglib_boolean@6.0.0.9'])
        self.assertEqual(plan['files'][0]['labview_version'], '2026')
        self.assertEqual(plan['files'][0]['sha256'], hashlib.sha256(Path(path).read_bytes()).hexdigest())

    def test_malformed_and_unsupported_declarations_are_not_silently_dropped(self):
        for text in ('[vipm.dependencies\nbad="1.0"',
                     '[other.dependencies]\npackage="1.0"',
                     '[project]\nactivity="run-code"',
                     '[project]\nvipc="other.vipc"',
                     '[project]\nlabview-version=2026\nlabview_version=2025',
                     '[nipm.dependencies]\nni_driver="1.0"',
                     '[nipm.feeds]\nfeed="https://example.test"',
                     '[vipm.feeds]\nfeed="https://example.test"',
                     '[vipm.dependencies]\npackage={version="1.0",feed="private"}',
                     '[vipm.dependencies]\npackage={version="1.0", extra=true}',
                     '[vipm.dependencies]\npackage=true',
                     '[vipm.dependencies]\npackage=">=1.0"',
                     '[vipm.dependencies]\npackage="latest"',
                     '[vipm.dependencies]\npackage=""',
                     '[vipm.dependencies]\n"--argument"="1.0"'):
            with self.subTest(text=text):
                with self.assertRaises(DRAGON.DragonParseError):
                    DRAGON.windows_install_plan([self.source(text)])

    def test_missing_selected_source_fails(self):
        with self.assertRaisesRegex(DRAGON.DragonParseError, 'regular file'):
            DRAGON.windows_install_plan([str(self.root / 'Missing.dragon')])

    def test_selected_source_cannot_escape_repository(self):
        source = self.source('[vipm.dependencies]\npkg="1.0"\n')
        (self.root / 'repository').mkdir()
        with self.assertRaisesRegex(DRAGON.DragonParseError, 'outside the repository'):
            DRAGON.windows_install_plan([source], repository_root=str(self.root / 'repository'))

    def test_conflicting_versions_or_target_years_fail(self):
        first = self.source('[project]\nlabview-version=2026\n[vipm.dependencies]\npkg="1.0"\n')
        for text in ('[vipm.dependencies]\npkg="2.0"\n',
                     '[project]\nlabview-version=2025\n[vipm.dependencies]\nother="1.0"\n'):
            with self.subTest(text=text):
                with self.assertRaisesRegex(DRAGON.DragonParseError, 'conflicting|differs from Windows target'):
                    DRAGON.windows_install_plan([first, self.source(text, 'Other.dragon')])

    def test_same_basename_sources_cannot_overwrite_each_other(self):
        first = self.source('[vipm.dependencies]\npkg="1.0"\n', 'one/Same.dragon')
        second = self.source('[vipm.dependencies]\nother="1.0"\n', 'two/Same.dragon')
        with self.assertRaisesRegex(DRAGON.DragonParseError, 'duplicate'):
            DRAGON.windows_install_plan([first, second])

    def test_empty_and_duplicate_compatible_declarations(self):
        self.assertEqual(DRAGON.windows_install_plan([]), {
            'schema': 1, 'platform': 'windows', 'labview_version': '2026',
            'labview_bitness': '64', 'files': [], 'specs': [],
        })
        first = self.source('[nipm]\n[vipm.dependencies]\npkg="1.0-1"\n')
        second = self.source('[vipm.dependencies]\npkg={version="1.0-1"}\n', 'Other.dragon')
        self.assertEqual(DRAGON.windows_install_plan([first, second])['specs'], ['pkg@1.0-1'])

    def stage(self, dragon_text, config='', vipc=False, expected_exit=0):
        root = INSTALLER.parents[3]
        workflow = (root / '.github/workflows/build-labview-image.yml').read_text()
        stage = workflow.split('- name: Stage repo VIPC and Dragon files', 1)[1]
        stage = stage.split("          python -I - <<'PY'\n", 1)[1]
        stage = stage.split('\n          PY', 1)[0]
        code = '\n'.join(line[10:] for line in stage.splitlines())
        parser_dest = self.root / '.github/labview/dragon_deps.py'
        parser_dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(PARSER_PATH, parser_dest)
        self.source(dragon_text, 'example/Example.dragon')
        config_path = self.root / '.github/labview-ci.yml'
        if config:
            config_path.write_text(config)
        elif config_path.exists():
            config_path.unlink()
        if vipc:
            with zipfile.ZipFile(self.root / 'Dependencies.vipc', 'w') as archive:
                archive.writestr('config.xml', '<VI_Package_Configuration/>')
        environment = dict(os.environ, GITHUB_OUTPUT=str(self.root / 'outputs'))
        environment['PYTHONPATH'] = str(self.root / 'shadow')
        result = subprocess.run([os.sys.executable, '-I', '-c', code], cwd=self.root, env=environment,
                                text=True, capture_output=True, timeout=30)
        self.assertEqual(result.returncode, expected_exit, result.stdout + result.stderr)
        return self.root / '.github/labview/vipm'

    def push_gate(self, config, expected_exit=0, changed_path='example/Example.dragon'):
        root = INSTALLER.parents[3]
        workflow = (root / '.github/workflows/build-labview-image.yml').read_text()
        gate = workflow.split('- name: Decide whether to rebuild', 1)[1]
        gate = gate.split("          python -I - <<'PY'\n", 1)[1].split('\n          PY', 1)[0]
        code = '\n'.join(line[10:] for line in gate.splitlines())
        parser_dest = self.root / '.github/labview/dragon_deps.py'
        parser_dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(PARSER_PATH, parser_dest)
        (self.root / '.github/labview-ci.yml').write_text(config)
        code = (
            "import subprocess\nfrom unittest.mock import patch\n"
            "patch('subprocess.run', return_value=subprocess.CompletedProcess("
            "[], 0, " + repr(changed_path + '\n') + ", '')).start()\n"
        ) + code
        output = self.root / 'gate-output'
        output.unlink(missing_ok=True)
        result = subprocess.run(
            [os.sys.executable, '-I', '-c', code], cwd=self.root,
            env=dict(os.environ, GITHUB_OUTPUT=str(output), EVENT_NAME='push', BEFORE='abc', SHA='def'),
            text=True, capture_output=True, timeout=30)
        self.assertEqual(result.returncode, expected_exit, result.stdout + result.stderr)
        return output.read_text()

    def test_actual_stage_and_push_gate_share_yaml_scalar_flow_and_block_semantics(self):
        for platforms in ('windows', '"windows"', "'windows'", '[windows]', '[windows, linux]',
                          'windows+linux', '\n          - windows\n          - linux',
                          '"windows" # selected Windows'):
            with self.subTest(platforms=platforms):
                config = ('config:\n  container:\n    dragon:\n'
                          '      - path: "example/Example.dragon" # source\n'
                          '        monitor: true # enable\n'
                          f'        monitorOn: {platforms}\n')
                staged = self.stage('[vipm.dependencies]\npkg="1.0"\n', config=config)
                self.assertEqual(json.loads((staged / 'dragon-plan.json').read_text())['specs'], ['pkg@1.0'])
                self.assertEqual(self.push_gate(config), 'build=true\n')

    def test_actual_stage_and_push_gate_reject_bad_monitor_shapes_and_yaml(self):
        for setting in ('monitor: "true"', 'monitor: "false"', 'monitor: 1', 'monitor: null',
                        'monitor: true\n        monitorOn: {windows: true}',
                        'monitor: true\n        monitorOn: [windows, 1]',
                        'monitor: true\n        monitorOn: null',
                        'monitor: true\n        monitorOn: macos',
                        'monitor: true\n        monitorOn: [windows'):
            with self.subTest(setting=setting):
                config = ('config:\n  container:\n    dragon:\n'
                          f"      - path: 'example/Example.dragon'\n        {setting}\n")
                self.stage('[vipm.dependencies]\npkg="1.0"\n', config=config, expected_exit=1)
                self.assertEqual(self.push_gate(config, expected_exit=1), 'build=false\n')

    def test_actual_stage_and_push_gate_preserve_quoted_paths_with_spaces_and_hash(self):
        path = 'example/My # deps.dragon'
        self.source('[vipm.dependencies]\npkg="1.0"\n', path)
        config = ('config:\n  container:\n    dragon:\n'
                  f"      - path: '{path}' # quoted path\n"
                  '        monitor: true\n        monitorOn: [windows]\n')
        staged = self.stage('[invalid TOML', config=config)
        plan = json.loads((staged / 'dragon-plan.json').read_text())
        self.assertEqual(plan['specs'], ['pkg@1.0'])
        self.assertEqual(plan['files'][0]['source_file'], path)
        self.assertEqual(self.push_gate(config, changed_path=path), 'build=true\n')

    def test_actual_stage_and_push_gate_reject_invalid_list_and_path_shapes(self):
        for entries in ('null', '{}', 'true', '"example/Example.dragon"',
                        '[{path: true, monitor: true}]', '[{monitor: true}]'):
            with self.subTest(entries=entries):
                config = f'config:\n  container:\n    dragon: {entries}\n'
                self.stage('[vipm.dependencies]\npkg="1.0"\n', config=config, expected_exit=1)
                self.assertEqual(self.push_gate(config, expected_exit=1), 'build=false\n')

    def test_actual_stage_and_push_gate_preserve_linux_and_empty_list_optouts(self):
        for entry in ('    dragon: [] # opt out\n',
                      '    dragon:\n      - path: "example/Example.dragon"\n'
                      '        monitor: true\n        monitorOn: [linux]\n',
                      '    dragon:\n      - path: "example/Example.dragon"\n        monitor: false\n'):
            config = 'config:\n  container:\n' + entry
            with self.subTest(entry=entry):
                staged = self.stage('[invalid TOML', config=config)
                self.assertEqual(json.loads((staged / 'dragon-plan.json').read_text())['files'], [])
                self.assertEqual(self.push_gate(config), 'build=false\n')

    def test_actual_workflow_stages_dragon_only_and_vipc_plus_dragon(self):
        for vipc in (False, True):
            with self.subTest(vipc=vipc):
                staged = self.stage('[vipm.dependencies]\noglib_boolean="6.0.0.9"\n', vipc=vipc)
                plan = json.loads((staged / 'dragon-plan.json').read_text())
                self.assertEqual(plan['specs'], ['oglib_boolean@6.0.0.9'])
                self.assertTrue((staged / 'Example.dragon').is_file())
                self.assertEqual((staged / 'Dependencies.vipc').is_file(), vipc)

    def test_actual_workflow_respects_empty_and_other_platform_monitoring(self):
        for entry in ('    dragon: []\n',
                      '    dragon:\n      - path: example/Example.dragon\n        monitor: false\n',
                      '    dragon:\n      - path: example/Example.dragon\n        monitor: true\n        monitorOn: linux\n'):
            with self.subTest(entry=entry):
                staged = self.stage('[vipm.dependencies]\noglib_boolean="6.0.0.9"\n',
                                    config='config:\n  container:\n' + entry)
                self.assertEqual(json.loads((staged / 'dragon-plan.json').read_text())['specs'], [])
                self.assertFalse((staged / 'Example.dragon').exists())

    def test_isolated_staging_ignores_client_imports_and_startup_hooks(self):
        shadow = self.root / 'shadow'
        shadow.mkdir()
        tooling = self.root / '.github/labview'
        tooling.mkdir(parents=True)
        poison = "raise RuntimeError('Client-controlled Python import executed')\n"
        for directory in (self.root, shadow, tooling):
            for module in ('json.py', 'hashlib.py', 'sitecustomize.py', 'yaml.py'):
                (directory / module).write_text(poison)
        (self.root / 'dragon_deps.py').write_text(poison)
        staged = self.stage('[vipm.dependencies]\noglib_boolean="6.0.0.9"\n')
        self.assertEqual(json.loads((staged / 'dragon-plan.json').read_text())['specs'],
                         ['oglib_boolean@6.0.0.9'])
        config = ('config:\n  container:\n    dragon:\n'
                  '      - path: "example/Example.dragon"\n'
                  '        monitor: true\n        monitorOn: "windows"\n')
        self.assertEqual(self.push_gate(config), 'build=true\n')

    def test_opted_out_malformed_dragon_is_not_parsed(self):
        self.source('[invalid TOML', '.github/labview/vipm/Unmonitored.dragon')
        self.source('[invalid TOML', '.github/labview/vipm/Unmonitored.DRAGON')
        staged = self.stage('[broken TOML', config='config:\n  container:\n    dragon: [] # deliberately opt out\n')
        self.assertEqual(json.loads((staged / 'dragon-plan.json').read_text())['files'], [])
        self.assertFalse((staged / 'Unmonitored.dragon').exists())
        self.assertFalse((staged / 'Unmonitored.DRAGON').exists())

    def test_selected_dragon_can_already_live_inside_copy_directory(self):
        self.source('[vipm.dependencies]\npkg="1.0"\n', '.github/labview/vipm/Local.dragon')
        staged = self.stage('[broken TOML', config=(
            'config:\n  container:\n    dragon:\n'
            '      - path: .github/labview/vipm/Local.dragon\n        monitor: true\n        monitorOn: windows\n'
        ))
        self.assertEqual(json.loads((staged / 'dragon-plan.json').read_text())['specs'], ['pkg@1.0'])
        self.assertTrue((staged / 'Local.dragon').is_file())
        self.assertFalse((staged / 'Example.dragon').exists())

    def test_target_identity_is_validated_when_creating_plan(self):
        source = self.source('[project]\nlabview-version=2025\n[vipm.dependencies]\npkg="1.0"\n')
        with self.assertRaisesRegex(DRAGON.DragonParseError, 'differs from Windows target'):
            DRAGON.windows_install_plan([source])
        plan = DRAGON.windows_install_plan([source], target_year='2025', target_bitness='32')
        self.assertEqual((plan['platform'], plan['labview_version'], plan['labview_bitness']),
                         ('windows', '2025', '32'))
        for year, bitness in (('latest', '64'), ('2026', '128')):
            with self.subTest(year=year, bitness=bitness):
                with self.assertRaisesRegex(DRAGON.DragonParseError, 'invalid Windows Dragon target'):
                    DRAGON.windows_install_plan([], target_year=year, target_bitness=bitness)

    def test_selected_symlinks_are_rejected(self):
        source = Path(self.source('[vipm.dependencies]\npkg="1.0"\n'))
        link = self.root / 'Link.dragon'
        try:
            link.symlink_to(source)
        except OSError as exc:
            self.skipTest(f'Symlink creation unavailable: {exc}')
        with self.assertRaisesRegex(DRAGON.DragonParseError, 'symlink|regular file'):
            DRAGON.windows_install_plan([str(link)], repository_root=str(self.root))

    def test_workflow_fingerprint_and_docker_closure_include_dragon_plan(self):
        root = INSTALLER.parents[3]
        workflow = (root / '.github/workflows/build-labview-image.yml').read_text()
        self.assertIn("$files += '.github/labview/vipm/dragon-plan.json'", workflow)
        self.assertIn("$files += '.github/labview/dragon_deps.py'", workflow)
        docker = (root / '.github/docker/labview-ci.Dockerfile').read_text()
        self.assertIn('COPY .github/labview/vipm/ C:/vipm/', docker)
        self.assertIn('if ($vipcFiles -or $dragonFiles)', docker)
        self.assertNotIn('dragon apply', docker)
        self.assertIn("if ($LASTEXITCODE -ne 0) { throw", docker)


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
$LabVIEWVersion = '2026'
$LabVIEWBitness = '64'
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

    def test_hash_bound_dragon_plan_is_read_and_rejects_tampered_sources(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / 'Example.dragon'
            source.write_text('[project]\nlabview-version=2026\n[vipm.dependencies]\noglib_boolean="6.0.0.9"\n')
            (root / 'dragon-plan.json').write_text(json.dumps(DRAGON.windows_install_plan([str(source)])))
            escaped = str(root).replace("'", "''")
            output = self.run_script(f"""
$plan = Get-WindowsDragonPlan -Directory '{escaped}' -TargetYear '2026'
if (($plan.Specs -join ',') -ne 'oglib_boolean@6.0.0.9') {{ throw 'Wrong required specs' }}
Add-Content -LiteralPath (Join-Path '{escaped}' 'Example.dragon') -Value 'changed'
try {{ Get-WindowsDragonPlan -Directory '{escaped}' -TargetYear '2026'; throw 'Tampered source accepted' }}
catch {{ if ($_.Exception.Message -notlike '*changed since trusted TOML parsing*') {{ throw }} }}
'HASH-BOUND=True'
""")
            self.assertIn('HASH-BOUND=True', output)

    def test_dragon_plan_requires_matching_target_and_source_set(self):
        for scenario in ('missing-plan', 'wrong-year', 'missing-source', 'changed-specs', 'custom-hook',
                         'wrong-platform', 'wrong-bitness', 'unknown-setting', 'invalid-array',
                         'string-schema', 'array-schema', 'missing-source-year', 'invalid-source-type'):
            with self.subTest(scenario=scenario), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                source = root / 'Example.dragon'
                source.write_text('[project]\nlabview-version=2026\n[vipm.dependencies]\npkg="1.0"\n')
                plan = DRAGON.windows_install_plan([str(source)])
                if scenario != 'missing-plan':
                    if scenario == 'changed-specs':
                        plan['specs'] = ['unrelated@1.0']
                    elif scenario == 'wrong-platform':
                        plan['platform'] = 'linux'
                    elif scenario == 'wrong-bitness':
                        plan['labview_bitness'] = '32'
                    elif scenario == 'unknown-setting':
                        plan['execute'] = 'arbitrary-code'
                    elif scenario == 'invalid-array':
                        plan['files'][0]['specs'] = 'pkg@1.0'
                    elif scenario == 'string-schema':
                        plan['schema'] = '1'
                    elif scenario == 'array-schema':
                        plan['schema'] = []
                    elif scenario == 'missing-source-year':
                        del plan['files'][0]['labview_version']
                    elif scenario == 'invalid-source-type':
                        plan['files'][0]['source_file'] = ['Example.dragon']
                    (root / 'dragon-plan.json').write_text(json.dumps(plan))
                if scenario == 'missing-source':
                    source.unlink()
                if scenario == 'custom-hook':
                    (root / 'install-dragon.ps1').write_text("throw 'must not execute'\n")
                escaped = str(root).replace("'", "''")
                year = '2025' if scenario == 'wrong-year' else '2026'
                output = self.run_script(f"""
try {{ Get-WindowsDragonPlan -Directory '{escaped}' -TargetYear '{year}'; 'ACCEPTED'; exit 2 }}
catch {{ Write-Output "REJECTED=$($_.Exception.Message)" }}
""")
                self.assertIn('REJECTED=', output)
                self.assertNotIn('ACCEPTED', output)

    def test_dragon_empty_plan_is_valid_without_vipc_or_sources(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'dragon-plan.json').write_text(json.dumps(DRAGON.windows_install_plan([])))
            escaped = str(root).replace("'", "''")
            output = self.run_script(f"""
$plan = Get-WindowsDragonPlan -Directory '{escaped}' -TargetYear '2026'
if ($plan.Specs.Count) {{ throw 'Unexpected packages' }}
'EMPTY=True'
""")
            self.assertIn('EMPTY=True', output)

    def test_dragon_required_install_cannot_be_bypassed_by_allow_missing(self):
        for mode in ('nonzero', 'missing-command', 'dead-engine'):
            with self.subTest(mode=mode):
                output = self.run_script(f"""
$Env:VIPM_ALLOW_MISSING_PACKAGES = '1'
$script:VipmEngineDead = {'$true' if mode == 'dead-engine' else '$false'}
function Install-VipmSpecs {{
    if ('{mode}' -eq 'dead-engine') {{ throw 'Must not install against dead engine' }}
    if ('{mode}' -eq 'missing-command') {{ throw [System.Management.Automation.CommandNotFoundException]::new('VIPM command unavailable') }}
    return $false
}}
try {{ Install-RequiredWindowsDragon @('oglib_boolean@6.0.0.9'); 'ACCEPTED'; exit 2 }}
catch {{ Write-Output "REQUIRED-FAILED=$($_.Exception.Message)" }}
""")
                self.assertIn('REQUIRED-FAILED=', output)
                self.assertNotIn('ACCEPTED', output)

    def test_dragon_install_uses_shared_specs_helper(self):
        output = self.run_script("""
function Install-VipmSpecs {
    param($Specs)
    if (($Specs -join ',') -ne 'oglib_boolean@6.0.0.9') { throw 'Wrong install request' }
    return $true
}
Install-RequiredWindowsDragon @('oglib_boolean@6.0.0.9')
'REQUIRED=True'
""")
        self.assertIn('REQUIRED=True', output)

    def test_final_dragon_inventory_is_target_specific_and_fails_closed(self):
        good = 'Using LabVIEW 2026 (64-bit)'
        for banner, package, exit_code, success in (
                (good, 'oglib_boolean 6.0.0.9', 0, True),
                (good, '', 0, False),
                (good, 'oglib_boolean 6.0.0.10', 0, False),
                ('Using LabVIEW 2025 (64-bit)', 'oglib_boolean 6.0.0.9', 0, False),
                ('Using LabVIEW 2026 (32-bit)', 'oglib_boolean 6.0.0.9', 0, False),
                ('', 'oglib_boolean 6.0.0.9', 0, False),
                (good, 'oglib_boolean 6.0.0.9', 8, False)):
            with self.subTest(banner=banner, package=package, exit_code=exit_code):
                output = self.run_script(f"""
$Env:VIPM_ALLOW_MISSING_PACKAGES = '1'
$script:InventoryExit = {exit_code}
$script:Inventory = @('{banner}', '{package}')
try {{ Assert-FinalWindowsDragonInventory @('oglib_boolean@6.0.0.9'); 'VERIFIED=True' }}
catch {{ "VERIFIED=False $($_.Exception.Message)" }}
""")
                self.assertIn(f'VERIFIED={success}', output)

    def test_dragon_cannot_be_verified_against_dead_engine(self):
        output = self.run_script("""
$script:VipmEngineDead = $true
function Test-VipmSpecsInstalled { throw 'Must not query dead engine' }
try { Assert-FinalWindowsDragonInventory @('oglib_boolean@6.0.0.9'); exit 2 }
catch { $_.Exception.Message }
""")
        self.assertIn('FINAL required Windows Dragon target inventory', output)

    def test_dragon_and_vipc_conflicting_pins_rejected_before_installs(self):
        for vipc_spec, accepted in (('oglib_boolean@6.0.0.9', True),
                                    ('oglib_boolean@6.0.0.10', False),
                                    ('oglib_boolean', True),
                                    ('oglib_boolean>=6.0.0.9', True),
                                    ('oglib_boolean>=7.0.0.0', False)):
            with self.subTest(spec=vipc_spec):
                output = self.run_script(f"""
function Get-VipcPackageSpecs {{ '{vipc_spec}' }}
$files = @([pscustomobject]@{{ FullName = 'core.vipc'; Name = 'ci-tooling.vipc' }})
try {{ Assert-WindowsDragonVipcPins @('oglib_boolean@6.0.0.9') $files; 'ACCEPTED=True' }}
catch {{ 'ACCEPTED=False'; $_.Exception.Message }}
""")
                self.assertIn(f'ACCEPTED={accepted}', output)

    def test_dragon_required_install_and_final_gate_surround_all_tooling(self):
        output = self.run_script("""
$statements = $ast.EndBlock.Statements
$main = $statements | Where-Object { $_ -is [System.Management.Automation.Language.TryStatementAst] -and
    $_.Extent.Text.Contains('$installWorkdir = New-PublicRepoWorkdir') }
if (-not $main) { throw 'Installer main block not found' }
$text = $main.Body.Extent.Text
$required = $text.IndexOf('Install-RequiredWindowsDragon -Specs $dragonPlan.Specs')
$optional = $text.IndexOf('$earlyRaw = ')
$vipcs = $text.IndexOf('foreach ($vipc in $vipcFiles)')
$final = $text.IndexOf('Assert-FinalWindowsDragonInventory -Specs $dragonPlan.Specs')
if ($required -lt 0 -or $required -ge $optional -or $optional -ge $vipcs -or $vipcs -ge $final) {
    throw 'Required Dragon install/final gate ordering is incorrect'
}
'ORDER=True'
""")
        self.assertIn('ORDER=True', output)

    def test_dragon_only_does_not_take_no_vipc_early_exit(self):
        output = self.run_script("""
$vipcFiles = @()
$dragonPlan = [pscustomobject]@{ Specs = @('oglib_boolean@6.0.0.9') }
$guard = $ast.Find({
    param($node)
    $node -is [System.Management.Automation.Language.IfStatementAst] -and
    $node.Extent.Text.StartsWith('if ($vipcFiles.Count -eq 0 -and')
}, $true)
if (-not $guard) { throw 'No-input guard not found' }
. ([scriptblock]::Create($guard.Extent.Text))
'DRAGON-ONLY=True'
""")
        self.assertIn('DRAGON-ONLY=True', output)

    def test_actual_docker_hook_runs_for_dragon_only_and_propagates_failures(self):
        docker = (INSTALLER.parents[3] / '.github/docker/labview-ci.Dockerfile').read_text()
        hook = docker.split('RUN $vipcFiles = ', 1)[1].split('\n\n# Stamp', 1)[0]
        hook = '$vipcFiles = ' + hook.replace('`\n', '\n')
        for mode, failed in (('dragon-only', False), ('mixed', False),
                             ('nonzero', True), ('missing-command', True)):
            with self.subTest(mode=mode):
                output = self.run_script(f"""
$script:Calls = 0
function Get-ChildItem {{
    param($Path, $Filter, [switch]$Recurse, $ErrorAction)
    if ($Filter -eq '*.dragon') {{ return [pscustomobject]@{{ Name = 'Example.dragon' }} }}
    if ('{mode}' -eq 'mixed') {{ return [pscustomobject]@{{ Name = 'Project.vipc' }} }}
}}
function Test-Path {{ return $true }}
function powershell {{
    param([Parameter(ValueFromRemainingArguments = $true)] $Arguments)
    $script:Calls++
    if (($Arguments -join ' ') -ne '-NoLogo -NoProfile -ExecutionPolicy Bypass -File C:\\vipm\\install-vipc.ps1') {{
        throw 'Docker invoked the wrong dependency hook'
    }}
    if ('{mode}' -eq 'missing-command') {{ throw [System.Management.Automation.CommandNotFoundException]::new('installer unavailable') }}
    $global:LASTEXITCODE = $(if ('{mode}' -eq 'nonzero') {{ 8 }} else {{ 0 }})
}}
$failed = $false
try {{ {hook} }} catch {{ $failed = $true; $_.Exception.Message }}
if ($script:Calls -ne 1) {{ throw 'Dependency hook did not execute exactly once' }}
Write-Output "HOOK-FAILED=$failed"
""")
                self.assertIn(f'HOOK-FAILED={failed}', output)

    def test_actual_install_phase_rejects_later_tooling_upgrade_of_dragon_root(self):
        with tempfile.TemporaryDirectory() as directory:
            escaped = directory.replace("'", "''")
            output = self.run_script(f"""
$PublicRepoUrl = 'https://example.test/public.git'
$Env:VIPM_REQUIRED_PACKAGES = '-'
$Env:VIPM_EARLY_PACKAGES = '-'
$dragonPlan = [pscustomobject]@{{ Specs = @('oglib_boolean@6.0.0.9') }}
$vipcFiles = @([pscustomobject]@{{ Name = 'ci-tooling.vipc'; FullName = 'ci-tooling.vipc' }})
$script:InventoryExit = 0
$script:LastVipmOutput = ''
function New-PublicRepoWorkdir {{ return '{escaped}' }}
function Get-Command {{ return $null }}
function Install-VipmSpecs {{
    param($Specs)
    $script:Inventory = @('Using LabVIEW 2026 (64-bit)', 'oglib_boolean 6.0.0.9')
    return $true
}}
function Invoke-VipmInstall {{
    param([Parameter(ValueFromRemainingArguments = $true)] $Targets)
    $script:Inventory = @('Using LabVIEW 2026 (64-bit)', 'oglib_boolean 6.0.0.10')
    return 0
}}
function Invoke-VipmRefresh {{ return 0 }}
$main = $ast.EndBlock.Statements | Where-Object {{
    $_ -is [System.Management.Automation.Language.TryStatementAst] -and
    $_.Extent.Text.Contains('$installWorkdir = New-PublicRepoWorkdir')
}}
try {{
    . ([scriptblock]::Create($main.Extent.Text))
    throw 'Later tooling upgrade was accepted'
}} catch {{
    if ($_.Exception.Message -notlike 'FINAL required Windows Dragon*') {{ throw }}
    'LATER-UPGRADE-REJECTED=True'
}}
""")
            self.assertIn('LATER-UPGRADE-REJECTED=True', output)

    def test_worker_identity_changes_with_effective_dragon_inputs(self):
        repository = INSTALLER.parents[3]
        workflow = (repository / '.github/workflows/build-labview-image.yml').read_text()
        start = workflow.index("          $vipcFiles = @(Get-ChildItem", workflow.index('- name: Compute worker version'))
        end = workflow.index('          "version=$version"', start)
        code = '\n'.join(line[10:] for line in workflow[start:end].splitlines())
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name in ('.github/docker/labview-ci.Dockerfile',
                         '.github/labview/vipm/install-vipc.ps1',
                         '.github/labview/dragon_deps.py'):
                target = root / name
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(repository / name, target)
            source = root / '.github/labview/vipm/Example.dragon'
            source.write_text('[vipm.dependencies]\noglib_boolean="6.0.0.9"\n')
            plan = source.parent / 'dragon-plan.json'
            plan.write_text(json.dumps(DRAGON.windows_install_plan([str(source)])))
            escaped = str(root).replace("'", "''")
            output = self.run_script(f"""
Set-Location '{escaped}'
$seen = @()
$first = & {{ {code}; $version }}
if ($first -cnotmatch '^win-[0-9a-f]{{12}}$') {{ throw 'Invalid worker identity' }}
$seen += $first
foreach ($file in @(
    '.github/labview/vipm/Example.dragon',
    '.github/labview/vipm/dragon-plan.json',
    '.github/labview/dragon_deps.py',
    '.github/labview/vipm/install-vipc.ps1'
)) {{
    Add-Content -LiteralPath $file -Value ''
    $changed = & {{ {code}; $version }}
    if ($seen -contains $changed) {{ throw "Effective Dragon input reused worker identity: $file" }}
    $seen += $changed
}}
'WORKER-HASH=True'
""")
            self.assertIn('WORKER-HASH=True', output)

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
