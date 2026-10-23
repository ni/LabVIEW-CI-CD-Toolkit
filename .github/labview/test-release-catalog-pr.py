import copy
import importlib.util
import json
import subprocess
import unittest
from pathlib import Path
from unittest.mock import patch


SPEC = importlib.util.spec_from_file_location('release_pr', Path(__file__).with_name('release-catalog-pr.py'))
RELEASE_PR = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(RELEASE_PR)


class ReleaseCatalogPrTests(unittest.TestCase):
    def setUp(self):
        self.pull = {
            'merged_at': '2026-10-05T00:00:00Z',
            'user': {'login': 'github-actions[bot]'},
            'head': {'ref': 'lvci-release/release-4.18.5',
                     'repo': {'full_name': 'ni/LabVIEW-CI-CD-Toolkit'}},
            'base': {'ref': 'main'},
        }
        self.files = ['.github/labview-ci/catalog.json', '.github/labview-ci/notes/fix.md']

    def prepared(self, pull=None, files=None):
        return RELEASE_PR.prepared_pr(pull or self.pull, files or self.files,
                                      'ni/LabVIEW-CI-CD-Toolkit', 'main')

    def test_merged_bot_catalog_pr_resumes(self):
        self.assertTrue(self.prepared())

    def test_unmerged_pr_cannot_resume(self):
        self.pull['merged_at'] = None
        self.assertFalse(self.prepared())

    def test_user_named_branch_cannot_resume(self):
        self.pull['user']['login'] = 'contributor'
        self.assertFalse(self.prepared())

    def test_fork_or_other_base_cannot_resume(self):
        pull = copy.deepcopy(self.pull)
        pull['head']['repo']['full_name'] = 'someone/fork'
        self.assertFalse(self.prepared(pull))
        self.pull['base']['ref'] = 'other'
        self.assertFalse(self.prepared())

    def test_unrelated_files_cannot_resume(self):
        self.assertFalse(self.prepared(files=self.files + ['.github/workflows/release.yml']))
        self.assertFalse(self.prepared(files=['.github/labview-ci/notes/fix.md']))

    def test_only_explicit_pr_rule_rejection_uses_fallback(self):
        result = subprocess.CompletedProcess([], 1, '', 'GH013: Changes must be made through a pull request')
        self.assertTrue(RELEASE_PR.requires_pr(result))
        result.stderr = 'GH013: Required status check is missing'
        self.assertFalse(RELEASE_PR.requires_pr(result))
        result.stderr = 'Authentication failed'
        self.assertFalse(RELEASE_PR.requires_pr(result))

    def test_successful_push_publishes_without_pr(self):
        with patch.object(RELEASE_PR, 'run', return_value=subprocess.CompletedProcess([], 0, '', '')) as command:
            with patch.object(RELEASE_PR, 'output') as output:
                RELEASE_PR.push('ni/LabVIEW-CI-CD-Toolkit', 'main', '4.18.5', 'release')
        command.assert_called_once_with('git', 'push', 'origin', 'HEAD:main', check=False)
        output.assert_called_once_with('published', 'true')

    def test_authentication_failure_never_opens_pr(self):
        with patch.object(RELEASE_PR, 'run', return_value=subprocess.CompletedProcess([], 1, '', 'Authentication failed')) as command:
            with self.assertRaises(RuntimeError):
                RELEASE_PR.push('ni/LabVIEW-CI-CD-Toolkit', 'main', '4.18.5', 'release')
        self.assertEqual(command.call_count, 1)

    def test_protected_push_creates_pr_and_defers_publication(self):
        results = [
            subprocess.CompletedProcess([], 1, '', 'GH013: Changes must be made through a pull request'),
            subprocess.CompletedProcess([], 0, '', ''),
            subprocess.CompletedProcess([], 0, '[]', ''),
            subprocess.CompletedProcess([], 0, 'https://github.com/ni/LabVIEW-CI-CD-Toolkit/pull/123\n', ''),
        ]
        with patch.object(RELEASE_PR, 'run', side_effect=results) as command:
            with patch.object(RELEASE_PR, 'output') as output:
                RELEASE_PR.push('ni/LabVIEW-CI-CD-Toolkit', 'main', '4.18.5', 'promotion')
        self.assertEqual(command.call_args_list[1].args,
                         ('git', 'push', 'origin', 'HEAD:refs/heads/lvci-release/promotion-4.18.5'))
        output.assert_any_call('published', 'false')
        output.assert_any_call('pr_url', 'https://github.com/ni/LabVIEW-CI-CD-Toolkit/pull/123')

    def test_protected_push_reuses_existing_pr(self):
        results = [
            subprocess.CompletedProcess([], 1, '', 'GH013: Changes must be made through a pull request'),
            subprocess.CompletedProcess([], 0, '', ''),
            subprocess.CompletedProcess([], 0, '[{"url":"existing-pr"}]', ''),
        ]
        with patch.object(RELEASE_PR, 'run', side_effect=results) as command:
            with patch.object(RELEASE_PR, 'output') as output:
                RELEASE_PR.push('ni/LabVIEW-CI-CD-Toolkit', 'main', '4.18.5', 'release')
        self.assertEqual(command.call_count, 3)
        output.assert_any_call('pr_url', 'existing-pr')

    def test_stale_preparation_branch_gets_a_commit_scoped_review_branch(self):
        commit = 'a' * 40
        branch = f'lvci-release/release-4.18.5-{commit}'

        def commands(*args, check=True):
            if args == ('git', 'push', 'origin', 'HEAD:main'):
                return subprocess.CompletedProcess(args, 1, '', 'GH013: Changes must be made through a pull request')
            if args == ('git', 'push', 'origin', 'HEAD:refs/heads/lvci-release/release-4.18.5'):
                return subprocess.CompletedProcess(args, 1, '', 'non-fast-forward')
            if args == ('git', 'rev-parse', 'HEAD'):
                return subprocess.CompletedProcess(args, 0, commit + '\n', '')
            if args == ('git', 'push', 'origin', f'HEAD:refs/heads/{branch}'):
                return subprocess.CompletedProcess(args, 0, '', '')
            if args[:3] == ('gh', 'pr', 'list'):
                self.assertIn(branch, args)
                return subprocess.CompletedProcess(args, 0, '[]', '')
            if args[:3] == ('gh', 'pr', 'create'):
                self.assertIn(branch, args)
                return subprocess.CompletedProcess(args, 0, 'new-review-pr\n', '')
            self.fail(f'Unexpected command: {args}')

        with patch.object(RELEASE_PR, 'run', side_effect=commands) as command:
            with patch.object(RELEASE_PR, 'output') as output:
                RELEASE_PR.push('ni/LabVIEW-CI-CD-Toolkit', 'main', '4.18.5', 'release')
        self.assertFalse(any('--force' in argument for call in command.call_args_list for argument in call.args))
        output.assert_any_call('published', 'false')
        output.assert_any_call('pr_url', 'new-review-pr')

    def test_preparation_authentication_failure_does_not_try_another_branch(self):
        results = [
            subprocess.CompletedProcess([], 1, '', 'GH013: Changes must be made through a pull request'),
            subprocess.CompletedProcess([], 1, '', 'Authentication failed'),
        ]
        with patch.object(RELEASE_PR, 'run', side_effect=results) as command:
            with patch.object(RELEASE_PR, 'output') as output:
                with self.assertRaisesRegex(RuntimeError, 'Authentication failed'):
                    RELEASE_PR.push('ni/LabVIEW-CI-CD-Toolkit', 'main', '4.18.5', 'release')
        self.assertEqual(command.call_count, 2)
        output.assert_not_called()

    def test_commit_scoped_push_failure_does_not_open_pr(self):
        results = [
            subprocess.CompletedProcess([], 1, '', 'GH013: Changes must be made through a pull request'),
            subprocess.CompletedProcess([], 1, '', 'non-fast-forward'),
            subprocess.CompletedProcess([], 0, 'a' * 40 + '\n', ''),
            subprocess.CompletedProcess([], 1, '', 'remote rejected: protected branch'),
        ]
        with patch.object(RELEASE_PR, 'run', side_effect=results) as command:
            with patch.object(RELEASE_PR, 'output') as output:
                with self.assertRaisesRegex(RuntimeError, 'remote rejected: protected branch'):
                    RELEASE_PR.push('ni/LabVIEW-CI-CD-Toolkit', 'main', '4.18.5', 'release')
        self.assertEqual(command.call_count, 4)
        output.assert_not_called()

    def test_commit_scoped_promotion_branch_reuses_its_existing_pr(self):
        commit = 'a' * 40
        branch = f'lvci-release/promotion-4.18.5-{commit}'
        results = [
            subprocess.CompletedProcess([], 1, '', 'GH013: Changes must be made through a pull request'),
            subprocess.CompletedProcess([], 1, '', 'fetch first'),
            subprocess.CompletedProcess([], 0, commit + '\n', ''),
            subprocess.CompletedProcess([], 0, '', ''),
            subprocess.CompletedProcess([], 0, '[{"url":"existing-promotion-pr"}]', ''),
        ]
        with patch.object(RELEASE_PR, 'run', side_effect=results) as command:
            with patch.object(RELEASE_PR, 'output') as output:
                RELEASE_PR.push('ni/LabVIEW-CI-CD-Toolkit', 'main', '4.18.5', 'promotion')
        self.assertEqual(command.call_args_list[3].args,
                         ('git', 'push', 'origin', f'HEAD:refs/heads/{branch}'))
        self.assertEqual(command.call_count, 5)
        output.assert_any_call('pr_url', 'existing-promotion-pr')

    def test_pr_creation_failure_does_not_claim_publication(self):
        results = [
            subprocess.CompletedProcess([], 1, '', 'GH013: Changes must be made through a pull request'),
            subprocess.CompletedProcess([], 0, '', ''),
            subprocess.CompletedProcess([], 0, '[]', ''),
            subprocess.CalledProcessError(1, ['gh', 'pr', 'create']),
        ]
        with patch.object(RELEASE_PR, 'run', side_effect=results):
            with patch.object(RELEASE_PR, 'output') as output:
                with self.assertRaises(subprocess.CalledProcessError):
                    RELEASE_PR.push('ni/LabVIEW-CI-CD-Toolkit', 'main', '4.18.5', 'release')
        output.assert_not_called()

    def test_resume_publishes_prepared_version_without_bumping(self):
        self.pull.update({'number': 123, 'merge_commit_sha': 'merged-sha'})
        results = [subprocess.CompletedProcess([], 0, json.dumps([self.pull]), ''),
                   subprocess.CompletedProcess([], 0, '\n'.join(self.files), '')]
        catalog = {'version': '4.18.5', 'history': {'releases': [{'version': '4.18.5'}]}}
        with patch.object(RELEASE_PR, 'run', side_effect=results):
            with patch.object(Path, 'read_text', return_value=json.dumps(catalog)):
                with patch.object(RELEASE_PR, 'output') as output:
                    RELEASE_PR.resume('ni/LabVIEW-CI-CD-Toolkit', 'main', 'merged-sha')
        output.assert_any_call('prepared', 'true')
        output.assert_any_call('version', '4.18.5')

    def test_resume_rejects_history_mismatch(self):
        self.pull.update({'number': 123, 'merge_commit_sha': 'merged-sha'})
        results = [subprocess.CompletedProcess([], 0, json.dumps([self.pull]), ''),
                   subprocess.CompletedProcess([], 0, '\n'.join(self.files), '')]
        catalog = {'version': '4.18.5', 'history': {'releases': [{'version': '4.18.4'}]}}
        with patch.object(RELEASE_PR, 'run', side_effect=results):
            with patch.object(Path, 'read_text', return_value=json.dumps(catalog)):
                with self.assertRaises(RuntimeError):
                    RELEASE_PR.resume('ni/LabVIEW-CI-CD-Toolkit', 'main', 'merged-sha')

    def test_ordinary_merge_generates_a_new_release(self):
        with patch.object(RELEASE_PR, 'run', return_value=subprocess.CompletedProcess([], 0, '[]', '')):
            with patch.object(RELEASE_PR, 'output') as output:
                RELEASE_PR.resume('ni/LabVIEW-CI-CD-Toolkit', 'main', 'ordinary-sha')
        output.assert_called_once_with('prepared', 'false')


if __name__ == '__main__':
    unittest.main()