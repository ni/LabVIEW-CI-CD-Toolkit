import argparse
import json
import os
import re
import subprocess
from pathlib import Path


CATALOG = Path('.github/labview-ci/catalog.json')


def validate_title(title):
    title = ' '.join(title.split()).strip()
    if not title or re.fullmatch(
            r'(?:Prepare LabVIEW CI (?:release|promotion)(?: v\d+\.\d+\.\d+)?|'
            r'Reconfigure LabVIEW CI|Release(?: v\d+\.\d+\.\d+)?|v\d+\.\d+\.\d+)',
            title, re.IGNORECASE):
        raise ValueError('PR title must describe concrete features, fixes, or settings; version/process-only titles are not allowed')
    return title


def build_pr_metadata(catalog, version, kind, repo):
    releases = catalog.get('history', {}).get('releases', [])
    if catalog.get('version') != version or not releases or releases[0].get('version') != version:
        raise ValueError('Prepared PR version must match the newest catalog release')
    release = releases[0]
    notes = str(release.get('notes') or '').strip()
    summary = str(release.get('summary') or notes).strip()
    summary = re.sub(r'\[([^\]]+)\]\([^)]*\)', r'\1', summary)
    summary = ' '.join(re.sub(r'[`*#]', '', summary).split())
    summary = re.split(r'(?<=[.!?])\s+', summary, maxsplit=1)[0].rstrip('.!?')
    validate_title(summary)
    if len(summary) > 110:
        summary = summary[:107].rsplit(' ', 1)[0] + '…'
    title = validate_title(f'{summary} — v{version}')
    baseline = str(releases[1]['version']) if len(releases) > 1 else None
    target_parts = tuple(int(part) for part in version.split('.'))
    previous_parts = tuple(int(part) for part in baseline.split('.')) if baseline else None
    bump = ('major' if target_parts[0] > previous_parts[0] else
            'minor' if target_parts[1] > previous_parts[1] else 'patch') if previous_parts else 'patch'
    update_type = {'major': 'Major', 'minor': 'Minor', 'patch': 'Incremental (patch)'}[bump]
    release_url = f'https://github.com/{repo}/releases/tag/v{version}'
    baseline_text = (f'[v{baseline}](https://github.com/{repo}/releases/tag/v{baseline})'
                     if baseline else 'No earlier catalog release recorded; verify published tags before merge')
    scope = ('This PR prepares release metadata and consumes release-note fragments for implementation already '
             'merged on the base branch. It does not introduce that implementation a second time.')
    guidance = (f'After publication and containment verification, select the exact immutable tooling version '
                f'`v{version}` and confirm the installed version. Publication alone does not promote beta or stable.')
    if kind == 'promotion':
        scope = ('This PR records the channel change described above for an already-published release. '
                 'It does not introduce new implementation; the metadata version is distinct from the promoted version.')
        guidance = ('Verify the already-published target and channel described above separately from this metadata '
                    'release. Do not treat the metadata version as the promoted software version.')
    body = (f'## Features and fixes\n\n{notes or summary}\n\n'
            f'## Release impact\n\n'
            f'- **Target version:** `v{version}` — prepared / not published; [immutable release]({release_url}) becomes usable only after verified publication.\n'
            f'- **Update type:** {update_type}; `release:{bump}`, derived from the prepared version and baseline.\n'
            f'- **Published baseline:** {baseline_text}; reconcile against published tags before merge.\n'
            f'- **Client update:** {guidance}\n\n'
            f'## Scope and compatibility\n\n{scope}\n\n'
            f'## Validation and publication\n\n'
            f'Review the actual catalog diff, release notes, and required checks; this proposal is not evidence that '
            f'runtime checks passed. Merge only the current proposal through normal review. If merging does not '
            f'trigger Release, run it manually on the default branch immediately afterward. The workflow recognizes '
            f'the merged bot catalog PR and publishes the prepared version without another bump. '
            f'Do not include unrelated changes or bypass protection.\n')
    return {'title': title, 'body': body, 'label': f'release:{bump}'}


def catalog_pr_metadata(repo, version, kind):
    return build_pr_metadata(json.loads(CATALOG.read_text(encoding='utf-8')), version, kind, repo)


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
            prepared = run('git', 'push', 'origin', f'HEAD:refs/heads/{branch}', check=False)
            print(prepared.stdout + prepared.stderr)
            if prepared.returncode != 0 and ('non-fast-forward' in prepared.stderr or 'fetch first' in prepared.stderr):
                commit = run('git', 'rev-parse', 'HEAD').stdout.strip()
                branch = f'{branch}-{commit}'
                prepared = run('git', 'push', 'origin', f'HEAD:refs/heads/{branch}', check=False)
                print(prepared.stdout + prepared.stderr)
            if prepared.returncode != 0:
                raise RuntimeError(f'Prepared catalog branch push failed:\n{prepared.stdout}{prepared.stderr}')
            metadata = catalog_pr_metadata(repo, version, kind)
            existing = json.loads(run('gh', 'pr', 'list', '--repo', repo, '--head', branch,
                                      '--base', base, '--json', 'url').stdout)
            if existing:
                url = existing[0]['url']
                run('gh', 'pr', 'edit', url, '--repo', repo,
                    '--title', metadata['title'], '--body', metadata['body'], '--add-label', metadata['label'])
            else:
                url = run('gh', 'pr', 'create', '--repo', repo, '--base', base, '--head', branch,
                          '--title', metadata['title'], '--body', metadata['body'],
                          '--label', metadata['label']).stdout.strip()
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
    parser.add_argument('command', choices=('resume', 'push', 'check-title'))
    parser.add_argument('--title')
    parser.add_argument('--version')
    parser.add_argument('--kind', choices=('release', 'promotion'), default='release')
    args = parser.parse_args()
    if args.command == 'check-title':
        title = args.title
        if title is None:
            event = json.loads(Path(os.environ['GITHUB_EVENT_PATH']).read_text(encoding='utf-8'))
            if 'pull_request' not in event:
                return
            title = event['pull_request']['title']
        validate_title(title)
        return
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
