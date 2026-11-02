import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';

const root = new URL('../', import.meta.url);
const html = fs.readFileSync(new URL('pages/dependencies.html', root), 'utf8');
const source = html.match(/<script>\s*\(\(\) => \{[\s\S]*?<\/script>/i)[0].replace(/^<script>|<\/script>$/gi, '');
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
let postBarrier = null;
const context = vm.createContext({
  window:{ LVCI:{ repo:'owner/project' } },
  location:{ hostname:'owner.github.io', pathname:'/project/dependencies.html', href:'https://owner.github.io/project/dependencies.html?sha=' + revision },
  history:{ replaceState(_state, _title, url){ context.location.href = String(url); } },
  document:{ getElementById:id => elements.get(id) || null },
  localStorage:{ getItem:() => 'test-token' },
  URL, URLSearchParams, console, setInterval:() => 0, clearInterval:() => {},
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
    if (new URL(url).hostname === 'raw.githubusercontent.com') return { ok:true, text:async () => activeConfig };
    if (options.method === 'POST') {
      if (postBarrier) await postBarrier;
      return { status:204 };
    }
    throw new Error('Unexpected request: ' + url);
  }
});
vm.runInContext(source.replace('    init();', `
    globalThis.hooks = { parseManifestYaml, resolveColumn, packagesInManifest, pkgCellState,
      candidateSelection, loadLinuxBuildAvailability, linuxWorkflowPayload, dispatchLinuxWorker, dispatchContainerUpdate, loadManifestFor, updTargets, renderLinuxInstaller, nipmCell, enrichPlatformState,
      lvpmFreshness, localFreshnessText, loadPlatformStates, renderGenerated, renderContainers,
      dragonFileState, updMissingList, renderDragonFiles, platformCoverage, visibleColumns, selectPlatform,
      renderUpdatePanel, renderVipc, encodeMonitorList, setFileMonitor, syncPlatformTabs, initPlatformTabs,
      monitorCounts, monitorInputs, dispatchMonitor, openUpdateApprovalDialog,
      pendingAction:() => pendingTokenAction,
      setModal:modal => { updModal = modal; },
      getColumnStates: () => currentColStates,
      setState: state => {
        activePlatform = state.platform || "linux";
        linuxToolingView = state.tooling || "vipm";
        if (state.vipcs) currentVipcs = state.vipcs;
        if (state.dragons) currentDragons = state.dragons;
        if (state.columns) currentColStates = state.columns;
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
elements.set('updatePanel', { classList:{remove(){}}, querySelectorAll:() => [{ checked:true, value:'linux' }] });
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
  elements.set('linuxVipmView', { hidden:false });
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
for (const retainedDefault of ['base', 'ghcr.io/owner/custom-linux:reviewed@' + digest]) {
  const resetConfig = { linuxUse:selection, use:retainedDefault, actions:{masscompile:'custom-action-worker'} };
  const originalConfig = JSON.stringify(resetConfig);
  hooks.setState({ candidate, config:resetConfig });
  const resetView = installerView();
  await hooks.renderLinuxInstaller();
  assert.match(resetView.host.innerHTML, />Reset Linux to existing default selection via PR<\/button>/);
  assert.match(resetView.host.innerHTML, /To return to the existing default selection/);
  assert.match(resetView.host.innerHTML, /Reset removes only <code>linuxUse<\/code>/);
  assert.ok(resetView.host.innerHTML.includes('existing default selection remains <code>' + retainedDefault + '</code>'));
  assert.match(resetView.host.innerHTML, /per-action overrides are retained/);
  assert.match(resetView.host.innerHTML, /Reset does not guarantee VIPM/);
  assert.doesNotMatch(resetView.host.innerHTML, /Reset Linux default to VIPM|return to the existing VIPM selection/);
  const beforeReset = requests.length;
  await hooks.dispatchLinuxWorker('reset');
  const resetRequests = requests.slice(beforeReset).filter(request => request.options.method === 'POST');
  assert.equal(resetRequests.length, 1);
  assert.ok(resetRequests[0].url.endsWith('/reconfigure.yml/dispatches'));
  assert.deepEqual(JSON.parse(resetRequests[0].options.body), {
    ref:'trunk', inputs:{linux_worker:'reset'}
  });
  assert.equal(JSON.stringify(resetConfig), originalConfig);
}
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
assert.doesNotMatch(missingView.host.innerHTML, /id="lvpmReset"/);
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
  assert.doesNotMatch(unavailableView.host.innerHTML, /id="lvpmReset"/);
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
for (const control of ['lvpmBuild', 'lvpmActivate']) {
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
const dragonPath = 'project.dragon';
const monitoredDragon = { path:dragonPath, monitored:true, monitorOn:['linux'] };
function dragonInventory(file, available = true){
  return { sha:revision, dragon:{ available, files:[{ source_file:dragonPath, ...file }] } };
}
const parsedDragon = { inspection:{ status:'parsed', reason:'Trusted declarations parsed.' },
  dependencies:[{ package_id:'actual', status:'installed', platform_status:{windows:{status:'installed'},linux:{status:'installed'}} }] };
hooks.setState({ candidate, inventory:dragonInventory(parsedDragon) });
assert.match(hooks.dragonFileState(monitoredDragon), />installed</);
for (const testCase of [
  { platforms:['linux'], evidence:{ linux:{ status:'installed', scope:'package-files' } }, expected:'installed' },
  { platforms:['linux'], evidence:{ linux:{ status:'wrong_version' } }, expected:'missing' },
  { platforms:['linux'], evidence:{ linux:{ status:'unknown', message:'stale or NIPM unsupported' } }, expected:'unknown' },
  { platforms:['windows'], evidence:{ windows:{ status:'installed' } }, expected:'installed' },
  { platforms:['windows', 'linux'], evidence:{ windows:{ status:'installed' }, linux:{ status:'unknown' } }, expected:'unknown' },
  { platforms:['windows', 'linux'], evidence:{ windows:{ status:'missing' }, linux:{ status:'installed' } }, expected:'missing' },
  { platforms:['windows', 'linux'], evidence:{ windows:{ status:'installed' }, linux:{ status:'installed' } }, expected:'installed' },
  { platforms:['windows', 'linux'], evidence:{ windows:{ status:'installed' } }, expected:'unknown' }
]) {
  hooks.setState({ candidate, inventory:dragonInventory({ ...parsedDragon,
    dependencies:[{ status:'installed', platform_status:testCase.evidence }] }) });
  assert.match(hooks.dragonFileState({ ...monitoredDragon, monitorOn:testCase.platforms }),
    new RegExp('>' + testCase.expected + '<'), JSON.stringify(testCase));
}
for (const status of ['missing', 'wrong_version', 'conflict']) {
  hooks.setState({ candidate, inventory:dragonInventory({ ...parsedDragon, dependencies:[{ status }] }) });
  assert.match(hooks.dragonFileState({...monitoredDragon, monitorOn:['windows']}), />missing</);
}
for (const testCase of [
  { name:'missing parser', inventory:dragonInventory({}, false) },
  { name:'missing backend', inventory:dragonInventory({ inspection:{ status:'unknown', reason:'TOML backend unavailable.' } }, false) },
  { name:'malformed declaration', inventory:dragonInventory({ error:'invalid TOML', dependencies:[] }) },
  { name:'unknown inspection', inventory:dragonInventory({ inspection:{ status:'unknown', reason:'Cannot inspect.' } }) },
  { name:'legacy empty fallback', inventory:dragonInventory({ dependencies:[] }) },
  { name:'no inventory', inventory:null },
  { name:'historical revision', inventory:dragonInventory(parsedDragon), revision:'e'.repeat(40) },
  { name:'unmeasured packages', inventory:dragonInventory({ ...parsedDragon, dependencies:[{ status:'pending' }] }) },
  { name:'unattempted packages', inventory:dragonInventory({ ...parsedDragon, dependencies:[{ status:'not_attempted' }] }) }
]) {
  hooks.setState({ candidate, inventory:testCase.inventory, revision:testCase.revision });
  assert.match(hooks.dragonFileState(monitoredDragon), />unknown</, testCase.name);
  assert.doesNotMatch(hooks.dragonFileState(monitoredDragon), />installed</, testCase.name);
  assert.match(hooks.dragonFileState({ ...monitoredDragon, monitored:false }), />unmonitored</, testCase.name);
  assert.match(hooks.dragonFileState({ ...monitoredDragon, monitorOn:[] }), />unmonitored</, testCase.name);
}
hooks.setState({ candidate, inventory:dragonInventory({ inspection:{ status:'parsed' }, dependencies:[] }) });
assert.match(hooks.dragonFileState(monitoredDragon), />no declarations</);
hooks.setState({ candidate, inventory:{ sha:revision, dragon:{ available:true, files:[
  { source_file:dragonPath, ...parsedDragon },
  { source_file:'broken.dragon', inspection:{ status:'unknown', reason:'<untrusted input>' } }
] } } });
assert.match(hooks.dragonFileState(monitoredDragon), />installed</);
assert.match(hooks.dragonFileState({ ...monitoredDragon, path:'broken.dragon' }), />unknown</);
assert.doesNotMatch(hooks.dragonFileState({ ...monitoredDragon, path:'broken.dragon' }), /<untrusted input>/);
context.document.querySelectorAll = () => [];
hooks.renderDragonFiles([monitoredDragon, { ...monitoredDragon, path:'broken.dragon' }]);
assert.match(elements.get('dragonWrap').innerHTML, />unknown</);
assert.match(elements.get('dragonWrap').innerHTML, />installed</);
const unknownPendingHtml = hooks.updMissingList({ dragon:[{ name:dragonPath, status:'unknown', message:'<parser unavailable>' }] });
assert.match(unknownPendingHtml, /Unresolved .dragon dependencies/);
assert.match(unknownPendingHtml, /unknown/);
assert.doesNotMatch(unknownPendingHtml, /<parser unavailable>/);
function uiElement(){
  const classes = new Set();
  return { innerHTML:'', hidden:false, style:{},
    classList:{ add:value => classes.add(value), remove:value => classes.delete(value), contains:value => classes.has(value) },
    addEventListener(){}, setAttribute(){}, querySelector:() => null, querySelectorAll:() => []
  };
}
for (const id of ['updatePanel', 'platformSummary', 'containers', 'vipcWrap', 'nipmWrap', 'systemWrap', 'dragonWrap', 'monitorStatus', 'saveMonitor', 'tokPanel', 'updStatus']) {
  elements.set(id, uiElement());
}
const projectVipc = { path:'project.vipc', packages:['actual-1.2'], role:'project', monitored:true, configured:true, monitorOn:['windows', 'linux'] };
function platformFixture(platform, windowsPackages, linuxPackages){
  hooks.setState({ candidate, platform, vipcs:[{ ...projectVipc }], dragons:[],
    columns:{
      windows:{ key:'windows', tag:'win-existing', ready:true, packages:new Set(windowsPackages) },
      linux:{ key:'linux', tag:'linux-existing', ready:true, packages:new Set(linuxPackages) }
    } });
}
for (const testCase of [
  { platform:'windows', windows:['actual-1.2'], linux:[], pending:false },
  { platform:'linux', windows:['actual-1.2'], linux:[], pending:true },
  { platform:'windows', windows:[], linux:['actual-1.2'], pending:true },
  { platform:'linux', windows:[], linux:['actual-1.2'], pending:false }
]) {
  platformFixture(testCase.platform, testCase.windows, testCase.linux);
  assert.equal(hooks.platformCoverage(testCase.platform).pending, testCase.pending);
  await hooks.renderUpdatePanel();
  const label = testCase.platform === 'linux' ? 'Linux' : 'Windows';
  const other = testCase.platform === 'linux' ? 'Windows' : 'Linux';
  assert.match(elements.get('platformSummary').innerHTML,
    new RegExp(label + (testCase.pending ? ' dependencies need attention' : ' monitored project dependencies are satisfied')));
  assert.doesNotMatch(elements.get('platformSummary').innerHTML, new RegExp(other + ' dependencies need attention'));
  hooks.renderVipc([{ ...projectVipc }], hooks.getColumnStates());
  assert.match(elements.get('vipcWrap').innerHTML, new RegExp('data-monitor-plat="' + testCase.platform + '"'));
  assert.doesNotMatch(elements.get('vipcWrap').innerHTML, new RegExp('data-monitor-plat="' + other.toLowerCase() + '"'));
  assert.deepEqual(plain(hooks.updTargets()).map(item => item.key), [testCase.platform]);
}
for (const platform of ['windows', 'linux']) {
  const oldConfig = activeConfig;
  activeConfig = 'config:\n  container:\n    use: vipm-default\n';
  hooks.setState({ candidate, platform, vipcs:[], dragons:[], config:{linuxUse:selection} });
  if (platform === 'linux') hooks.setState({ candidate, platform, vipcs:[], dragons:[] });
  const start = requests.length;
  await hooks.dispatchContainerUpdate();
  const sent = requests.slice(start).filter(request => request.options.method === 'POST');
  assert.equal(sent.length, 1);
  assert.ok(sent[0].url.endsWith(platform === 'windows' ? '/build-labview-image.yml/dispatches' : '/build-labview-linux-image.yml/dispatches'));
  assert.equal(JSON.parse(sent[0].options.body).inputs.update_mode, 'replace');
  if (platform === 'windows') assert.ok(requests.slice(start).every(request => !request.url.includes('raw.githubusercontent.com')));
  activeConfig = oldConfig;
}
let releasePost;
postBarrier = new Promise(resolve => { releasePost = resolve; });
hooks.setState({ candidate, platform:'windows', vipcs:[], dragons:[] });
const inFlightUpdate = hooks.dispatchContainerUpdate();
hooks.setState({ candidate, platform:'linux', vipcs:[], dragons:[] });
elements.get('updStatus').innerHTML = 'Linux status unchanged';
releasePost();
await inFlightUpdate;
assert.equal(elements.get('updStatus').innerHTML, 'Linux status unchanged');
postBarrier = null;
platformFixture('windows', ['actual-1.2'], []);
const beforeWindowsRender = requests.length;
hooks.renderContainers(hooks.getColumnStates());
assert.match(elements.get('containers').innerHTML, /Windows uses VIPM/);
assert.doesNotMatch(elements.get('containers').innerHTML, /id="linuxInstaller"|name="linuxInstaller"|LVPM Experimental/);
await hooks.dispatchLinuxWorker('build');
assert.equal(requests.length, beforeWindowsRender);
const splitDragon = dragonInventory({ ...parsedDragon, dependencies:[{
  package_id:'actual', status:'unknown',
  platform_status:{windows:{status:'installed'},linux:{status:'unknown',message:'No current Linux package evidence'}}
}] });
hooks.setState({candidate, vipcs:[], dragons:[], inventory:dragonInventory({...parsedDragon, dependencies:[{status:'installed'}]})});
assert.match(hooks.dragonFileState(monitoredDragon), />unknown</);
assert.match(hooks.dragonFileState({...monitoredDragon, monitorOn:['windows']}), />installed</);
for (const platform of ['windows', 'linux']) {
  hooks.setState({candidate, platform, vipcs:[], dragons:[{...monitoredDragon, monitorOn:['windows','linux']}], inventory:splitDragon, columns:{}});
  assert.equal(hooks.platformCoverage(platform).pending, platform === 'linux');
  hooks.renderDragonFiles([{...monitoredDragon, monitorOn:['windows','linux']}]);
  assert.match(elements.get('dragonWrap').innerHTML, platform === 'windows' ? />installed</ : />unknown</);
}
for (const evidence of [
  { vipcs:[{ ...projectVipc, error:'Malformed VIPC' }], columns:{windows:{ready:true, packages:new Set(['actual-1.2'])}} },
  { vipcs:[projectVipc], columns:{windows:{ready:false, packages:new Set(['actual-1.2'])}} },
  { vipcs:[projectVipc], columns:{windows:{ready:true, unsupported:true, packages:new Set(['actual-1.2'])}} },
  { vipcs:[], dragons:[{ ...monitoredDragon, monitorOn:['windows'] }], inventory:dragonInventory({}, false) },
  { vipcs:[], dragons:[{ ...monitoredDragon, monitorOn:['windows'] }], inventory:dragonInventory({error:'Malformed Dragon'}) },
  { vipcs:[], dragons:[{ ...monitoredDragon, monitorOn:['windows'] }], inventory:dragonInventory({inspection:{status:'parsed'}, dependencies:{}}) },
  { vipcs:[], dragons:[{ ...monitoredDragon, monitorOn:['windows'] }], inventory:{sha:revision, dragon:{available:true, files:{}}} }
]) {
  hooks.setState({ candidate, platform:'windows', dragons:[], ...evidence });
  assert.equal(hooks.platformCoverage('windows').pending, true);
  await hooks.renderUpdatePanel();
  assert.doesNotMatch(elements.get('platformSummary').innerHTML, /are satisfied/);
}
for (const status of ['stale', 'unknown']) {
  hooks.setState({ candidate, vipcs:[projectVipc], dragons:[], config:{linuxUse:selection},
    inventory:inventoryFor({...currentFreshness, status}), columns:{linux:state} });
  await hooks.renderUpdatePanel();
  assert.match(elements.get('platformSummary').innerHTML, /Linux dependencies need attention/);
  assert.doesNotMatch(elements.get('updatePanel').innerHTML, /id="updRun"/);
  assert.match(elements.get('updatePanel').innerHTML, /active image is never rebuilt/);
}
hooks.setState({ candidate, vipcs:[{ ...projectVipc, monitored:false, monitorOn:[] }], dragons:[], columns:{} });
await hooks.renderUpdatePanel();
assert.match(elements.get('platformSummary').innerHTML, /No project dependency files are monitored/);
assert.doesNotMatch(elements.get('platformSummary').innerHTML, /are satisfied/);
hooks.setState({ candidate, vipcs:[{ ...projectVipc, monitorOn:['windows'] }], dragons:[], columns:{} });
assert.equal(hooks.monitorCounts().monitored, 0);
hooks.setFileMonitor('vipc', 'project.vipc', 'linux', true);
assert.equal(hooks.monitorInputs().container_vipc, 'project.vipc::windows+linux');
hooks.setFileMonitor('vipc', 'project.vipc', 'linux', false);
assert.equal(hooks.monitorInputs().container_vipc, 'project.vipc::windows');
assert.equal(hooks.monitorCounts().monitored, 0);
await hooks.dispatchMonitor();
await hooks.renderUpdatePanel();
assert.match(elements.get('monitorStatus').innerHTML, /Review and merge its pull request/);
assert.match(elements.get('updatePanel').innerHTML, /id="updRun" disabled/);
for (const staleState of [{ revision:'historical' }, { dirty:true }]) {
  hooks.setState({ candidate, vipcs:[], dragons:[], ...staleState });
  await hooks.renderUpdatePanel();
  assert.match(elements.get('updatePanel').innerHTML, /id="updRun" disabled/);
  const start = requests.length;
  await hooks.dispatchContainerUpdate();
  for (const action of ['build', 'activate', 'reset']) await hooks.dispatchLinuxWorker(action);
  assert.ok(requests.slice(start).every(request => request.options.method !== 'POST'));
}
hooks.setState({ candidate, vipcs:[], dragons:[], tooling:'lvpm' });
await hooks.renderUpdatePanel();
assert.match(elements.get('updatePanel').innerHTML, /You are exploring LVPM/);
assert.doesNotMatch(elements.get('updatePanel').innerHTML, /id="updRun"/);
const postStart = requests.length;
const explorationView = installerView();
await hooks.renderLinuxInstaller();
for (const input of explorationView.inputs) input.listener();
assert.ok(requests.slice(postStart).every(request => request.options.method !== 'POST'));
for (const operation of ['vipm', 'lvpm']) {
  for (const change of [{platform:'windows'}, {revision:'historical'}, {dirty:true}]) {
    hooks.setState({ candidate, vipcs:[], dragons:[] });
    const start = requests.length;
    const pending = operation === 'vipm' ? hooks.dispatchContainerUpdate() : hooks.dispatchLinuxWorker('activate');
    hooks.setState({ candidate, vipcs:[], dragons:[], ...change });
    await pending;
    assert.ok(requests.slice(start).every(request => request.options.method !== 'POST'), operation + JSON.stringify(change));
  }
}
const modalFields = new Map(['#updConfirmSub', '#updConfirmBody', '#updConfirmApprove'].map(key => [key, uiElement()]));
const modal = uiElement();
modal.querySelector = selector => modalFields.get(selector);
hooks.setModal(modal);
platformFixture('windows', ['actual-1.2'], []);
hooks.openUpdateApprovalDialog();
assert.match(modalFields.get('#updConfirmBody').innerHTML, /Windows preview/);
assert.doesNotMatch(modalFields.get('#updConfirmBody').innerHTML, /Linux preview/);
const approvedBeforeSwitch = modalFields.get('#updConfirmApprove').onclick;
hooks.selectPlatform('linux');
const beforeObsoleteApproval = requests.length;
approvedBeforeSwitch();
assert.equal(requests.length, beforeObsoleteApproval);
assert.equal(modal.classList.contains('hidden'), true);
hooks.setModal(null);
const savedStorage = context.localStorage;
context.localStorage = { getItem:() => '' };
hooks.setState({ candidate, vipcs:[], dragons:[] });
await hooks.dispatchLinuxWorker('activate');
assert.equal(typeof hooks.pendingAction(), 'function');
hooks.selectPlatform('windows');
assert.equal(hooks.pendingAction(), null);
assert.equal(elements.get('tokPanel').hidden, true);
context.localStorage = savedStorage;
const platformTabs = ['windows', 'linux'].map(platform => ({
  platform, attributes:{}, listeners:{}, tabIndex:0,
  getAttribute(){ return platform; },
  setAttribute(name, value){ this.attributes[name] = value; },
  addEventListener(event, listener){ this.listeners[event] = listener; },
  focus(){ this.focused = true; }
}));
context.document.querySelectorAll = selector => selector === '[data-platform]' ? platformTabs : [];
hooks.setState({ candidate, platform:'windows', vipcs:[], dragons:[] });
hooks.initPlatformTabs();
assert.equal(platformTabs[0].attributes['aria-selected'], 'true');
platformTabs[0].listeners.keydown({key:'ArrowRight', preventDefault(){}});
assert.equal(platformTabs[1].attributes['aria-selected'], 'true');
assert.equal(platformTabs[1].tabIndex, 0);
assert.equal(platformTabs[1].focused, true);
assert.equal(new URL(context.location.href).searchParams.get('platform'), 'linux');
assert.equal(new URL(context.location.href).searchParams.get('sha'), revision);
platformTabs[1].listeners.keydown({key:'Home', preventDefault(){}});
assert.equal(platformTabs[0].attributes['aria-selected'], 'true');
const header = fs.readFileSync(new URL('pages/lvci-header.js', root), 'utf8');
const bannerSource = header.slice(header.indexOf("    try {\n      fetch(base + '/deps-pending.json'"), header.indexOf('    // VI Browser owns #commit-select'));
assert.ok(bannerSource.includes('platform not identified'));
for (const testCase of [
  { path:'/project/dependencies.html', containers:['linux'], show:false },
  { path:'/project/dashboard.html', containers:['linux'], show:true, labels:['Linux'], other:'Windows' },
  { path:'/project/vi-browser.html', containers:['windows-beta'], show:true, labels:['Windows'], other:'Linux' },
  { path:'/project/configure.html', containers:['windows','linux'], show:true, labels:['Windows','Linux'] },
  { path:'/project/dashboard.html', containers:[], show:true, labels:[] }
]) {
  const banner = uiElement();
  vm.runInNewContext(bannerSource, {
    base:'/project', navBase:'/project', location:{pathname:testCase.path}, pendbar:banner,
    fetch:async () => ({ok:true, json:async () => ({pending:true, containers:testCase.containers, packages:['a'],dragon:[]})})
  });
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(banner.classList.contains('show'), testCase.show);
  if (!testCase.show) continue;
  assert.match(banner.innerHTML, /aggregate, not per-platform/);
  for (const label of testCase.labels) assert.match(banner.innerHTML, new RegExp('dependencies.html\\?platform=' + label.toLowerCase()));
  if (testCase.other) assert.doesNotMatch(banner.innerHTML, new RegExp('Review ' + testCase.other + ' dependencies'));
}
console.log('LVPM UX: existing dispatch/inventory/freshness/Dragon checks plus OS-isolated status, monitoring preservation, tab keyboard/deep links, preview-only tooling, stale/historical blocking, cross-tab/revision/dirty dispatch races, obsolete approval/token cancellation, and shared-banner scope passed.');
