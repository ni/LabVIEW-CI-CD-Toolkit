import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';

const root = new URL('../', import.meta.url);
const html = fs.readFileSync(new URL('pages/dependencies.html', root), 'utf8');
const source = html.match(/<script>\s*\(\(\) => \{[\s\S]*?<\/script>/)[0].replace(/^<script>|<\/script>$/g, '');
const tag = 'linux-lvpm-' + 'a'.repeat(64);
const digest = 'sha256:' + 'b'.repeat(64);
const revision = 'd'.repeat(40);
const candidate = {
  platform:'linux', architecture:'amd64',
  installer:'lvpm', tag, manifest_path:'workers/linux-lvpm/' + tag + '/manifest.json', image_digest:digest,
  image_ref:'ghcr.io/owner/project-labview:' + tag + '@' + digest,
  verification:{ status:'passed', scope:'package-files', activity_checks:'not_run', hook_error_output:'not_observable_by_upstream' },
  workflow_run:{ id:42, attempt:1, head_sha:revision, workflow_path:'.github/workflows/build-labview-lvpm-image.yml' },
  packages:[{ name:'actual', version:'1.2' }], vipc:[{ packages:['declared-only-1.0'] }]
};
const elements = new Map();
const requests = [];
let responseCandidate = candidate;
let activeConfig = 'config:\n  container:\n    linuxUse: "' + tag + '@' + digest + '"\n';
let failMetadata = false;
let workflowResponse = 'available';
const context = vm.createContext({
  window:{ LVCI:{ repo:'owner/project' } },
  location:{ hostname:'owner.github.io', pathname:'/project/dependencies.html' },
  document:{ getElementById:id => elements.get(id) || null },
  localStorage:{ getItem:() => 'test-token' },
  URL, URLSearchParams, console,
  fetch:async (url, options = {}) => {
    requests.push({ url, options });
    if (url === 'https://api.github.com/repos/owner/project') {
      if (failMetadata) throw new Error('unavailable');
      return { ok:true, json:async () => ({ default_branch:'trunk' }) };
    }
    if (url.includes('/commits/')) return { ok:true, json:async () => ({ sha:revision }) };
    if (url.includes('/contents/.github/workflows?ref=')) {
      assert.equal(url, 'https://api.github.com/repos/owner/project/contents/.github/workflows?ref=trunk');
      assert.equal(options.cache, 'no-cache');
      assert.equal(options.headers.Authorization, 'Bearer test-token');
      if (workflowResponse === 'network') throw new Error('network unavailable');
      if (typeof workflowResponse === 'number') return { ok:false, status:workflowResponse };
      return { ok:true, json:async () => workflowResponse === 'malformed' ? {} : workflowResponse === 'missing' ? [] : [
        { type:'file', path:'.github/workflows/build-labview-lvpm-image.yml' }
      ] };
    }
    if (url.includes('latest.json')) return { ok:true, json:async () => ({ tag, manifest_path:candidate.manifest_path, image_digest:digest }) };
    if (url.includes('manifest.json')) return { ok:true, json:async () => responseCandidate };
    if (url.includes('raw.githubusercontent.com')) return { ok:true, text:async () => activeConfig };
    if (options.method === 'POST') return { status:204 };
    throw new Error('Unexpected request: ' + url);
  }
});
vm.runInContext(source.replace('    init();', `
    globalThis.hooks = { parseManifestYaml, resolveColumn, packagesInManifest, pkgCellState,
      candidateSelection, loadLinuxBuildAvailability, linuxWorkflowPayload, dispatchLinuxWorker, dispatchContainerUpdate, loadManifestFor, updTargets, renderLinuxInstaller, nipmCell, enrichPlatformState,
      lvpmFreshness, localFreshnessText, loadPlatformStates, renderGenerated, renderContainers,
      getColumnStates: () => currentColStates,
      setState: state => {
        selectedRevision = state.revision || "${revision}";
        generatedInventory = state.inventory || null;
        lvpmCandidate = state.candidate;
        liveLinuxConfig = state.liveConfig || state.config || {};
        selectedConfig = state.config || {};
        defaultRevision = state.defaultRevision || "${revision}";
        monitorDirty = !!state.dirty;
      }
    };`), context);
const hooks = context.hooks;
const plain = value => JSON.parse(JSON.stringify(value));
hooks.setState({ candidate });
assert.equal((await hooks.loadLinuxBuildAvailability()).state, 'available');
assert.deepEqual(plain(await hooks.linuxWorkflowPayload('build')), {
  file:'build-labview-lvpm-image.yml', body:{ ref:'trunk', inputs:{} }
});
assert.deepEqual(plain(await hooks.linuxWorkflowPayload('activate')), {
  file:'reconfigure.yml', body:{ ref:'trunk', inputs:{ linux_worker:tag + '@' + digest } }
});
assert.deepEqual(plain(await hooks.linuxWorkflowPayload('reset')), {
  file:'reconfigure.yml', body:{ ref:'trunk', inputs:{ linux_worker:'reset' } }
});
assert.equal(hooks.candidateSelection({ ...candidate, verification:{ status:'passed', scope:'activities' } }), '');
assert.equal(hooks.candidateSelection(candidate), tag + '@' + digest);
for (const invalidPlatform of [undefined, 'windows']) {
  assert.equal(hooks.candidateSelection({ ...candidate, platform:invalidPlatform }), '');
}
for (const invalidArchitecture of [undefined, 'arm64']) {
  const invalidCandidate = { ...candidate, architecture:invalidArchitecture };
  assert.equal(hooks.candidateSelection(invalidCandidate), '');
  responseCandidate = invalidCandidate;
  await assert.rejects(hooks.linuxWorkflowPayload('activate'), /Candidate changed or is unavailable/);
}
responseCandidate = candidate;
hooks.setState({ revision:'historical', candidate });
for (const action of ['build', 'activate', 'reset']) {
  await assert.rejects(hooks.linuxWorkflowPayload(action), /current default-branch revision/);
}
hooks.setState({ candidate, dirty:true });
await assert.rejects(hooks.linuxWorkflowPayload('build'), /Save and merge/);
hooks.setState({ candidate });
responseCandidate = { ...candidate, image_digest:'sha256:' + 'c'.repeat(64) };
await assert.rejects(hooks.linuxWorkflowPayload('activate'), /Candidate changed or is unavailable/);
responseCandidate = candidate;
const config = hooks.parseManifestYaml('config:\n  container:\n    use: vipm-default\n    actions:\n      masscompile: base\n    linuxUse: "' + tag + '@' + digest + '"\n');
assert.equal(config.linuxUse, tag + '@' + digest);
assert.equal(hooks.resolveColumn({ platform:'linux' }, config).tag, tag + '@' + digest);
assert.equal(hooks.resolveColumn({ platform:'linux', action:'masscompile' }, config).tag, 'base');
assert.equal(hooks.resolveColumn({ platform:'windows' }, config).tag, 'vipm-default');
assert.deepEqual([...hooks.packagesInManifest(candidate)].sort(), ['actual', 'actual-1.2']);
const selection = tag + '@' + digest;
const currentFreshness = { status:'current', reason:'Committed declarations match the selected worker.', revision, selection };
function inventoryFor(freshness = currentFreshness){
  return {
    sha:revision, config:{ linuxUse:selection }, vipc:[],
    columns:[
      { key:'linux', installer:'lvpm', tag:selection, ready:true, freshness, packages:['actual', 'actual-1.2'], labview_version:'2026', nipkg_packages:[] },
      { key:'windows', installer:'vipm', tag:'win-existing', ready:true, packages:['actual-1.2'] }
    ]
  };
}
hooks.setState({ candidate, inventory:inventoryFor() });
const state = { key:'linux', tag:selection, installer:'lvpm', ready:true, label:tag, packages:hooks.packagesInManifest(candidate) };
assert.equal(hooks.pkgCellState('actual-1.2', state).state, 'present');
assert.equal(hooks.pkgCellState('actual-9.0', state).state, 'missing');
const linuxColumn = { platform:'linux' };
const nativeState = hooks.enrichPlatformState({ tag:tag + '@' + digest }, { ...candidate, labview_version:'2026' });
const analyzer = { key:'via', platforms:['windows', 'linux'], packages:{ linux:['ni-vialin-labview-support'] } };
assert.equal(nativeState.nipkg_ready, false);
assert.match(hooks.nipmCell({ key:'labview', labview:true }, linuxColumn, nativeState), /aria-label="Present"/);
assert.match(hooks.nipmCell({ key:'vipm' }, linuxColumn, nativeState), /N\/A/);
const unknownAnalyzer = hooks.nipmCell(analyzer, linuxColumn, nativeState);
assert.match(unknownAnalyzer, /Unknown: native package not inventoried/);
assert.doesNotMatch(unknownAnalyzer, /dep-missing|Missing dependency/);
assert.match(hooks.nipmCell({ key:'utf', platforms:['windows'] }, linuxColumn, nativeState), /N\/A/);
const emptyNativeState = hooks.enrichPlatformState({ tag:tag + '@' + digest }, { ...candidate, labview_version:'2026', nipkg_packages:[] });
assert.equal(emptyNativeState.nipkg_ready, true);
assert.match(hooks.nipmCell(analyzer, linuxColumn, emptyNativeState), /Missing dependency/);
const manifestStart = requests.length;
assert.equal((await hooks.loadManifestFor(tag + '@' + digest, 'linux')).image_digest, digest);
assert.ok(requests.slice(manifestStart).every(request => !request.url.includes('latest.json')));
await assert.rejects(hooks.loadManifestFor(tag, 'linux'), /digest-pinned/);
elements.set('updatePanel', { querySelectorAll:() => [{ checked:true, value:'linux' }] });
elements.set('updStatus', {});
hooks.setState({ candidate, config:{ linuxUse:tag + '@' + digest } });
assert.deepEqual(plain(hooks.updTargets()), []);
const beforeUpdate = requests.length;
await hooks.dispatchContainerUpdate();
assert.ok(requests.slice(beforeUpdate).every(request => request.options.method !== 'POST'));
assert.match(elements.get('updStatus').innerHTML, /VIPM was not dispatched/);
hooks.setState({ candidate, config:{} });
failMetadata = true;
const beforeFailure = requests.length;
await hooks.dispatchContainerUpdate();
assert.ok(requests.slice(beforeFailure).every(request => request.options.method !== 'POST'));
failMetadata = false;
elements.set('lvpmStatus', {});
hooks.setState({ candidate });
await hooks.dispatchLinuxWorker('activate');
const dispatched = requests.filter(request => request.options.method === 'POST');
assert.equal(dispatched.length, 1);
assert.deepEqual(JSON.parse(dispatched[0].options.body), { ref:'trunk', inputs:{ linux_worker:tag + '@' + digest } });
assert.match(html, /Package files verified; CI activities not yet tested/);
assert.doesNotMatch(html, /CI checks passed/);
function installerView(){
  const review = { hidden:true, innerHTML:'' };
  const inputs = ['vipm', 'lvpm'].map(value => ({
    value, checked:false, listener:null,
    addEventListener(event, listener){ this.listener = listener; }
  }));
  let markup = '';
  const host = {
    get innerHTML(){ return markup; },
    set innerHTML(value){
      markup = value;
      for (const input of inputs){
        input.checked = value.includes('value="' + input.value + '" checked');
      }
      review.hidden = value.includes('id="lvpmReview" hidden');
    },
    querySelectorAll:() => inputs
  };
  elements.set('linuxInstaller', host);
  elements.set('lvpmReview', review);
  for (const id of ['lvpmBuild', 'lvpmActivate', 'lvpmReset']) elements.set(id, { addEventListener(){} });
  return { host, review, inputs };
}
hooks.setState({ candidate, config:{ linuxUse:tag + '@' + digest }, liveConfig:{ use:'vipm-live-default' } });
const selectedView = installerView();
const selectedRender = hooks.renderLinuxInstaller();
assert.equal(selectedView.inputs.find(input => input.value === 'lvpm').checked, true);
assert.equal(selectedView.inputs.find(input => input.value === 'vipm').checked, false);
assert.equal(selectedView.review.hidden, false);
assert.match(selectedView.host.innerHTML, /Configured Linux default \(selected revision/);
assert.match(selectedView.host.innerHTML, /<strong>LVPM Experimental<\/strong>/);
assert.match(selectedView.host.innerHTML, /Explicit per-action overrides win/);
assert.match(selectedView.host.innerHTML, /Switching these views does not change configuration or dispatch a workflow/);
await selectedRender;
assert.match(selectedView.review.innerHTML, /<h4>Currently selected Linux default<\/h4>/);
assert.doesNotMatch(selectedView.review.innerHTML, /Alternative candidate/);
const beforeViewSwitch = requests.length;
selectedView.inputs.find(input => input.value === 'vipm').listener();
assert.equal(selectedView.review.hidden, true);
assert.match(selectedView.host.innerHTML, /<strong>LVPM Experimental<\/strong>/);
selectedView.inputs.find(input => input.value === 'lvpm').listener();
assert.equal(selectedView.review.hidden, false);
assert.equal(requests.length, beforeViewSwitch);
hooks.setState({ candidate, config:{ use:'vipm-selected-default' }, liveConfig:{ linuxUse:tag + '@' + digest } });
const vipmView = installerView();
await hooks.renderLinuxInstaller();
assert.equal(vipmView.inputs.find(input => input.value === 'vipm').checked, true);
assert.equal(vipmView.review.hidden, true);
assert.match(vipmView.host.innerHTML, /<strong>VIPM<\/strong>/);
assert.match(vipmView.host.innerHTML, /vipm-selected-default/);
assert.match(vipmView.review.innerHTML, /<h4>Alternative candidate — not automatically used<\/h4>/);
hooks.setState({ candidate, config:{ linuxUse:tag + '@sha256:' + 'c'.repeat(64) } });
const differentDigestView = installerView();
await hooks.renderLinuxInstaller();
assert.equal(differentDigestView.review.hidden, false);
assert.match(differentDigestView.review.innerHTML, /<h4>Alternative candidate — not automatically used<\/h4>/);
hooks.setState({ candidate });
const availableView = installerView();
await hooks.renderLinuxInstaller();
assert.match(availableView.review.innerHTML, /id="lvpmBuild">/);
assert.match(availableView.review.innerHTML, /Candidate build workflow is installed on the default branch/);
workflowResponse = 'missing';
const beforeRemovedBuild = requests.length;
await hooks.dispatchLinuxWorker('build');
assert.ok(requests.slice(beforeRemovedBuild).some(request => request.url.includes('/contents/.github/workflows?ref=trunk')));
assert.ok(requests.slice(beforeRemovedBuild).every(request => request.options.method !== 'POST'));
assert.match(elements.get('lvpmStatus').textContent, /Install the Linux custom-image capability/);
assert.equal((await hooks.loadLinuxBuildAvailability()).state, 'missing');
const missingView = installerView();
await hooks.renderLinuxInstaller();
assert.match(missingView.review.innerHTML, /id="lvpmBuild" disabled/);
assert.match(missingView.review.innerHTML, /Install the Linux custom-image capability on the default branch/);
assert.match(missingView.review.innerHTML, /id="lvpmActivate">/);
assert.match(missingView.review.innerHTML, /id="lvpmReset">/);
assert.equal((await hooks.linuxWorkflowPayload('activate')).file, 'reconfigure.yml');
assert.equal((await hooks.linuxWorkflowPayload('reset')).body.inputs.linux_worker, 'reset');
for (const failure of [401, 403, 404, 500, 'network', 'malformed']) {
  workflowResponse = failure;
  assert.equal((await hooks.loadLinuxBuildAvailability()).state, 'unknown');
  const unavailableView = installerView();
  await hooks.renderLinuxInstaller();
  assert.match(unavailableView.review.innerHTML, /id="lvpmBuild" disabled/);
  assert.match(unavailableView.review.innerHTML, /Candidate build availability is unknown/);
  assert.doesNotMatch(unavailableView.review.innerHTML, /Install the Linux custom-image capability/);
  assert.match(unavailableView.review.innerHTML, /id="lvpmActivate">/);
  assert.match(unavailableView.review.innerHTML, /id="lvpmReset">/);
  const beforeBlockedBuild = requests.length;
  await hooks.dispatchLinuxWorker('build');
  assert.ok(requests.slice(beforeBlockedBuild).every(request => request.options.method !== 'POST'));
  assert.match(elements.get('lvpmStatus').textContent, /Candidate build availability is unknown/);
}
failMetadata = true;
assert.equal((await hooks.loadLinuxBuildAvailability()).state, 'unknown');
const metadataFailureView = installerView();
await hooks.renderLinuxInstaller();
assert.match(metadataFailureView.review.innerHTML, /id="lvpmBuild" disabled/);
assert.match(metadataFailureView.review.innerHTML, /Candidate build availability is unknown/);
failMetadata = false;
workflowResponse = 'available';
hooks.setState({ revision:'historical', candidate });
const historicalView = installerView();
await hooks.renderLinuxInstaller();
for (const control of ['lvpmBuild', 'lvpmActivate', 'lvpmReset']) {
  assert.ok(historicalView.review.innerHTML.includes('id="' + control + '" disabled'));
}
hooks.setState({ candidate });
elements.set('lvpmStatus', {});
const beforeAvailableBuild = requests.length;
await hooks.dispatchLinuxWorker('build');
const buildRequests = requests.slice(beforeAvailableBuild);
assert.ok(buildRequests.at(-2).url.includes('/contents/.github/workflows?ref=trunk'));
assert.equal(buildRequests.at(-1).options.method, 'POST');
assert.ok(buildRequests.at(-1).url.endsWith('/actions/workflows/build-labview-lvpm-image.yml/dispatches'));
assert.deepEqual(JSON.parse(buildRequests.at(-1).options.body), { ref:'trunk', inputs:{} });
const freshnessCases = [
  { name:'genuine current', inventory:inventoryFor(), status:'current' },
  { name:'stale unchanged pins', inventory:inventoryFor({ ...currentFreshness, status:'stale', reason:'Declaration paths or ordering changed.' }), status:'stale' },
  { name:'explicit unknown', inventory:inventoryFor({ ...currentFreshness, status:'unknown', reason:'Local declarations could not be inspected.' }), status:'unknown' },
  { name:'missing record', inventory:inventoryFor(null), status:'unknown' },
  { name:'missing inventory', inventory:null, status:'unknown' },
  { name:'inventory revision mismatch', inventory:{ ...inventoryFor(), sha:'e'.repeat(40) }, status:'unknown' },
  { name:'inspected revision mismatch', inventory:inventoryFor({ ...currentFreshness, revision:'e'.repeat(40) }), status:'unknown' },
  { name:'selection mismatch', inventory:inventoryFor({ ...currentFreshness, selection:tag + '@sha256:' + 'c'.repeat(64) }), status:'unknown' },
  { name:'missing inspected revision', inventory:inventoryFor({ ...currentFreshness, revision:undefined }), status:'unknown' },
  { name:'missing inspected selection', inventory:inventoryFor({ ...currentFreshness, selection:undefined }), status:'unknown' },
  { name:'missing reason', inventory:inventoryFor({ ...currentFreshness, reason:undefined }), status:'unknown' },
  { name:'invalid status', inventory:inventoryFor({ ...currentFreshness, status:'verified' }), status:'unknown' },
  { name:'short revision', inventory:{ ...inventoryFor(), sha:'short' }, revision:'short', status:'unknown' }
];
const notReadyInventory = inventoryFor();
notReadyInventory.columns[0].ready = false;
freshnessCases.push({ name:'backend not ready', inventory:notReadyInventory, status:'unknown' });
for (const id of ['vipcWrap', 'nipmWrap', 'systemWrap', 'dragonWrap']) elements.set(id, { innerHTML:'' });
for (const testCase of freshnessCases) {
  const ready = testCase.status === 'current';
  hooks.setState({ candidate, revision:testCase.revision, config:{ use:'base', linuxUse:selection }, inventory:testCase.inventory });
  const enriched = hooks.enrichPlatformState({ key:'linux', tag:selection }, { ...candidate, labview_version:'2026', nipkg_packages:[] });
  assert.equal(enriched.freshness.status, testCase.status, testCase.name);
  assert.equal(enriched.ready, ready, testCase.name);
  assert.equal(hooks.pkgCellState('actual-1.2', enriched).state, ready ? 'present' : 'missing', testCase.name);
  const nativeCell = hooks.nipmCell({ key:'labview', labview:true }, linuxColumn, enriched);
  assert.match(nativeCell, ready ? /aria-label="Present"/ : /Missing dependency/, testCase.name);
  const rawStates = await hooks.loadPlatformStates({ use:'base', linuxUse:selection });
  assert.equal(rawStates.linux.ready, ready, testCase.name);
  assert.equal(rawStates.linux.freshness.status, testCase.status, testCase.name);
  assert.equal(rawStates.windows.ready, true, testCase.name);
  const generated = inventoryFor(testCase.inventory && testCase.inventory.columns[0].freshness);
  generated.sha = testCase.revision || revision;
  generated.columns[0].ready = ready;
  generated.platforms = [{ ...generated.columns[0], ready:true }];
  generated.system = [{ name:'Go', source:'worker', linux:'Go 1.26' }];
  hooks.renderGenerated(generated);
  const generatedStates = hooks.getColumnStates();
  assert.equal(generatedStates.linux.ready, ready, testCase.name);
  assert.equal(generatedStates.linux.freshness.status, testCase.status, testCase.name);
  assert.equal(hooks.pkgCellState('actual-1.2', generatedStates.linux).state, ready ? 'present' : 'missing', testCase.name);
  assert.equal(hooks.pkgCellState('actual-1.2', generatedStates.windows).state, 'present', testCase.name);
  assert.match(elements.get('nipmWrap').innerHTML, ready ? /LabVIEW 2026/ : /Build a new candidate, reverify it, then reactivate/, testCase.name);
  assert.match(elements.get('systemWrap').innerHTML, ready ? /aria-label="Present"/ : /Build a new candidate, reverify it, then reactivate/, testCase.name);
  const toolingView = installerView();
  await hooks.renderLinuxInstaller();
  assert.ok(toolingView.review.innerHTML.includes('Local freshness: ' + testCase.status), testCase.name);
  assert.match(toolingView.review.innerHTML, /not remote provenance, registry, or activity verification/, testCase.name);
  elements.set('containers', { innerHTML:'' });
  hooks.renderContainers(rawStates);
  assert.ok(elements.get('containers').innerHTML.includes('Local freshness: ' + testCase.status), testCase.name);
  await hooks.renderLinuxInstaller();
  elements.delete('containers');
  if (!ready) {
    assert.match(hooks.pkgCellState('actual-1.2', { ...enriched, ready:true }).title, /Build a new candidate, reverify it, then reactivate/, testCase.name);
    assert.match(toolingView.review.innerHTML, /Build a new candidate, reverify it, then reactivate/, testCase.name);
  }
}
hooks.setState({ candidate, inventory:inventoryFor() });
const preservedNotReady = hooks.enrichPlatformState({ key:'linux', tag:selection, ready:false }, candidate);
assert.equal(preservedNotReady.ready, false);
assert.equal(hooks.pkgCellState('actual-1.2', preservedNotReady).state, 'missing');
const reusedState = hooks.enrichPlatformState({ key:'linux', tag:selection }, candidate);
hooks.setState({ candidate, revision:'e'.repeat(40), inventory:inventoryFor() });
assert.equal(hooks.pkgCellState('actual-1.2', reusedState).state, 'missing');
assert.equal(hooks.lvpmFreshness(reusedState).status, 'unknown');
hooks.setState({ candidate });
const vipmState = hooks.enrichPlatformState({ key:'linux', tag:'vipm-existing', ready:false }, { vipc:[{ packages:['actual-1.2'] }] });
assert.equal(vipmState.ready, true);
assert.equal(hooks.pkgCellState('actual-9.0', vipmState).state, 'present');
assert.equal(hooks.lvpmFreshness(vipmState), null);
console.log('LVPM UX dispatch, inventory, historical-revision, no-fallback, tooling-view, build-availability, and revision-bound local freshness tests passed.');
