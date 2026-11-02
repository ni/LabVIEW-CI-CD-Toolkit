#!/usr/bin/env python3
"""Exercise the Linux runner, install selection and report with fake LabVIEWCLI."""
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import unittest
import xml.etree.ElementTree as ET
import zipfile

ROOT = Path(__file__).resolve().parent


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


REPORT = load('linux_unit_report', ROOT / 'build-unittest-report.py')
INSTALL = load('linux_unit_install', ROOT.parent / 'labview-ci/install.py')


@unittest.skipIf(os.name == 'nt', 'POSIX fake CLI')
class LinuxRunnerTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='lvci Linux tests ')
        self.addCleanup(temporary.cleanup)
        self.workspace = Path(temporary.name).resolve()
        self.results = self.workspace / 'ci-out/unit-tests/results'
        self.config = self.workspace / '.github/labview-ci.yml'
        self.config.parent.mkdir()
        self.cli = self.workspace / 'fake cli'
        self.cli.write_text('''#!/usr/bin/env python3
import json, os, pathlib, subprocess, sys
args = sys.argv[1:]
pathlib.Path(os.environ['CAPTURE']).write_text(json.dumps(args))
mode = os.environ.get('MODE', 'pass')
if mode == 'child':
    child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])
    pathlib.Path(os.environ['CAPTURE'] + '.pid').write_text(str(child.pid))
if mode == 'missing':
    log = pathlib.Path(os.environ['CAPTURE'] + '.log')
    log.write_text('Error -350053: missing or bad files')
    print('Started logging in file: ' + str(log))
    sys.exit(255)
if mode == 'no-report': sys.exit(0)
out = pathlib.Path(args[args.index('-ReportPath') + 1])
case = '<failure message="assertion failed"/>' if mode == 'failed' else '<skipped/>' if mode == 'skipped' else ''
out.write_text('<testsuite name="Core" tests="1"><testcase name="example">' + case + '</testcase></testsuite>')
if mode == 'nonzero': sys.exit(7)
''')
        self.cli.chmod(0o755)
        self.capture = self.workspace / 'argv.json'
        self.env = dict(os.environ, LABVIEWCLI=str(self.cli), LABVIEW_EXE='/fake/LabVIEW-2026/labviewprofull', CAPTURE=str(self.capture))
        self.addCleanup(self.stop_child)

    def stop_child(self):
        pid = Path(str(self.capture) + '.pid')
        if pid.exists():
            try:
                os.kill(int(pid.read_text()), 15)
            except ProcessLookupError:
                pass

    def configure(self, tools=None, nested=True):
        tools = tools if tools is not None else {'lunit': {'enabled': True, 'locations': []}}
        lines = ['unitTests:', '  tools:']
        for tool, settings in tools.items():
            lines += [f'    {tool}:', '      enabled: ' + str(settings.get('enabled', True)).lower()]
            if 'command' in settings:
                lines += ['      command: ' + json.dumps(settings['command'])]
            if settings.get('locations'):
                lines += ['      locations:'] + ['        - ' + json.dumps(p) for p in settings['locations']]
            else:
                lines += ['      locations: []']
        self.config.write_text(('config:\n' if nested else '') + '\n'.join(('  ' if nested else '') + line for line in lines) + '\n')

    def run_runner(self, mode='pass'):
        return subprocess.run(['bash', str(ROOT / 'run-unit-tests.sh'), str(self.workspace), str(self.results)],
                              env=dict(self.env, MODE=mode), capture_output=True, text=True, timeout=10)

    def cases(self):
        return [case for path in self.results.glob('*.xml') for case in ET.parse(path).iter('testcase')]

    def test_nested_config_spaces_literal_metacharacters_and_default_arguments(self):
        location = 'tests $(touch PWNED); "quoted"'
        (self.workspace / location).mkdir()
        self.configure({'lunit': {'locations': [location]}})
        result = self.run_runner()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        args = json.loads(self.capture.read_text())
        self.assertEqual(args[args.index('-Path') + 1], str(self.workspace / location))
        self.assertEqual(args[-1], '-Headless')
        self.assertFalse((self.workspace / 'PWNED').exists())
        self.assertEqual(len(self.cases()), 1)

    def test_top_level_config_and_single_quoted_locations(self):
        (self.workspace / "test's").mkdir()
        self.config.write_text("unitTests:\n  tools:\n    lunit:\n      enabled: true\n      locations:\n        - 'test''s'\n")
        self.assertEqual(self.run_runner().returncode, 0)
        self.assertEqual(json.loads(self.capture.read_text())[5], str(self.workspace / "test's"))

    def test_autodetect_excludes_tooling_and_collapses_nested_roots(self):
        for directory in ['tests', 'tests/nested', '.github/hidden', 'ci-out/old', 'build/tmp']:
            path = self.workspace / directory
            path.mkdir(parents=True, exist_ok=True)
            (path / 'Test.vi').write_bytes(b'LabVIEW\x00LUnit.lvlib')
        result = self.run_runner()
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertEqual(len(self.cases()), 1)
        self.assertIn(str(self.workspace / 'tests'), json.loads(self.capture.read_text()))

    def test_explicit_disabled_block_never_autodetects(self):
        (self.workspace / 'Test.vi').write_bytes(b'LUnit.lvlib')
        self.configure({'lunit': {'enabled': False}})
        self.assertNotEqual(self.run_runner().returncode, 0)
        self.assertFalse(self.capture.exists())

    def test_explicit_empty_mapping_never_autodetects(self):
        (self.workspace / 'Test.vi').write_bytes(b'LUnit.lvlib')
        self.config.write_text('config:\n  unitTests: {}\n')
        self.assertNotEqual(self.run_runner().returncode, 0)
        self.assertFalse(self.capture.exists())

    def test_quoted_scalars_allow_trailing_yaml_comments(self):
        (self.workspace / 'tests').mkdir()
        self.config.write_text('unitTests:\n  tools:\n    lunit:\n      enabled: true\n'
                               '      locations:\n        - "tests" # a directory\n')
        result = self.run_runner()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_non_linux_tools_have_informational_findings(self):
        self.configure({'caraya': {}, 'utf': {}, 'vi-tester': {}})
        self.assertEqual(self.run_runner().returncode, 0)
        findings = json.loads((self.results / '_tooling.json').read_text())['missing']
        self.assertEqual(len(findings), 3)
        self.assertTrue(all(x['kind'] == 'unsupported-on-linux' for x in findings))
        self.assertIn('Not run on Linux', REPORT.unsupported_note_html(findings))
        self.assertEqual(REPORT.tooling_banner_html(findings, ''), '')

    def test_globs_with_spaces_and_empty_matches(self):
        path = self.workspace / 'some tests'
        path.mkdir()
        (path / 'Test.lvclass').touch()
        self.configure({'lunit': {'locations': ['**/*.lvclass']}})
        self.assertEqual(self.run_runner().returncode, 0)
        self.assertIn(str(path), json.loads(self.capture.read_text()))
        self.configure({'lunit': {'locations': ['absent/*.lvclass']}})
        self.assertNotEqual(self.run_runner().returncode, 0)
        self.assertEqual(len(self.cases()), 1)
        self.assertIsNotNone(self.cases()[0].find('error'))

    def test_stale_results_cannot_hide_missing_tooling_or_no_report(self):
        self.configure()
        self.assertEqual(self.run_runner().returncode, 0)
        self.assertNotEqual(self.run_runner('missing').returncode, 0)
        self.assertEqual(len(self.cases()), 1)
        self.assertIsNotNone(self.cases()[0].find('error'))
        self.assertEqual(json.loads((self.results / '_tooling.json').read_text())['missing'][0]['kind'], 'missing-tooling')
        self.assertNotEqual(self.run_runner('no-report').returncode, 0)
        self.assertEqual(json.loads((self.results / '_tooling.json').read_text())['missing'][0]['kind'], 'error')
        self.assertEqual(self.run_runner().returncode, 0)
        self.assertFalse((self.results / '_tooling.json').exists())

    def test_missing_executable_and_no_tools_fail(self):
        self.assertNotEqual(self.run_runner().returncode, 0)
        self.configure()
        self.env['LABVIEWCLI'] = '/no/such/cli'
        self.assertNotEqual(self.run_runner().returncode, 0)
        self.assertIsNotNone(self.cases()[0].find('error'))

    def test_nonzero_exit_with_valid_xml_adds_runner_error(self):
        self.configure()
        self.assertNotEqual(self.run_runner('nonzero').returncode, 0)
        self.assertEqual(len(self.cases()), 2)
        self.assertTrue(any(c.find('error') is not None for c in self.cases()))

    def test_test_assertion_results_are_preserved(self):
        self.configure()
        self.assertEqual(self.run_runner('failed').returncode, 0)
        self.assertIsNotNone(self.cases()[0].find('failure'))
        self.assertEqual(self.run_runner('skipped').returncode, 0)
        self.assertIsNotNone(self.cases()[0].find('skipped'))

    def test_custom_argv_and_inherited_child_stdout_do_not_hang(self):
        self.configure({'lunit': {'command': '"{cli}" -OperationName LUnit -Path "{dir}" -ReportPath "{out}" -LabVIEWPath "{lv}" -ver {ver} -Headless'}})
        result = self.run_runner('child')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        args = json.loads(self.capture.read_text())
        self.assertEqual(args[args.index('-ver') + 1], '2026')

    def test_malformed_command_fails_and_custom_paths_cannot_inject_commands(self):
        self.configure({'lunit': {'command': '"unterminated'}})
        self.assertNotEqual(self.run_runner().returncode, 0)
        self.assertIsNotNone(self.cases()[0].find('error'))
        path = self.workspace / 'tests $(touch PWNED)'
        path.mkdir()
        self.configure({'lunit': {'locations': [path.name], 'command':
                                 '"{cli}" -Path "{dir}" -ReportPath "{out}" -Headless'}})
        self.assertEqual(self.run_runner().returncode, 0)
        self.assertIn(str(path), json.loads(self.capture.read_text()))
        self.assertFalse((self.workspace / 'PWNED').exists())


