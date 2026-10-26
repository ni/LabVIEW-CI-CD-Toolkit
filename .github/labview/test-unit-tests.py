import argparse
import ast
import base64
import importlib.util
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import textwrap
import unittest
import xml.etree.ElementTree as ET


sys.dont_write_bytecode = True
ROOT = Path(os.environ.get('LVCI_UNIT_TEST_RUNTIME_ROOT', Path(__file__).parent))
SPEC = importlib.util.spec_from_file_location('unit_report', ROOT / 'build-unittest-report.py')
REPORT = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(REPORT)


class UnitTestUpgradeTests(unittest.TestCase):
    def test_configure_pipeline_preserves_native_command_input(self):
        workflow = (ROOT.parent / 'workflows' / 'reconfigure.yml').read_text(encoding='utf-8')
        script = workflow.split("          python3 - <<'PY'\n", 1)[1].split('\n          PY', 1)[0]
        tree = ast.parse(textwrap.dedent(script))
        function = next(node for node in tree.body if isinstance(node, ast.FunctionDef)
                        and node.name == '_unit_tests_block_from_json')
        namespace = {'json': json, 're': re}
        exec(compile(ast.Module(body=[function], type_ignores=[]), 'reconfigure.yml', 'exec'), namespace)
        command = 'powershell -File "{dir}\\Invoke-CoreTests.ps1" -ResultsPath "{out}"'
        block = namespace['_unit_tests_block_from_json'](json.dumps({
            'tools': {'caraya': {'enabled': True, 'locations': ['tests'], 'command': command}}}))
        self.assertIn('      command: ' + json.dumps(command), block)
        with self.assertRaisesRegex(ValueError, 'single-line string'):
            namespace['_unit_tests_block_from_json'](json.dumps({
                'caraya': {'enabled': True, 'command': 'first\nsecond'}}))

    def test_upgrade_preserves_legacy_top_level_unit_tests(self):
        installer_spec = importlib.util.spec_from_file_location(
            'unit_test_installer', ROOT.parent / 'labview-ci' / 'install.py')
        installer = importlib.util.module_from_spec(installer_spec)
        installer_spec.loader.exec_module(installer)
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            manifest = workspace / '.github' / 'labview-ci.yml'
            manifest.parent.mkdir()
            tests = ('unitTests:\n  tools:\n    caraya:\n      enabled: true\n'
                     '      command: powershell -File "{dir}\\Invoke-CoreTests.ps1"\n')
            manifest.write_text('config:\n  os: [windows]\nactivities:\n  - unit-tests\n' +
                                tests, encoding='utf-8')
            installer.write_manifest(workspace, {'version': '4.18.6'}, ['unit-tests'],
                                     ['windows'], '2026', None, 'main', False)
            self.assertTrue(manifest.read_text(encoding='utf-8').endswith(tests))

    def test_upgrade_preserves_caraya_and_project_configuration(self):
        installer_spec = importlib.util.spec_from_file_location(
            'unit_test_installer', ROOT.parent / 'labview-ci' / 'install.py')
        installer = importlib.util.module_from_spec(installer_spec)
        installer_spec.loader.exec_module(installer)
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            manifest = workspace / '.github' / 'labview-ci.yml'
            manifest.parent.mkdir()
            project_config = (
                '  imageName: project-worker\n'
                '  unitTests:\n    tools:\n      caraya:\n        enabled: true\n'
                '        command: powershell -File "{dir}\\Invoke-CoreTests.ps1" -ResultsPath "{out}"\n'
                '        locations:\n          - "tests"\n'
                '  container:\n    vipc:\n      - path: "tests/Dependencies.vipc"\n        monitor: true\n'
                '  builds:\n    runAll: false\n'
                '  concurrency:\n    maxParallel: 2\n')
            manifest.write_text(
                'schemaVersion: 1\nconfig:\n  labviewVersion: "2025"\n'
                '  branch: old\n  os: [linux]\n' + project_config +
                'activities:\n  - unit-tests\n', encoding='utf-8')
            installer.write_manifest(workspace, {'version': '4.18.6'}, ['unit-tests'],
                                     ['windows'], '2026', None, 'main', False)
            updated = manifest.read_text(encoding='utf-8')
            self.assertIn(project_config, updated)
            self.assertIn('  labviewVersion: "2026"\n', updated)
            self.assertIn('  os: [windows]\n', updated)
            self.assertNotIn('  branch: old\n', updated)
            installer.write_manifest(workspace, {'version': '4.18.6'}, ['unit-tests'],
                                     ['windows'], '2026', 'replacement-worker', 'main', False)
            updated = manifest.read_text(encoding='utf-8')
            self.assertIn('  imageName: replacement-worker\n', updated)
            self.assertNotIn('project-worker', updated)
            self.assertIn(project_config.split('\n', 1)[1], updated)
            installer.write_manifest(workspace, {'version': 'next'}, ['unit-tests'],
                                     ['windows'], '2027', None, 'other', True)
            self.assertEqual(manifest.read_text(encoding='utf-8'), updated)


class UnitReportTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)

    def parse(self, xml):
        path = self.directory / 'caraya-0.xml'
        path.write_text(xml, encoding='utf-8')
        return REPORT.parse_junit(path, 'caraya', {})

    def test_caraya_assertions_and_failures(self):
        suites = self.parse('<testsuites><testsuite name="Core" tests="2">'
                            '<testcase name="passes"/><testcase name="fails">'
                            '<failure message="not equal">expected 2</failure></testcase>'
                            '</testsuite></testsuites>')
        self.assertEqual([case['status'] for case in suites[0]['cases']], ['passed', 'failed'])

    def test_malformed_report_is_an_error_not_zero_tests(self):
        suites = self.parse('<testsuites>')
        self.assertEqual(suites[0]['cases'][0]['status'], 'error')

    def test_non_junit_xml_is_an_error(self):
        suites = self.parse('<not-a-report/>')
        self.assertEqual(suites[0]['cases'][0]['status'], 'error')

    def test_cases_without_a_suite_are_an_error(self):
        suites = self.parse('<testsuites><testcase name="not in a suite"/></testsuites>')
        self.assertEqual(suites[0]['cases'][0]['status'], 'error')

    def test_empty_report_is_an_error(self):
        suites = self.parse('<testsuites><testsuite name="Core" tests="0"/></testsuites>')
        self.assertEqual(suites[0]['cases'][0]['status'], 'error')

    def test_declared_counts_cannot_hide_missing_assertions(self):
        suites = self.parse('<testsuite name="Core" tests="44" failures="0">'
                            '<testcase name="only one"/></testsuite>')
        self.assertTrue(any(case['status'] == 'error' for suite in suites for case in suite['cases']))

    def test_all_skipped_is_not_a_passing_run(self):
        self.assertEqual(REPORT.classify(0, 0, 0, 3), ('failed', 0))

    def test_nested_suites_count_each_assertion_once(self):
        suites = self.parse('<testsuite name="parent" tests="1"><testsuite name="child" tests="1">'
                            '<testcase name="assertion"/></testsuite></testsuite>')
        cases = [case for suite in suites for case in suite['cases']]
        self.assertEqual([case['status'] for case in cases], ['passed'])


