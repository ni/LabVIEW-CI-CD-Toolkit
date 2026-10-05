import argparse
import json
import os
import subprocess
from pathlib import Path


CATALOG = Path('.github/labview-ci/catalog.json')


def run(*args, check=True):
    return subprocess.run(args, check=check, capture_output=True, text=True)


def output(name, value):
    print(f'{name}={value}')
    if os.environ.get('GITHUB_OUTPUT'):
        with open(os.environ['GITHUB_OUTPUT'], 'a', encoding='utf-8') as stream:
            stream.write(f'{name}={value}\n')


def prepared_pr(pull, files, repo, base):
    return (
        bool(pull.get('merged_at'))
        and pull.get('user', {}).get('login') == 'github-actions[bot]'
        and pull.get('head', {}).get('ref', '').startswith('lvci-release/')
        and (pull.get('head', {}).get('repo') or {}).get('full_name') == repo
        and pull.get('base', {}).get('ref') == base
        and '.github/labview-ci/catalog.json' in files
        and all(path == '.github/labview-ci/catalog.json'
                or path.startswith('.github/labview-ci/notes/') for path in files)
    )


def requires_pr(result):
    text = result.stdout + result.stderr
    return result.returncode != 0 and 'GH013' in text and 'Changes must be made through a pull request' in text


def resume(repo, base, sha):
    pulls = json.loads(run('gh', 'api', f'repos/{repo}/commits/{sha}/pulls').stdout)
    for pull in pulls:
        if pull.get('merge_commit_sha') != sha:
            continue
        files = run('gh', 'api', '--paginate',
                    f'repos/{repo}/pulls/{pull["number"]}/files', '--jq', '.[].filename').stdout.splitlines()
        if prepared_pr(pull, files, repo, base):
            catalog = json.loads(CATALOG.read_text(encoding='utf-8'))
            version = catalog['version']
            if catalog['history']['releases'][0]['version'] != version:
                raise RuntimeError('Prepared catalog version/history mismatch')
            output('prepared', 'true')
            output('version', version)
            return
    output('prepared', 'false')


def push(repo, base, version, kind):
    for attempt in range(5):
        result = run('git', 'push', 'origin', f'HEAD:{base}', check=False)
        print(result.stdout + result.stderr)
        if result.returncode == 0:
            output('published', 'true')
            return
        if requires_pr(result):
            branch = f'lvci-release/{kind}-{version}'
            run('git', 'push', 'origin', f'HEAD:refs/heads/{branch}')
            existing = json.loads(run('gh', 'pr', 'list', '--repo', repo, '--head', branch,
                                      '--base', base, '--json', 'url').stdout)
            if existing:
                url = existing[0]['url']
            else:
                url = run('gh', 'pr', 'create', '--repo', repo, '--base', base, '--head', branch,
                          '--title', f'Prepare LabVIEW CI {kind} v{version}',
                          '--body', 'Prepared by the release workflow because the default branch requires a pull request. '
                          'Review the catalog and merge this PR. Then run Release manually on the default branch '
                          'if GitHub did not trigger it: it recognizes this merged bot PR and publishes the prepared '
                          'version without another bump. Do not squash unrelated changes into this PR.').stdout.strip()
            output('published', 'false')
            output('pr_url', url)
            if os.environ.get('GITHUB_STEP_SUMMARY'):
                with open(os.environ['GITHUB_STEP_SUMMARY'], 'a', encoding='utf-8') as stream:
                    stream.write(f'## Catalog review required\n\nReview and merge {url}. '
                                 'Publication is deferred until this catalog PR is merged.\n')
            return
        if 'non-fast-forward' not in result.stderr and 'fetch first' not in result.stderr:
            raise RuntimeError('Catalog push failed for a reason other than branch protection or a push race')
        run('git', 'fetch', 'origin', base)
        run('git', 'rebase', f'origin/{base}')
    raise RuntimeError('Could not push the catalog after retries')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('command', choices=('resume', 'push'))
    parser.add_argument('--version')
    parser.add_argument('--kind', choices=('release', 'promotion'), default='release')
    args = parser.parse_args()
    repo = os.environ['GITHUB_REPOSITORY']
    base = os.environ['GITHUB_REF_NAME']
    default_branch = run('gh', 'api', f'repos/{repo}', '--jq', '.default_branch').stdout.strip()
    if base != default_branch:
        raise RuntimeError('Release and promotion must run on the default branch')
    if args.command == 'resume':
        resume(repo, base, os.environ['GITHUB_SHA'])
    else:
        if not args.version:
            parser.error('push requires --version')
        push(repo, base, args.version, args.kind)


if __name__ == '__main__':
    main()