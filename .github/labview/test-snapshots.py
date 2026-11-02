import ast
import importlib.util
import json
from pathlib import Path
import re
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