class LinuxIntegrationTests(unittest.TestCase):
    def test_install_file_selection_and_native_gitlab_mapping(self):
        catalog = json.loads((ROOT.parent / 'labview-ci/catalog.json').read_text())
        _, provider = INSTALL.load_gitlab_provider(ROOT.parents[1])
        for platforms in [['windows'], ['linux'], ['windows', 'linux']]:
            files = INSTALL.resolve_file_list(catalog, ['unit-tests'], platforms)
            self.assertEqual('.github/labview/run-unit-tests.sh' in files, 'linux' in platforms)
            self.assertEqual('.github/labview/run-unit-tests.ps1' in files, 'windows' in platforms)
            selected = INSTALL.gitlab_supported_activities(catalog, provider, ['unit-tests'], platforms)
            pipeline = INSTALL.gitlab_pipeline_yml(selected, platforms, '2026', provider)
            self.assertEqual('templates/unit-tests-linux.yml' in pipeline, 'linux' in platforms)

    def test_vipc_contains_linux_capable_lunit_cli(self):
        with zipfile.ZipFile(ROOT / 'vipm/ci-tooling.vipc') as archive:
            config = archive.read('config.xml')
            self.assertIn(b'astemes_lib_lunit_cli-1.7.1.32', config)
            self.assertIn('astemes_lib_lunit_cli-1.7.1.32.spec', archive.namelist())
            self.assertNotIn(b'astemes_lib_lunit_cli-1.6.1.23', config)

    @unittest.skipIf(os.name == 'nt', 'Bash revision parser')
    def test_vipm_revision_suffix_preserved(self):
        # Execute the real production function, without invoking VIPM or starting LabVIEW.
        text = (ROOT / 'vipm/install-vipc-linux.sh').read_text()
        function = text.split('package_install_spec() {', 1)[1].split('\n}\n', 1)[0]
        shell = 'package_install_spec() {' + function + '\n}\n'
        for package, expected in [('jki_rsc_toolkits_palette-1.1-1', 'jki_rsc_toolkits_palette@1.1-1'),
                                  ('astemes_lib_lunit_cli-1.7.1.32', 'astemes_lib_lunit_cli@1.7.1.32'),
                                  ('name-with-dashes-2.0-12', 'name-with-dashes@2.0-12'),
                                  ('unversioned', 'unversioned')]:
            result = subprocess.run(['bash', '-c', shell + '\npackage_install_spec "$1"', 'test', package], capture_output=True, text=True, check=True)
            self.assertEqual(result.stdout.strip(), expected)

    def test_platform_note_escapes_tool_names(self):
        note = REPORT.unsupported_note_html([{'name': '<script>alert(1)</script>', 'kind': 'unsupported-on-linux'}])
        self.assertNotIn('<script>', note)
        self.assertIn('&lt;script&gt;', note)

    @unittest.skipIf(os.name == 'nt', 'POSIX GitLab job')
    def test_native_gitlab_job_uses_report_result_and_runner_exit(self):
        template = (ROOT.parent / 'labview-ci/providers/gitlab/templates/unit-tests-linux.yml').read_text()
        body = template.split('    - |\n', 1)[1].split('\n  artifacts:', 1)[0]
        script = '\n'.join(line[6:] for line in body.splitlines())
        for mode, expected in [('pass', 0), ('failed', 1), ('skipped', 1), ('empty', 1), ('runner', 1), ('pull', 1)]:
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as directory:
                workspace = Path(directory)
                report_script = workspace / '.github/labview/build-unittest-report.py'
                report_script.parent.mkdir(parents=True)
                shutil.copy2(ROOT / 'build-unittest-report.py', report_script)
                stage_script = workspace / '.gitlab/labview-ci/scripts/stage-report.py'
                stage_script.parent.mkdir(parents=True)
                shutil.copy2(ROOT.parent / 'labview-ci/providers/gitlab/scripts/stage-report.py', stage_script)
                bin_dir = workspace / 'bin'
                bin_dir.mkdir()
                docker = bin_dir / 'docker'
                docker.write_text('''#!/usr/bin/env python3
import os, pathlib, sys
mode = os.environ['MODE']
if sys.argv[1] == 'pull': sys.exit(1 if mode == 'pull' else 0)
if mode == 'runner': sys.exit(127)
if mode == 'empty': sys.exit(0)
case = '<failure message="assertion failed"/>' if mode == 'failed' else '<skipped/>' if mode == 'skipped' else ''
pathlib.Path('ci-out/unit-tests/results/lunit-0.xml').write_text('<testsuite tests="1"><testcase name="example">' + case + '</testcase></testsuite>')
''')
                docker.chmod(0o755)
                env = dict(os.environ, MODE=mode, PATH=str(bin_dir) + os.pathsep + os.environ['PATH'],
                           CI_PROJECT_DIR=str(workspace), CI_PROJECT_PATH='example/client', CI_COMMIT_SHA='a' * 40,
                           CI_PAGES_URL='https://example.gitlab.io/client', CI_REGISTRY_IMAGE='registry.example/client',
                           LVCI_LINUX_IMAGE='', LVCI_USE_CUSTOM_IMAGE='false', LVCI_LABVIEW_VERSION='2026')
                result = subprocess.run(['sh', '-c', script], cwd=workspace, env=env, capture_output=True, text=True)
                self.assertEqual(result.returncode, expected, result.stdout + result.stderr)
                staged = workspace / ('ci-out/gitlab-pages/unit-tests/' + 'a' * 40 + '/linux')
                self.assertTrue((staged / 'index.html').is_file())
                self.assertEqual(json.loads((staged / 'results.json').read_text())['meta']['platform'], 'linux')

    @unittest.skipUnless(shutil.which('node'), 'Node.js for platform-switch UI')
    def test_report_platform_switch_refreshes_notices(self):
        source = (ROOT / 'build-unittest-report.py').read_text()
        function = source.split('function renderTooling(){', 1)[1].split('function renderAll(){', 1)[0]
        harness = '''
const assert = require('node:assert/strict');
const host = {innerHTML:'', children:[], replaceChildren(){this.children=[];}, appendChild(n){this.children.push(n);}};
const document = {getElementById(){return host;}, createElement(){return {setAttribute(){}};}};
const SELF_PLATFORM = 'windows', SELF_TOOLING_HTML = '<div>Windows tooling</div>';
let CUR='linux', D={tooling:{missing:[{name:'<script>unsafe</script>',kind:'unsupported-on-linux'}]}};
'''
        harness += 'function renderTooling(){' + function
        harness += '''
renderTooling();
assert.equal(host.children.length, 1);
assert.match(host.children[0].textContent, /Not run on Linux/);
assert.equal(host.children[0].innerHTML, undefined);
D={tooling:{missing:[{name:'LUnit',kind:'missing-tooling'}]}};
renderTooling();
assert.equal(host.children.length,1);
assert.match(host.children[0].textContent,/Container is missing required tooling/);
D={}; renderTooling(); assert.equal(host.children.length,0);
CUR='windows'; renderTooling(); assert.equal(host.innerHTML, SELF_TOOLING_HTML);
'''
        subprocess.run(['node', '-e', harness], check=True, capture_output=True, text=True)

    @unittest.skipUnless(os.name != 'nt' and shutil.which('jq'), 'POSIX workflow status checks')
    def test_actual_github_status_step_fails_closed(self):
        workflow = (ROOT.parent / 'workflows/unit-tests-linux-container.yml').read_text()
        step = workflow.split('      - name: Post commit status\n', 1)[1].split('\n      - name:', 1)[0]
        script = '\n'.join(line[10:] for line in step.split('        run: |\n', 1)[1].splitlines())
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            report_dir = workspace / 'ci-out/unit-tests'
            report_dir.mkdir(parents=True)
            bin_dir = workspace / 'bin'
            bin_dir.mkdir()
            curl = bin_dir / 'curl'
            curl.write_text('#!/usr/bin/env python3\nimport pathlib,sys\npathlib.Path("status.json").write_text(sys.argv[-1])\n')
            curl.chmod(0o755)
            for name, summary, pull, runner, expected in [
                ('passing', {'tests': 1, 'passed': 1}, 'success', 'success', 'success'),
                ('empty', {'tests': 0, 'passed': 0}, 'success', 'success', 'failure'),
                ('skipped', {'tests': 1, 'passed': 0, 'skipped': 1}, 'success', 'success', 'failure'),
                ('assertion', {'tests': 1, 'failed': 1}, 'success', 'success', 'failure'),
                ('runner', {'tests': 1, 'passed': 1}, 'success', 'failure', 'failure'),
                ('pull', {'tests': 1, 'passed': 1}, 'failure', 'skipped', 'failure'),
            ]:
                with self.subTest(name=name):
                    (report_dir / 'results.json').write_text(json.dumps({'summary': summary}))
                    values = {'github.workspace': str(workspace), 'steps.report.outputs.exists': 'true',
                              'steps.pull.outcome': pull, 'steps.run.outcome': runner}
                    executable = re.sub(r'\$\{\{\s*(.*?)\s*\}\}', lambda m: values.get(m[1], 'test'), script)
                    result = subprocess.run(['bash', '-e', '-c', executable], cwd=workspace,
                                            env=dict(os.environ, PATH=str(bin_dir) + os.pathsep + os.environ['PATH'], GH_TOKEN='fake'),
                                            capture_output=True, text=True)
                    self.assertEqual(json.loads((workspace / 'status.json').read_text())['state'], expected)
                    self.assertEqual(result.returncode == 0, expected == 'success', result.stderr)


if __name__ == '__main__':
    unittest.main()
