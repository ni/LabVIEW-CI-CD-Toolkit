"""Build/reuse the Windows worker from a closed, content-addressed context."""
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile

SOURCE = Path(__file__).resolve().parents[2]
DIGEST = re.compile(r'sha256:[0-9a-f]{64}')
EXTENSIONS = {'.vipc', '.vip', '.dragon'}


def single_line(value):
    if any(c in value for c in '\r\n\0'):
        raise ValueError('Action inputs must be single-line values')
    return value


def boolean(value):
    if value not in ('true', 'false'):
        raise ValueError('Boolean inputs must be true or false')
    return value == 'true'


def stage(context, workspace, deps_dir, include_tooling):
    vipm = context / '.github/labview/vipm'
    vipm.mkdir(parents=True)
    shutil.copyfile(Path(__file__).with_name('Dockerfile'), context / 'Dockerfile')
    shutil.copyfile(SOURCE / '.github/labview/vipm/install-vipc.ps1', vipm / 'install-vipc.ps1')
    if include_tooling:
        shutil.copyfile(SOURCE / '.github/labview/vipm/ci-tooling.vipc', vipm / 'ci-tooling.vipc')
    if deps_dir:
        deps = (workspace / single_line(deps_dir)).resolve()
        if Path(deps_dir).is_absolute() or not deps.is_relative_to(workspace) or not deps.is_dir():
            raise ValueError('deps-dir must be an existing directory inside the workspace')
        names = {path.name.lower() for path in vipm.iterdir()}
        for path in sorted(deps.rglob('*')):
            if path.suffix.lower() not in EXTENSIONS:
                continue
            single_line(path.name)
            if path.is_symlink() or path.parent != deps or not path.is_file():
                raise ValueError('Stage dependency files directly in deps-dir; nested files and links are unsupported')
            if path.name.lower() in names or path.name.lower().startswith('ci-tooling'):
                raise ValueError(f'Reserved or duplicate dependency filename: {path.name}')
            names.add(path.name.lower())
            shutil.copyfile(path, vipm / path.name)
    files = list(vipm.iterdir())
    if any(p.suffix.lower() == '.vip' for p in files) and not any(p.suffix.lower() == '.vipc' for p in files):
        raise ValueError('Loose .vip payloads require a VIPC referencing them; they are not installed on their own')
    return any(p.suffix.lower() in EXTENSIONS for p in files)


def fingerprint(context, inputs):
    files = {path.relative_to(context).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
             for path in sorted(context.rglob('*')) if path.is_file()}
    files['worker.py'] = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    payload = json.dumps({'files': files, 'inputs': inputs}, sort_keys=True, separators=(',', ':'))
    return 'win-' + hashlib.sha256(payload.encode()).hexdigest()[:24]


def run(args):
    return subprocess.run(args, check=True, capture_output=True, text=True).stdout.strip()


def digest(reference, allow_missing=False):
    result = subprocess.run(['crane', 'digest', reference], capture_output=True, text=True)
    if result.returncode:
        if allow_missing and re.search(r'\b(?:MANIFEST_UNKNOWN|NAME_UNKNOWN)\b', result.stderr):
            return None
        raise RuntimeError(f'Cannot resolve registry image {reference}: {result.stderr.strip()}')
    value = result.stdout.strip()
    if not DIGEST.fullmatch(value):
        raise ValueError(f'Invalid registry digest for {reference}: {value!r}')
    return value


def immutable(reference, resolved_digest):
    name = reference.split('@', 1)[0]
    if ':' in name.rsplit('/', 1)[-1]:
        name = name.rsplit(':', 1)[0]
    return name + '@' + resolved_digest


def verify_base(base):
    config = json.loads(run(['crane', 'config', '--platform', 'windows/amd64', base]))
    if config.get('os') != 'windows' or config.get('architecture') != 'amd64':
        raise ValueError('base-image must provide a windows/amd64 image')


def publish(context, image, version, base, inputs, has_dependencies, latest):
    ref = f'{image}:{version}'
    existing = digest(ref, allow_missing=True)
    if existing is None:
        if has_dependencies:
            subprocess.run(['powershell.exe', '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass',
                            '-File', str(SOURCE / '.github/labview/ensure-docker.ps1')], check=True)
            args = ['docker', 'build', '-m', '8GB', '-f', str(context / 'Dockerfile'),
                    '--build-arg', f'LCWC_BASE_IMAGE={base}', '--build-arg', f'CI_WORKER_VERSION={version}']
            for key, value in inputs.items():
                args += ['--build-arg', f'{key}={value}']
            subprocess.run(args + ['-t', ref, str(context)], check=True)
            subprocess.run(['docker', 'push', ref], check=True)
        else:
            run(['crane', 'copy', base, ref])
    final = digest(ref)
    if existing is not None and final != existing:
        raise RuntimeError('Content tag changed concurrently; refusing to move aliases')
    for tag in [inputs['LABVIEW_VERSION']] + (['latest'] if latest else []):
        run(['crane', 'tag', f'{image}@{final}', tag])
    return final, existing is None


def main():
    if os.name != 'nt':
        raise RuntimeError('This action requires Windows containers (windows-2022)')
    repo = single_line(os.environ['GITHUB_REPOSITORY'])
    owner, name = repo.lower().split('/', 1)
    name = single_line(os.environ.get('WI_IMAGE', '')).lower() or name + '-labview'
    if not re.fullmatch(r'[a-z0-9]+(?:(?:[._]|__|-+)[a-z0-9]+)*', name):
        raise ValueError('image must be an unqualified GHCR package name, without tag or owner')
    image = f'ghcr.io/{owner}/{name}'
    base_ref = single_line(os.environ['WI_BASE'])
    if not base_ref or base_ref.startswith('-'):
        raise ValueError('base-image must be an explicit readable Windows base image')
    version = single_line(os.environ['WI_VERSION'])
    if not re.fullmatch(r'20\d{2}', version):
        raise ValueError('labview-version must be a four-digit year matching the base image')
    tooling = boolean(os.environ['WI_TOOLING'])
    latest = boolean(os.environ['WI_LATEST'])
    base = immutable(base_ref, digest(base_ref))
    verify_base(base)
    inputs = {'LABVIEW_VERSION': version, 'LVCI_UNIT_TESTS_INSTALLED': str(tooling).lower(),
              'VIPM_PUBLIC_REPO_URL': 'https://github.com/ni/LabVIEW-CI-CD-Toolkit.git'}
    workspace = Path(os.environ['GITHUB_WORKSPACE']).resolve()
    with tempfile.TemporaryDirectory(prefix='lvci-worker-', dir=os.environ['RUNNER_TEMP']) as directory:
        context = Path(directory)
        deps = stage(context, workspace, os.environ.get('WI_DEPS', ''), tooling)
        tag = fingerprint(context, {'base': base, **inputs})
        final, built = publish(context, image, tag, base, inputs, deps, latest)
    with open(os.environ['GITHUB_OUTPUT'], 'a', encoding='utf-8') as output:
        output.write(f'image={image}\ntag={tag}\ndigest={final}\nbuilt={str(built).lower()}\n')


if __name__ == '__main__':
    main()
