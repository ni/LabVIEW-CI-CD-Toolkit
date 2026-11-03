"""Portable composite regression checks; Windows runner cases skip explicitly elsewhere."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

import yaml

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[2]


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, ROOT / path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


WORKER = load('composite_worker', 'actions/worker-image/worker.py')
UNIT = load('composite_unit', 'actions/unit-tests/run.py')
D1 = 'sha256:' + 'a' * 64
D2 = 'sha256:' + 'b' * 64


class CompositeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='composite tests ')
        self.addCleanup(self.temp.cleanup)
        self.workspace = Path(self.temp.name).resolve()
        self.context = self.workspace / 'context'
        self.deps = self.workspace / 'deps'
        self.deps.mkdir()
        self.inputs = {'LABVIEW_VERSION': '2026', 'LVCI_UNIT_TESTS_INSTALLED': 'true',
                       'VIPM_PUBLIC_REPO_URL': 'https://github.com/ni/LabVIEW-CI-CD-Toolkit.git'}

    def test_action_inputs_never_interpolated_into_shell(self):
        for name in ('unit-tests', 'worker-image'):
            action = yaml.safe_load((ROOT / f'actions/{name}/action.yml').read_text())
            self.assertEqual(action['runs']['using'], 'composite')
            for step in action['runs']['steps']:
                self.assertNotIn('${{', step.get('run', ''))
                self.assertFalse(step.get('continue-on-error', False))
        worker = yaml.safe_load((ROOT / 'actions/worker-image/action.yml').read_text())
        self.assertTrue(worker['inputs']['base-image']['required'])
        self.assertNotIn('default', worker['inputs']['base-image'])

    def test_current_source_assets_and_mirrored_documentation(self):
        self.assertEqual((ROOT / '.github/pages/documentation.html').read_bytes(),
                         (ROOT / 'actions/dashboard/documentation.html').read_bytes())
        self.assertFalse((ROOT / 'actions/unit-tests/run-unit-tests.ps1').exists())
        self.assertFalse((ROOT / 'actions/worker-image/install-vipc.ps1').exists())
        WORKER.stage(self.context, self.workspace, '', True)
        for name in ('install-vipc.ps1', 'ci-tooling.vipc'):
            self.assertEqual((self.context / '.github/labview/vipm' / name).read_bytes(),
                             (ROOT / '.github/labview/vipm' / name).read_bytes())
        dockerfile = (self.context / 'Dockerfile').read_text()
        self.assertIn('COPY .github/labview/vipm/ C:/vipm/', dockerfile)
        self.assertIn('Get-Command dragon -ErrorAction Stop', dockerfile)
        self.assertIn('Required Dragon bake failed', dockerfile)
        self.assertIn("$env:VIPM_REQUIRED_PACKAGES = '-'", dockerfile)

    def test_hash_covers_payload_paths_bytes_build_args_and_base(self):
        (self.deps / 'project.vipc').write_bytes(b'vipc')
        (self.deps / 'local-1.0.vip').write_bytes(b'first')
        WORKER.stage(self.context, self.workspace, 'deps', False)
        identity = {'base': 'base@' + D1, **self.inputs}
        original = WORKER.fingerprint(self.context, identity)
        self.assertEqual(original, WORKER.fingerprint(self.context, identity))
        self.assertNotEqual(original, WORKER.fingerprint(self.context, {**identity, 'base': 'base@' + D2}))
        for key in self.inputs:
            self.assertNotEqual(original, WORKER.fingerprint(self.context, {**identity, key: 'different'}))
        payload = self.context / '.github/labview/vipm/local-1.0.vip'
        payload.write_bytes(b'changed')
        self.assertNotEqual(original, WORKER.fingerprint(self.context, identity))
        payload.write_bytes(b'first')
        payload.rename(payload.with_name('renamed-1.0.vip'))
        self.assertNotEqual(original, WORKER.fingerprint(self.context, identity))

    def test_missing_nested_collision_reserved_and_orphan_dependencies_rejected(self):
        for filename in ('ci-tooling.vipc', 'ci-tooling-project.vipc'):
            with self.subTest(filename=filename), tempfile.TemporaryDirectory() as context:
                path = self.deps / filename
                path.write_bytes(b'project')
                with self.assertRaisesRegex(ValueError, 'Reserved'):
                    WORKER.stage(Path(context), self.workspace, 'deps', True)
                path.unlink()
        with self.assertRaisesRegex(ValueError, 'existing directory'):
            WORKER.stage(self.context, self.workspace, 'missing', False)
        (self.deps / 'nested').mkdir()
        (self.deps / 'nested/hidden.vipc').write_bytes(b'project')
        with tempfile.TemporaryDirectory() as context, self.assertRaisesRegex(ValueError, 'nested'):
            WORKER.stage(Path(context), self.workspace, 'deps', False)
        (self.deps / 'nested/hidden.vipc').unlink()
        (self.deps / 'only-1.0.vip').write_bytes(b'local')
        with tempfile.TemporaryDirectory() as context, self.assertRaisesRegex(ValueError, 'Loose'):
            WORKER.stage(Path(context), self.workspace, 'deps', False)

    def test_deps_escape_and_symlink_rejected(self):
        with self.assertRaisesRegex(ValueError, 'inside the workspace'):
            WORKER.stage(self.context, self.workspace, '..', False)
        (self.deps / 'link.vipc').symlink_to(ROOT / '.github/labview/vipm/ci-tooling.vipc')
        with tempfile.TemporaryDirectory() as context, self.assertRaisesRegex(ValueError, 'links'):
            WORKER.stage(Path(context), self.workspace, 'deps', False)

    def test_registry_only_explicit_manifest_absence_builds(self):
        for error in ('UNAUTHORIZED', 'DENIED', 'timeout', '429 Too Many Requests'):
            with self.subTest(error=error), patch.object(WORKER.subprocess, 'run',
                    return_value=subprocess.CompletedProcess([], 1, '', error)):
                with self.assertRaises(RuntimeError):
                    WORKER.digest('image:tag', allow_missing=True)
        with patch.object(WORKER.subprocess, 'run', return_value=subprocess.CompletedProcess([], 1, '', 'MANIFEST_UNKNOWN: missing')):
            self.assertIsNone(WORKER.digest('image:tag', allow_missing=True))
        with patch.object(WORKER.subprocess, 'run', return_value=subprocess.CompletedProcess([], 1, '', 'NAME_UNKNOWN: repository missing')):
            self.assertIsNone(WORKER.digest('image:tag', allow_missing=True))
        with patch.object(WORKER.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, 'not a digest', '')):
            with self.assertRaises(ValueError):
                WORKER.digest('image:tag')

    def test_immutable_reference_strips_tag_keeps_registry_port(self):
        self.assertEqual(WORKER.immutable('registry:5000/path/image:2026', D1), 'registry:5000/path/image@' + D1)
        self.assertEqual(WORKER.immutable('registry:5000/path/image@' + D1, D1), 'registry:5000/path/image@' + D1)

    def test_base_platform_must_be_windows_amd64_before_build_or_copy(self):
        with patch.object(WORKER, 'run', return_value='{"os":"windows","architecture":"amd64"}') as registry:
            WORKER.verify_base('base@' + D1)
            self.assertEqual(registry.call_args.args[0], ['crane', 'config', '--platform', 'windows/amd64', 'base@' + D1])
        for config in ('{"os":"linux","architecture":"amd64"}', '{"os":"windows","architecture":"arm64"}', '{}'):
            with self.subTest(config=config), patch.object(WORKER, 'run', return_value=config), self.assertRaises(ValueError):
                WORKER.verify_base('base@' + D1)

    def test_worker_main_stages_current_context_emits_outputs_and_cleans_up(self):
        env = {**os.environ, 'GITHUB_WORKSPACE': str(self.workspace), 'RUNNER_TEMP': str(self.workspace),
               'GITHUB_REPOSITORY': 'Owner/Project', 'GITHUB_OUTPUT': str(self.workspace / 'output'),
               'WI_BASE': 'registry:5000/base:2026', 'WI_VERSION': '2026', 'WI_TOOLING': 'true', 'WI_LATEST': 'false'}
        contexts = []

        def publish(context, image, tag, base, inputs, deps, latest):
            contexts.append(context)
            self.assertTrue((context / '.github/labview/vipm/install-vipc.ps1').is_file())
            self.assertEqual(image, 'ghcr.io/owner/project-labview')
            self.assertEqual(base, 'registry:5000/base@' + D1)
            self.assertTrue(deps)
            self.assertFalse(latest)
            return D2, True

        with patch.object(WORKER, 'os', types.SimpleNamespace(name='nt', environ=env)), \
                patch.object(WORKER, 'digest', return_value=D1), patch.object(WORKER, 'verify_base') as verify, \
                patch.object(WORKER, 'publish', side_effect=publish):
            WORKER.main()
        verify.assert_called_once_with('registry:5000/base@' + D1)
        output = (self.workspace / 'output').read_text()
        self.assertIn('image=ghcr.io/owner/project-labview', output)
        self.assertIn('digest=' + D2, output)
        self.assertIn('built=true', output)
        self.assertFalse(contexts[0].exists())

    def test_build_pins_base_sets_memory_and_passes_all_build_args(self):
        with patch.object(WORKER, 'digest', side_effect=[None, D1]), patch.object(WORKER, 'run') as registry, \
                patch.object(WORKER.subprocess, 'run') as native:
            final, built = WORKER.publish(self.context, 'ghcr.io/owner/image', 'win-test', 'base@' + D2,
                                          self.inputs, True, True)
        self.assertTrue(built)
        self.assertEqual(final, D1)
        commands = [c.args[0] for c in native.call_args_list]
        build = next(c for c in commands if c[:2] == ['docker', 'build'])
        self.assertIn('8GB', build)
        self.assertIn('LCWC_BASE_IMAGE=base@' + D2, build)
        for key, value in self.inputs.items():
            self.assertIn(f'{key}={value}', build)
        self.assertEqual(registry.call_args_list[-1].args[0], ['crane', 'tag', 'ghcr.io/owner/image@' + D1, 'latest'])

    def test_reuse_never_runs_docker_and_verifies_before_tagging(self):
        with patch.object(WORKER, 'digest', side_effect=[D1, D1]), patch.object(WORKER, 'run') as registry, \
                patch.object(WORKER.subprocess, 'run') as native:
            final, built = WORKER.publish(self.context, 'image', 'win-test', 'base@' + D2, self.inputs, True, False)
        self.assertFalse(built)
        native.assert_not_called()
        self.assertEqual(registry.call_args.args[0], ['crane', 'tag', 'image@' + D1, '2026'])
        with patch.object(WORKER, 'digest', side_effect=[D1, D2]), patch.object(WORKER, 'run') as registry:
            with self.assertRaisesRegex(RuntimeError, 'changed concurrently'):
                WORKER.publish(self.context, 'image', 'win-test', 'base@' + D2, self.inputs, True, True)
            registry.assert_not_called()

    def test_copy_uses_resolved_base_and_no_daemon(self):
        with patch.object(WORKER, 'digest', side_effect=[None, D1]), patch.object(WORKER, 'run') as registry, \
                patch.object(WORKER.subprocess, 'run') as native:
            WORKER.publish(self.context, 'image', 'win-test', 'base@' + D2, self.inputs, False, False)
        native.assert_not_called()
        self.assertEqual(registry.call_args_list[0].args[0], ['crane', 'copy', 'base@' + D2, 'image:win-test'])

    def test_build_push_final_digest_and_alias_errors_propagate(self):
        for fail_at in (0, 1, 2):
            with self.subTest(fail_at=fail_at), patch.object(WORKER, 'digest', return_value=None), \
                    patch.object(WORKER, 'run') as registry, patch.object(WORKER.subprocess, 'run',
                    side_effect=[None] * fail_at + [subprocess.CalledProcessError(7, 'native')]):
                with self.assertRaises(subprocess.CalledProcessError):
                    WORKER.publish(self.context, 'image', 'win-test', 'base@' + D2, self.inputs, True, True)
                registry.assert_not_called()
        with patch.object(WORKER, 'digest', side_effect=[D1, RuntimeError('cannot verify')]), patch.object(WORKER, 'run') as registry:
            with self.assertRaises(RuntimeError):
                WORKER.publish(self.context, 'image', 'win-test', 'base@' + D2, self.inputs, True, True)
            registry.assert_not_called()
        with patch.object(WORKER, 'digest', side_effect=[D1, D1]), patch.object(WORKER, 'run', side_effect=RuntimeError('tag failed')):
            with self.assertRaises(RuntimeError):
                WORKER.publish(self.context, 'image', 'win-test', 'base@' + D2, self.inputs, True, True)

    def test_unit_failure_policy(self):
        summary = {'tests': 2, 'passed': 1, 'failed': 1, 'errored': 0}
        self.assertEqual(UNIT.verdict(summary, 0, True), 1)
        self.assertEqual(UNIT.verdict(summary, 0, False), 0)
        self.assertEqual(UNIT.verdict(summary, 7, False), 1)
        self.assertEqual(UNIT.verdict({**summary, 'errored': 1}, 0, False), 1)
        self.assertEqual(UNIT.verdict(dict(tests=0, passed=0, failed=0, errored=0), 0, False), 1)
        self.assertEqual(UNIT.verdict(dict(tests=2, passed=0, failed=0, errored=0), 0, False), 1)

    def test_report_paths_are_confined(self):
        for path in ('', '.', '..', '.github/output', '.git/output'):
            with self.subTest(path=path), self.assertRaises(ValueError):
                UNIT.report_path(self.workspace, path)
        self.assertEqual(UNIT.report_path(self.workspace, 'ci-out/tests'), self.workspace / 'ci-out/tests')
        for module in (UNIT, WORKER):
            with self.assertRaises(ValueError):
                module.single_line('value\nINJECTED=true')

    def test_unit_host_runs_current_renderer_and_preserves_input_bytes(self):
        env = {**os.environ, 'GITHUB_WORKSPACE': str(self.workspace), 'RUNNER_TEMP': str(self.workspace),
               'GITHUB_REPOSITORY': 'owner/project', 'GITHUB_SHA': 'a' * 40,
               'GITHUB_OUTPUT': str(self.workspace / 'output'), 'UT_REPORT_DIR': 'ci-out/tests',
               'UT_IMAGE': 'image:tag', 'UT_FRAMEWORK': 'custom', 'UT_TEST_DIR': "tests/a'b & x",
               'UT_COMMAND': 'runner "{dir}" --out "{out}"', 'UT_LV_VERSION': '2026', 'UT_FAIL': 'false'}
        report = self.workspace / 'ci-out/tests'
        (report / 'results').mkdir(parents=True)
        (report / 'results/old.xml').write_text('<testsuite><testcase name="stale"/></testsuite>')
        (report / 'index.html').write_text('old')
        calls = []

        def native(args, **kwargs):
            calls.append(args)
            if args[:2] == ['docker', 'run']:
                self.assertFalse((report / 'results/old.xml').exists())
                text = Path(args[args.index('--env-file') + 1]).read_text()
                self.assertIn('LVCI_COMMAND=' + env['UT_COMMAND'] + '\n', text)
                self.assertIn('LVCI_TEST_DIR=' + env['UT_TEST_DIR'] + '\n', text)
                self.assertIn('C:\\ci-toolkit\\.github\\labview\\run-unit-tests.ps1', args)
                self.assertNotIn('-LabVIEWPath', args)  # keep the current autodetector
                (report / 'results/custom.xml').write_text('<testsuite><testcase name="fresh"/></testsuite>')
            return subprocess.CompletedProcess(args, 0)

        with patch.object(UNIT, 'os', types.SimpleNamespace(name='nt', environ=env)), \
                patch.object(UNIT.subprocess, 'run', side_effect=native), patch.object(sys, 'argv', []):
            self.assertEqual(UNIT.main(), 0)
        self.assertEqual(json.loads((report / 'results.json').read_text())['summary']['tests'], 1)
        self.assertIn('tests=1', (self.workspace / 'output').read_text())
        self.assertEqual(calls[1], ['docker', 'pull', 'image:tag'])

    def test_unit_host_pull_run_and_report_errors_never_succeed(self):
        env = {**os.environ, 'GITHUB_WORKSPACE': str(self.workspace), 'RUNNER_TEMP': str(self.workspace),
               'GITHUB_REPOSITORY': 'owner/project', 'GITHUB_SHA': 'a' * 40,
               'GITHUB_OUTPUT': str(self.workspace / 'output'), 'UT_REPORT_DIR': 'ci-out/tests',
               'UT_IMAGE': 'image:tag', 'UT_FRAMEWORK': 'custom', 'UT_LV_VERSION': '2026', 'UT_FAIL': 'false'}
        # A previous passing XML and summary must never rescue a Docker or renderer failure.
        for failure in ('pull', 'run-exception', 'run-nonzero', 'absent-xml', 'report'):
            with self.subTest(failure=failure):
                report = self.workspace / 'ci-out/tests'
                (report / 'results').mkdir(parents=True, exist_ok=True)
                (report / 'results/old.xml').write_text('<testsuite><testcase name="stale"/></testsuite>')
                (report / 'results.json').write_text('{"summary":{"tests":1,"passed":1}}')

                def native(args, **kwargs):
                    if args[:2] == ['docker', 'pull'] and failure == 'pull':
                        raise subprocess.CalledProcessError(1, args)
                    if args[:2] == ['docker', 'run']:
                        if failure == 'run-exception':
                            raise OSError('docker could not launch')
                        if failure in ('run-nonzero', 'report'):
                            (report / 'results/custom.xml').write_text('<testsuite><testcase name="fresh"/></testsuite>')
                        return subprocess.CompletedProcess(args, 7 if failure == 'run-nonzero' else 0)
                    return subprocess.CompletedProcess(args, 0)

                with patch.object(UNIT, 'os', types.SimpleNamespace(name='nt', environ=env)), \
                        patch.object(UNIT.subprocess, 'run', side_effect=native), patch.object(sys, 'argv', []):
                    if failure == 'report':
                        with patch.object(UNIT, 'render_report', side_effect=RuntimeError('renderer failed')), \
                                self.assertRaisesRegex(RuntimeError, 'renderer failed'):
                            UNIT.main()
                    elif failure in ('pull', 'run-exception'):
                        with self.assertRaises((subprocess.CalledProcessError, OSError)):
                            UNIT.main()
                    else:
                        self.assertEqual(UNIT.main(), 1)

    def test_hosted_ci_covers_composites_on_windows_and_linux(self):
        workflow = yaml.safe_load((ROOT / '.github/workflows/composite-action-checks.yml').read_text())
        # PyYAML's YAML 1.1 parser treats the unquoted `on` key as True.
        events = workflow.get('on', workflow.get(True))
        for event in ('pull_request', 'push'):
            self.assertIn('actions/unit-tests/**', events[event]['paths'])
            self.assertIn('actions/worker-image/**', events[event]['paths'])
            self.assertIn('.github/labview/test-composite-actions.py', events[event]['paths'])
        job = workflow['jobs']['contracts']
        self.assertEqual(set(job['strategy']['matrix']['os']), {'ubuntu-latest', 'windows-2022'})
        self.assertTrue(any(step.get('run') == 'python .github/labview/test-composite-actions.py'
                            for step in job['steps']))


@unittest.skipUnless(os.name == 'nt', 'Requires Windows PowerShell; no LabVIEW needed')
class ExplicitRunnerTests(unittest.TestCase):
    def test_native_cli_nonzero_cannot_pass_even_with_success_xml(self):
        runner = str(ROOT / '.github/labview/run-unit-tests.ps1').replace("'", "''")
        with tempfile.TemporaryDirectory(prefix='native runner ') as directory:
            workspace = str(Path(directory)).replace("'", "''")
            for framework in ('utf', 'lunit'):
                with self.subTest(framework=framework):
                    code = (
                        "$ErrorActionPreference='Stop'; $errors=$null;"
                        f" $ast=[System.Management.Automation.Language.Parser]::ParseFile('{runner}', [ref]$null, [ref]$errors);"
                        " if ($errors.Count) { throw ($errors | Out-String) };"
                        " foreach ($name in @('Write-RunnerFailure','Invoke-UtfTests','Invoke-LUnitTests')) {"
                        " $node=$ast.Find({ param($n) $n -is [System.Management.Automation.Language.FunctionDefinitionAst]"
                        " -and $n.Name -eq $name }, $true); Invoke-Expression $node.Extent.Text };"
                        f" $WorkspaceRoot='{workspace}'; $ResultsDir='{workspace}'; $Framework='{framework}';"
                        " $CliExe='fake'; $LabVIEWPath='fake'; $LabVIEWVersion='2026';"
                        " $UTF_DEFAULT_CMD='fake'; $LUNIT_DEFAULT_CMD='fake'; $Script:RunnerFailed=$false;"
                        " function Resolve-UtfProjects { Join-Path $WorkspaceRoot 'Demo.lvproj' };"
                        " function Resolve-TestRoots { $WorkspaceRoot }; function Show-UtfAddonsDiag {};"
                        " function cmd.exe { [IO.File]::WriteAllText($out, '<testsuite><testcase name=\"passing\"/></testsuite>');"
                        " $global:LASTEXITCODE=7; 'CLI returned an error after writing XML' };"
                        f" $tool=@{{tool='{framework}';command='';locations=@()}};"
                        f" if ('{framework}' -eq 'utf') {{ Invoke-UtfTests $tool 0 }} else {{ Invoke-LUnitTests $tool 0 }};"
                        " if (-not $Script:RunnerFailed) { throw 'Native CLI failure was swallowed' };"
                        f" if (-not (Test-Path (Join-Path $ResultsDir '{framework}-runner-0.xml'))) {{ throw 'Missing failure XML' }}"
                    )
                    result = subprocess.run(['powershell.exe', '-NoProfile', '-NonInteractive', '-Command', code],
                                            capture_output=True, text=True, timeout=30)
                    self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_single_framework_overrides_config_and_honors_quoted_command(self):
        with tempfile.TemporaryDirectory(prefix='explicit runner ') as directory:
            workspace = Path(directory)
            (workspace / 'tests').mkdir()
            (workspace / '.github').mkdir()
            (workspace / '.github/labview-ci.yml').write_text('unitTests:\n  tools: {}\n')
            script = workspace / 'write.ps1'
            script.write_text('param($Output)\n[IO.File]::WriteAllText($Output, '\
                              "'<testsuite><testcase name=\"explicit\"/></testsuite>')")
            command = f'powershell.exe -NoProfile -File "{script}" -Output "{{out}}"'
            env = {**os.environ, 'LVCI_FRAMEWORK': 'lunit', 'LVCI_TEST_DIR': 'tests', 'LVCI_COMMAND': command}
            result = subprocess.run(['powershell.exe', '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass',
                    '-File', str(ROOT / '.github/labview/run-unit-tests.ps1'), '-WorkspaceRoot', str(workspace),
                    '-ResultsDir', str(workspace / 'results'), '-LabVIEWPath', sys.executable],
                    env=env, capture_output=True, text=True, timeout=45)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertTrue((workspace / 'results/lunit-0.xml').is_file())
            env['LVCI_COMMAND'] = 'cmd.exe /c exit 7'
            result = subprocess.run(['powershell.exe', '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass',
                    '-File', str(ROOT / '.github/labview/run-unit-tests.ps1'), '-WorkspaceRoot', str(workspace),
                    '-ResultsDir', str(workspace / 'results'), '-LabVIEWPath', sys.executable],
                    env=env, capture_output=True, text=True, timeout=45)
            self.assertNotEqual(result.returncode, 0)
            self.assertTrue((workspace / 'results/lunit-runner-0.xml').is_file())


if __name__ == '__main__':
    unittest.main()
