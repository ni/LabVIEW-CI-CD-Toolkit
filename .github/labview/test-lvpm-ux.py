import ast
import copy
from contextlib import contextmanager
from html.parser import HTMLParser
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
    namespace = {'os': os, 're': re, 'json': json, 'sys': sys, 'pages_url': 'https://pages.invalid',
                 '__file__': str(ROOT / 'actions/dashboard/dashboard.py')}
    exec(compile(ast.Module(body=nodes, type_ignores=[]), 'dashboard.py', 'exec'), namespace)
    return namespace


class DependenciesPlatformMarkupTests(unittest.TestCase):
    def test_platform_tabs_have_valid_accessible_panel_targets(self):
        elements = []

        class ElementParser(HTMLParser):
            def handle_starttag(self, tag, attributes):
                elements.append((tag, dict(attributes)))

        ElementParser().feed((ROOT / '.github/pages/dependencies.html').read_text())
        tabs = [attributes for _, attributes in elements if 'data-platform' in attributes]
        self.assertEqual([tab['data-platform'] for tab in tabs], ['windows', 'linux'])
        self.assertEqual([tab['role'] for tab in tabs], ['tab', 'tab'])
        panel = next(attributes for _, attributes in elements
                     if attributes.get('id') == 'platform-panel')
        self.assertEqual(panel['role'], 'tabpanel')
        self.assertEqual(panel['aria-labelledby'], tabs[0]['id'])
        self.assertEqual([tab['aria-controls'] for tab in tabs], [panel['id']] * 2)
        self.assertEqual([tab['tabindex'] for tab in tabs], ['0', '-1'])
        summary = next(attributes for _, attributes in elements
                       if attributes.get('id') == 'platformSummary')
        self.assertEqual(summary['aria-live'], 'polite')

    def test_dependency_and_header_assets_are_identical(self):
        for name in ('dependencies.html', 'lvci-header.js'):
            with self.subTest(asset=name):
                self.assertEqual((ROOT / '.github/pages' / name).read_bytes(),
                                 (ROOT / 'actions/dashboard' / name).read_bytes())

    def test_linux_next_action_details_and_live_feedback_contract(self):
        page = (ROOT / '.github/pages/dependencies.html').read_text()
        self.assertIn('Update container image', page)
        self.assertNotIn('Next:', page)
        self.assertIn('id="linuxPrimary"', page)
        self.assertIn('<details><summary>Candidate evidence and build inputs</summary>', page)
        self.assertIn('<details><summary>Current selection and rollback</summary>', page)
        self.assertIn('role="status" aria-live="polite" id="lvpmStatus"', page)
        self.assertIn('id="lvpmRunLink" target="_blank" rel="noopener" hidden', page)
        self.assertNotIn('<h3>1. Build a separate LVPM candidate</h3>', page)

    def test_linux_monitoring_prerequisites_distinguish_acceptance_from_merge(self):
        page = (ROOT / '.github/pages/dependencies.html').read_text()
        self.assertNotIn('Find current Linux settings', page)
        self.assertNotIn('Open matching committed settings', page)
        self.assertIn('Save request accepted, not applied', page)
        self.assertIn('committedMonitoringMatches(settings, intent.context)', page)
        self.assertIn('Review proposals (unattributed list)', page)
        self.assertNotIn('LVPM Experimental', page)
        self.assertNotIn('Build experimental LVPM', page)
        update = page.split('function updateMonitorUi(){', 1)[1].split('function token()', 1)[0]
        self.assertIn('renderLinuxPrerequisites()', update)
        self.assertIn('matchingLinuxMonitoringRequest()', update)

    def test_head_check_is_read_only_and_preserves_page_context(self):
        page = (ROOT / '.github/pages/dependencies.html').read_text()
        check = page.split('async function checkLinuxMonitoringHead(){', 1)[1].split('function renderLinuxProgress', 1)[0]
        for forbidden in ('method:"POST"', 'dispatchMonitor(', 'dispatchLinuxWorker(', 'loadRevision(',
                          'location.href =', 'monitorDirty =', 'selectedRevision =', 'defaultRevision ='):
            self.assertNotIn(forbidden, check)
        self.assertGreaterEqual(check.count('linuxMonitoringContextCurrent(check)'), 6)
        self.assertIn('linuxHeadCheck !== check', check)
        self.assertIn('local edits stay here', page)

    def test_linux_primary_retains_source_and_full_settings_guards(self):
        page = (ROOT / '.github/pages/dependencies.html').read_text()
        save = page.split('async function dispatchMonitor(){', 1)[1].split('async function linuxSaveRestriction()', 1)[0]
        self.assertLess(save.index('await linuxSaveRestriction()'), save.index('method:"POST"'))
        self.assertIn('linuxMonitoringContextCurrent(request)', save)
        compare = page.split('function committedMonitoringMatches(', 1)[1].split('async function checkLinuxMonitoringHead()', 1)[0]
        for required in ('config.hasVipcList', 'config.hasDragonList', 'workerSelectionSettings(config)',
                         'normalizedMonitorInputs(JSON.parse(context.inputs))', 'Duplicate declaration paths',
                         'Explicit valid per-file monitor flags'):
            self.assertIn(required, compare)
        primary = page.split('async function runLinuxPrimaryAction()', 1)[1].split('function workerSelectionSettings', 1)[0]
        self.assertIn('linuxDispatchPending', primary)
        self.assertIn('linuxOperation.busy', primary)
        self.assertIn('openUpdateApprovalDialog(intent)', primary)
        self.assertIn('Object.freeze({...linuxMonitoringContext()', primary)
        self.assertNotIn('dispatchMonitor()', primary)

    def test_dragon_details_separate_file_and_package_evidence(self):
        page = (ROOT / '.github/pages/dependencies.html').read_text()
        details = page.split('function dragonInspectionDetails(file){', 1)[1].split('function parseManifestYaml', 1)[0]
        for label in ('File present · parsed', 'File absent', 'File parse error', 'File inspection unavailable',
                      'Package missing', 'Version mismatch', 'Package unverified', 'Declared packages'):
            self.assertIn(label, details)
        self.assertIn('generatedInventory.sha === selectedRevision', details)
        self.assertIn('section.available !== true', details)
        self.assertIn('esc(dependency.declared_version', details)
        self.assertIn('activePlatform === "windows" ? dependency : null', details)

    def test_linux_dispatch_feedback_documents_acceptance_not_verification(self):
        page = (ROOT / '.github/pages/dependencies.html').read_text()
        documentation = (ROOT / '.github/pages/documentation.html').read_text()
        self.assertIn('return_run_details:true', page)
        self.assertIn('Dispatch accepted, not completed', page)
        self.assertIn('GitHub returned no run identity', page)
        self.assertIn('Refresh to check for a published candidate', page)
        self.assertIn('Workflow success alone does not establish a published, verified candidate', documentation)
        self.assertEqual((ROOT / '.github/pages/documentation.html').read_bytes(),
                         (ROOT / 'actions/dashboard/documentation.html').read_bytes())

    def test_linux_visual_progress_is_indeterminate_and_separate_from_windows(self):
        page = (ROOT / '.github/pages/dependencies.html').read_text()
        self.assertIn('class="lvpm-progress" id="lvpmProgress" hidden', page)
        self.assertIn('class="upd-prog-phase" id="lvpmProgressPhase"', page)
        self.assertIn('class="upd-prog-track" aria-hidden="true"', page)
        self.assertIn('class="upd-prog-bar" id="lvpmProgressBar"', page)
        self.assertIn('@media(prefers-reduced-motion:reduce){.lvpm-progress .indeterminate', page)
        render = page.split('function renderLinuxProgress(phase){', 1)[1].split('function linuxOperationStatus', 1)[0]
        self.assertIn('moving ? "indeterminate"', render)
        self.assertIn('label.textContent = text', render)
        self.assertNotIn('innerHTML', render)
        self.assertNotIn('updProgBar', render)
        self.assertNotIn('aria-valuenow', render)
        status = page.split('function linuxOperationStatus(', 1)[1].split('function validateLinuxRun', 1)[0]
        self.assertLess(status.index('if (!linuxOperationCurrent(operation)) return'),
                        status.index('renderLinuxProgress(operation.phase)'))


