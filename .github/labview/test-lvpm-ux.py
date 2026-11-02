import ast
import copy
from contextlib import contextmanager
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import yaml


ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = yaml.safe_load((ROOT / '.github/workflows/reconfigure.yml').read_text())
STEPS = WORKFLOW['jobs']['reconfigure']['steps']
APPLY = next(step for step in STEPS if step['name'] == 'Apply settings to config files')
SCRIPT = APPLY['run'].split("python3 - <<'PY'\n", 1)[1].rsplit('\nPY', 1)[0]
TAG = 'linux-lvpm-' + 'a' * 64
DIGEST = 'sha256:' + 'b' * 64
SELECTION = TAG + '@' + DIGEST
MANIFEST = ('schemaVersion: 1\nconfig:\n  labviewVersion: "2026"\n'
            '  os: [windows, linux]\n  container:\n    use: win-existing\n'
            f'    linuxUse: "{SELECTION}"\n'
            '    actions:\n      masscompile: base\n      vidiff: win-pinned\n'
            'activities:\n  - dashboard\n')


@contextmanager
def working_directory(directory):
    previous = Path.cwd()
    os.chdir(directory)
    try:
        yield
    finally:
        os.chdir(previous)


def dashboard_functions(*names):
    tree = ast.parse((ROOT / 'actions/dashboard/dashboard.py').read_text())
    nodes = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name in names]
    namespace = {'os': os, 're': re, 'json': json, 'sys': sys, 'pages_url': 'https://pages.invalid'}
    exec(compile(ast.Module(body=nodes, type_ignores=[]), 'dashboard.py', 'exec'), namespace)
    return namespace


