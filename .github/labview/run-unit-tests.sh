#!/usr/bin/env bash
set -euo pipefail
exec python3 -I -S - "$@" <<'PY'
import glob
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys
import xml.etree.ElementTree as ET

workspace = Path(sys.argv[1] if len(sys.argv) > 1 else '/workspace').resolve()
results = Path(sys.argv[2] if len(sys.argv) > 2 else workspace / 'ci-out/unit-tests/results').resolve()
config = Path(sys.argv[3] if len(sys.argv) > 3 else workspace / '.github/labview-ci.yml')
results.mkdir(parents=True, exist_ok=True)
for path in results.iterdir():
    if path.is_file() and (path.suffix in ('.xml', '.log') or path.name == '_tooling.json'):
        path.unlink()
issues = []
runner_failed = False
names = {'lunit': 'LUnit', 'caraya': 'Caraya', 'utf': 'NI Unit Test Framework', 'vi-tester': 'JKI VI Tester'}


def failure(tool, index, detail):
    global runner_failed
    runner_failed = True
    suite = ET.Element('testsuite', name=tool + ' runner', tests='1', errors='1')
    case = ET.SubElement(suite, 'testcase', name='Runner failure')
    ET.SubElement(case, 'error', message=detail.splitlines()[0]).text = detail
    ET.ElementTree(suite).write(results / f'{tool}-runner-{index}.xml', encoding='utf-8', xml_declaration=True)
    print(detail, flush=True)


def scalar(value):
    value = value.strip()
    if value.startswith('"'):
        decoded, end = json.JSONDecoder().raw_decode(value)
        if value[end:].strip() and not value[end:].lstrip().startswith('#'):
            raise ValueError('Unexpected text after quoted scalar')
        return decoded
    if value.startswith("'"):
        match = re.fullmatch(r"'((?:[^']|'')*)'\s*(?:#.*)?", value)
        if not match:
            raise ValueError('Unterminated quoted scalar')
        return match[1].replace("''", "'")
    return re.split(r'\s+#', value, maxsplit=1)[0].strip()


def read_tools():
    tools = []
    block_indent = None
    tools_indent = None
    current = None
    locations = False
    explicit = False
    for raw in config.read_text(encoding='utf-8-sig').splitlines() if config.exists() else []:
        line = raw.expandtabs(4)
        text = line.strip()
        if not text or text.startswith('#'):
            continue
        indent = len(line) - len(line.lstrip())
        if text.startswith('unitTests:'):
            explicit = True
            block_indent, tools_indent, current = indent, None, None
            remainder = scalar(text.split(':', 1)[1])
            if remainder not in ('', '{}', 'null', '~'):
                raise ValueError('Use a block mapping or {} for unitTests')
            continue
        if block_indent is None:
            continue
        if indent <= block_indent:
            block_indent, tools_indent, current = None, None, None
            continue
        if indent == block_indent + 2 and text == 'tools:':
            tools_indent = indent
            continue
        if tools_indent is None:
            continue
        if indent <= tools_indent:
            tools_indent, current = None, None
            continue
        if indent == tools_indent + 2 and re.fullmatch(r'[\w.-]+:', text):
            current = {'tool': text[:-1].lower(), 'enabled': False, 'command': '', 'locations': []}
            tools.append(current)
            locations = False
        elif current is not None and indent == tools_indent + 4:
            key, sep, value = text.partition(':')
            locations = key == 'locations'
            if key == 'enabled':
                current['enabled'] = scalar(value) == 'true'
            elif key == 'command':
                current['command'] = scalar(value)
            elif key == 'locations' and value.strip() not in ('', '[]'):
                raise ValueError('Use a block list or [] for unit-test locations')
        elif current is not None and locations and indent == tools_indent + 6 and text.startswith('- '):
            current['locations'].append(scalar(text[2:]))
    return explicit, [tool for tool in tools if tool['enabled']]


def discover():
    found = {}
    signatures = {'caraya': b'Caraya.lvlib', 'lunit': b'LUnit.lvlib', 'vi-tester': b'TestCase.lvclass'}
    for directory, dirs, files in os.walk(workspace):
        dirs[:] = sorted(d for d in dirs if d not in ('.github', 'ci-out', 'build', '.git') and not Path(directory, d).is_symlink())
        for name in sorted(files):
            path = Path(directory, name)
            if path.is_symlink():
                continue
            if path.suffix == '.lvproj':
                data = path.read_bytes()
                if b'Type="TestItem"' in data or b'.lvtest' in data:
                    found.setdefault('utf', set()).add(path.parent)
            elif path.suffix == '.vi':
                data = path.read_bytes()
                for tool, signature in signatures.items():
                    if signature in data:
                        found.setdefault(tool, set()).add(path.parent)
    tools = []
    for tool, directories in found.items():
        roots = []
        for directory in sorted(directories, key=lambda p: (len(p.parts), str(p))):
            if not any(root == directory or root in directory.parents for root in roots):
                roots.append(directory)
        tools.append({'tool': tool, 'command': '', 'locations': [str(p.relative_to(workspace)) for p in roots]})
    return tools


