#!/usr/bin/env python3
"""Prevent seed jobs from replacing initialized worker images or inventories."""
import base64
import importlib.util
import io
import json
from pathlib import Path
import unittest
from urllib.error import HTTPError, URLError

import yaml

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location('seed_worker', Path(__file__).with_name('seed-worker.py'))
SEED = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(SEED)


class SeedTests(unittest.TestCase):
    def response(self, platform, pointer):
        return {'type': 'file', 'path': f'workers/{platform}/latest.json', 'encoding': 'base64',
                'content': base64.b64encode(json.dumps(pointer).encode()).decode()}

    def test_initialized_seed_and_rebuilt_workers_are_preserved(self):
        for platform in ('windows', 'linux'):
            for version in ('win-982773c5fc86', 'win-6ced8a5aabaf', 'linux-012345678abc'):
                record = self.response(platform, {'platform': platform, 'version': version})
                def fetch(request, timeout):
                    self.assertEqual(request.full_url,
                        f'https://api.github.com/repos/owner/client/contents/workers/{platform}/latest.json?ref=gh-pages')
                    self.assertEqual(timeout, 30)
                    return io.BytesIO(json.dumps(record).encode())
                self.assertFalse(SEED.seed_needed('owner/client', platform, 'test-token', fetch))

    def test_only_missing_pointer_allows_fresh_seeding(self):
        for status in (404, 401, 403, 429, 500, 503):
            def fetch(request, timeout):
                raise HTTPError(request.full_url, status, 'test', {}, None)
            if status == 404:
                self.assertTrue(SEED.seed_needed('owner/client', 'windows', 'test-token', fetch))
            else:
                with self.assertRaises(HTTPError):
                    SEED.seed_needed('owner/client', 'windows', 'test-token', fetch)

    def test_unavailable_or_malformed_evidence_never_allows_seeding(self):
        def unavailable(request, timeout):
            raise URLError('offline')
        with self.assertRaises(URLError):
            SEED.seed_needed('owner/client', 'linux', 'test-token', unavailable)
        for record in ({}, {'type': 'dir'}, self.response('linux', {}),
                       self.response('linux', {'platform': 'windows', 'version': 'win-valid'}),
                       self.response('linux', {'platform': 'linux', 'version': '../escape'})):
            with self.assertRaises(ValueError):
                SEED.seed_needed('owner/client', 'linux', 'test-token',
                                 lambda *args, **kwargs: io.BytesIO(json.dumps(record).encode()))

    def test_invalid_inputs_do_not_send_requests(self):
        def forbidden(*args, **kwargs):
            self.fail('Invalid request was sent')
        for repo, platform, token in (('../escape', 'linux', 'token'),
                                      ('owner/repo', 'mac', 'token'), ('owner/repo', 'linux', '')):
            with self.assertRaises(ValueError):
                SEED.seed_needed(repo, platform, token, forbidden)

    def test_both_workflows_guard_all_external_mutations(self):
        for platform, filename in (('windows', 'copy-labview-image.yml'),
                                   ('linux', 'copy-labview-linux-image.yml')):
            workflow = yaml.safe_load((ROOT / '.github/workflows' / filename).read_text())
            job = workflow['jobs']['copy-and-push']
            self.assertEqual(job['if'], "github.ref == format('refs/heads/{0}', github.event.repository.default_branch)")
            self.assertFalse(workflow['concurrency']['cancel-in-progress'])
            self.assertEqual(workflow['concurrency']['group'],
                "${{ github.ref == format('refs/heads/{0}', github.event.repository.default_branch) && "
                + f"'build-labview-{platform}-image' || format('seed-{platform}-{{0}}', github.ref)" + ' }}')
            steps = job['steps']
            gate = next(index for index, step in enumerate(steps) if step.get('id') == 'existing')
            login = next(index for index, step in enumerate(steps) if 'docker/login-action@' in step.get('uses', ''))
            self.assertLess(gate, login)
            self.assertEqual(steps[gate]['run'], f'python .github/labview/seed-worker.py --platform {platform}')
            for step in steps[gate + 1:]:
                self.assertEqual(step['if'], "steps.existing.outputs.seed == 'true'", step)

    def test_helper_is_distributed_to_clients(self):
        catalog = json.loads((ROOT / '.github/labview-ci/catalog.json').read_text())
        self.assertIn('.github/labview/seed-worker.py', catalog['base']['files'])


if __name__ == '__main__':
    unittest.main()
