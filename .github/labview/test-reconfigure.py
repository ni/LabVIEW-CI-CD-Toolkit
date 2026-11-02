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
        self.assertEqual(parsed['vipc'][0]['path'], 'project.vipc')
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
            assert.equal(config.vipc[0].path, 'project.vipc', file);
            assert.equal(config.dragon[0].path, 'project.dragon', file);
            assert.equal(config.vipc[0].monitor, true, file);
            assert.equal(config.dragon[0].monitor, true, file);
            assert.equal(config.actions.masscompile, 'base', file);
            if ('version' in config) {
              assert.equal(config.version, '2026', file);
              assert.deepEqual(config.os, ['linux'], file);
            }
          }
        '''
        result = subprocess.run(['node', '-e', node_script], input=json.dumps({'text': text, 'files': [
            str(ROOT / '.github/pages/configure.html'), str(ROOT / '.github/pages/dependencies.html'),
            str(ROOT / 'actions/dashboard/configure.html'), str(ROOT / 'actions/dashboard/dependencies.html')]}),
            text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)

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

    def apply_settings(self, initial=MANIFEST, **inputs):
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / '.github/labview-ci.yml'
            config.parent.mkdir()
            config.write_text(initial)
            environment = {key: value for key, value in os.environ.items() if not key.startswith('IN_') and key != 'GITHUB_ENV'}
            environment.update({key: '' for key in APPLY['env']})
            environment.update(inputs)
            result = subprocess.run([sys.executable, '-c', SCRIPT], cwd=directory,
                                    env=environment, capture_output=True, text=True)
            return result, config.read_text()


if __name__ == '__main__':
    unittest.main()
