'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');

// Exercise the real range and headline functions without starting network loading.
const elements = Object.fromEntries(['clone-summary', 'clone-total', 'clone-window', 'clone-coverage']
  .map((id) => [id, {}]));
const trafficButton = {};
elements.presets = {querySelector: () => trafficButton};
const context = vm.createContext({
  Intl, Date, document: {getElementById: (id) => elements[id]},
});
const source = fs.readFileSync(path.join(__dirname, 'site/dashboard.js'), 'utf8');
vm.runInContext(source.slice(0, source.indexOf('init().catch')), context);
vm.runInContext(`
  data = {
    traffic: {clones: {'2026-11-01': [100, 20], '2026-11-02': [50, 15], '2026-11-03': [45, 10]}},
    clone_window_history: {'2026-11-01/2026-11-02':25},
    traffic_windows: {clones: {from:'2026-11-02', to:'2026-11-03', count:95, uniques:21}},
    clients: {}
  };
  min = '2026-06-15'; max = '2026-11-25';
  preset(defaultRange()); renderCloneWindow();
`, context);
assert.equal(vm.runInContext('defaultMetric()', context), 'unique_cloners');
assert.equal(vm.runInContext('selected', context), 'archive');
assert.equal(elements['clone-total'].textContent, '25–35');
vm.runInContext("preset('all'); renderCloneWindow();", context);
assert.equal(elements['clone-total'].textContent, 'At least 25');
vm.runInContext("preset('traffic'); renderCloneWindow();", context);
assert.equal(elements['clone-total'].textContent, '21');
vm.runInContext("from='2026-11-01'; through='2026-11-02'; renderCloneWindow();", context);
assert.equal(elements['clone-total'].textContent, '25');
vm.runInContext("through=from; renderCloneWindow();", context);
assert.equal(elements['clone-total'].textContent, '20');
vm.runInContext("preset('14'); renderCloneWindow();", context);
assert.equal(elements['clone-total'].textContent, 'Unknown');
console.log('Selected-range cloner counts passed');
