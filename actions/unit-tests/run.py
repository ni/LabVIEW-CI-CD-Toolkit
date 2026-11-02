"""Host orchestration; runner and renderer come from this action revision's source tree."""
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile

SOURCE = Path(__file__).resolve().parents[2]


def single_line(value):
    if any(c in value for c in '\r\n\0'):
        raise ValueError('Action inputs must be single-line values')
    return value


def report_path(workspace, value):
    path = (workspace / value).resolve()
    if (not value or Path(value).is_absolute() or path == workspace or not path.is_relative_to(workspace)
            or path.relative_to(workspace).parts[0].lower() in ('.git', '.github')):
        raise ValueError('report-dir must be a dedicated directory inside the workspace')
    return path


def verdict(summary, runner_code, fail_on_test_failure):
    if runner_code or summary['errored'] or not summary['tests']:
        return 1
    if not summary['passed'] and not summary['failed']:
        return 1
    return int(fail_on_test_failure and summary['failed'] > 0)


def render_report(report, workspace, version, pages):
    spec = importlib.util.spec_from_file_location('unit_report', SOURCE / '.github/labview/build-unittest-report.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    import sys
    previous_args = sys.argv
    try:
        sys.argv = ['build-unittest-report.py', '--results', str(report / 'results'), '--out', str(report),
                    '--workspace', str(workspace), '--platform', 'windows', '--sha', os.environ['GITHUB_SHA'],
                    '--repo', os.environ['GITHUB_REPOSITORY'], '--pages-url', pages, '--labview-version', version]
        module.main()
    finally:
        sys.argv = previous_args


def main():
    if os.name != 'nt':
        raise RuntimeError('This action requires a Windows runner with Windows containers (windows-2022)')
    workspace = Path(os.environ['GITHUB_WORKSPACE']).resolve()
    report = report_path(workspace, single_line(os.environ['UT_REPORT_DIR']))
    version = single_line(os.environ['UT_LV_VERSION'])
    if not re.fullmatch(r'20\d{2}', version):
        raise ValueError('labview-version must be a four-digit year matching the image')
    framework = single_line(os.environ['UT_FRAMEWORK']).lower()
    if not re.fullmatch(r'[a-z0-9_.-]+', framework):
        raise ValueError('framework must be a nonempty identifier')
    fail = os.environ['UT_FAIL']
    if fail not in ('true', 'false'):
        raise ValueError('fail-on-test-failure must be true or false')
    values = {'LVCI_FRAMEWORK': framework,
              'LVCI_TEST_DIR': single_line(os.environ.get('UT_TEST_DIR', '')),
              'LVCI_COMMAND': single_line(os.environ.get('UT_COMMAND', ''))}
    if any(c in values['LVCI_TEST_DIR'] for c in '"<>|'):
        raise ValueError('test-dir contains invalid Windows path characters')
    image = single_line(os.environ['UT_IMAGE'])
    if not image or image.startswith('-'):
        raise ValueError('image must be an image reference')
    report.mkdir(parents=True, exist_ok=True)
    for name in ('index.html', 'results.json'):
        (report / name).unlink(missing_ok=True)
    results = report / 'results'
    if results.is_symlink():
        raise ValueError('The results directory must not be a symbolic link')
    if results.exists():
        shutil.rmtree(results)
    results.mkdir()
    subprocess.run(['powershell.exe', '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass',
                    '-File', str(SOURCE / '.github/labview/ensure-docker.ps1')], check=True)
    subprocess.run(['docker', 'pull', image], check=True)
    relative = report.relative_to(workspace).as_posix().replace('/', '\\')
    with tempfile.TemporaryDirectory(prefix='lvci-unit-', dir=os.environ['RUNNER_TEMP']) as directory:
        env_file = Path(directory) / 'runner.env'
        env_file.write_text(''.join(f'{key}={value}\n' for key, value in values.items()), encoding='utf-8')
        result = subprocess.run([
            'docker', 'run', '--rm', '--env-file', str(env_file),
            '-e', 'GITHUB_REPOSITORY', '-e', 'GITHUB_SHA',
            '-v', f'{workspace}:C:\\workspace', '-v', f'{SOURCE}:C:\\ci-toolkit:ro',
            image, 'powershell', '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass',
            '-File', 'C:\\ci-toolkit\\.github\\labview\\run-unit-tests.ps1',
            '-WorkspaceRoot', 'C:\\workspace', '-ResultsDir', f'C:\\workspace\\{relative}\\results',
            '-LabVIEWVersion', version])
    pages = single_line(os.environ.get('UT_PAGES_URL', '')) or 'https://{}.github.io/{}'.format(
        *os.environ['GITHUB_REPOSITORY'].split('/', 1))
    render_report(report, workspace, version, pages)
    summary = json.loads((report / 'results.json').read_text(encoding='utf-8'))['summary']
    with open(os.environ['GITHUB_OUTPUT'], 'a', encoding='utf-8') as output:
        output.write(f'report-exists=true\ntests={summary["tests"]}\npassed={summary["passed"]}\n'
                     f'failed={summary["failed"] + summary["errored"]}\n')
    return verdict(summary, result.returncode, fail == 'true')


if __name__ == '__main__':
    raise SystemExit(main())
