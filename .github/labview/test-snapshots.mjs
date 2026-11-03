import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import test from 'node:test';
import vm from 'node:vm';
import {fileURLToPath} from 'node:url';

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../..');
const read = file => fs.readFileSync(path.join(root, file), 'utf8');
const browser = read('actions/snapshots/vi-browser.html');
const configure = read('actions/dashboard/configure.html');

test('shared dashboard assets match their canonical sources byte-for-byte', () => {
  for (const entry of fs.readdirSync(path.join(root, '.github/pages'), {withFileTypes: true})) {
    const canonical = path.join(root, '.github/pages', entry.name);
    const bundled = path.join(root, 'actions/dashboard', entry.name);
    if (entry.isFile() && fs.existsSync(bundled)) {
      assert.deepEqual(fs.readFileSync(canonical), fs.readFileSync(bundled), entry.name);
    }
  }
});

function functionSource(source, name) {
  const asyncStart = source.indexOf('async function ' + name + '(');
  const start = asyncStart === -1 ? source.indexOf('function ' + name + '(') : asyncStart;
  assert.notEqual(start, -1, name);
  return source.slice(start, source.indexOf('\n}', start) + 2);
}

function treeHarness() {
  const rows = [];
  const createElement = () => ({
    dataset: {}, style: {}, children: [], attributes: {}, events: {},
    label: {},
    setAttribute(name, value) { this.attributes[name] = value; },
    addEventListener(name, callback) { this.events[name] = callback; },
    appendChild(child) { child.parent = this; this.children.push(child); },
    insertBefore(child) { this.appendChild(child); },
    querySelector(selector) {
      return selector === '.label' ? this.label : this.children.find(child => '.' + child.className === selector);
    },
    remove() { this.parent.children.splice(this.parent.children.indexOf(this), 1); },
  });
  const context = vm.createContext({
    document: {createElement, querySelectorAll: () => rows},
    json2Set: null, json2WinSet: null, json2IndexKnown: false, json2WinIndexKnown: false,
    activeHtml: null, changeMap: {}, changedFolders: new Set(), expanded: new Set(), changedOnly: false,
    ICON: {vi: '<svg></svg>'}, fileVisible: () => true, escapeHtml: value => value,
    openVI: row => { context.opened = row; },
    BASE: 'snapshots/', syncVb2PlatToggle: () => {}, vb2Prefer2Default: () => false,
    currentSnapshot: null, vb2DidWinRestore: true,
  });
  for (const name of ['renderTree', 'rowBlob', 'decorate2', 'loadJson2Index']) {
    vm.runInContext(functionSource(browser, name), context);
  }
  const render = files => {
    rows.length = 0;
    context.renderTree({dirs: new Map(), files}, 0, '', {appendChild: row => rows.push(row)});
    context.decorate2();
  };
  return {context, rows, render};
}

