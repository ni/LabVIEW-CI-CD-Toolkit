import argparse
import importlib.util
import json
from pathlib import Path
import shutil
import subprocess
import sys


sys.dont_write_bytecode = True
SPEC = importlib.util.spec_from_file_location('snapshot_gallery', Path(__file__).with_name('build-gallery.py'))
GALLERY = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(GALLERY)


def git(workspace, *arguments):
    return subprocess.check_output(['git', '-C', str(workspace), *arguments]).decode('utf-8')


def build_metadata(workspace, output, sha, pages, existing=None):
    output.mkdir(parents=True, exist_ok=True)
    commit = git(workspace, 'rev-parse', '--verify', '--end-of-options', sha + '^{commit}').strip()
    vimap = []
    for record in git(workspace, 'ls-tree', '-r', '-z', commit).split('\0'):
        if not record or '\t' not in record:
            continue
        metadata, relative = record.split('\t', 1)
        if not relative.lower().endswith(('.vi', '.ctl')):
            continue
        if relative.split('/')[0] in {'.github', 'actions', '_lvci', 'ci-out', 'build'}:
            continue
        vimap.append((metadata.split()[2], relative))
    vimap.sort(key=lambda entry: entry[1].lower())
    manifest = GALLERY.build_manifest(vimap, commit, 'by-blob')
    commit_dir = output / commit
    commit_dir.mkdir(parents=True, exist_ok=True)
    (commit_dir / 'manifest.json').write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding='utf-8')
    commits_file = output / 'commits.json'
    if not commits_file.exists() and existing and (existing / 'commits.json').is_file():
        shutil.copy2(existing / 'commits.json', commits_file)
    message = git(workspace, 'show', '-s', '--format=%s', commit).strip()
    author = git(workspace, 'show', '-s', '--format=%an', commit).strip()
    date = git(workspace, 'show', '-s', '--format=%aI', commit).strip()
    commits = GALLERY.update_commits_json(commits_file, commit, message, author, date, len(manifest))
    commits_file.write_text(json.dumps(commits, indent=2, ensure_ascii=False), encoding='utf-8')
    for source, destination in [('vi-browser.html', 'index.html'), ('vi-interactive.html', 'vi-interactive.html'), ('vi-render.js', 'vi-render.js')]:
        shutil.copy2(pages / source, output / destination)
    return manifest


def main():
    parser = argparse.ArgumentParser(description='Publish snapshot revision metadata and interactive browser assets.')
    parser.add_argument('--workspace', type=Path, default=Path.cwd())
    parser.add_argument('--out', type=Path, required=True)
    parser.add_argument('--sha', default='HEAD')
    parser.add_argument('--pages', type=Path, default=Path(__file__).resolve().parents[1] / 'pages')
    parser.add_argument('--existing', type=Path)
    arguments = parser.parse_args()
    build_metadata(arguments.workspace, arguments.out, arguments.sha, arguments.pages, arguments.existing)


if __name__ == '__main__':
    main()
