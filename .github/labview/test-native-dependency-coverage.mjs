import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
const fixture = JSON.parse(fs.readFileSync(0, 'utf8'));
const html = fs.readFileSync(new URL('../pages/dependencies.html', import.meta.url), 'utf8');
const script = html.match(/<script>\s*\(\(\) => \{[\s\S]*?<\/script>/i)[0].replace(/^<script>|<\/script>$/gi, '');
const context = vm.createContext({fixture, URL, URLSearchParams, console,
  window: {LVCI: {repo: 'native/validation'}},
  location: {hostname:'native.github.io', pathname:'/validation/dependencies.html', href:'https://native.github.io/validation/dependencies.html'},
  document: {getElementById: () => null}, localStorage: {getItem: () => ''}});
vm.runInContext(script.replace('    init();', `
  selectedRevision = fixture.index.sha;
  generatedInventory = fixture.index;
  currentVipcs = fixture.index.vipc;
  currentDragons = fixture.index.dragon.files.map(file => ({...file, path:file.source_file}));
  activePlatform = fixture.platform;
  const state = enrichPlatformState({key:fixture.platform, tag:fixture.index.columns[0].tag}, fixture.manifest);
  currentColStates = {[fixture.platform]:state};
  const column = COLS.find(item => item.key === fixture.platform);
  globalThis.result = {
    cells: currentVipcs.flatMap(file => file.packages.map(pkg => ({pkg, ...pkgCellState(pkg,state,file,column)}))),
    vipcs: currentVipcs.map(file => vipcFileState(file,currentColStates)),
    dragons: currentDragons.map(dragonFileState),
    native: fixture.index.nipm.filter(dep => dep.platforms.includes(fixture.platform)).map(dep => ({name:dep.name, key:dep.key, html:nipmCell(dep,column,state)})),
    coverage: platformCoverage(fixture.platform)
  };
`), context);
for (const cell of context.result.cells) assert.equal(cell.state, 'present', cell.pkg + ': ' + cell.title);
for (const pill of [...context.result.vipcs, ...context.result.dragons]) assert.match(pill, /dep-file-state installed/);
for (const cell of context.result.native) {
  const expected = fixture.manifest.installer === 'lvpm' && cell.key === 'vipm' ? /LVPM selected; VIPM is not the Linux installer/ : /aria-label="Present"/;
  assert.match(cell.html, expected, cell.name + ': ' + cell.html);
}
assert.equal(context.result.coverage.pending, false, JSON.stringify(context.result.coverage));
console.log(`${fixture.platform}: ${context.result.cells.length} package cells, native NI packages, VIPC and Dragon status pills, and platform coverage clear`);
