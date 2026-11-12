#!/usr/bin/env python3
"""Check real native VIPM evidence through the dashboard and Dependencies renderer."""
import argparse
import ast
import importlib.util
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import xml.etree.ElementTree as ET
import zipfile


def check(root, evidence, platform):
    manifest_path = evidence / ('manifest.json' if platform == 'windows' else 'manifest/manifest.json')
    manifest = json.loads(manifest_path.read_text(encoding='utf-8-sig'))
    inventory = manifest['vipm_inventory']
    if (inventory.get('status') != 'observed' or inventory.get('target_verified') is not True
            or inventory.get('platform') != platform or inventory.get('labview_bitness') != '64'):
        raise ValueError('Native coverage requires target-verified observed inventory')
    names = {'_manifest_packages', '_vipm_version_index', '_nipkg_version_index',
             '_infer_dragon_status', '_linux_dragon_status', '_compute_deps_pending', '_known_nipm_dependencies'}
    tree = ast.parse((root / 'actions/dashboard/dashboard.py').read_text(encoding='utf-8'))
    namespace = {'os': os, 're': re, 'json': json}
    exec(compile(ast.Module(body=[node for node in tree.body if isinstance(node, ast.FunctionDef)
                                 and node.name in names], type_ignores=[]), 'dashboard.py', 'exec'), namespace)
    packages = namespace['_manifest_packages'](manifest)
    installed = {value.lower() for value in packages}
    vipcs = []
    paths = ['.github/labview/vipm/ci-tooling.vipc', 'example/Dependencies.vipc']
    if platform == 'windows':
        paths.append('.github/labview/antidoc/antidoc.vipc')
    for relative in paths:
        with zipfile.ZipFile(root / relative) as archive:
            declared = [node.text.strip() for node in ET.fromstring(archive.read('config.xml')).findall('.//Package/Name')]
        missing = [package for package in declared if package.lower() not in installed]
        if missing:
            raise ValueError(f'{platform} {relative}: missing exact installed packages: {missing}')
        vipcs.append({'path': relative, 'packages': declared, 'role': 'core' if 'ci-tooling' in relative else 'project',
                      'configured': True, 'monitored': True, 'monitorOn': [platform]})
    spec = importlib.util.spec_from_file_location('trusted_dragon', root / '.github/labview/dragon_deps.py')
    dragon = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(dragon)
    record = dragon.parse_dragon_file(str(root / 'example/Example.dragon'))
    record.update(source_file='example/Example.dragon', monitored=True, monitorOn=[platform],
                  inspection={'status': 'parsed', 'reason': 'Trusted native fixture declaration'})
    for dependency in record['dependencies']:
        if platform == 'linux':
            measured = namespace['_linux_dragon_status'](dependency, 'latest', manifest, {})
        else:
            measured = namespace['_infer_dragon_status'](dependency, True, installed,
                namespace['_vipm_version_index'](manifest), namespace['_nipkg_version_index'](manifest))
        if measured['status'] != 'installed':
            raise ValueError(f'{platform} Dragon package {dependency["package_id"]}: {measured}')
        dependency.update(measured, platform_status={platform: measured})
    index = {'sha': manifest['git_sha'], 'repo': 'native/validation', 'vipc': vipcs,
             'columns': [{'key': platform, 'tag': 'latest', 'ready': True, 'packages': packages}],
             'nipm': namespace['_known_nipm_dependencies'](),
             'dragon': {'available': True, 'files': [record], 'install_set': record['dependencies']}}
    pending = namespace['_compute_deps_pending'](index)
    if pending['pending']:
        raise ValueError(f'Dashboard still reports pending dependencies: {pending}')
    payload = {'platform': platform, 'manifest': manifest, 'index': index}
    result = subprocess.run(['node', str(root / '.github/labview/test-native-dependency-coverage.mjs')],
                            input=json.dumps(payload), capture_output=True, text=True)
    if result.returncode:
        raise ValueError(result.stdout + result.stderr)
    report = {'status': 'passed', 'platform': platform, 'commit': manifest['git_sha'],
              'observed_packages': len(inventory['packages']), 'vipcs_checked': paths,
              'dragon': 'example/Example.dragon', 'dashboard_pending': False,
              'ui': result.stdout.strip(), 'scope': 'local native evidence through production dashboard functions; publication not tested'}
    (evidence / 'dashboard-coverage.json').write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps(report, indent=2))


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--root', type=Path, default=Path(__file__).resolve().parents[2])
    parser.add_argument('--evidence', type=Path, required=True)
    parser.add_argument('--platform', choices=['windows', 'linux'], required=True)
    args = parser.parse_args()
    check(args.root, args.evidence, args.platform)
