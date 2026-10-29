import ast
import json
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[2]
DASHBOARD = ROOT / 'actions/dashboard/dashboard.py'
SOURCE = DASHBOARD.read_text()
TREE = ast.parse(SOURCE)
TARGETS = ast.literal_eval(next(node.value for node in TREE.body
                                if isinstance(node, ast.Assign)
                                and any(isinstance(target, ast.Name) and target.id == 'RUN_TARGETS'
                                        for target in node.targets)))
NEWEST, MIDDLE, OLDEST = 'a' * 40, 'b' * 40, 'c' * 40


def javascript_function(name):
    start = SOURCE.index(f'    function {name}(')
    end = SOURCE.index('\n    function ', start + 1)
    return SOURCE[start:end]


class SnapshotScopeTests(unittest.TestCase):
    def run_javascript(self, body):
        functions = ['bfFill', 'bfInputs', 'dispatchTotal', 'dispatchCells',
                     'histIncludedShas', 'histIdxIn', 'histCells', 'captureSnapshotRun']
        harness = '''
const assert = require('node:assert/strict');
const RT = TARGETS;
const newest = NEWEST, middle = MIDDLE, oldest = OLDEST;
const HIST = [newest, middle, oldest].map(sha => ({sha}));
let scope = 'specific', selected = [newest], from = '', to = '';
let cells = HIST.map((revision, order) => ({cap:'snapshots', sha:revision.sha, order}));
let actMode = {snapshots:'fill'};
let store = {}, dispatched = [], response = {ok:true};
const document = {
  querySelectorAll: () => selected.map(value => ({value})),
  getElementById: id => ({value:id==='cidash-hist-from' ? from : to})
};
const histScopeMode = () => scope;
const histVisible = () => HIST;
const histAllCells = () => cells;
const histSelectedPlatforms = cap => Object.keys(RT[cap].platforms);
const cellPlatforms = cell => cell.platform ? [cell.platform] : histSelectedPlatforms(cell.cap);
const markQueued = (cap, sha, plats, parent, ts) => { store[cap+'|'+sha] = {plats, parent, ts}; };
const qLoad = () => store;
const qSave = value => { store = value; };
const qForget = (cap, sha) => { delete store[cap+'|'+sha]; };
const dispatchOne = async (wf, inputs) => { dispatched.push({wf, inputs}); return response; };
const captureRuns = () => {};
const captureBatchRun = () => {};
const clearTok = () => {};
const getTok = () => 'test';
const REPO = 'owner/repo';
const setTimeout = callback => callback();
let runs = [];
const fetch = async () => ({ok:true, json:async () => ({workflow_runs:runs})});
'''
        for key, value in [('TARGETS', TARGETS), ('NEWEST', NEWEST), ('MIDDLE', MIDDLE), ('OLDEST', OLDEST)]:
            harness = harness.replace(key, json.dumps(value))
        script = harness + '\n'.join(javascript_function(name) for name in functions)
        script += '\n(async () => {\n' + body + '\n})().catch(error => { console.error(error); process.exitCode = 1; });'
        result = subprocess.run(['node', '-e', script], text=True, capture_output=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_latest_revision_only(self):
        self.run_javascript('''
const selectedCells = histCells();
assert.equal(dispatchTotal(selectedCells), 1);
assert.deepEqual(await dispatchCells(selectedCells, () => {}), {ok:1, err:0, total:1, fails:[]});
assert.deepEqual(dispatched, [{wf:'vi-snapshots.yml', inputs:{mode:'backfill', shas:newest}}]);
assert.deepEqual(Object.keys(store), ['snapshots|'+newest]);
''')

    def test_range_excludes_older_revisions(self):
        self.run_javascript('''
scope = 'range'; from = middle; to = newest;
await dispatchCells(histCells(), () => {});
assert.equal(dispatched[0].inputs.shas, middle+' '+newest);
assert.equal(store['snapshots|'+oldest], undefined);
''')

    def test_all_history_is_still_an_explicit_selection(self):
        self.run_javascript('''
scope = 'all';
await dispatchCells(histCells(), () => {});
assert.equal(dispatched.length, 1);
assert.equal(dispatched[0].inputs.shas, oldest+' '+middle+' '+newest);
assert.equal(Object.keys(store).length, 3);
''')

    def test_empty_selection_dispatches_nothing(self):
        self.run_javascript('''
selected = [];
await dispatchCells(histCells(), () => {});
assert.deepEqual(dispatched, []);
assert.deepEqual(store, {});
''')

    def test_nonconsecutive_selection_and_force(self):
        self.run_javascript('''
selected = [newest, oldest]; actMode.snapshots = 'rerun'; cells[0].done = true;
await dispatchCells(histCells(), () => {}, {reuse:true});
assert.deepEqual(dispatched[0].inputs, {mode:'backfill', shas:oldest+' '+newest, force:'true'});
assert.equal(store['snapshots|'+middle], undefined);
''')

    def test_fill_skips_completed_selected_revisions(self):
        self.run_javascript('''
scope = 'all'; cells[1].done = true;
await dispatchCells(histCells(), () => {});
assert.equal(dispatched[0].inputs.shas, oldest+' '+newest);
''')

    def test_duplicate_cells_are_deduplicated(self):
        self.run_javascript('''
cells.push({...cells[0]});
await dispatchCells(histCells(), () => {});
assert.equal(dispatched.length, 1);
assert.equal(dispatched[0].inputs.shas, newest);
''')

    def test_other_activities_do_not_queue_snapshots(self):
        self.run_javascript('''
cells = [{cap:'masscompile', sha:newest, order:0, platform:'windows'}];
actMode = {masscompile:'fill'};
await dispatchCells(histCells(), () => {});
assert.equal(dispatched.length, 1);
assert.equal(dispatched[0].wf, 'masscompile-windows-container.yml');
assert.equal(dispatched[0].inputs.commit_sha, newest);
''')

    def test_rejected_new_input_never_falls_back_to_all_history(self):
        self.run_javascript('''
response = {ok:false, status:422};
const result = await dispatchCells(histCells(), () => {});
assert.equal(result.err, 1);
assert.equal(dispatched.length, 1);
assert.equal(dispatched[0].inputs.shas, newest);
assert.deepEqual(store, {});
''')

    def test_browser_run_capture_is_scoped(self):
        self.run_javascript('''
const timestamp = Date.now();
store = Object.fromEntries(HIST.map(revision => ['snapshots|'+revision.sha, {ts:timestamp}]));
runs = [
  {id:1, display_title:'VI Snapshots and Browser - '+newest, created_at:new Date(timestamp+1).toISOString()},
  {id:2, display_title:'VI Snapshots and Browser - '+middle, created_at:new Date(timestamp).toISOString()}
];
captureSnapshotRun(timestamp);
await new Promise(resolve => setImmediate(resolve));
assert.equal(store['snapshots|'+newest].runs[0].id, 1);
assert.equal(store['snapshots|'+middle].runs[0].id, 2);
assert.equal(store['snapshots|'+oldest].runs, undefined);
''')

    def test_single_cell_inputs_include_its_revision(self):
        self.assertEqual(TARGETS['snapshots']['platforms']['all']['inputs'],
                         {'mode': 'backfill', 'shas': '{sha}'})

    def test_server_run_attribution(self):
        function = next(node for node in TREE.body if isinstance(node, ast.FunctionDef)
                        and node.name == '_target_shas_for_run')
        namespace = {'re': re}
        exec(compile(ast.Module(body=[function], type_ignores=[]), str(DASHBOARD), 'exec'), namespace)
        target_shas = namespace['_target_shas_for_run']
        for title, expected in [
            (f'VI Snapshots and Browser - {NEWEST}', [NEWEST]),
            (f'VI Snapshots and Browser - {OLDEST} {NEWEST}', [OLDEST, NEWEST]),
            ('VI Snapshots and Browser - backfill', ['*']),
            ('VI Snapshots and VI Browser', []),
        ]:
            with self.subTest(title=title):
                self.assertEqual(target_shas({'event': 'workflow_dispatch', 'display_title': title},
                                            'snapshots'), expected)
        self.assertEqual(target_shas({'event': 'push', 'head_sha': NEWEST}, 'snapshots'), [NEWEST])


@unittest.skipUnless(shutil.which('pwsh'), 'PowerShell is required for renderer commit-selection tests')
class RendererSelectionTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.workspace = Path(self.temporary.name)
        self.git('init', '-q')
        self.git('config', 'user.name', 'Test')
        self.git('config', 'user.email', 'test@example.com')
        self.commits = []
        for revision in range(3):
            (self.workspace / 'test.vi').write_text(str(revision))
            self.git('add', '.')
            self.git('commit', '-qm', f'Revision {revision}')
            self.commits.append(self.git('rev-parse', 'HEAD'))

    def git(self, *arguments):
        return subprocess.check_output(['git', '-C', str(self.workspace), *arguments], text=True).strip()

    def select(self, shas, mode='backfill', max_commits=0):
        for path in ['.github/labview/build-snapshots.ps1', 'actions/snapshots/build-snapshots.ps1']:
            source = (ROOT / path).read_text()
            selection = source[source.index('if (-not [string]::IsNullOrWhiteSpace($Shas))'):]
            selection = selection[:selection.index('Write-Host "Mode=')]
            script = self.workspace / 'selection.ps1'
            script.write_text('param($WorkspaceRoot, $Shas, $Mode, [int]$MaxCommits)\n'
                              "$TargetSha = ''\n" + selection + '\nConvertTo-Json -Compress -InputObject @($Commits)')
            result = subprocess.run(['pwsh', '-NoProfile', '-File', str(script), str(self.workspace),
                                     shas, mode, str(max_commits)], text=True, capture_output=True, timeout=15)
            yield path, result

    def test_explicit_selection_overrides_history_and_cap(self):
        expected = [self.commits[0], self.commits[2]]
        for path, result in self.select(' '.join(expected + [expected[0]]), max_commits=1):
            with self.subTest(path=path):
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(json.loads(result.stdout), expected)

    def test_blank_selection_preserves_head_and_backfill(self):
        for mode, maximum, expected in [('head', 0, self.commits[-1:]),
                                        ('backfill', 0, self.commits),
                                        ('backfill', 1, self.commits[-1:])]:
            for path, result in self.select('', mode=mode, max_commits=maximum):
                with self.subTest(path=path, mode=mode, maximum=maximum):
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertEqual(json.loads(result.stdout), expected)

    def test_invalid_or_missing_commit_does_not_expand_scope(self):
        for shas in ['not-a-sha', 'd' * 40]:
            for path, result in self.select(shas):
                with self.subTest(path=path, shas=shas):
                    self.assertNotEqual(result.returncode, 0)


if __name__ == '__main__':
    unittest.main()