class ReconfigureLvpmTests(unittest.TestCase):
    def preservation_fixture(self):
        document = yaml.safe_load(MANIFEST)
        document.update({'provider': 'github', 'installedVersion': 'scratch-unreleased-lvpm',
                         'source': {'repo': 'ni/LabVIEW-CI-CD-Toolkit', 'ref': 'pinned'},
                         'futureMetadata': {'retained': True}})
        config = document['config']
        config['container']['vipc'] = [{'path': 'project.vipc', 'monitor': True, 'monitorOn': 'linux'}]
        config['container']['dragon'] = [{'path': 'project.dragon', 'monitor': True, 'monitorOn': 'linux'}]
        config['dependencyPolicy'] = {'requireInstalled': False, 'autoInstall': False}
        config['futureSettings'] = {'retained': ['linux', 'windows']}
        config['viAnalyzer'] = {'default': 'custom.viancfg', 'rules': [{'config': 'none', 'paths': 'skip/**'}]}
        document['unitTests'] = {'tools': {'custom-tool': {'enabled': False, 'locations': ['tests']}}}
        return document

    def assert_preserved(self, actual, expected):
        actual.pop('configuredAt', None)
        self.assertEqual(actual, expected)

    def test_activation_preserves_complete_manifest(self):
        original = self.preservation_fixture()
        original['config']['container'].pop('linuxUse')
        expected = copy.deepcopy(original)
        expected['config']['container']['linuxUse'] = SELECTION
        result, text = self.apply_settings(SELECTION, initial=yaml.safe_dump(original))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assert_preserved(yaml.safe_load(text), expected)

    def test_reset_preserves_complete_manifest(self):
        original = self.preservation_fixture()
        expected = copy.deepcopy(original)
        expected['config']['container'].pop('linuxUse')
        result, text = self.apply_settings('reset', initial=yaml.safe_dump(original))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assert_preserved(yaml.safe_load(text), expected)

    def test_blank_inputs_are_byte_preserving(self):
        initial = '# Retain client comments on a no-op.\n' + yaml.safe_dump(self.preservation_fixture())
        result, text = self.apply_settings(initial=initial)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(text, initial)

    def test_explicit_setting_changes_do_not_rewrite_other_fields(self):
        original = self.preservation_fixture()
        expected = copy.deepcopy(original)
        expected['config']['concurrency'] = {'maxParallel': 3}
        expected['config']['container']['vipc'] = [{'path': 'new.vipc', 'monitor': True, 'monitorOn': 'linux'}]
        result, text = self.apply_settings(initial=yaml.safe_dump(original), IN_RUNNERS='3',
                                           IN_CONTAINER_VIPC='new.vipc::linux')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assert_preserved(yaml.safe_load(text), expected)

    def test_activation_and_explicit_platform_change_preserve_other_fields(self):
        original = self.preservation_fixture()
        original['config']['container'].pop('linuxUse')
        expected = copy.deepcopy(original)
        expected['config']['container']['linuxUse'] = SELECTION
        expected['config']['os'] = ['linux']
        result, text = self.apply_settings(SELECTION, initial=yaml.safe_dump(original), IN_OS='linux')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assert_preserved(yaml.safe_load(text), expected)

    def test_explicit_false_dependency_policy_is_not_dropped(self):
        original = self.preservation_fixture()
        original['config']['dependencyPolicy']['requireInstalled'] = True
        expected = copy.deepcopy(original)
        expected['config']['dependencyPolicy']['requireInstalled'] = False
        result, text = self.apply_settings(initial=yaml.safe_dump(original), IN_REQUIRE_DEPS='false')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assert_preserved(yaml.safe_load(text), expected)

    def test_explicit_dragon_change_keeps_vipc_and_existing_platform_settings(self):
        original = self.preservation_fixture()
        original['config']['viBrowser'] = {'platforms': 'linux', 'futureOption': True}
        expected = copy.deepcopy(original)
        expected['config']['container']['dragon'] = [{'path': 'new.dragon', 'monitor': True, 'monitorOn': 'linux'}]
        result, text = self.apply_settings(initial=yaml.safe_dump(original), IN_CONTAINER_DRAGON='new.dragon::linux')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assert_preserved(yaml.safe_load(text), expected)

    def test_explicit_analyzer_default_clears_only_the_requested_analyzer_fields(self):
        original = self.preservation_fixture()
        original['config']['viAnalyzer']['futureOption'] = True
        expected = copy.deepcopy(original)
        expected['config']['viAnalyzer'] = {'futureOption': True}
        result, text = self.apply_settings(initial=yaml.safe_dump(original), IN_VIA_DEFAULT='builtin')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assert_preserved(yaml.safe_load(text), expected)

    def test_same_selection_and_absent_reset_are_noops(self):
        for selected, original in ((SELECTION, self.preservation_fixture()), ('reset', {'schemaVersion': 1})):
            with self.subTest(selected=selected):
                initial = yaml.safe_dump(original)
                result, text = self.apply_settings(selected, initial=initial)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(text, initial)

    def test_invalid_manifest_is_not_overwritten(self):
        for initial in ('config: [\n', '- not-a-mapping\n', 'config: scalar\n', 'config:\n  container: scalar\n'):
            with self.subTest(initial=initial):
                result, text = self.apply_settings(SELECTION, initial=initial)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(text, initial)

    def apply_settings(self, selection='', initial=MANIFEST, **inputs):
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / '.github/labview-ci.yml'
            config.parent.mkdir()
            config.write_text(initial)
            environment = {key: value for key, value in os.environ.items() if not key.startswith('IN_') and key != 'GITHUB_ENV'}
            environment.update({key: '' for key in APPLY['env']})
            environment.update({'IN_LINUX_WORKER': selection, **inputs})
            result = subprocess.run([sys.executable, '-c', SCRIPT], cwd=directory,
                                    env=environment, capture_output=True, text=True)
            return result, config.read_text()

    def test_keep_preserves_linux_windows_and_action_overrides(self):
        result, text = self.apply_settings(IN_RUNNERS='3', IN_CONTAINER_VIPC='project.vipc::linux')
        self.assertEqual(result.returncode, 0, result.stderr)
        container = yaml.safe_load(text)['config']['container']
        self.assertEqual(container['linuxUse'], SELECTION)
        self.assertEqual(container['use'], 'win-existing')
        self.assertEqual(container['actions'], {'masscompile': 'base', 'vidiff': 'win-pinned'})

    def test_reset_removes_only_linux_default(self):
        result, text = self.apply_settings('reset')
        self.assertEqual(result.returncode, 0, result.stderr)
        config = yaml.safe_load(text)['config']
        self.assertNotIn('linuxUse', config['container'])
        self.assertEqual(config['container']['use'], 'win-existing')
        self.assertEqual(config['container']['actions'], {'masscompile': 'base', 'vidiff': 'win-pinned'})
        self.assertEqual(config['os'], ['windows', 'linux'])

    def test_activation_writes_exact_digest_selection(self):
        selected = 'linux-lvpm-' + 'c' * 64 + '@sha256:' + 'd' * 64
        result, text = self.apply_settings(selected)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(yaml.safe_load(text)['config']['container']['linuxUse'], selected)

    def test_invalid_selection_fails_before_mutation(self):
        for selected in (TAG, SELECTION + '\n', 'latest', 'reset;echo unsafe', 'linux-lvpm-a@' + DIGEST):
            with self.subTest(selected=selected):
                result, text = self.apply_settings(selected)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(text, MANIFEST)

    def test_server_verifier_precedes_mutation_and_has_token_permissions(self):
        verify = next(step for step in STEPS if step['name'].startswith('Verify Linux LVPM'))
        self.assertLess(STEPS.index(verify), STEPS.index(APPLY))
        self.assertIn("inputs.linux_worker != 'reset'", verify['if'])
        self.assertIn('GH_TOKEN', verify['env'])
        self.assertEqual(verify['env']['LABVIEW_IMAGE_NAME'], '${{ vars.LABVIEW_IMAGE_NAME }}')
        parser = next(step for step in STEPS if step['name'] == 'Install configuration parser')
        self.assertLess(STEPS.index(parser), STEPS.index(verify))
        self.assertLess(STEPS.index(parser), STEPS.index(APPLY))
        self.assertNotIn('if', parser)
        self.assertEqual(parser['run'], 'python3 -m pip install PyYAML==6.0.3')
        self.assertIn('--candidate "$IN_LINUX_WORKER" --repository "$GITHUB_REPOSITORY" --image-name "${LABVIEW_IMAGE_NAME:-}"', verify['run'])
        self.assertNotIn('${{', verify['run'])
        self.assertIn('.github/workflows/release.yml', verify['run'])
        self.assertEqual(WORKFLOW['permissions']['packages'], 'read')
        self.assertEqual(WORKFLOW['permissions']['actions'], 'read')
        checkout = next(step for step in STEPS if step['name'] == 'Checkout repository')
        self.assertIn('github.event.repository.default_branch', checkout['with']['ref'])

    def test_reset_preserves_explicit_lvpm_action_override(self):
        initial = MANIFEST.replace('masscompile: base', f'masscompile: "{SELECTION}"')
        result, text = self.apply_settings('reset', initial=initial)
        self.assertEqual(result.returncode, 0, result.stderr)
        container = yaml.safe_load(text)['config']['container']
        self.assertNotIn('linuxUse', container)
        self.assertEqual(container['actions']['masscompile'], SELECTION)

    def test_mirrored_pages_are_identical(self):
        for page in ('dependencies.html', 'configure.html'):
            self.assertEqual((ROOT / '.github/pages' / page).read_bytes(),
                             (ROOT / 'actions/dashboard' / page).read_bytes())


