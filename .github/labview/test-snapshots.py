import ast
import copy
import importlib.util
import json
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import textwrap
import unittest
from unittest.mock import patch


sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location('snapshot_installer', ROOT / '.github/labview-ci/install.py')
INSTALLER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(INSTALLER)


class SnapshotTests(unittest.TestCase):
    def setUp(self):
        self.catalog = json.loads((ROOT / '.github/labview-ci/catalog.json').read_text())

    def test_one_capability_installs_rich_renderers(self):
        capabilities = {entry['id']: entry for entry in self.catalog['capabilities']}
        self.assertNotIn('snapshots-2', capabilities)
        self.assertEqual(capabilities['snapshots']['supportsOs'], ['windows', 'linux'])
        for platform, workflow in [('windows', 'vi-snapshots-json-windows.yml'), ('linux', 'vi-snapshots-json.yml')]:
            files = INSTALLER.resolve_file_list(self.catalog, ['snapshots'], [platform])
            self.assertIn('.github/workflows/' + workflow, files)
            self.assertIn('.github/pages/vi-render.js', files)
            self.assertIn('.github/labview/toimages/', files)

    def test_legacy_activity_alias_migrates(self):
        self.assertEqual(INSTALLER.resolve_activities(self.catalog, ['snapshots-2', 'snapshots']), ['snapshots'])

    def test_snapshot_opt_out_is_preserved(self):
        self.assertEqual(INSTALLER.resolve_activities(self.catalog, ['dashboard']), ['dashboard'])
        self.assertNotIn('.github/workflows/vi-snapshots-json-windows.yml',
                         INSTALLER.resolve_file_list(self.catalog, ['dashboard'], ['windows']))

    def test_retirement_contract_rejects_unsafe_or_live_paths(self):
        retired = '.github/workflows/build-toimages-image.yml'
        self.assertIn(retired, INSTALLER.retired_file_rules(self.catalog, ROOT))
        for path in ['../victim.yml', '/tmp/victim.yml', '.github/workflows/../victim.yml',
                     '.github/workflows/x/y.yml', '.github/workflows/vi-snapshots.yml']:
            catalog = copy.deepcopy(self.catalog)
            catalog['retiredFiles']['files'] = [path]
            with self.subTest(path=path), self.assertRaises(ValueError):
                INSTALLER.retired_file_rules(catalog, ROOT)
        for entry in [retired, '.github/workflows/']:
            catalog = copy.deepcopy(self.catalog)
            catalog['base']['files'].append(entry)
            with self.assertRaises(ValueError):
                INSTALLER.retired_file_rules(catalog, ROOT)
        catalog = copy.deepcopy(self.catalog)
        catalog['userConfig']['files'].append(retired)
        with self.assertRaises(ValueError):
            INSTALLER.retired_file_rules(catalog, ROOT)
        catalog = copy.deepcopy(self.catalog)
        catalog['retiredFiles']['signatures'] = {}
        with self.assertRaises(ValueError):
            INSTALLER.retired_file_rules(catalog, ROOT)

    def test_retired_workflow_cleanup_preserves_unknown_content_and_symlinks(self):
        rules = INSTALLER.retired_file_rules(self.catalog, ROOT)
        rel, markers = next(iter(rules.items()))
        known = '\n'.join(markers)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / rel
            target.parent.mkdir(parents=True)
            stats = {'planned': 0, 'pruned': 0}
            target.write_text(known)
            INSTALLER.prune_retired_files(rules, root, True, stats)
            self.assertTrue(target.exists())
            self.assertEqual(stats, {'planned': 1, 'pruned': 0})
            INSTALLER.prune_retired_files(rules, root, False, stats)
            self.assertFalse(target.exists())
            self.assertEqual(stats['pruned'], 1)
            target.write_text('name: My own image build')
            INSTALLER.prune_retired_files(rules, root, False, stats)
            self.assertTrue(target.exists())
            target.unlink()
            outside = root / 'outside.yml'
            outside.write_text(known)
            try:
                target.symlink_to(outside)
            except OSError:
                self.skipTest('symlinks are unavailable on this host')
            INSTALLER.prune_retired_files(rules, root, False, stats)
            self.assertTrue(target.is_symlink())
            target.unlink()
            target.parent.rmdir()
            target.parent.symlink_to(root, target_is_directory=True)
            victim = root / target.name
            victim.write_text(known)
            INSTALLER.prune_retired_files(rules, root, False, stats)
            self.assertEqual(victim.read_text(), known)
            target.parent.unlink()
            target.mkdir(parents=True)
            INSTALLER.prune_retired_files(rules, root, False, stats)
            self.assertTrue(target.is_dir())

    def test_cli_retirement_runs_only_in_scratch_clients(self):
        rel = '.github/workflows/build-toimages-image.yml'
        rules = INSTALLER.retired_file_rules(self.catalog, ROOT)
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory)
            retired = target / rel
            retired.parent.mkdir(parents=True)
            retired.write_text('\n'.join(rules[rel]))
            command = [sys.executable, str(ROOT / '.github/labview-ci/install.py'),
                       '--source', str(ROOT), '--target', directory, '--activities', 'dashboard',
                       '--repo', 'scratch/client', '--no-vars']
            dry = subprocess.run(command + ['--dry-run'], capture_output=True, text=True)
            self.assertEqual(dry.returncode, 0, dry.stderr)
            self.assertTrue(retired.exists())
            for options in ([], ['--update'], ['--thin']):
                retired.write_text('\n'.join(rules[rel]))
                result = subprocess.run(command + options, capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertFalse(retired.exists())
                self.assertNotIn('snapshots', INSTALLER.read_manifest(target)['activities'])
            (target / '.github/workflows/release.yml').write_text('name: Client release')
            result = subprocess.run(command + ['--update'], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            sentinel = target / '.github/labview-ci/source.json'
            sentinel.write_text('source-only fixture')
            before = (target / '.github/labview-ci.yml').read_bytes()
            result = subprocess.run(command + ['--update'], capture_output=True, text=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('refusing to install', result.stderr)
            self.assertEqual((target / '.github/labview-ci.yml').read_bytes(), before)

    def test_browser_keep_settings_and_retired_inventory(self):
        source = (ROOT / '.github/pages/integrate.html').read_text()
        names = ['resolveActivities', 'resolveFiles', 'effectiveValues', 'userConfigPaths', 'isUserConfig',
                 'managedUniverse', 'retiredWorkflowSignatures', 'inventoryManagedFiles', 'staleFor']
        functions = []
        for name in names:
            match = re.search(r'    (?:async )?function ' + name + r'\(', source)
            end = re.search(r'\n    (?:async )?function ', source[match.end():])
            functions.append(source[match.start():match.end() + end.start()])
        script = "const assert = require('node:assert/strict');\nconst catalog = " + json.dumps(self.catalog) + ";\n"
        script += next(line.strip() for line in source.splitlines() if line.strip().startswith('const LVCI_WF_SIG ='))
        script += "\n".join(functions)
        script += r'''
const selected = new Set(['dashboard']), osSel = new Set(['windows']);
const $ = () => ({value:'2026'});
const byId = id => catalog.capabilities.find(c => c.id === id);
let priorState = {manifest:{activities:['dashboard','snapshots-2'],os:['windows']}};
assert.deepEqual(effectiveValues(true).activities, ['dashboard','snapshots']);
assert.deepEqual(effectiveValues(false).activities, ['dashboard']);
assert.deepEqual(resolveActivities(['snapshots-2','snapshots']), ['snapshots']);
const retired = catalog.retiredFiles.files[0];
const markers = catalog.retiredFiles.signatures[retired];
const textFor = {};
textFor[retired] = markers.join('\n');
const paths = Array.from({length:45}, (_,i) => `.github/workflows/user-${i}.yml`).concat(retired);
const ghWrite = async () => ({tree: paths.map(path => ({type:'blob',path}))});
const b64decodeText = text => text;
const gh = async url => ({ok:true,json:async () => ({content:textFor[decodeURIComponent(url.split('/contents/')[1].split('?')[0])] || 'name: User workflow'})});
(async () => {
  priorState.managedFiles = await inventoryManagedFiles('scratch/client','main');
  assert.deepEqual([...priorState.managedFiles], [retired]);
  assert.deepEqual(staleFor(true), [retired]);
  textFor[retired] = 'name: User workflow with LABVIEW_VERSION';
  assert.equal((await inventoryManagedFiles('scratch/client','main')).has(retired), false);
  assert.equal(retiredWorkflowSignatures('../victim.yml'), null);
  catalog.base.files.push(retired);
  assert.equal(retiredWorkflowSignatures(retired), null);
})().catch(error => {console.error(error);process.exitCode=1;});
'''
        result = subprocess.run(['node', '-'], input=script, text=True, capture_output=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        for name in ['integrate.html', 'documentation.html', 'configure.html']:
            self.assertEqual((ROOT / '.github/pages' / name).read_bytes(),
                             (ROOT / 'actions/dashboard' / name).read_bytes())

    def test_gitlab_installs_snapshot_jobs_for_both_platforms(self):
        provider = json.loads((ROOT / '.github/labview-ci/providers/gitlab/files.json').read_text())
        pipeline = INSTALLER.gitlab_pipeline_yml(['snapshots'], ['windows', 'linux'], '2026', provider)
        for template in ['snapshots-windows.yml', 'snapshots-linux.yml']:
            self.assertIn(template, pipeline)
        self.assertNotIn('snapshots-2', pipeline)

    def test_coverage_counts_frames_not_html_exports(self):
        source = ast.parse((ROOT / 'actions/dashboard/dashboard.py').read_text())
        function = next(node for node in source.body if isinstance(node, ast.FunctionDef) and node.name == 'snapshot_coverage')
        namespace = {
            '_snap_cov_cache': {},
            'vi_tree': lambda sha: [{'vi_rel': 'one.vi', 'blob': 'one'}, {'vi_rel': 'two.vi', 'blob': 'two'}, {'vi_rel': 'three.vi', 'blob': 'three'}],
            'rendered_json_blobs': lambda platform: {'one'} if platform == 'windows' else {'two'},
        }
        exec(compile(ast.Module(body=[function], type_ignores=[]), '<coverage>', 'exec'), namespace)
        self.assertEqual(namespace['snapshot_coverage']('commit'), (2, 3))

    def test_generation_scripts_only_render_frames(self):
        builder = (ROOT / '.github/labview/build-snapshots.ps1').read_text()
        renderer = (ROOT / '.github/labview/render-snapshots.ps1').read_text()
        self.assertIn("-Filter '*.windows.json'", builder)
        self.assertIn("'json-blobs.windows.json'", builder)
        self.assertIn('toimages\\windows-render.ps1', renderer)
        self.assertNotIn('PrintToSingleFileHtml', renderer)
        self.assertNotIn('PrintToImagesJson', renderer)

    def test_distributed_viewers_match(self):
        for name in ['vi-browser.html', 'vi-interactive.html', 'vi-render.js']:
            expected = (ROOT / 'actions/snapshots' / name).read_bytes()
            for directory in ['actions/dashboard', '.github/pages']:
                self.assertEqual(expected, (ROOT / directory / name).read_bytes())

    def test_reconfigure_embedded_python_parses(self):
        source = (ROOT / '.github/workflows/reconfigure.yml').read_text()
        for match in re.finditer(r"python3 - <<'PY'\n(.*?)\n\s+PY", source, re.S):
            ast.parse(textwrap.dedent(match.group(1)))

    def test_report_previews_use_interactive_viewer(self):
        for path in ['.github/labview/build-analyzer-report.py', '.github/labview/build-masscompile-report.py', '.github/labview/build-unittest-report.py', 'actions/vi-analyzer/build-analyzer-report.py', 'actions/masscompile/build-masscompile-report.py']:
            source = (ROOT / path).read_text()
            self.assertIn('vi-interactive.html?file=', source, path)
            self.assertNotIn('frame.src = SNAP_BASE+html;', source, path)

    def test_metadata_supports_frame_only_revisions(self):
        specification = importlib.util.spec_from_file_location('snapshot_metadata', ROOT / '.github/labview/build-snapshot-metadata.py')
        metadata = importlib.util.module_from_spec(specification)
        specification.loader.exec_module(metadata)
        commit = 'a' * 40
        blob = 'b' * 40
        replies = [commit, f'100644 blob {blob}\tproject/Code VI.vi\0', 'Code change', 'Author', '2026-10-29T10:00:00Z']
        with tempfile.TemporaryDirectory() as temporary, patch.object(metadata, 'git', side_effect=replies):
            output = Path(temporary)
            manifest = metadata.build_metadata(ROOT, output, commit, ROOT / '.github/pages')
            self.assertEqual(manifest[0]['vi_rel'], 'project/Code VI.vi')
            self.assertTrue((output / commit / 'manifest.json').is_file())
            self.assertTrue((output / 'commits.json').is_file())
            self.assertTrue((output / 'index.html').is_file())
            self.assertFalse(any(output.rglob(blob + '.html')))


if __name__ == '__main__':
    unittest.main()
