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
  const start = source.indexOf('function ' + name + '(');
  assert.notEqual(start, -1, name);
  return source.slice(start, source.indexOf('\n}', start) + 2);
}

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
