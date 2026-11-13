import ast
import copy
import json
import os
import re
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

import yaml


ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = yaml.safe_load((ROOT / '.github/workflows/reconfigure.yml').read_text())
APPLY = next(step for step in WORKFLOW['jobs']['reconfigure']['steps']
             if step['name'] == 'Apply settings to config files')
SCRIPT = APPLY['run'].split("python3 - <<'PY'\n", 1)[1].rsplit('\nPY', 1)[0]
MANIFEST = ('schemaVersion: 1\nconfig:\n  labviewVersion: "2026"\n'
            '  os: [windows, linux]\n  container:\n    use: win-existing\n'
            '    futureSelection: pinned-extension\n'
            '    actions:\n      masscompile: base\n      vidiff: win-pinned\n'
            'activities:\n  - dashboard\n')


class ReconfigurePreservationTests(unittest.TestCase):
    def pr_title(self, before, after):
        tree = ast.parse(SCRIPT)
        function = next(node for node in tree.body if isinstance(node, ast.FunctionDef)
                        and node.name == 'reconfigure_pr_title')
        namespace = {}
        exec(compile(ast.Module(body=[function], type_ignores=[]), 'reconfigure.yml', 'exec'), namespace)
        return namespace['reconfigure_pr_title'](before, after)

    def test_pr_title_names_actual_changed_settings(self):
        before = self.preservation_fixture()
        after = copy.deepcopy(before)
        after['config']['concurrency'] = {'maxParallel': 3}
        after['config']['container']['vipc'] = []
        self.assertEqual(self.pr_title(before, after), 'Update runner concurrency, VIPC dependency monitoring')

    def test_pr_title_distinguishes_linux_activation_and_reset(self):
        before = self.preservation_fixture()
        after = copy.deepcopy(before)
        after['config']['container']['linuxUse'] = 'linux-lvpm-fixture@sha256:fixture'
        self.assertEqual(self.pr_title(before, after), 'Activate digest-pinned Linux LVPM worker')
        self.assertEqual(self.pr_title(after, before), 'Reset Linux worker to the existing default selection')

    def test_pr_title_ignores_timestamp_and_commit_only_vipc_changes_are_specific(self):
        before = self.preservation_fixture()
        after = copy.deepcopy(before)
        after['configuredAt'] = 'later'
        self.assertEqual(self.pr_title(before, after), 'Update committed VIPC package selections')
        step = next(step for step in WORKFLOW['jobs']['reconfigure']['steps'] if step['name'] == 'Open pull request')
        self.assertIn('--title "$RECONFIGURE_PR_TITLE"', step['run'])
        self.assertNotIn('--title "Reconfigure LabVIEW CI"', step['run'])

    def preservation_fixture(self):
        document = yaml.safe_load(MANIFEST)
        document.update({'provider': 'github', 'installedVersion': 'unreleased-fixture',
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

    def test_explicit_legacy_snapshot_activity_migrates_without_forcing_snapshots(self):
        result, text = self.apply_settings(IN_ACTS='dashboard,snapshots-2,snapshots')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(yaml.safe_load(text)['activities'], ['dashboard', 'snapshots'])
        result, text = self.apply_settings(IN_ACTS='dashboard')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(yaml.safe_load(text)['activities'], ['dashboard'])

    def test_blank_inputs_are_byte_preserving(self):
        initial = '# Retain client comments on a no-op.\n' + yaml.safe_dump(self.preservation_fixture())
        result, text = self.apply_settings(initial=initial)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(text, initial)

    def test_keep_defaults_preserve_missing_or_block_platforms(self):
        for platforms in (None, ['linux'], ['windows']):
            with self.subTest(platforms=platforms):
                original = self.preservation_fixture()
                if platforms is None:
                    original['config'].pop('os')
                else:
                    original['config']['os'] = platforms
                initial = yaml.safe_dump(original)
                result, text = self.apply_settings(initial=initial, IN_OS='keep', IN_ANTIDOC='keep',
                                                   IN_REQUIRE_DEPS='keep', IN_AUTO_INSTALL_DEPS='keep')
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(text, initial)

    def test_changed_output_remains_readable_by_existing_config_readers(self):
        original = self.preservation_fixture()
        original['config']['os'] = ['linux']
        original['config']['container']['vipc'][0]['path'] = 'client folder with spaces/' * 6 + 'project.vipc'
        result, text = self.apply_settings(initial=yaml.safe_dump(original), IN_RUNNERS='3')
        self.assertEqual(result.returncode, 0, result.stderr)
        tree = ast.parse((ROOT / 'actions/dashboard/dashboard.py').read_text())
        parser = next(node for node in tree.body if isinstance(node, ast.FunctionDef)
                      and node.name == '_parse_container_config')
        namespace = {'os': os, 're': re}
        exec(compile(ast.Module(body=[parser], type_ignores=[]), 'dashboard.py', 'exec'), namespace)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'config.yml'
            path.write_text(text)
            parsed = namespace['_parse_container_config'](str(path))
        self.assertTrue(parsed['vipc'], parsed)
        self.assertTrue(parsed['dragon'], parsed)
        self.assertEqual(parsed['vipc'][0]['path'], original['config']['container']['vipc'][0]['path'])
        self.assertEqual(parsed['vipc'][0]['monitorOn'], ['linux'])
        self.assertEqual(parsed['dragon'][0]['path'], 'project.dragon')
        self.assertEqual(parsed['dragon'][0]['monitorOn'], ['linux'])
        year = re.search(r'^\s*labviewVersion:\s*"?([^"\s]+)"?', text, re.MULTILINE)
        self.assertEqual(year[1], '2026')
        platforms = re.search(r'^\s*os:\s*\[([^\]]*)\]', text, re.MULTILINE)
        self.assertIsNotNone(platforms)
        self.assertEqual(platforms[1].strip(), 'linux')
        node_script = r'''
          const assert = require('node:assert/strict');
          const fs = require('node:fs');
          const vm = require('node:vm');
          const input = JSON.parse(fs.readFileSync(0, 'utf8'));
          for (const file of input.files) {
            const source = fs.readFileSync(file, 'utf8');
            const match = source.match(/    function (parseManifest(?:Yaml)?)\(text\)\s*\{[\s\S]*?\n    \}/);
            assert.ok(match, file);
            const parser = vm.runInNewContext(match[0] + '; ' + match[1], {parseViAnalyzerBlock:()=>({})});
            const config = JSON.parse(JSON.stringify(parser(input.text)));
            assert.equal(config.containerUse || config.use, 'win-existing', file);
            assert.equal(config.vipc[0].path, input.vipcPath, file);
            assert.equal(config.dragon[0].path, 'project.dragon', file);
            assert.equal(config.vipc[0].monitor, true, file);
            assert.equal(config.dragon[0].monitor, true, file);
            assert.equal(config.actions.masscompile, 'base', file);
            if ('version' in config) {
              assert.equal(config.version, '2026', file);
              assert.deepEqual(config.os, ['linux'], file);
            }
            const empty = JSON.parse(JSON.stringify(parser('config:\n  container:\n    use: base\n    vipc: []\n    dragon: []\n    actions: {}\n')));
            assert.deepEqual(empty.vipc, [], file);
            assert.deepEqual(empty.dragon, [], file);
            if ('hasVipcList' in empty) {
              assert.equal(empty.hasVipcList, true, file);
              assert.equal(empty.hasDragonList, true, file);
            }
          }
        '''
        result = subprocess.run(['node', '-e', node_script], input=json.dumps({'text': text, 'vipcPath': original['config']['container']['vipc'][0]['path'], 'files': [
            str(ROOT / '.github/pages/configure.html'), str(ROOT / '.github/pages/dependencies.html'),
            str(ROOT / 'actions/dashboard/configure.html'), str(ROOT / 'actions/dashboard/dependencies.html')]}),
            text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_reversed_analyzer_rules_remain_readable_after_settings_change(self):
        original = self.preservation_fixture()
        original['config']['viAnalyzer']['rules'] = [{'paths': 'src/**|test/**', 'config': 'custom.viancfg'}]
        result, text = self.apply_settings(initial=yaml.safe_dump(original, sort_keys=False), IN_RUNNERS='3')
        self.assertEqual(result.returncode, 0, result.stderr)
        expected = copy.deepcopy(original)
        expected['config']['concurrency'] = {'maxParallel': 3}
        self.assert_preserved(yaml.safe_load(text), expected)
        tree = ast.parse(SCRIPT)
        parser = next(node for node in tree.body if isinstance(node, ast.FunctionDef)
                      and node.name == '_parse_via_block')
        namespace = {'re': re}
        exec(compile(ast.Module(body=[parser], type_ignores=[]), 'reconfigure.yml', 'exec'), namespace)
        self.assertEqual(namespace['_parse_via_block'](text)['rules'],
                         [{'config': 'custom.viancfg', 'paths': ['src/**', 'test/**']}])
        node_script = r'''
          const assert = require('node:assert/strict');
          const fs = require('node:fs');
          const vm = require('node:vm');
          const input = JSON.parse(fs.readFileSync(0, 'utf8'));
          for (const file of input.files) {
            const source = fs.readFileSync(file, 'utf8');
            const match = source.match(/    function parseViAnalyzerBlock\(text\)\s*\{[\s\S]*?\n    \}/);
            assert.ok(match, file);
            const parser = vm.runInNewContext(match[0] + '; parseViAnalyzerBlock');
            const config = JSON.parse(JSON.stringify(parser(input.text)));
            assert.deepEqual(config.rules, [{config: 'custom.viancfg', paths: ['src/**', 'test/**']}], file);
          }
        '''
        result = subprocess.run(['node', '-e', node_script], input=json.dumps({'text': text, 'files': [
            str(ROOT / '.github/pages/configure.html'), str(ROOT / 'actions/dashboard/configure.html')]}),
            text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_explicit_none_retains_configured_empty_dependency_lists(self):
        original = self.preservation_fixture()
        expected = copy.deepcopy(original)
        expected['config']['container']['vipc'] = []
        expected['config']['container']['dragon'] = []
        result, text = self.apply_settings(initial=yaml.safe_dump(original), IN_CONTAINER_VIPC='none', IN_CONTAINER_DRAGON='none')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assert_preserved(yaml.safe_load(text), expected)
        tree = ast.parse((ROOT / 'actions/dashboard/dashboard.py').read_text())
        parser = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == '_parse_container_config')
        namespace = {'os': os, 're': re}
        exec(compile(ast.Module(body=[parser], type_ignores=[]), 'dashboard.py', 'exec'), namespace)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'config.yml'
            path.write_text(text)
            parsed = namespace['_parse_container_config'](str(path))
        self.assertEqual(parsed['vipc'], [])
        self.assertEqual(parsed['dragon'], [])
        self.assertTrue(parsed['hasVipcList'])
        self.assertTrue(parsed['hasDragonList'])

    def test_updating_an_aliased_mapping_preserves_other_paths(self):
        shared = {'use': 'latest', 'customSetting': True}
        original = {'config': {'container': shared}, 'clientExtension': shared}
        expected = copy.deepcopy(original)
        expected['config']['container'] = {**shared, 'use': 'base'}
        result, text = self.apply_settings(initial=yaml.safe_dump(original), IN_CONTAINER_USE='base')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assert_preserved(yaml.safe_load(text), expected)

    def test_explicit_setting_changes_do_not_rewrite_other_fields(self):
        original = self.preservation_fixture()
        expected = copy.deepcopy(original)
        expected['config']['concurrency'] = {'maxParallel': 3}
        expected['config']['container']['vipc'] = [{'path': 'new.vipc', 'monitor': True, 'monitorOn': 'linux'}]
        result, text = self.apply_settings(initial=yaml.safe_dump(original), IN_RUNNERS='3',
                                           IN_CONTAINER_VIPC='new.vipc::linux')
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

    def test_invalid_manifest_is_not_overwritten(self):
        for initial in ('config: [\n', '- not-a-mapping\n', 'config: scalar\n', 'config:\n  container: scalar\n'):
            with self.subTest(initial=initial):
                result, text = self.apply_settings(initial=initial, IN_CONTAINER_USE='base')
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(text, initial)

    def test_source_monitoring_changes_only_requested_lists(self):
        original = self.preservation_fixture()
        result, text = self.apply_settings(initial=yaml.safe_dump(original), source=True,
            IN_MONITORING_ONLY='true', IN_EXPECTED_SHA='HEAD',
            IN_CONTAINER_VIPC='.github/labview/antidoc/antidoc.vipc::windows+linux;.github/labview/experimental.vipc::linux')
        self.assertEqual(result.returncode, 0, result.stderr)
        actual = yaml.safe_load(text)
        entries = actual['config']['container']['vipc']
        self.assertEqual([entry.get('monitorOn', 'windows+linux') for entry in entries], ['windows+linux', 'linux'])
        original['config']['container']['vipc'] = entries
        self.assert_preserved(actual, original)

    def test_source_save_rejects_unscoped_or_stale_requests_without_changes(self):
        for inputs in ({}, {'IN_MONITORING_ONLY':'true'},
                       {'IN_MONITORING_ONLY':'true','IN_EXPECTED_SHA':'a'*40},
                       {'IN_MONITORING_ONLY':'true','IN_EXPECTED_SHA':'HEAD','IN_LINUX_WORKER':'reset'},
                       {'IN_MONITORING_ONLY':'true','IN_EXPECTED_SHA':'HEAD','IN_ACTS':'dashboard'},
                       {'IN_MONITORING_ONLY':'true','IN_EXPECTED_SHA':'HEAD','IN_VERSION':'2025'}):
            result, text = self.apply_settings(source=True, **inputs)
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(text, MANIFEST)

    def apply_settings(self, initial=MANIFEST, source=False, **inputs):
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / '.github/labview-ci.yml'
            config.parent.mkdir()
            config.write_text(initial)
            if source:
                for name in ('.github/workflows/release.yml', '.github/labview-ci/catalog.json'):
                    path = Path(directory) / name
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_text('source-only sentinel')
                for arguments in (['init','-q'], ['add','.'], ['-c','user.name=Test','-c','user.email=test@example.invalid','commit','-qm','fixture']):
                    subprocess.run(['git', *arguments], cwd=directory, check=True, capture_output=True)
                if inputs.get('IN_EXPECTED_SHA') == 'HEAD':
                    inputs['IN_EXPECTED_SHA'] = subprocess.check_output(['git','rev-parse','HEAD'], cwd=directory, text=True).strip()
            environment = {key: value for key, value in os.environ.items() if not key.startswith('IN_') and key != 'GITHUB_ENV'}
            environment.update({key: '' for key in APPLY['env']})
            environment.update(inputs)
            result = subprocess.run([sys.executable, '-c', SCRIPT], cwd=directory,
                                    env=environment, capture_output=True, text=True)
            return result, config.read_text()


if __name__ == '__main__':
    unittest.main()
