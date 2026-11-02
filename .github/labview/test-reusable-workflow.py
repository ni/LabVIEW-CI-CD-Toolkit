"""Focused regressions for reusable-workflow tooling selection (requires PyYAML)."""

import os
from pathlib import Path
import subprocess
import tempfile
import unittest

import yaml


ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = yaml.safe_load((ROOT / '.github/workflows/labview-ci.reusable.yml').read_text())
CONFIG = WORKFLOW['jobs']['config']
SOURCE_REPO = 'ni/LabVIEW-CI-CD-Toolkit'


class ReusableWorkflowTests(unittest.TestCase):
    def test_config_reader_uses_current_major_from_canonical_source(self):
        reader = next(step for step in CONFIG['steps'] if step.get('id') == 'cfg')
        self.assertEqual(reader['uses'], f'{SOURCE_REPO}/actions/load-config@v4')

    def test_tooling_ref_defaults_and_explicit_pins(self):
        resolver = next(step for step in CONFIG['steps'] if step.get('id') == 'ref')
        cases = [
            ('missing manifest', None, 'v4'),
            ('missing ref', 'source:\n  repo: ni/LabVIEW-CI-CD-Toolkit\n', 'v4'),
            ('empty ref', 'source:\n  ref:\n', 'v4'),
            ('immutable pin', 'source:\n  ref: v4.19.0\n', 'v4.19.0'),
            ('explicit older major', 'source:\n  ref: v1\n', 'v1'),
        ]
        for label, manifest, expected in cases:
            with self.subTest(label=label), tempfile.TemporaryDirectory() as directory:
                workspace = Path(directory)
                if manifest is not None:
                    (workspace / '.github').mkdir()
                    (workspace / '.github/labview-ci.yml').write_text(manifest)
                output = workspace / 'output'
                # Match GitHub's explicit bash shell, including errexit/pipefail.
                result = subprocess.run(
                    ['bash', '--noprofile', '--norc', '-e', '-o', 'pipefail', '-c', resolver['run']],
                    cwd=workspace, env={**os.environ, 'GITHUB_OUTPUT': str(output)},
                    capture_output=True, text=True, timeout=10,
                )
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertEqual(output.read_text(), f'ref={expected}\n')
                self.assertIn(f'Capability version (source.ref) this repo opted into: {expected}', result.stdout)

    def test_all_capabilities_checkout_selected_tooling_from_canonical_source(self):
        self.assertEqual(CONFIG['outputs']['tooling-ref'], '${{ steps.ref.outputs.ref }}')
        for job_name, action in [('mass-compile', 'masscompile'), ('vi-analyzer', 'vi-analyzer'),
                                 ('vidiff', 'vidiff'), ('snapshots', 'snapshots')]:
            with self.subTest(job=job_name):
                steps = WORKFLOW['jobs'][job_name]['steps']
                checkout = next(step for step in steps if step.get('with', {}).get('path') == '_lvci')
                self.assertEqual(checkout['uses'], 'actions/checkout@v5')
                self.assertEqual(checkout['with']['repository'], SOURCE_REPO)
                self.assertEqual(checkout['with']['ref'], '${{ needs.config.outputs.tooling-ref }}')
                self.assertLess(steps.index(checkout), next(
                    index for index, step in enumerate(steps)
                    if step.get('uses') == f'./_lvci/actions/{action}'))


if __name__ == '__main__':
    unittest.main()