@unittest.skipUnless(os.name == 'nt', 'Windows PowerShell runner')
class UnitRunnerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='lvci unit tests ')
        self.addCleanup(self.temporary.cleanup)
        self.workspace = Path(self.temporary.name)
        self.results = self.workspace / 'results'
        self.results.mkdir()
        (self.workspace / 'tests').mkdir()
        self.config = self.workspace / 'config.yml'

    def configure(self, command, location='tests'):
        self.config.write_text(
            'config:\n  unitTests:\n    tools:\n      caraya:\n        enabled: true\n'
            f'        command: {command}\n        locations:\n          - "{location}"\n'
            '  unrelated:\n    tools:\n      not-a-test:\n        enabled: true\n',
            encoding='utf-8')

    def command(self, body):
        encoded = base64.b64encode(body.encode('utf-16-le')).decode('ascii')
        return f'powershell.exe -NoProfile -NonInteractive -EncodedCommand {encoded}'

    def run_driver(self):
        return subprocess.run(
            ['powershell.exe', '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass',
             '-File', str(ROOT / 'run-unit-tests.ps1'), '-WorkspaceRoot', str(self.workspace),
             '-ResultsDir', str(self.results), '-ConfigPath', str(self.config),
             '-LabVIEWPath', sys.executable], capture_output=True, text=True, timeout=45)

    def errors(self):
        return [node for path in self.results.glob('*.xml')
                for node in ET.parse(path).getroot().iter('error')]

    def test_nested_config_preserves_command_quotes_and_boundary(self):
        self.configure('powershell -File "{dir}\\Invoke-CoreTests.ps1"')
        script = str(ROOT / 'run-unit-tests.ps1').replace("'", "''")
        config = str(self.config).replace("'", "''")
        code = (f"$ast = [System.Management.Automation.Language.Parser]::ParseFile('{script}',"
                " [ref]$null, [ref]$null); $reader = $ast.Find({ param($node)"
                " $node -is [System.Management.Automation.Language.FunctionDefinitionAst]"
                " -and $node.Name -eq 'Read-UnitTestTools' }, $true);"
                f" Invoke-Expression $reader.Extent.Text; $tools = Read-UnitTestTools '{config}';"
                " if ($tools.Count -ne 1) { throw 'Unexpected tool count' };"
                " [pscustomobject]$tools[0] | ConvertTo-Json -Depth 5")
        result = subprocess.run(['powershell.exe', '-NoProfile', '-Command', code],
                                capture_output=True, text=True, check=True, timeout=15)
        tools = json.loads(result.stdout)
        self.assertEqual(tools['tool'], 'caraya')
        self.assertEqual(tools['command'], 'powershell -File "{dir}\\Invoke-CoreTests.ps1"')
        self.assertEqual(tools['locations'], ['tests'])

    def test_runner_nonzero_is_reported_and_fails(self):
        self.configure('cmd.exe /c exit 7')
        result = self.run_driver()
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue(self.errors())

    def test_missing_junit_is_reported_and_fails(self):
        self.configure('cmd.exe /c exit 0')
        result = self.run_driver()
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue(self.errors())

    def test_no_enabled_framework_is_reported_and_fails(self):
        self.config.write_text('config:\n  unitTests:\n    tools: {}\n', encoding='utf-8')
        result = self.run_driver()
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue(self.errors())
        self.assertEqual(REPORT.tool_for_filename('unit-tests-runner-0.xml'), 'runner')

    def test_unresolved_location_is_reported_and_fails(self):
        self.configure('cmd.exe /c exit 0', 'missing')
        result = self.run_driver()
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue(self.errors())

    def test_stale_report_cannot_pass(self):
        (self.results / 'caraya-0.xml').write_text(
            '<testsuite><testcase name="stale"/></testsuite>', encoding='utf-8')
        self.configure('cmd.exe /c exit 0')
        result = self.run_driver()
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertFalse((self.results / 'caraya-0.xml').exists())

    def test_successful_assertions_produce_unified_report(self):
        output = str(self.results / 'caraya-0.xml').replace("'", "''")
        xml = '<testsuites><testsuite name="Core"><testcase name="assertion"/></testsuite></testsuites>'
        self.configure(self.command(f"[System.IO.File]::WriteAllText('{output}', '{xml}')"))
        result = self.run_driver()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        args = argparse.Namespace(results=str(self.results), junit=[], workspace=str(self.workspace),
                                  sha='a' * 40, platform='windows', repo='owner/repo', pages_url='',
                                  labview_version='2026', commit_msg='', author='', date='', meta=None)
        data = REPORT.build_data(args)
        self.assertEqual(data['summary']['passed'], 1)
        self.assertEqual(data['summary']['errored'], 0)
        self.assertEqual(data['meta']['tools'][0]['id'], 'caraya')

    def test_command_with_quoted_paths_and_tokens(self):
        wrapper = self.workspace / 'tests' / 'Invoke-CoreTests.ps1'
        wrapper.write_text(
            'param([string]$ResultsPath, [string]$LabVIEWPath)\n'
            "if (-not (Test-Path -LiteralPath $LabVIEWPath)) { exit 7 }\n"
            "[System.IO.File]::WriteAllText($ResultsPath, '<testsuite><testcase name=\"quoted\"/></testsuite>')",
            encoding='utf-8')
        self.configure(json.dumps('powershell.exe -NoProfile -NonInteractive -File "{dir}\\Invoke-CoreTests.ps1" '
                      '-ResultsPath "{out}" -LabVIEWPath "{lv}"'))
        result = self.run_driver()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue((self.results / 'caraya-0.xml').exists())

    def test_caraya_is_a_required_worker_dependency(self):
        installer = str(ROOT / 'vipm' / 'install-vipc.ps1').replace("'", "''")
        code = ("Remove-Item Env:VIPM_REQUIRED_PACKAGES -ErrorAction SilentlyContinue;"
                f" $ast = [System.Management.Automation.Language.Parser]::ParseFile('{installer}',"
                " [ref]$null, [ref]$null); $assignment = $ast.Find({ param($node)"
                " $node -is [System.Management.Automation.Language.AssignmentStatementAst]"
                " -and $node.Left.Extent.Text -eq '$requiredRaw' }, $true);"
                " Invoke-Expression $assignment.Extent.Text; Write-Output $requiredRaw")
        result = subprocess.run(['powershell.exe', '-NoProfile', '-Command', code],
                                capture_output=True, text=True, check=True, timeout=15)
        self.assertIn('jki_lib_caraya@1.4.4.148', result.stdout.strip().split(','))

    def test_docker_hook_propagates_required_bake_failure(self):
        dockerfile = (ROOT.parent / 'docker' / 'labview-ci.Dockerfile').read_text(encoding='utf-8')
        hook = dockerfile.split('RUN $vipcFiles = ', 1)[1].split('# Optional Dragon', 1)[0]
        hook = '$vipcFiles = ' + hook
        code = ("$ErrorActionPreference = 'Stop';"
                " function Get-ChildItem { ,@([pscustomobject]@{Name='Tests.vipc'}) };"
                " function Test-Path { $true };"
                " function powershell { $global:LASTEXITCODE = 9 }; " + hook)
        result = subprocess.run(['powershell.exe', '-NoProfile', '-Command', code],
                                capture_output=True, text=True, timeout=15)
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('Required VIPM dependency bake failed with exit 9', result.stderr)


if __name__ == '__main__':
    unittest.main()