class ReconfigureLvpmTests(unittest.TestCase):
    def test_actual_monitoring_writer_output_matches_linux_readonly_check(self):
        fixtures = []
        for platforms in ('windows+linux', 'windows', 'linux', 'none'):
            inputs = {'container_vipc': 'project.vipc::' + platforms,
                      'container_dragon': 'project.dragon::' + platforms}
            result, text = self.apply_settings(IN_CONTAINER_VIPC=inputs['container_vipc'],
                                               IN_CONTAINER_DRAGON=inputs['container_dragon'])
            self.assertEqual(result.returncode, 0, result.stderr)
            if platforms in ('windows+linux', 'none'):
                self.assertNotIn('monitorOn:', text)
            fixtures.append({'yaml': text, 'inputs': inputs,
                             'selection': yaml.safe_load(MANIFEST)['config']['container']})
        environment = dict(os.environ, LVCI_MONITORING_WRITER_FIXTURE=json.dumps(fixtures))
        result = subprocess.run(['node', str(ROOT / '.github/labview/test-lvpm-ux.mjs')],
                                cwd=ROOT, env=environment, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('Real Reconfigure writer comparison: 4 outputs passed', result.stdout)

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


class WorkerInventoryTests(unittest.TestCase):
    def generate(self, inventory=None, raw='diagnostic_package 1.0', platform='windows'):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            raw_path = root / 'vipm-list.txt'
            raw_path.write_text(raw)
            arguments = [sys.executable, str(ROOT / '.github/labview/build-worker-manifest.py'),
                         '--platform', platform, '--version', 'test-worker',
                         '--labview-version', '2026', '--labview-bitness', '64',
                         '--dependency-sha', 'd' * 40, '--vipm-list', str(raw_path),
                         '--out-dir', str(root / 'out')]
            if inventory is not None:
                path = root / 'observed.json'
                path.write_text(json.dumps(inventory), encoding='utf-8-sig')
                arguments += ['--observed-inventory', str(path)]
            result = subprocess.run(arguments, text=True, capture_output=True)
            output = root / 'out/manifest.json'
            return result, json.loads(output.read_text()) if output.exists() else None

    def evidence(self, **overrides):
        return {'schema': 1, 'platform': 'windows', 'labview_version': '2026',
                'labview_bitness': '64', 'source': 'vipm list --installed',
                'target_verified': True, 'required_project': True, 'status': 'observed',
                'raw': 'LabVIEW 2026 (64-bit)\nactual 1.2',
                'packages': [{'name': 'actual', 'version': '1.2'}], **overrides}

    def test_only_structured_target_bound_evidence_is_installed(self):
        result, manifest = self.generate(self.evidence())
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(manifest['schema'], 3)
        self.assertEqual(manifest['dependency_sha'], 'd' * 40)
        self.assertEqual(manifest['vipm_packages'], [{'name': 'actual', 'version': '1.2'}])
        self.assertTrue(manifest['vipm_inventory']['target_verified'])
        result, manifest = self.generate()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(manifest['vipm_packages'], [])
        self.assertEqual(manifest['vipm_inventory']['status'], 'unverified')

    def test_baseline_unavailable_never_claims_packages(self):
        result, manifest = self.generate(self.evidence(status='unavailable',
            target_verified=False, required_project=False, packages=[]))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(manifest['vipm_packages'], [])

    def test_invalid_or_wrong_target_inventory_cannot_publish(self):
        invalid = [None, [], {'schema': True}, {'platform': 'linux'},
                   {'labview_version': '2025'}, {'labview_bitness': '32'},
                   {'source': 'requested VIPC'}, {'target_verified': 'true'},
                   {'required_project': 'false'}, {'status': 'verified'},
                   {'target_verified': False}, {'packages': []},
                   {'packages': [{'name': 'bad package', 'version': '1.2'}]},
                   {'packages': [{'name': 'actual', 'version': ''}]},
                   {'packages': [{'name': 'actual', 'version': '1.2'}] * 2},
                   {'status': 'unavailable', 'target_verified': False}]
        for overrides in invalid:
            with self.subTest(overrides=overrides):
                evidence = self.evidence(**overrides) if isinstance(overrides, dict) else overrides
                if overrides is None:
                    evidence = 'not-an-object'
                result, manifest = self.generate(evidence)
                self.assertNotEqual(result.returncode, 0)
                self.assertIsNone(manifest)


class DashboardLvpmTests(unittest.TestCase):
    def test_vipm_coverage_uses_observations_not_requests_or_seed_inference(self):
        namespace = dashboard_functions('_manifest_packages')
        namespace['_core_tooling_packages'] = lambda: ['inferred-core-1.0']
        manifest = {'platform': 'windows', 'copied_from_base': True,
                    'vipc': [{'packages': ['declared-only-1.0']}]}
        self.assertEqual(namespace['_manifest_packages'](manifest), [])
        manifest['vipm_packages'] = [{'name': 'actual', 'version': 'v1.2'}]
        self.assertIn('actual-1.2', namespace['_manifest_packages'](manifest))
        self.assertNotIn('declared-only-1.0', namespace['_manifest_packages'](manifest))
        manifest['vipm_inventory'] = {'status': 'unavailable'}
        self.assertEqual(namespace['_manifest_packages'](manifest), [])

    def test_vipm_wrong_version_remains_pending(self):
        namespace = dashboard_functions('_compute_deps_pending')
        data = {'columns': [{'key': 'windows', 'ready': True, 'tag': 'win-existing',
                             'packages': ['actual', 'actual-v1.2']}],
                'vipc': [{'path': 'project.vipc', 'role': 'project', 'configured': True,
                          'monitorOn': ['windows'], 'packages': ['actual-1.2']}]}
        self.assertFalse(namespace['_compute_deps_pending'](data)['pending'])
        data['vipc'][0]['packages'] = ['actual-9.0']
        self.assertTrue(namespace['_compute_deps_pending'](data)['pending'])

    def test_selected_linux_worker_with_unavailable_inventory_remains_pending(self):
        namespace = dashboard_functions('_compute_deps_pending')
        data = {'columns': [{'key': 'linux', 'ready': False, 'tag': 'linux-existing',
                             'packages': ['actual-1.2']}],
                'vipc': [{'path': 'project.vipc', 'role': 'project', 'configured': True,
                          'monitorOn': ['linux'], 'packages': ['actual-1.2']}]}
        pending = namespace['_compute_deps_pending'](data)
        self.assertTrue(pending['pending'])
        self.assertIn('linux', pending['containers'])
        data['columns'][0]['tag'] = 'base'
        self.assertFalse(namespace['_compute_deps_pending'](data)['pending'])

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


class DashboardDragonInspectionTests(unittest.TestCase):
    def test_actual_example_parser_output_reaches_actual_ui_renderer(self):
        result = self.inspect(declarations=(ROOT / 'example/Example.dragon').read_text())
        dependencies = result['dragon']['files'][0]['dependencies']
        boolean = next(item for item in dependencies if item['package_id'] == 'oglib_boolean')
        self.assertEqual(boolean['declared_version'], '6.0.0.9')
        self.assertEqual(result['dragon']['files'][0]['inspection']['status'], 'parsed')
        completed = subprocess.run(['node', str(ROOT / '.github/labview/test-lvpm-ux.mjs')],
                                   env={**os.environ, 'LVCI_DRAGON_UI_FIXTURE': json.dumps(result['dragon'])},
                                   capture_output=True, text=True)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn('renderer cases passed', completed.stdout)

    def inspect(self, declarations='[vipm.dependencies]\nactual = "1.2"\n', monitored=True,
                trusted_parser=True, backend=True, manifest=None, configured=True, platforms=None,
                linux_manifest=None, freshness=None, selection=SELECTION, other_files=None):
        names = {'_load_dragon_module', '_dragon_inventory', '_repo_dragons', '_build_dragon_section',
                 '_monitor_on', '_manifest_packages', '_dragon_status_index', '_vipm_version_index',
                 '_nipkg_version_index', '_infer_dragon_status', '_annotate_dragon_status', '_compute_deps_pending',
                 '_linux_dragon_status', '_aggregate_dragon_status'}
        tree = ast.parse((ROOT / 'actions/dashboard/dashboard.py').read_text())
        definitions = '\n\n'.join(ast.unparse(node) for node in tree.body
                                  if isinstance(node, ast.FunctionDef) and node.name in names)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            client = root / 'client'
            client.mkdir()
            sentinel = client / 'client-code-executed'
            malicious = f'open({str(sentinel)!r}, "w").write("executed")\nraise RuntimeError("client code executed")\n'
            client_parser = client / '.github/labview/dragon_deps.py'
            client_parser.parent.mkdir(parents=True)
            client_parser.write_text(malicious)
            for name in ('pathlib', 'tomllib', 'tomli', 'sitecustomize'):
                (client / f'{name}.py').write_text(malicious)
            if declarations is not None:
                (client / 'project.dragon').write_text(declarations)
            for name, declaration, _entry in other_files or []:
                (client / name).write_text(declaration)
            trusted = root / 'trusted'
            script = trusted / 'actions/dashboard/dashboard.py'
            script.parent.mkdir(parents=True)
            if trusted_parser:
                helper = trusted / '.github/labview/dragon_deps.py'
                helper.parent.mkdir(parents=True)
                helper.write_text((ROOT / '.github/labview/dragon_deps.py').read_text())
            script.write_text('import json, os, re, sys\n' + definitions + '\n'
                '_DRAGON_MOD = None\nMONITOR_PLATFORMS = ["windows", "linux"]\n'
                + ('' if backend else 'sys.modules["tomllib"] = None\nsys.modules["tomli"] = None\n')
                + 'def _worker_manifest(platform, tag): return json.loads(os.environ["TEST_LINUX_MANIFEST" if platform == "linux" else "TEST_MANIFEST"])\n'
                'def _lvpm_local_freshness(selection, manifest): return json.loads(os.environ["TEST_FRESHNESS"])\n'
                'section = _build_dragon_section(json.loads(os.environ["TEST_CONFIG"]))\n'
                'print(json.dumps({"dragon": section, "pending": _compute_deps_pending({"dragon": section})}))\n')
            config = {'hasDragonList': True, 'dragon': [
                {'path': 'project.dragon', 'monitor': monitored,
                 'monitorOn': platforms if platforms is not None else ['linux']}]} if configured else {}
            config['linuxUse'] = selection
            for name, _declaration, entry in other_files or []:
                config.setdefault('dragon', []).append({'path': name, **entry})
            result = subprocess.run([sys.executable, '-I', '-B', str(script)], cwd=client,
                env={**os.environ, 'PYTHONPATH': str(client), 'TEST_CONFIG': json.dumps(config),
                     'TEST_MANIFEST': json.dumps(manifest), 'TEST_LINUX_MANIFEST': json.dumps(linux_manifest),
                     'TEST_FRESHNESS': json.dumps(freshness or {}), 'GITHUB_SHA': 'f' * 40},
                capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertFalse(sentinel.exists(), result.stderr)
            return json.loads(result.stdout)

    def assert_unknown(self, result, reason):
        section = result['dragon']
        self.assertFalse(section['ready'])
        self.assertEqual(section['health'], 'unknown')
        self.assertEqual(section['files'][0]['inspection']['status'], 'unknown')
        self.assertIn(reason, section['files'][0]['inspection']['reason'])
        self.assertTrue(result['pending']['pending'])
        self.assertEqual(result['pending']['containers'], ['linux'])
        self.assertEqual(result['pending']['dragon'][0]['status'], 'unknown')
        self.assertEqual(result['pending']['dragon'][0]['source_file'], 'project.dragon')

    def test_real_trusted_parser_executes_without_client_helper_or_import_shadows(self):
        result = self.inspect(platforms=['windows'], manifest={'vipm_packages': [{'name': 'actual', 'version': '1.2'}]})
        self.assertTrue(result['dragon']['available'])
        self.assertEqual(result['dragon']['files'][0]['inspection']['status'], 'parsed')
        dependency = result['dragon']['install_set'][0]
        self.assertEqual((dependency['package_id'], dependency['declared_version'], dependency['status']),
                         ('actual', '1.2', 'installed'))
        self.assertFalse(result['pending']['pending'])

    def test_missing_trusted_parser_does_not_fall_back_to_client_code(self):
        result = self.inspect(trusted_parser=False, manifest={'vipm_packages': [{'name': 'actual', 'version': '1.2'}]})
        self.assertFalse(result['dragon']['available'])
        self.assert_unknown(result, 'Trusted Dragon parser unavailable')

    def test_missing_toml_backend_is_unknown(self):
        result = self.inspect(backend=False)
        self.assertFalse(result['dragon']['available'])
        self.assert_unknown(result, 'TOML backend unavailable')

    def test_unknown_files_target_only_monitored_platforms(self):
        for platforms in (['windows'], ['linux'], ['windows', 'linux']):
            with self.subTest(platforms=platforms):
                result = self.inspect(trusted_parser=False, platforms=platforms)
                self.assertEqual(result['pending']['containers'], sorted(platforms))
                self.assertTrue(result['pending']['pending'])

    def test_legacy_unconfigured_files_keep_default_monitoring(self):
        result = self.inspect(trusted_parser=False, configured=False)
        self.assertEqual(result['pending']['containers'], ['linux', 'windows'])
        self.assertEqual(result['pending']['dragon'][0]['status'], 'unknown')

    def test_malformed_monitored_declaration_is_unknown(self):
        result = self.inspect(declarations='[vipm.dependencies\n',
                              manifest={'vipm_packages': [{'name': 'actual', 'version': '1.2'}]})
        self.assert_unknown(result, 'invalid TOML')

    def test_missing_monitored_file_is_unknown(self):
        self.assert_unknown(self.inspect(declarations=None), 'Declared Dragon file is missing')

    def test_unknown_unmonitored_files_do_not_raise_pending(self):
        for options in ({'trusted_parser': False}, {'backend': False},
                        {'declarations': '[vipm.dependencies\n'}, {'declarations': None}):
            with self.subTest(options=options):
                result = self.inspect(monitored=False, **options)
                self.assertEqual(result['dragon']['files'][0]['inspection']['status'], 'unknown')
                self.assertFalse(result['pending']['pending'])
                self.assertEqual(result['pending']['dragon'], [])
                self.assertEqual(result['pending']['containers'], [])

    def test_no_declarations_do_not_create_pending(self):
        result = self.inspect(declarations=None, configured=False, trusted_parser=False)
        self.assertEqual(result['dragon']['files'], [])
        self.assertFalse(result['pending']['pending'])

    def test_valid_empty_declarations_are_parsed(self):
        result = self.inspect(declarations='[vipm.dependencies]\n', manifest={})
        self.assertEqual(result['dragon']['files'][0]['inspection']['status'], 'parsed')
        self.assertEqual(result['dragon']['install_set'], [])
        self.assertFalse(result['pending']['pending'])

    def test_measured_windows_statuses_are_preserved(self):
        for manifest, expected in ((None, 'pending'), ({}, 'missing'),
                ({'vipm_packages': [{'name': 'actual', 'version': '9.0'}]}, 'wrong_version'),
                ({'vipm_packages': [{'name': 'actual', 'version': '1.2'}]}, 'installed'),
                ({'dragon': {'items': [{'manager': 'vipm', 'package_id': 'actual',
                                      'status': 'installed', 'installed_version': '1.2'}]}}, 'installed')):
            with self.subTest(expected=expected, manifest=manifest):
                result = self.inspect(manifest=manifest, platforms=['windows'])
                self.assertEqual(result['dragon']['install_set'][0]['status'], expected)
                self.assertEqual(result['pending']['pending'], expected != 'installed')
                self.assertEqual(result['pending']['containers'], [] if expected == 'installed' else ['windows'])
                self.assertEqual(set(result['dragon']['install_set'][0]['platform_status']), {'windows'})

    def linux_evidence(self):
        return {'linux_manifest': {'installer': 'lvpm', 'platform': 'linux', 'architecture': 'amd64',
                                  'packages': [{'name': 'ACTUAL', 'version': '1.2'}]},
                'freshness': {'status': 'current', 'selection': SELECTION, 'revision': 'f' * 40}}

    def test_linux_exact_current_inventory_satisfies_without_windows_evidence(self):
        result = self.inspect(**self.linux_evidence())
        section = result['dragon']
        self.assertTrue(section['ready'])
        self.assertFalse(result['pending']['pending'])
        for dependency in (section['files'][0]['dependencies'][0], section['install_set'][0]):
            self.assertEqual(dependency['status'], 'installed')
            self.assertEqual(set(dependency['platform_status']), {'linux'})
            self.assertEqual(dependency['platform_status']['linux']['scope'], 'package-files')
            self.assertIn('runtime activities are not verified', dependency['message'])

    def test_linux_exact_version_required_and_missing_packages_pending(self):
        for version, expected in (('9.0', 'wrong_version'), ('v1.2', 'wrong_version'), (None, 'missing')):
            with self.subTest(version=version):
                options = self.linux_evidence()
                options['linux_manifest']['packages'] = [{'name': 'actual', 'version': version}] if version else []
                result = self.inspect(**options)
                self.assertEqual(result['dragon']['install_set'][0]['status'], expected)
                self.assertTrue(result['pending']['pending'])
                self.assertEqual(result['pending']['containers'], ['linux'])

    def test_linux_stale_unknown_or_unbound_freshness_is_unknown_pending(self):
        current = self.linux_evidence()['freshness']
        for freshness in ({}, {**current, 'status': 'stale'}, {**current, 'status': 'unknown'},
                          {**current, 'selection': 'different'}, {**current, 'revision': '0' * 40}):
            with self.subTest(freshness=freshness):
                options = self.linux_evidence()
                options['freshness'] = freshness
                result = self.inspect(**options)
                self.assertFalse(result['dragon']['ready'])
                self.assertEqual(result['dragon']['install_set'][0]['status'], 'unknown')
                self.assertTrue(result['pending']['pending'])
                self.assertEqual(result['pending']['containers'], ['linux'])

    def test_linux_unsupported_or_noncanonical_inventory_is_unknown(self):
        for change in ({'selection': 'latest'}, {'linux_manifest': None},
                       {'linux_manifest': {'installer': 'vipm'}},
                       {'linux_manifest': {'installer': 'lvpm', 'platform': 'windows', 'architecture': 'amd64'}},
                       {'linux_manifest': {'installer': 'lvpm', 'platform': 'linux', 'architecture': 'arm64'}},
                       {'declarations': '[nipm.dependencies]\nactual = "1.2"\n'}):
            with self.subTest(change=change):
                result = self.inspect(**{**self.linux_evidence(), **change})
                self.assertEqual(result['dragon']['install_set'][0]['status'], 'unknown')
                self.assertTrue(result['pending']['pending'])
                self.assertEqual(result['pending']['containers'], ['linux'])

    def test_mixed_platforms_aggregate_conservatively_and_target_unresolved_only(self):
        windows = {'vipm_packages': [{'name': 'actual', 'version': '1.2'}]}
        for manifest, linux_current, expected, pending_platforms in (
                (windows, True, 'installed', []), (windows, False, 'unknown', ['linux']),
                ({}, True, 'missing', ['windows']), ({}, False, 'missing', ['linux', 'windows']),
                (None, True, 'pending', ['windows'])):
            with self.subTest(expected=expected, platforms=pending_platforms):
                options = self.linux_evidence()
                if not linux_current:
                    options['freshness']['status'] = 'stale'
                result = self.inspect(platforms=['windows', 'linux'], manifest=manifest, **options)
                dependency = result['dragon']['install_set'][0]
                self.assertEqual(dependency['status'], expected)
                self.assertEqual(set(dependency['platform_status']), {'linux', 'windows'})
                self.assertEqual(result['pending']['pending'], expected != 'installed')
                self.assertEqual(result['pending']['containers'], pending_platforms)

    def test_windows_only_does_not_read_linux_evidence(self):
        options = self.linux_evidence()
        options['freshness']['status'] = 'stale'
        result = self.inspect(platforms=['windows'],
                              manifest={'vipm_packages': [{'name': 'actual', 'version': '1.2'}]}, **options)
        self.assertEqual(result['dragon']['install_set'][0]['status'], 'installed')
        self.assertTrue(result['dragon']['ready'])
        self.assertFalse(result['pending']['pending'])

    def test_unmonitored_conflicting_file_cannot_pollute_monitored_evidence(self):
        result = self.inspect(other_files=[('ignored.dragon', '[vipm.dependencies]\nactual = "9.0"\n',
                                           {'monitor': False})], **self.linux_evidence())
        self.assertEqual(result['dragon']['install_set'][0]['status'], 'installed')
        self.assertFalse(result['pending']['pending'])

    def test_shared_packages_aggregate_only_their_own_monitored_platforms(self):
        result = self.inspect(other_files=[('windows.dragon', '[vipm.dependencies]\nactual = "1.2"\n',
                                           {'monitor': True, 'monitorOn': ['windows']})],
                              manifest={}, **self.linux_evidence())
        self.assertEqual(result['dragon']['install_set'][0]['status'], 'missing')
        self.assertEqual(result['pending']['containers'], ['windows'])
        self.assertEqual(result['dragon']['files'][0]['dependencies'][0]['status'], 'installed')
        self.assertEqual(result['dragon']['install_set'][0]['platform_status']['linux']['scope'], 'package-files')

    def test_conflicting_monitored_versions_remain_pending(self):
        result = self.inspect(other_files=[('other.dragon', '[vipm.dependencies]\nactual = "9.0"\n',
                                           {'monitor': True, 'monitorOn': ['linux']})], **self.linux_evidence())
        self.assertEqual(result['dragon']['install_set'][0]['status'], 'conflict')
        self.assertEqual(result['pending']['containers'], ['linux'])
        self.assertTrue(result['pending']['pending'])


if __name__ == '__main__':
    unittest.main()