def resolve_roots(locations):
    if not locations:
        return [workspace]
    roots = set()
    for location in locations:
        if not location:
            continue
        location = location.replace('\\', '/')
        full = workspace / location
        if full.is_dir():
            roots.add(full.resolve())
        else:
            pattern = str(full) if '/' in location else str(workspace / '**' / location)
            for match in glob.iglob(pattern, recursive=True):
                if Path(match).is_file():
                    roots.add(Path(match).resolve().parent)
    return sorted(roots)


cli = os.environ.get('LABVIEWCLI') or shutil.which('LabVIEWCLI') or ''
labview = os.environ.get('LABVIEW_EXE') or next(iter(sorted(glob.glob('/usr/local/natinst/LabVIEW-*/labviewprofull'))), '')
version = os.environ.get('LABVIEW_VERSION') or next(iter(re.findall(r'LabVIEW-(\d{4})', labview)), '2026')
default = '"{cli}" -LogToConsole TRUE -OperationName LUnit -Path "{dir}" -ReportPath "{out}" -LabVIEWPath "{lv}" -Headless'
print(f'=== Unit Tests (Linux) ===\nWorkspace: {workspace}\nLabVIEW: {labview}\nLabVIEWCLI: {cli}', flush=True)
try:
    explicit, tools = read_tools()
    if not explicit:
        tools = discover()
        print('Auto-detected frameworks: ' + ', '.join(t['tool'] for t in tools), flush=True)
    if not tools:
        failure('unit-tests', 0, 'No enabled unit-test tools configured or detected.')
    for index, tool in enumerate(tools):
        id = tool['tool']
        name = names.get(id, id)
        if id != 'lunit':
            issues.append(dict(tool=id, name=name, kind='unsupported-on-linux', detail=f'{name} has no Linux runner. Run it on Windows.'))
            continue
        roots = resolve_roots(tool['locations'])
        if not roots:
            failure(id, index, 'No test locations resolved for LUnit.')
        for offset, root in enumerate(roots):
            number = f'{index}-{offset}'
            out = results / f'lunit-{number}.xml'
            if not cli or not labview:
                detail = 'LabVIEWCLI or the LabVIEW executable was not found in this container.'
                issues.append(dict(tool=id, name=name, kind='missing-tooling', detail=detail))
                failure(id, number, detail)
                continue
            tokens = {'cli': cli, 'lv': labview, 'ver': version, 'dir': str(root), 'out': str(out)}
            argv = [re.sub(r'\{(cli|lv|ver|dir|out)\}', lambda m: tokens[m[1]], arg)
                    for arg in shlex.split(tool['command'] or default)]
            if not argv or argv[-1] != '-Headless':
                print('WARNING: Linux LabVIEWCLI commands should end with -Headless.', flush=True)
            log = out.with_suffix('.log')
            print('Running: ' + shlex.join(argv), flush=True)
            try:
                with log.open('wb') as stream:
                    code = subprocess.run(argv, cwd=workspace, stdout=stream, stderr=subprocess.STDOUT).returncode
            except OSError as error:
                code = -1
                log.write_text(str(error), encoding='utf-8')
            output = log.read_text(encoding='utf-8', errors='replace')
            if code != 0 or not out.is_file():
                match = re.search(r'started logging in file:\s*([^\r\n]+\.log)', output, re.I)
                if match and Path(match[1].strip()).is_file():
                    output += '\n' + Path(match[1].strip()).read_text(encoding='utf-8', errors='replace')
                    log.write_text(output, encoding='utf-8')
                missing = re.search(r'350053|350006|missing or bad files|required modules or toolkits', output, re.I)
                detail = f'LUnit runner exited {code}' + (' without JUnit output.' if not out.is_file() else '.')
                issues.append(dict(tool=id, name=name, kind='missing-tooling' if missing else 'error', detail=detail))
                failure(id, number, detail + '\n' + output)
            print(output, flush=True)
except (ValueError, OSError) as error:
    failure('unit-tests', 0, 'Unit-test configuration or discovery failed: ' + str(error))
if issues:
    (results / '_tooling.json').write_text(json.dumps({'missing': issues}), encoding='utf-8')
sys.exit(1 if runner_failed else 0)
PY