test('VI file rows with either-platform snapshots have no status pills and still open', () => {
  assert.equal(browser, read('.github/pages/vi-browser.html'));
  assert.equal(browser, read('actions/dashboard/vi-browser.html'));
  for (const platform of ['json2Set', 'json2WinSet']) {
    const {context, rows, render} = treeHarness();
    context[platform] = new Set(['available']);
    const files = ['added', 'modified', 'deleted'].map(type => {
      const vi = {vi_rel: type + '.vi', vi_name: type + '.vi', html: type + '.html', blob: 'available'};
      context.changeMap[vi.vi_rel] = {type, isDiff: true};
      return vi;
    });
    render(files);
    assert.equal(rows.length, 3);
    for (const row of rows) {
      assert.equal(row.children.length, 0);
      assert.doesNotMatch(row.innerHTML, /Snapshot|New|Diff|snapshot-missing|class="chg/);
      row.events.click();
      assert.equal(context.opened, row);
    }
  }
});

test('only confirmed missing snapshots receive a small accessible X', () => {
  const {context, rows, render} = treeHarness();
  const files = [{vi_rel: 'missing.vi', vi_name: 'missing.vi', html: 'missing.html', blob: 'missing'}];
  render(files);
  assert.equal(rows[0].children.length, 0);
  context.json2Set = new Set();
  context.decorate2();
  assert.equal(rows[0].children.length, 0);
  context.json2WinSet = new Set();
  context.decorate2();
  context.decorate2();
  const [marker] = rows[0].children;
  assert.equal(rows[0].children.length, 1);
  assert.equal(marker.className, 'snapshot-missing');
  assert.equal(marker.textContent, '\u00d7');
  assert.equal(marker.title, 'No snapshot available');
  assert.equal(marker.attributes.role, 'img');
  assert.equal(marker.attributes['aria-label'], 'No snapshot available');
  assert.match(browser, /\.snapshot-missing\{[^}]*color:var\(--error\)/);
  render([{...files[0], blob: ''}]);
  assert.equal(rows[0].children.length, 0);
});

test('real index polling removes a missing X when either platform publishes frames', async () => {
  for (const platform of ['json-blobs.json', 'json-blobs.windows.json']) {
    const {context, rows, render} = treeHarness();
    context.fetchJson = async () => { throw new Error('404 missing index'); };
    render([{vi_rel: 'code.vi', vi_name: 'code.vi', blob: 'code', html: 'code.html'}]);
    await context.loadJson2Index();
    assert.equal(rows[0].children.length, 1);
    context.fetchJson = async url => url.endsWith(platform) ? ['CODE'] : [];
    await context.loadJson2Index();
    assert.equal(rows[0].children.length, 0);
    await context.loadJson2Index();
    assert.equal(rows[0].children.length, 0);
  }
});

test('failed or invalid index requests leave unknown availability unmarked', async () => {
  for (const result of [new Error('Network unavailable'), new Error('503 unavailable'), {}]) {
    const {context, rows, render} = treeHarness();
    context.fetchJson = async () => {
      if (result instanceof Error) throw result;
      return result;
    };
    render([{vi_rel: 'code.vi', vi_name: 'code.vi', blob: 'code', html: 'code.html'}]);
    await context.loadJson2Index();
    assert.equal(rows[0].children.length, 0);
  }
});

test('a missing platform index does not settle the other platform or lose cached frames', async () => {
  const {context, rows, render} = treeHarness();
  context.fetchJson = async url => {
    throw new Error(url.endsWith('windows.json') ? '503 unavailable' : '404 missing index');
  };
  render([{vi_rel: 'code.vi', vi_name: 'code.vi', blob: 'code', html: 'code.html'}]);
  await context.loadJson2Index();
  assert.equal(context.json2WinSet, null);
  assert.equal(rows[0].children.length, 0);
  context.json2WinSet = new Set(['code']);
  await context.loadJson2Index();
  assert.equal(rows[0].children.length, 0);
  assert.equal(context.json2WinSet.has('code'), true);
});

test('header Diff remains enabled for modified VIs and selected in diff view', () => {
  const attributes = {}, classes = {};
  const button = {
    dataset: {viewer: 'diff'}, classList: {toggle: (name, value) => { classes[name] = value; }},
    setAttribute: (name, value) => { attributes[name] = value; },
  };
  const context = vm.createContext({
    document: {querySelectorAll: () => [button], querySelector: () => button},
    openView: 'snapshot', viewerMode: 'snapshot', openViRel: 'code.vi', changeMap: {'code.vi': {type: 'modified'}},
  });
  vm.runInContext(functionSource(browser, 'syncViewerToggle'), context);
  context.syncViewerToggle();
  assert.equal(attributes['aria-disabled'], 'false');
  assert.equal(classes['is-disabled'], false);
  context.openView = 'diff';
  context.syncViewerToggle();
  assert.equal(classes.active, true);
  assert.match(browser, /<button[^>]*data-viewer="diff"[^>]*>Diff<\/button>/);
});

test('all distributed page scripts parse', () => {
  for (const file of ['actions/snapshots/vi-browser.html', 'actions/snapshots/vi-interactive.html', 'actions/dashboard/configure.html', 'actions/dashboard/dependencies.html']) {
    for (const match of read(file).matchAll(/<script\b([^>]*)>([\s\S]*?)<\/script>/g)) {
      if (match[2].trim() && !match[1].includes('application/json')) new vm.Script(match[2], {filename: file});
    }
  }
});

test('snapshot viewer ignores old viewer query and preferences', () => {
  assert.match(browser, /const viewerMode = 'snapshot'/);
  assert.doesNotMatch(browser, /get\('viewer'\)|VB2_VIEWER_KEY|enhanceSnapshot|data-viewer="[12]"/);
  assert.match(browser, /data-viewer="snapshot"/);
  assert.match(browser, /data-viewer="diff"/);
});

test('snapshot platform controls select Windows and Linux frame URLs', () => {
  const context = vm.createContext({vb2Plat: 'windows'});
  vm.runInContext(functionSource(browser, 'vb2EffPlat') + '\n' + functionSource(browser, 'vb2JsonUrl'), context);
  assert.equal(context.vb2JsonUrl('by-blob/ab/code.html'), 'by-blob/ab/code.windows.json');
  context.vb2Plat = 'linux';
  assert.equal(context.vb2JsonUrl('by-blob/ab/code.html'), 'by-blob/ab/code.json');
});

test('legacy settings load but new settings only write unified platforms', () => {
  assert.match(configure, /classic\|positionAware\|platforms/);
  assert.match(configure, /m\.viBrowser\.platforms \|\| m\.viBrowser\.positionAware/);
  const context = vm.createContext({actSel: new Set(['snapshots']), actionOs: new Map([['snapshots', new Set(['linux'])]])});
  for (const name of ['snapshotPlatforms', 'viBrowserYamlLines', 'viBrowserInputEnc']) {
    const start = configure.indexOf('    function ' + name + '(');
    vm.runInContext(configure.slice(start, configure.indexOf('\n    }', start) + 6), context);
  }
  assert.equal(context.viBrowserInputEnc(), 'platforms::linux');
  assert.deepEqual(Array.from(context.viBrowserYamlLines()), ['  viBrowser:', '    platforms: linux']);
});

test('missing frames never load a static export', async () => {
  let resolveRender;
  const settled = new Promise(resolve => { resolveRender = resolve; });
  const context = vm.createContext({
    frameRequestId: 0,
    vb2Prefer2Default: () => {},
    vb2JsonUrl: () => 'code.windows.json',
    setViewerLoading: () => {},
    setViewerModeNote: () => {},
    showNeedsToken: () => resolveRender('missing'),
    showInFrame: () => resolveRender('static'),
    fetch: async () => ({ok: false, status: 404}),
  });
  vm.runInContext(functionSource(browser, 'openSnapshot'), context);
  context.openSnapshot('code.html', 'Code');
  assert.equal(await settled, 'missing');
});

test('available frames render interactively', async () => {
  let resolveRender;
  const settled = new Promise(resolve => { resolveRender = resolve; });
  const frames = [{name: 'Front Panel'}, {name: 'Block Diagram'}];
  const context = vm.createContext({
    frameRequestId: 0,
    vb2Prefer2Default: () => {},
    vb2JsonUrl: () => 'code.windows.json',
    setViewerLoading: () => {},
    setViewerModeNote: () => {},
    window: {LVRender: {}},
    showInPlace: rendered => resolveRender(rendered),
    showNeedsToken: () => resolveRender(null),
    fetch: async () => ({ok: true, json: async () => frames}),
  });
  vm.runInContext(functionSource(browser, 'openSnapshot'), context);
  context.openSnapshot('code.html', 'Code');
  assert.equal(await settled, frames);
});

test('standalone snapshots have both panes and no iframe fallback', () => {
  const standalone = read('actions/snapshots/vi-interactive.html');
  assert.match(standalone, /id="front-panel"/);
  assert.match(standalone, /id="block-diagram"/);
  assert.doesNotMatch(standalone, /showIframe|<iframe/);
});
