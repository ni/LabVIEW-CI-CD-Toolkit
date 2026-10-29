#!/usr/bin/env python3
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import textwrap
import unittest
import xml.etree.ElementTree as ET


SCRIPT = Path(__file__).with_name('vidiff.sh')
RENDERER = SCRIPT.with_name('build-vidiff-report.py')
specification = importlib.util.spec_from_file_location('vidiff_report', RENDERER)
renderer = importlib.util.module_from_spec(specification)
specification.loader.exec_module(renderer)


class VIDiffTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.bin_dir = self.root / 'bin'
        self.bin_dir.mkdir()
        self.base = self.root / 'base'
        self.head = self.root / 'head'
        self.report = self.root / 'report'
        self.base.mkdir()
        self.head.mkdir()
        self.operations = self.root / 'operations dir'
        self.operations.mkdir()
        (self.operations / RENDERER.name).write_text(RENDERER.read_text())
        self.mock('find', '#!/bin/sh\necho /mock/labviewprofull\n')
        self.mock('pkill', '''\
            #!/bin/sh
            printf '%s\\n' "$*" >> "$MOCK_ROOT/resets"
            if [ "${MOCK_RESET_STATUS:-0}" -gt 1 ]; then exit "$MOCK_RESET_STATUS"; fi
            rm -f "$MOCK_ROOT/poisoned"
            exit "${MOCK_RESET_STATUS:-0}"
        ''')
        self.mock('sleep', '#!/bin/sh\nexit 0\n')
        self.mock('sed', '''\
            #!/bin/sh
            if [ "$1" = -i ]; then exit 0; fi
            exec /usr/bin/sed "$@"
        ''')
        self.mock('LabVIEWCLI', '''\
            #!/usr/bin/env python3
            import os
            from pathlib import Path
            import sys

            arguments = sys.argv[1:]
            root = Path(os.environ['MOCK_ROOT'])
            output_flag = '-ReportPath' if '-ReportPath' in arguments else '-OutputPath'
            output = Path(arguments[arguments.index(output_flag) + 1])
            counter = output.parent / 'calls'
            attempt = int(counter.read_text()) + 1 if counter.exists() else 1
            counter.write_text(str(attempt))
            with (root / 'operations').open('a') as log:
                log.write(arguments[arguments.index('-OperationName') + 1] + '\\n')
            mode = os.environ.get('MOCK_MODE', 'success')
            if (root / 'poisoned').exists():
                print('Error code : -350000', file=sys.stderr)
                sys.exit(1)
            failing = 'bad.vi' in str(output.parent)
            if failing and mode in ('recover66', 'recover_connection', 'persistent', 'ordinary',
                                    'retry_missing_report'):
                if attempt == 1 or mode in ('persistent', 'ordinary'):
                    error = {'recover66': 66, 'recover_connection': -350000,
                             'persistent': 66, 'ordinary': 7, 'retry_missing_report': 66}[mode]
                    output.write_text('partial report')
                    if error in (66, -350000):
                        (root / 'poisoned').touch()
                    print(f'Error code : {error}', file=sys.stderr)
                    sys.exit(1)
            if not (failing and mode in ('missing_report', 'retry_missing_report')):
                if '-ReportType' in arguments:
                    assert arguments[arguments.index('-ReportType') + 1] == 'XML'
                    if mode == 'invalid_xml':
                        output.write_text('<report>')
                    else:
                        output.write_text('<report><detailInfo><comparedVIs path1="before.vi" '
                                          'path2="after.vi"/></detailInfo></report>')
                else:
                    output.write_text('<html><head></head><body>report</body></html>')
        ''')

    def mock(self, name, source):
        path = self.bin_dir / name
        path.write_text(textwrap.dedent(source).replace('#!/usr/bin/env python3',
                                                      f'#!{sys.executable}', 1), encoding='utf-8')
        path.chmod(0o755)

    def vi(self, directory, name):
        path = directory / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b'RSRC\0\0\0\0LVIN')

    def run_diff(self, names, mode='success', reset_status=0):
        environment = dict(os.environ, PATH=f'{self.bin_dir}:{os.environ["PATH"]}',
                           MOCK_ROOT=str(self.root), MOCK_MODE=mode,
                           MOCK_RESET_STATUS=str(reset_status), CHANGED_FILES='\n'.join(names))
        return subprocess.run(['bash', str(SCRIPT), str(self.base), str(self.head),
                               str(self.report), str(self.operations)],
                              env=environment, text=True, capture_output=True, timeout=15)

    def manifest(self):
        return json.loads((self.report / 'changes.json').read_text())['files']

    def test_modified_added_deleted_and_non_labview_files(self):
        self.vi(self.base, 'modified.vi')
        self.vi(self.head, 'modified.vi')
        self.vi(self.head, 'new file.ctl')
        self.vi(self.base, 'deleted.vi')
        (self.head / 'text.vi').write_text('not a VI')
        result = self.run_diff(['modified.vi', 'new file.ctl', 'deleted.vi', 'text.vi'])
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual([entry['type'] for entry in self.manifest()],
                         ['modified', 'added', 'deleted'])
        self.assertEqual((self.root / 'operations').read_text().splitlines(),
                         ['CreateComparisonReport', 'PrintToSingleFileHtml', 'PrintToSingleFileHtml'])
        self.assertFalse((self.root / 'resets').exists())

    def test_session_errors_retry_each_operation_once(self):
        for mode in ('recover66', 'recover_connection'):
            for kind in ('modified', 'added', 'deleted'):
                with self.subTest(mode=mode, kind=kind), tempfile.TemporaryDirectory() as scratch:
                    self.base = Path(scratch) / 'base'
                    self.head = Path(scratch) / 'head'
                    self.report = Path(scratch) / 'report'
                    self.base.mkdir()
                    self.head.mkdir()
                    name = 'folder with spaces/bad.vi'
                    if kind != 'added':
                        self.vi(self.base, name)
                    if kind != 'deleted':
                        self.vi(self.head, name)
                    result = self.run_diff([name], mode)
                    self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                    self.assertEqual(self.manifest()[0]['type'], kind)
                    output = self.report / 'folder_with_spaces-bad.vi'
                    self.assertEqual((output / 'calls').read_text(), '2')
                    self.assertIn('Error code :', (output / 'cli-attempt-1.log').read_text())
                    self.assertTrue((output / 'cli-attempt-2.log').exists())
                    self.assertIn('Retrying', result.stdout)
        self.assertEqual((self.root / 'resets').read_text().splitlines(),
                         ['-KILL -x labviewprofull'] * 6)

    def test_persistent_session_error_is_bounded_and_later_files_continue(self):
        for name in ('bad.vi', 'good.vi'):
            self.vi(self.base, name)
            self.vi(self.head, name)
        result = self.run_diff(['bad.vi', 'good.vi'], 'persistent')
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertEqual((self.report / 'bad.vi/calls').read_text(), '2')
        self.assertEqual([entry['file'] for entry in self.manifest()], ['good.vi'])
        self.assertEqual(len((self.root / 'resets').read_text().splitlines()), 2)

    def test_ordinary_operation_error_is_not_retried(self):
        self.vi(self.head, 'bad.vi')
        result = self.run_diff(['bad.vi'], 'ordinary')
        self.assertEqual(result.returncode, 1)
        self.assertEqual((self.report / 'bad.vi/calls').read_text(), '1')
        self.assertFalse((self.root / 'resets').exists())
        self.assertEqual(self.manifest(), [])
        index = (self.report / 'index.html').read_text()
        self.assertIn('No comparable VI changes', index)
        self.assertIn('</html>', index)

    def test_success_without_report_is_failure(self):
        self.vi(self.head, 'bad.vi')
        result = self.run_diff(['bad.vi'], 'missing_report')
        self.assertEqual(result.returncode, 1)
        self.assertIn('produced no report', result.stderr)
        self.assertEqual(self.manifest(), [])

    def test_invalid_xml_is_failure_and_cannot_reuse_previous_html(self):
        self.vi(self.base, 'good.vi')
        self.vi(self.head, 'good.vi')
        result = self.run_diff(['good.vi'])
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue((self.report / 'good.vi/index.html').exists())
        result = self.run_diff(['good.vi'], 'invalid_xml')
        self.assertEqual(result.returncode, 1)
        self.assertIn('Could not render comparison XML', result.stdout)
        self.assertFalse((self.report / 'good.vi/index.html').exists())
        self.assertEqual(self.manifest(), [])

    def test_reset_failure_prevents_retry(self):
        self.vi(self.head, 'bad.vi')
        result = self.run_diff(['bad.vi'], 'recover66', reset_status=2)
        self.assertEqual(result.returncode, 1)
        self.assertEqual((self.report / 'bad.vi/calls').read_text(), '1')
        self.assertIn('Could not reset', result.stderr)

    def test_partial_report_from_failed_attempt_cannot_mask_missing_retry_report(self):
        self.vi(self.head, 'bad.vi')
        result = self.run_diff(['bad.vi'], 'retry_missing_report')
        self.assertEqual(result.returncode, 1)
        self.assertIn('produced no report', result.stderr)
        self.assertEqual((self.report / 'bad.vi/calls').read_text(), '2')
        self.assertFalse((self.report / 'bad.vi/index.html').exists())
        self.assertEqual(self.manifest(), [])

    def test_already_exited_labview_can_be_restarted(self):
        self.vi(self.head, 'bad.vi')
        result = self.run_diff(['bad.vi'], 'recover66', reset_status=1)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(len(self.manifest()), 1)


class ComparisonReportTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.source = self.root / 'comparison.xml'
        self.destination = self.root / 'index.html'

    def write_report(self, image_path='images/before #1.png'):
        report = ET.Element('report')
        ET.SubElement(report, 'basicInfo', genTime='10/29/2026')
        details = ET.SubElement(report, 'detailInfo')
        comparison = ET.SubElement(details, 'comparedVIs', path1='before<&>.vi', path2='after.vi')
        for anchor, title, query in renderer.GROUPS:
            parent, child = query.split('/')
            container = comparison.find(parent)
            if container is None:
                container = ET.SubElement(comparison, parent)
            attributes = {'detailDesc': '<script>alert("unsafe")</script>\nmultiline',
                          'diffType': 'changed', 'objLabel': 'Label <&>', 'objType': 'String'}
            if anchor == 'fpfunc':
                attributes['imagePath1'] = image_path
                attributes['imagePath2'] = 'images/after.png'
            ET.SubElement(container, child, attributes)
        ET.ElementTree(report).write(self.source, encoding='utf-8')

    def test_all_change_categories_images_and_escaped_descriptions(self):
        self.write_report()
        (self.root / 'images').mkdir()
        (self.root / 'images/before #1.png').write_bytes(b'image')
        (self.root / 'images/after.png').write_bytes(b'image')
        renderer.render_report(self.source, self.destination)
        html = self.destination.read_text()
        self.assertEqual(html.count('<article>'), 5)
        self.assertEqual(html.count('<img '), 2)
        self.assertIn('images/before%20%231.png', html)
        self.assertIn('before&lt;&amp;&gt;.vi', html)
        self.assertIn('&lt;script&gt;', html)
        self.assertNotIn('<script>', html)
        for anchor, title, query in renderer.GROUPS:
            self.assertIn(f'{title} (1)', html)
        self.assertIn('</html>', html)

    def test_missing_native_images_have_explicit_placeholders(self):
        self.write_report()
        renderer.render_report(self.source, self.destination)
        html = self.destination.read_text()
        self.assertEqual(html.count('LabVIEW did not generate an image'), 2)
        self.assertNotIn('<img ', html)
        self.assertEqual(html.count('<article>'), 5)

    def test_images_cannot_escape_the_report_directory(self):
        self.write_report('../outside.png')
        with self.assertRaisesRegex(ValueError, 'Unsafe'):
            renderer.render_report(self.source, self.destination)

    def test_invalid_or_truncated_xml_is_rejected(self):
        for xml in ('<report>', '<unrelated/>', '<report/>'):
            with self.subTest(xml=xml):
                self.source.write_text(xml)
                with self.assertRaises((ValueError, ET.ParseError)):
                    renderer.render_report(self.source, self.destination)

    def test_comparison_without_changes_is_valid(self):
        self.source.write_text('<report><detailInfo><comparedVIs/></detailInfo></report>')
        renderer.render_report(self.source, self.destination)
        html = self.destination.read_text()
        self.assertEqual(html.count('No changes reported'), 5)
        self.assertNotIn('<article>', html)


if __name__ == '__main__':
    unittest.main()