class DashboardLvpmTests(unittest.TestCase):
    def test_selected_inventory_does_not_count_declarations_as_installed(self):
        namespace = dashboard_functions('_manifest_packages')
        namespace['_core_tooling_packages'] = lambda: ['inferred-core-1.0']
        packages = namespace['_manifest_packages']({
            'installer': 'lvpm', 'packages': [{'name': 'actual', 'version': '1.2'}],
            'vipm_packages': ['not-installed-1.0'],
            'vipc': [{'packages': ['declared-only-1.0']}], 'copied_from_base': True,
        })
        self.assertEqual(packages, ['actual', 'actual-1.2'])

    def test_selected_manifest_uses_exact_namespace_and_digest(self):
        namespace = dashboard_functions('_worker_manifest')
        requests = []
        manifest = {'installer': 'lvpm', 'tag': TAG, 'image_digest': DIGEST,
                    'manifest_path': f'workers/linux-lvpm/{TAG}/manifest.json'}

        def fetch(url):
            requests.append(url)
            return manifest

        namespace['http_json'] = fetch
        self.assertEqual(namespace['_worker_manifest']('linux', SELECTION), manifest)
        self.assertEqual(requests, [f'https://pages.invalid/workers/linux-lvpm/{TAG}/manifest.json'])
        self.assertIsNone(namespace['_worker_manifest']('linux', TAG))
        manifest['image_digest'] = 'sha256:' + 'e' * 64
        self.assertIsNone(namespace['_worker_manifest']('linux', SELECTION))
        self.assertNotIn('latest.json', ''.join(requests))

    def test_parser_preserves_linux_selection_after_action_overrides(self):
        namespace = dashboard_functions('_parse_container_config')
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / 'config.yml'
            config.write_text(MANIFEST.replace(f'    linuxUse: "{SELECTION}"\n', '').replace(
                'activities:', f'    linuxUse: "{SELECTION}"\nactivities:'))
            parsed = namespace['_parse_container_config'](str(config))
            self.assertEqual(parsed['linuxUse'], SELECTION)
            self.assertEqual(parsed['actions']['masscompile'], 'base')

    def test_index_keeps_candidate_separate_from_selected_inventory(self):
        namespace = dashboard_functions('_build_dependencies_index', '_manifest_packages')
        selected = {'installer': 'lvpm', 'packages': [{'name': 'selected', 'version': '1.0'}]}
        candidate = {'tag': TAG, 'installer': 'lvpm', 'packages': [{'name': 'alternative', 'version': '2.0'}]}
        namespace.update({
            '_parse_container_config': lambda: {'use': 'win-existing', 'linuxUse': SELECTION},
            '_repo_vipcs': lambda: [],
            '_worker_manifest': lambda platform, tag: selected if platform == 'linux' and tag == SELECTION else {},
            '_manifest_failed_packages': lambda manifest: [],
            '_manifest_nipkg_packages': lambda manifest: [],
            '_lvpm_local_freshness': lambda selection, manifest: {'status': 'unknown', 'selection': selection, 'revision': ''},
            'http_json': lambda url: {'tag': TAG, 'manifest_path': f'workers/linux-lvpm/{TAG}/manifest.json'} if url.endswith('latest.json') else candidate,
            '_known_nipm_dependencies': lambda: [], '_build_dragon_section': lambda config: {},
            '_system_dependencies': lambda: [], '_compute_deps_pending': lambda data: {},
            'repo': 'owner/project', 'commits_data': [],
        })
        with tempfile.TemporaryDirectory() as directory, working_directory(directory):
            Path('ci-out/dashboard/dependencies').mkdir(parents=True)
            namespace['_build_dependencies_index']()
            data = json.loads(Path('ci-out/dashboard/dependencies/index.json').read_text())
        columns = {column['key']: column for column in data['columns']}
        self.assertEqual(columns['linux']['tag'], SELECTION)
        self.assertEqual(columns['linux']['packages'], ['selected', 'selected-1.0'])
        self.assertFalse(columns['linux']['nipkg_ready'])
        self.assertEqual(columns['windows']['tag'], 'win-existing')
        self.assertEqual(data['linux_lvpm_candidate'], candidate)

    def test_candidate_cannot_create_main_missing_warnings(self):
        namespace = dashboard_functions('_compute_deps_pending')
        data = {'columns': [{'key': 'windows', 'ready': True, 'tag': 'win-existing', 'packages': ['actual-1.0']}],
                'vipc': [{'path': 'project.vipc', 'role': 'project', 'configured': True,
                          'monitorOn': ['windows'], 'packages': ['actual-1.0']}],
                'linux_lvpm_candidate': {'packages': []}}
        self.assertFalse(namespace['_compute_deps_pending'](data)['pending'])

    def test_selected_lvpm_stale_unknown_or_revision_mismatch_marks_linux_pending(self):
        namespace = dashboard_functions('_compute_deps_pending')
        revision = 'f' * 40
        current = {'status': 'current', 'revision': revision, 'selection': SELECTION}
        column = {'key': 'linux', 'installer': 'lvpm', 'ready': True, 'tag': SELECTION,
                  'packages': ['actual-1.0'], 'freshness': current}
        data = {'sha': revision, 'columns': [column], 'vipc': [
            {'path': 'project.vipc', 'role': 'project', 'configured': True,
             'monitorOn': ['linux'], 'packages': ['actual-1.0']}]}
        self.assertFalse(namespace['_compute_deps_pending'](data)['pending'])
        for freshness in ({**current, 'status': 'stale'}, {**current, 'status': 'unknown'}, {},
                          {**current, 'revision': '0' * 40}, {**current, 'selection': 'different'}):
            with self.subTest(freshness=freshness):
                column['freshness'] = freshness
                column['ready'] = False
                pending = namespace['_compute_deps_pending'](data)
                self.assertTrue(pending['pending'])
                self.assertIn('linux', pending['containers'])

    def test_dashboard_uses_only_trusted_action_helper_and_binds_actual_revision(self):
        namespace = dashboard_functions('_lvpm_local_freshness')
        revision = 'f' * 40
        current = {'status': 'current', 'revision': revision, 'selection': SELECTION, 'reason': 'local only'}
        with tempfile.TemporaryDirectory() as trusted, tempfile.TemporaryDirectory() as client:
            helper = Path(trusted) / 'worker.py'
            helper.write_text('trusted action-distributed helper')
            client_helper = Path(client) / '.github/labview/lvpm/worker.py'
            client_helper.parent.mkdir(parents=True)
            client_helper.write_text('raise RuntimeError("never execute inspected client code")')
            environment = {'GITHUB_SHA': revision, 'LVCI_LVPM_PYTHON': sys.executable,
                           'LVCI_LVPM_HELPER': str(helper)}
            response = subprocess.CompletedProcess([], 0, stdout=json.dumps(current), stderr='')
            with working_directory(client), patch.dict(os.environ, environment), \
                    patch('subprocess.check_output', return_value=revision), \
                    patch('subprocess.run', return_value=response) as run:
                self.assertEqual(namespace['_lvpm_local_freshness'](SELECTION, {}), current)
                arguments = run.call_args.args[0]
                self.assertEqual(arguments[:4], [sys.executable, '-I', str(helper), 'freshness'])
                self.assertIn(str(Path(client).resolve()), arguments)
                self.assertNotIn(str(client_helper), arguments)
                for evidence in ({**current, 'revision': '0' * 40}, {**current, 'selection': 'different'}):
                    response.stdout = json.dumps(evidence)
                    self.assertEqual(namespace['_lvpm_local_freshness'](SELECTION, {})['status'], 'unknown')
                run.reset_mock()
                with patch.dict(os.environ, {'GITHUB_SHA': '0' * 40}):
                    self.assertEqual(namespace['_lvpm_local_freshness'](SELECTION, {})['status'], 'unknown')
                run.assert_not_called()
                with patch.dict(os.environ, {'LVCI_LVPM_PYTHON': ''}):
                    self.assertEqual(namespace['_lvpm_local_freshness'](SELECTION, {})['status'], 'unknown')
                run.assert_not_called()

    def test_optional_freshness_setup_is_isolated_pinned_and_nonfatal(self):
        action = yaml.safe_load((ROOT / 'actions/dashboard/action.yml').read_text())
        steps = action['runs']['steps']
        setup = next(step for step in steps if step['name'] == 'Prepare optional local LVPM freshness checks')
        script = setup['run']
        self.assertIn('(?:linuxUse|use)', script)
        self.assertIn('python3 -I -m venv "$environment"', script)
        self.assertIn('PyYAML==6.0.3 tomli==2.4.0', script)
        self.assertIn('python3 -I - <<', script)
        self.assertIn('"$environment/bin/python" -I -m pip', script)
        self.assertIn('freshness unknown', script)
        self.assertNotIn('pip install --user', script)
        subprocess.run(['bash', '-n'], input=script, text=True, capture_output=True, check=True)
        build = next(step for step in steps if step['name'] == 'Build dashboard')
        self.assertEqual(build['env']['LVCI_LVPM_HELPER'],
                         '${{ github.action_path }}/../../.github/labview/lvpm/worker.py')
        self.assertEqual(build['env']['LVCI_LVPM_PYTHON'], '${{ steps.lvpm_freshness.outputs.python }}')
        self.assertIn('python3 -I "${{ github.action_path }}/dashboard.py"', build['run'])

    def test_optional_setup_failure_is_nonfatal_and_vipm_skips_environment_creation(self):
        action = yaml.safe_load((ROOT / 'actions/dashboard/action.yml').read_text())
        script = action['runs']['steps'][0]['run']
        for selected in (False, True):
            with self.subTest(lvpm=selected), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                config = root / '.github/labview-ci.yml'
                config.parent.mkdir()
                config.write_text(MANIFEST if selected else MANIFEST.replace(f'    linuxUse: "{SELECTION}"\n', ''))
                shadow_marker = root / 'shadow-executed'
                for name in ('pathlib', 'venv', 'pip', 'sitecustomize'):
                    (root / f'{name}.py').write_text(
                        f'open({str(shadow_marker)!r}, "w").write("executed")\n'
                        'raise RuntimeError("client import shadow must not execute")\n')
                executables = root / 'bin'
                executables.mkdir()
                shim = executables / 'python3'
                shim.write_text('#!' + sys.executable + ' -I\nimport os, sys\n'
                    'if sys.argv[1:4] == ["-I", "-m", "venv"]:\n'
                    '    with open(os.environ["VENV_PROBE"], "w") as probe: probe.write("attempted")\n'
                    '    sys.exit(1)\n'
                    'os.execv(sys.executable, [sys.executable, *sys.argv[1:]])\n')
                shim.chmod(0o755)
                probe = root / 'venv-probe'
                output = root / 'github-env'
                result = subprocess.run(['bash', '-e', '-c', script], cwd=root,
                    env={**os.environ, 'PATH': str(executables) + os.pathsep + os.environ['PATH'],
                         'RUNNER_TEMP': str(root), 'VENV_PROBE': str(probe), 'GITHUB_OUTPUT': str(output),
                         'PYTHONPATH': str(root)},
                    capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(probe.exists(), selected)
                self.assertFalse(output.exists())
                self.assertFalse(shadow_marker.exists())
                self.assertNotIn('client import shadow', result.stderr)
                if selected:
                    self.assertIn('freshness unknown', result.stdout)


if __name__ == '__main__':
    unittest.main()
