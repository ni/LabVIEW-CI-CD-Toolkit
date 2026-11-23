#!/usr/bin/env python3
"""Allow initial seeding only when no worker pointer has been published."""
import argparse
import base64
import json
import os
from pathlib import Path
import re
from urllib.error import HTTPError
from urllib.request import Request, urlopen


def seed_needed(repository, platform, token, fetch=urlopen):
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9-]*/[A-Za-z0-9_.-]+', repository):
        raise ValueError('Invalid repository')
    if platform not in ('windows', 'linux') or not token.strip():
        raise ValueError('Platform and authenticated repository access are required')
    path = f'workers/{platform}/latest.json'
    request = Request(f'https://api.github.com/repos/{repository}/contents/{path}?ref=gh-pages',
                      headers={'Authorization': f'Bearer {token}',
                               'Accept': 'application/vnd.github+json'})
    try:
        with fetch(request, timeout=30) as response:
            entry = json.load(response)
    except HTTPError as error:
        if error.code == 404:
            return True
        raise
    if (entry.get('type') != 'file' or entry.get('path') != path
            or entry.get('encoding') != 'base64'):
        raise ValueError('Existing worker pointer cannot be inspected; refusing to replace it')
    pointer = json.loads(base64.b64decode(entry['content']).decode('utf-8-sig'))
    if (pointer.get('platform') != platform
            or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]*', str(pointer.get('version', '')))):
        raise ValueError('Existing worker pointer is invalid; refusing to replace it')
    print(f'Preserving initialized {platform} worker {pointer["version"]}. Use the dependency build workflow to update it.')
    return False


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--platform', choices=('windows', 'linux'), required=True)
    args = parser.parse_args()
    needed = seed_needed(os.environ['GITHUB_REPOSITORY'], args.platform, os.environ['GH_TOKEN'])
    with Path(os.environ['GITHUB_OUTPUT']).open('a') as output:
        output.write(f'seed={str(needed).lower()}\n')


if __name__ == '__main__':
    main()
