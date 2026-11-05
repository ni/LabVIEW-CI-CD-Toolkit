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
let dispatchStatus = 204;
let dispatchDetails = null;
let runResponse = null;
let actorStatus = 200;
let runBarrier = null;
let runPoll = null;
let lookupBarrier = null;
let runJsonBarrier = null;
let dispatchJsonBarrier = null;
let workflowMetadata = null;
let workflowMetadataStatus = 200;
let dispatchError = null;
let commitResponses = [];
let headJsonBarrier = null;
let rawConfigStatus = 200;
let rawTextBarrier = null;
let repoMetadata = {full_name:'owner/project', default_branch:'trunk', fork:false};
const context = vm.createContext({
  window:{ LVCI:{ repo:'owner/project' } },
  location:{ hostname:'owner.github.io', pathname:'/project/dependencies.html', href:'https://owner.github.io/project/dependencies.html?sha=' + revision },
  history:{ replaceState(_state, _title, url){ context.location.href = String(url); } },
  document:{ getElementById:id => elements.get(id) || null },
  localStorage:{ getItem:() => 'test-token' },
  URL, URLSearchParams, console, setInterval:callback => { runPoll = callback; return 1; }, clearInterval:() => { runPoll = null; },
  fetch:async (url, options = {}) => {
    requests.push({ url, options });
    if (lookupBarrier && lookupBarrier.match(url)) await lookupBarrier.promise;
    if (url === 'https://api.github.com/user') return { ok:actorStatus === 200, status:actorStatus, json:async () => ({ id:17 }) };
    if (/\/actions\/workflows\/[^/]+\.yml$/.test(url)) return { ok:workflowMetadataStatus === 200, status:workflowMetadataStatus, json:async () => workflowMetadata || ({ id:91, path:'.github/workflows/' + url.split('/').at(-1) }) };
    if (/\/actions\/runs\/\d+$/.test(url)) {
      if (runBarrier) await runBarrier;
      if (runResponse instanceof Error) throw runResponse;
      return { ok:true, json:async () => { if (runJsonBarrier) await runJsonBarrier; return runResponse; }, ...runResponse?.response };
    }
    if (url === 'https://api.github.com/repos/owner/project') {
      if (failMetadata) throw new Error('unavailable');
      return { ok:true, json:async () => repoMetadata };
    }
    if (url.includes('/commits/')) return { ok:true, json:async () => { if (headJsonBarrier) await headJsonBarrier; return { sha:commitResponses.shift() || revision }; } };
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
    if (new URL(url).hostname === 'raw.githubusercontent.com') return { ok:rawConfigStatus === 200, status:rawConfigStatus, text:async () => { if (rawTextBarrier) await rawTextBarrier; return activeConfig; } };
    if (options.method === 'POST') {
      if (postBarrier) await postBarrier;
      if (dispatchError) throw dispatchError;
      return { status:dispatchStatus, json:async () => { if (dispatchJsonBarrier) await dispatchJsonBarrier; if (dispatchDetails instanceof Error) throw dispatchDetails; return dispatchDetails; } };
    }
    throw new Error('Unexpected request: ' + url);
  }
});
vm.runInContext(source.replace('    init();', `
    globalThis.hooks = { parseManifestYaml, resolveColumn, packagesInManifest, pkgCellState,
      candidateSelection, loadLinuxBuildAvailability, linuxWorkflowPayload, dispatchLinuxWorker, dispatchContainerUpdate, loadManifestFor, updTargets, renderLinuxInstaller, nipmCell, enrichPlatformState,
      pollLinuxOperation, validateLinuxRun, invalidateLinuxOperation, getLinuxOperation:() => linuxOperation,
      lvpmFreshness, localFreshnessText, loadPlatformStates, renderGenerated, renderContainers,
      dragonFileState, updMissingList, renderDragonFiles, platformCoverage, visibleColumns, selectPlatform,
      renderUpdatePanel, renderVipc, encodeMonitorList, setFileMonitor, syncPlatformTabs, initPlatformTabs,
      monitorCounts, monitorInputs, dispatchMonitor, openUpdateApprovalDialog, loadRevision,
      renderLinuxPrerequisites, checkLinuxMonitoringHead,
      runLinuxPrimaryAction, committedMonitoringMatches, linuxMonitoringContext, linuxSaveRestriction,
      headCheck:() => linuxHeadCheck,
      isMonitorDirty:() => monitorDirty,
      setRevisionFiles:files => {
        listDependencyFiles = async () => files;
        vipcPackages = async () => ['actual-1.2'];
        systemForRevision = async () => [];
      },
      pendingAction:() => pendingTokenAction,
      setModal:modal => { updModal = modal; },
      getColumnStates: () => currentColStates,
      setState: state => {
        invalidateLinuxOperation();
        linuxMonitoringRequest = null;
        linuxHeadCheck = null;
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
assert.deepEqual(JSON.parse(dispatched[0].options.body), { ref:'trunk', inputs:{ linux_worker:tag + '@' + digest }, return_run_details:true });
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
  elements.set('linuxPrerequisites', uiElement());
  elements.set('linuxPrimary', uiElement());
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
assert.match(selectedView.host.innerHTML, /<strong>LVPM<\/strong>/);
assert.match(selectedView.host.innerHTML, /Explicit per-action overrides win/);
assert.match(selectedView.host.innerHTML, /Switching these views does not change configuration or dispatch a workflow/);
await selectedRender;
assert.match(selectedView.review.innerHTML, /<h4>Currently selected Linux default<\/h4>/);
assert.doesNotMatch(selectedView.review.innerHTML, /Alternative candidate/);
const beforeViewSwitch = requests.length;
selectedView.inputs.find(input => input.value === 'vipm').listener();
assert.equal(selectedView.review.hidden, true);
assert.match(selectedView.host.innerHTML, /<strong>LVPM<\/strong>/);
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
  assert.match(resetView.host.innerHTML, /Reset removes only <code>linuxUse<\/code>/);
  assert.ok(resetView.host.innerHTML.includes('existing default selection remains <code>' + retainedDefault + '</code>'));
  assert.match(resetView.host.innerHTML, /per-action overrides are retained/);
  assert.match(resetView.host.innerHTML, /This may be a base or custom image, not VIPM/);
  assert.doesNotMatch(resetView.host.innerHTML, /Reset Linux default to VIPM|return to the existing VIPM selection/);
  const beforeReset = requests.length;
  await hooks.dispatchLinuxWorker('reset');
  const resetRequests = requests.slice(beforeReset).filter(request => request.options.method === 'POST');
  assert.equal(resetRequests.length, 1);
  assert.ok(resetRequests[0].url.endsWith('/reconfigure.yml/dispatches'));
  assert.deepEqual(JSON.parse(resetRequests[0].options.body), {
    ref:'trunk', inputs:{linux_worker:'reset'}, return_run_details:true
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
availableView.inputs.find(input => input.value === 'lvpm').listener();
assert.match(elements.get('linuxPrerequisites').innerHTML, /id="linuxPrimary">Build Linux LVPM container/);
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
missingView.inputs.find(input => input.value === 'lvpm').listener();
assert.match(elements.get('linuxPrerequisites').innerHTML, /id="linuxPrimary" disabled/);
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
  unavailableView.inputs.find(input => input.value === 'lvpm').listener();
  assert.match(elements.get('linuxPrerequisites').innerHTML, /id="linuxPrimary" disabled/);
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
metadataFailureView.inputs.find(input => input.value === 'lvpm').listener();
assert.match(elements.get('linuxPrerequisites').innerHTML, /id="linuxPrimary" disabled/);
assert.match(metadataFailureView.review.innerHTML, /Candidate build availability is unknown/);
failMetadata = false;
workflowResponse = 'available';
hooks.setState({ revision:'historical', candidate });
const historicalView = installerView();
await hooks.renderLinuxInstaller();
for (const control of ['lvpmActivate']) {
  assert.ok(historicalView.review.innerHTML.includes('id="' + control + '" disabled'));
}
hooks.setState({ candidate });
elements.set('lvpmStatus', {});
const beforeAvailableBuild = requests.length;
await hooks.dispatchLinuxWorker('build');
const buildRequests = requests.slice(beforeAvailableBuild);
assert.ok(buildRequests.some(request => request.url.includes('/contents/.github/workflows?ref=trunk')));
assert.equal(buildRequests.at(-1).options.method, 'POST');
assert.ok(buildRequests.at(-1).url.endsWith('/actions/workflows/build-labview-lvpm-image.yml/dispatches'));
assert.deepEqual(JSON.parse(buildRequests.at(-1).options.body), { ref:'trunk', inputs:{}, return_run_details:true });
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
    new RegExp('>' + (testCase.expected === 'missing' ? 'packages unresolved' : testCase.expected) + '<'), JSON.stringify(testCase));
}
for (const status of ['missing', 'wrong_version', 'conflict']) {
  hooks.setState({ candidate, inventory:dragonInventory({ ...parsedDragon, dependencies:[{ status }] }) });
  assert.match(hooks.dragonFileState({...monitoredDragon, monitorOn:['windows']}), />packages unresolved</);
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
  return { innerHTML:'', textContent:'', hidden:false, style:{},
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
  if (platform === 'windows') assert.ok(requests.slice(start).every(request => new URL(request.url).hostname !== 'raw.githubusercontent.com'));
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
for (const action of ['build', 'activate', 'reset']) {
  for (const change of [{platform:'windows'}, {revision:'historical'}, {dirty:true}]) {
    for (const responseStatus of [204, 500]) {
      hooks.setState({ candidate, vipcs:[], dragons:[] });
      elements.set('lvpmStatus', uiElement());
      postBarrier = new Promise(resolve => { releasePost = resolve; });
      dispatchStatus = responseStatus;
      const start = requests.length;
      const pending = hooks.dispatchLinuxWorker(action);
      for (let attempt = 0; attempt < 100 && !requests.slice(start).some(request => request.options.method === 'POST'); attempt++) {
        await Promise.resolve();
      }
      assert.equal(requests.slice(start).filter(request => request.options.method === 'POST').length, 1);
      hooks.setState({ candidate, vipcs:[], dragons:[], ...change });
      elements.get('tokPanel').hidden = false;
      elements.get('lvpmStatus').textContent = 'Current view unchanged';
      releasePost();
      await pending;
      assert.equal(elements.get('tokPanel').hidden, false, action + JSON.stringify(change));
      assert.equal(elements.get('lvpmStatus').textContent, 'Current view unchanged');
      postBarrier = null;
    }
  }
}
dispatchStatus = 204;
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
  assert.equal(elements.get('updatePanel').hidden, true);
  assert.equal(elements.get('updatePanel').innerHTML, '');
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
assert.match(elements.get('monitorStatus').innerHTML, /Save request accepted, not applied/);
assert.doesNotMatch(elements.get('updatePanel').innerHTML, /id="updRun"/);
for (const staleState of [{ revision:'historical' }, { dirty:true }]) {
  hooks.setState({ candidate, vipcs:[], dragons:[], ...staleState });
  await hooks.renderUpdatePanel();
  assert.doesNotMatch(elements.get('updatePanel').innerHTML, /id="updRun"/);
  const start = requests.length;
  await hooks.dispatchContainerUpdate();
  for (const action of ['build', 'activate', 'reset']) await hooks.dispatchLinuxWorker(action);
  assert.ok(requests.slice(start).every(request => request.options.method !== 'POST'));
}
hooks.setState({ candidate, vipcs:[], dragons:[], tooling:'lvpm' });
await hooks.renderUpdatePanel();
assert.equal(elements.get('updatePanel').hidden, true);
assert.equal(elements.get('updatePanel').innerHTML, '');
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
function bannerElement(tagName){
  const element = uiElement();
  element.tagName = tagName;
  element.children = [];
  let content = '';
  Object.defineProperty(element, 'innerHTML', {
    get(){ throw new Error('Banner tests must inspect DOM nodes, not HTML'); },
    set(){ throw new Error('Dependency banner must not parse HTML'); }
  });
  Object.defineProperty(element, 'textContent', {
    get(){ return content + element.children.map(child => child.textContent).join(''); },
    set(value){ content = String(value); element.children = []; }
  });
  element.appendChild = child => { element.children.push(child); return child; };
  return element;
}
function bannerLinks(element){
  return element.children.flatMap(child => child.tagName === 'a' ? [child] : bannerLinks(child));
}
for (const testCase of [
  { path:'/project/dependencies.html', containers:['linux'], show:false },
  { path:'/project/dashboard.html', containers:['linux'], show:true, labels:['Linux'], other:'Windows' },
  { path:'/project/vi-browser.html', containers:['windows-beta'], show:true, labels:['Windows'], other:'Linux' },
  { path:'/project/configure.html', containers:['windows','linux'], show:true, labels:['Windows','Linux'] },
  { path:'/project/dashboard.html', containers:[], show:true, labels:[] },
  { path:'/project/dashboard.html', containers:['linux', '<img src=x onerror=alert(1)>'], show:true, labels:['Linux'], other:'Windows', navBase:'https://owner.github.io/project/"/><img src=x onerror=alert(1)>' },
  { path:'/project/dashboard.html', containers:['linux'], show:false, navBase:'javascript:alert(1)' },
  { path:'/project/dashboard.html', containers:['linux'], show:false, navBase:'data:text/html,<script>alert(1)</script>' }
]) {
  const banner = bannerElement('div');
  vm.runInNewContext(bannerSource, {
    base:'/project', navBase:testCase.navBase || '/project', location:{pathname:testCase.path, href:'https://owner.github.io' + testCase.path}, pendbar:banner,
    URL,
    document:{ createElement:bannerElement, createTextNode:value => {
      const text = bannerElement('#text'); text.textContent = value; return text;
    } },
    fetch:async () => ({ok:true, json:async () => ({pending:true, containers:testCase.containers, packages:['a'],dragon:[]})})
  });
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(banner.classList.contains('show'), testCase.show);
  if (!testCase.show) continue;
  assert.match(banner.textContent, /aggregate, not per-platform/);
  const links = bannerLinks(banner);
  assert.equal(links.length, Math.max(1, testCase.labels.length));
  for (const label of testCase.labels) {
    const link = links.find(item => item.textContent === 'Review ' + label + ' dependencies ↗');
    assert.ok(link);
    assert.equal(new URL(link.href).searchParams.get('platform'), label.toLowerCase());
  }
  if (testCase.other) assert.doesNotMatch(banner.textContent, new RegExp('Review ' + testCase.other + ' dependencies'));
  assert.ok(links.every(link => new URL(link.href).protocol === 'https:'));
  assert.deepEqual(banner.children[0].children.map(child => child.tagName),
    testCase.labels.length === 2 ? ['strong', 'span', 'a', '#text', 'a'] : ['strong', 'span', 'a']);
}
elements.set('status', uiElement());
const monitoringFiles = {
  vipc:['project.vipc', 'other.vipc', '.github/labview/vipm/ci-tooling.vipc'],
  dragon:['project.dragon']
};
hooks.setRevisionFiles(monitoringFiles);
const monitoringPrefix = 'config:\n  container:\n    use: base\n';
const monitoringCases = [
  { name:'missing manifest', yaml:'',
    project:'windows+linux', other:'windows+linux', dragon:'windows+linux' },
  { name:'implicit legacy', yaml:monitoringPrefix,
    project:'windows+linux', other:'windows+linux', dragon:'windows+linux' },
  { name:'implicit Dragon with explicit VIPC', yaml:monitoringPrefix + '    vipc: []\n',
    project:'none', other:'none', dragon:'windows+linux' },
  { name:'implicit VIPC with explicit Dragon', yaml:monitoringPrefix + '    dragon: []\n',
    project:'windows+linux', other:'windows+linux', dragon:'none' },
  { name:'explicit platform lists', yaml:monitoringPrefix
      + '    vipc:\n      - path: project.vipc\n        monitorOn: windows\n'
      + '    dragon:\n      - path: project.dragon\n        monitorOn: linux\n',
    project:'windows', other:'none', dragon:'linux' },
  { name:'explicit empty lists', yaml:monitoringPrefix + '    vipc: []\n    dragon: []\n',
    project:'none', other:'none', dragon:'none' },
  { name:'explicit opt-out', yaml:monitoringPrefix
      + '    vipc:\n      - path: project.vipc\n        monitor: false\n'
      + '    dragon:\n      - path: project.dragon\n        monitor: false\n',
    project:'none', other:'none', dragon:'none' },
  { name:'legacy monitor true', yaml:monitoringPrefix
      + '    vipc:\n      - path: project.vipc\n        monitor: true\n'
      + '    dragon:\n      - path: project.dragon\n        monitor: true\n',
    project:'windows+linux', other:'none', dragon:'windows+linux' }
];
const savedMonitoringConfig = activeConfig;
for (const path of ['generated', 'live']) {
  for (const fixture of monitoringCases) {
    activeConfig = fixture.yaml;
    const config = hooks.parseManifestYaml(fixture.yaml);
    const generated = {
      sha:revision, config, columns:[], system:[],
      vipc:monitoringFiles.vipc.map(file => ({ path:file, packages:['actual-1.2'] })),
      dragon:{ files:monitoringFiles.dragon.map(file => ({ path:file, dependencies:[] })) }
    };
    const load = async sha => {
      if (path === 'generated') hooks.renderGenerated({ ...generated, sha });
      else await hooks.loadRevision(sha);
      await hooks.renderUpdatePanel();
    };
    hooks.setState({ platform:'windows', vipcs:[], dragons:[], dirty:true });
    const beforeLoad = requests.length;
    await load(revision);
    const label = path + ': ' + fixture.name;
    assert.equal(hooks.isMonitorDirty(), false, label);
    assert.equal(elements.get('saveMonitor').disabled, true, label);
    assert.match(elements.get('updatePanel').innerHTML, /id="updRun">Review Windows VIPM update/, label);
    assert.doesNotMatch(elements.get('monitorStatus').innerHTML, /Save to update/, label);
    const inputs = plain(hooks.monitorInputs());
    const vipcEntries = new Map(inputs.container_vipc.split(';').map(entry => entry.split('::')));
    assert.equal(vipcEntries.get('project.vipc'), fixture.project, label);
    assert.equal(vipcEntries.get('other.vipc'), fixture.other, label);
    assert.equal(vipcEntries.get('.github/labview/vipm/ci-tooling.vipc'), 'windows+linux', label);
    assert.equal(inputs.container_dragon, 'project.dragon::' + fixture.dragon, label);
    hooks.setFileMonitor('vipc', 'project.vipc', 'windows', fixture.project === 'none');
    await hooks.renderUpdatePanel();
    assert.equal(hooks.isMonitorDirty(), true, label);
    assert.equal(elements.get('saveMonitor').disabled, false, label);
    assert.match(elements.get('updatePanel').innerHTML, /id="updRun" disabled/, label);
    assert.match(elements.get('monitorStatus').innerHTML, /Save to update/, label);
    const edited = plain(hooks.monitorInputs());
    const projectEntry = edited.container_vipc.split(';').find(entry => entry.startsWith('project.vipc::'));
    assert.equal(projectEntry.includes('linux'), fixture.project.includes('linux'), label);
    assert.equal(edited.container_dragon, inputs.container_dragon, label);
    await load('e'.repeat(40));
    assert.equal(hooks.isMonitorDirty(), false, label);
    assert.equal(elements.get('saveMonitor').disabled, true, label);
    assert.match(elements.get('updatePanel').innerHTML, /id="updRun" disabled/, label);
    assert.match(elements.get('updatePanel').innerHTML, /Select the current default-branch revision/, label);
    await load(revision);
    assert.equal(hooks.isMonitorDirty(), false, label);
    assert.deepEqual(plain(hooks.monitorInputs()), inputs, label);
    assert.match(elements.get('updatePanel').innerHTML, /id="updRun">Review Windows VIPM update/, label);
    hooks.setFileMonitor('dragon', 'project.dragon', 'windows', fixture.dragon === 'none' || fixture.dragon === 'linux');
    await hooks.renderUpdatePanel();
    assert.equal(hooks.isMonitorDirty(), true, label);
    assert.match(elements.get('updatePanel').innerHTML, /id="updRun" disabled/, label);
    assert.equal(plain(hooks.monitorInputs()).container_vipc, inputs.container_vipc, label);
    await load(revision);
    assert.equal(hooks.isMonitorDirty(), false, label);
    assert.deepEqual(plain(hooks.monitorInputs()), inputs, label);
    assert.ok(requests.slice(beforeLoad).every(request => request.options.method !== 'POST'), label);
  }
}
activeConfig = savedMonitoringConfig;
const identifiedRun = {
  id:123, workflow_id:91, path:'.github/workflows/build-labview-lvpm-image.yml', event:'workflow_dispatch',
  repository:{full_name:'owner/project'}, head_repository:{full_name:'owner/project'}, head_branch:'trunk', head_sha:revision,
  actor:{id:17}, triggering_actor:{id:17}, run_attempt:1, status:'queued', conclusion:null
};
const acceptedDetails = {
  workflow_run_id:123, run_url:'https://api.github.com/repos/owner/project/actions/runs/123',
  html_url:'https://github.com/owner/project/actions/runs/123'
};
let progressCases = 0;
let visualChecks = 0;
function assertLinuxProgress(label, kind, moving){
  assert.equal(elements.get('lvpmProgress').hidden, false);
  assert.equal(elements.get('lvpmProgressPhase').textContent, label);
  assert.equal(elements.get('lvpmProgress').className, 'lvpm-progress ' + kind);
  assert.equal(elements.get('lvpmProgressBar').className.includes('indeterminate'), moving);
  assert.equal(elements.get('lvpmProgressBar').style.width, moving ? '' : '100%');
  visualChecks++;
}
function progressFixture(){
  hooks.setState({candidate, vipcs:[], dragons:[]});
  dispatchStatus = 200;
  dispatchDetails = {...acceptedDetails};
  runResponse = {...identifiedRun};
  actorStatus = 200;
  workflowMetadata = null;
  workflowMetadataStatus = 200;
  dispatchError = null;
  commitResponses = [];
  elements.set('lvpmStatus', uiElement());
  elements.set('lvpmRunLink', uiElement());
  for (const id of ['lvpmProgress','lvpmProgressPhase','lvpmProgressBar']) elements.set(id, uiElement());
  elements.get('lvpmProgress').hidden = true;
  for (const id of ['lvpmBuild','lvpmActivate','lvpmReset']) elements.set(id, {disabled:false});
  return requests.length;
}
const postCount = start => requests.slice(start).filter(request => request.options.method === 'POST').length;
async function until(predicate){
  for (let attempt = 0; attempt < 150 && !predicate(); attempt++) await Promise.resolve();
  assert.ok(predicate(), 'Expected asynchronous stage to be reached');
}
let progressStart = progressFixture();
postBarrier = new Promise(resolve => { releasePost = resolve; });
const pendingDispatch = hooks.dispatchLinuxWorker('build');
assertLinuxProgress('Checking request', '', true);
await hooks.dispatchLinuxWorker('build');
await until(() => postCount(progressStart) === 1);
assertLinuxProgress('Submitting request', '', true);
await hooks.dispatchLinuxWorker('activate');
assert.equal(postCount(progressStart), 1);
assert.equal(elements.get('lvpmBuild').disabled, true);
releasePost();
await pendingDispatch;
postBarrier = null;
assert.match(elements.get('lvpmStatus').textContent, /Workflow queued/);
assertLinuxProgress('Workflow queued', '', true);
assert.equal(elements.get('lvpmRunLink').href, acceptedDetails.html_url);
assert.equal(typeof runPoll, 'function');
await hooks.dispatchLinuxWorker('build');
assert.equal(postCount(progressStart), 1);
runResponse = {...identifiedRun, status:'in_progress'};
await runPoll();
assert.match(elements.get('lvpmStatus').textContent, /Workflow running/);
assertLinuxProgress('Workflow running', '', true);
runResponse = {...identifiedRun, status:'completed', conclusion:'failure'};
await runPoll();
assert.match(elements.get('lvpmStatus').textContent, /Workflow failure.*Open the logs/);
assertLinuxProgress('Workflow failed · inspect logs', 'err', false);
assert.equal(elements.get('lvpmRunLink').href, acceptedDetails.html_url);
assert.equal(elements.get('lvpmBuild').disabled, false);
assert.equal(runPoll, null);
assert.ok(requests.slice(progressStart).every(request => !request.url.includes('/runs?')));
progressCases++;

progressFixture();
dispatchDetails = {...acceptedDetails, run_url:acceptedDetails.run_url.replace('owner/project','Owner/Project'), html_url:acceptedDetails.html_url.replace('owner/project','Owner/Project')};
runResponse = {...identifiedRun, repository:{full_name:'Owner/Project'},head_repository:{full_name:'Owner/Project'}};
await hooks.dispatchLinuxWorker('build');
assert.match(elements.get('lvpmStatus').textContent, /Workflow queued/);
assert.equal(elements.get('lvpmRunLink').href, acceptedDetails.html_url);
progressCases++;

progressFixture();
runResponse = {response:{ok:false,status:404}};
await hooks.dispatchLinuxWorker('build');
assert.match(elements.get('lvpmStatus').textContent, /Awaiting the identified run becoming visible/);
assertLinuxProgress('Awaiting identified run', '', true);
runResponse = {...identifiedRun};
await runPoll();
assert.match(elements.get('lvpmStatus').textContent, /Workflow queued/);
progressCases++;

progressStart = progressFixture();
await hooks.dispatchLinuxWorker('build');
installerView();
for (const id of ['lvpmProgress','lvpmProgressPhase','lvpmProgressBar']) elements.set(id, uiElement());
await hooks.renderLinuxInstaller();
assertLinuxProgress('Workflow queued', '', true);
assert.equal(elements.get('lvpmRunLink').href, acceptedDetails.html_url);
assert.equal(postCount(progressStart), 1);
assert.equal(elements.get('lvpmBuild').disabled, true);
progressCases++;

for (const conclusion of ['success','cancelled','timed_out','action_required','neutral','skipped']) {
  progressStart = progressFixture();
  runResponse = {...identifiedRun, status:'completed', conclusion};
  const configBefore = plain(candidate);
  await hooks.dispatchLinuxWorker('build');
  assert.match(elements.get('lvpmStatus').textContent, conclusion === 'success' ? /Build workflow succeeded\. Refresh to check for a published candidate/ : new RegExp('Workflow ' + conclusion));
  assertLinuxProgress(conclusion === 'success' ? 'Workflow succeeded · review required' : conclusion === 'cancelled' ? 'Workflow cancelled' : conclusion === 'timed_out' ? 'Workflow failed · inspect logs' : 'Workflow did not succeed · inspect logs', conclusion === 'success' ? 'ok' : 'err', false);
  assert.equal(postCount(progressStart), 1);
  assert.equal(runPoll, null);
  assert.deepEqual(plain(candidate), configBefore);
  assert.equal(requests.slice(progressStart).filter(request => request.url.includes('manifest.json')).length, 0);
  assert.doesNotMatch(elements.get('lvpmStatus').textContent, /^(Candidate verified|CI activities passed|Activation complete)/);
  progressCases++;
}
for (const action of ['activate','reset']) {
  progressStart = progressFixture();
  runResponse = {...identifiedRun, path:'.github/workflows/reconfigure.yml', status:'completed', conclusion:'success'};
  await hooks.dispatchLinuxWorker(action);
  assert.match(elements.get('lvpmStatus').textContent, /Reconfigure workflow succeeded.*only merging the proposal applies/);
  assertLinuxProgress('Workflow succeeded · review required', 'ok', false);
  assert.equal(postCount(progressStart), 1);
  progressCases++;
}
for (const invalid of [
  null, {}, {...acceptedDetails, workflow_run_id:'123'}, {...acceptedDetails, workflow_run_id:-1},
  {...acceptedDetails, run_url:'https://api.github.com/repos/other/project/actions/runs/123'},
  {...acceptedDetails, html_url:'javascript:alert(1)'},
  {...acceptedDetails, html_url:acceptedDetails.html_url + '?untrusted=1'},
  new Error('Unreadable dispatch response')
]) {
  progressStart = progressFixture();
  dispatchDetails = invalid;
  await hooks.dispatchLinuxWorker('build');
  assert.match(elements.get('lvpmStatus').textContent, /run identity is invalid|Unreadable dispatch response/);
  assertLinuxProgress('Outcome uncertain · check workflow runs before retrying', 'warn', false);
  assert.equal(elements.get('lvpmRunLink').href, 'https://github.com/owner/project/actions/workflows/build-labview-lvpm-image.yml');
  await hooks.dispatchLinuxWorker('build');
  assert.equal(postCount(progressStart), 1);
  assert.equal(runPoll, null);
  progressCases++;
}
for (const invalid of [
  {id:124}, {workflow_id:92}, {path:'.github/workflows/other.yml'}, {event:'push'},
  {repository:{full_name:'foreign/project'}}, {head_repository:{full_name:'foreign/project'}},
  {head_branch:'other'}, {head_sha:'e'.repeat(40)}, {actor:{id:18}}, {triggering_actor:{id:18}}, {run_attempt:2}
]) {
  progressStart = progressFixture();
  runResponse = {...identifiedRun, ...invalid};
  await hooks.dispatchLinuxWorker('build');
  assert.match(elements.get('lvpmStatus').textContent, /Run identity could not be verified/);
  assertLinuxProgress('Tracking stopped · check workflow runs', 'warn', false);
  assert.equal(runPoll, null);
  await hooks.dispatchLinuxWorker('build');
  assert.equal(postCount(progressStart), 1);
  progressCases++;
}
progressStart = progressFixture();
dispatchStatus = 204;
await hooks.dispatchLinuxWorker('build');
assert.match(elements.get('lvpmStatus').textContent, /Dispatch accepted.*no run identity/);
assertLinuxProgress('Tracking stopped · check workflow runs', 'warn', false);
assert.equal(runPoll, null);
await hooks.dispatchLinuxWorker('build');
assert.equal(postCount(progressStart), 1);
assert.ok(requests.slice(progressStart).every(request => !/\/actions\/runs/.test(request.url)));
progressCases++;

progressStart = progressFixture();
dispatchError = new Error('Connection interrupted after submission');
await hooks.dispatchLinuxWorker('build');
assert.match(elements.get('lvpmStatus').textContent, /Do not retry yet; inspect workflow runs.*Connection interrupted/);
assert.match(elements.get('lvpmStatus').textContent, /Dispatch outcome is uncertain/);
assertLinuxProgress('Outcome uncertain · check workflow runs before retrying', 'warn', false);
assert.equal(elements.get('lvpmRunLink').href, 'https://github.com/owner/project/actions/workflows/build-labview-lvpm-image.yml');
assert.equal(elements.get('lvpmBuild').disabled, true);
await hooks.dispatchLinuxWorker('build');
assert.equal(postCount(progressStart), 1);
assert.ok(requests.slice(progressStart).every(request => !/\/actions\/runs/.test(request.url)));
progressCases++;

for (const status of [400,401,403,404,405,410,415,422,429]) {
  progressStart = progressFixture();
  dispatchStatus = status;
  await hooks.dispatchLinuxWorker('build');
  assert.match(elements.get('lvpmStatus').textContent, new RegExp('Workflow dispatch rejected \\(' + status + '\\)'));
  assertLinuxProgress('Request not submitted or rejected', 'err', false);
  assert.equal(postCount(progressStart), 1);
  assert.equal(elements.get('lvpmBuild').disabled, false);
  await hooks.dispatchLinuxWorker('build');
  assert.equal(postCount(progressStart), 2);
  assert.equal(runPoll, null);
  progressCases++;
}

for (const status of [500,502,503,504,408,409,202,302]) {
  progressStart = progressFixture();
  dispatchStatus = status;
  await hooks.dispatchLinuxWorker('build');
  assert.match(elements.get('lvpmStatus').textContent, /Dispatch outcome is uncertain/);
  assert.match(elements.get('lvpmStatus').textContent, new RegExp('HTTP ' + status));
  assert.match(elements.get('lvpmStatus').textContent, /request may already have taken effect/);
  assertLinuxProgress('Outcome uncertain · check workflow runs before retrying', 'warn', false);
  assert.equal(elements.get('lvpmBuild').disabled, true);
  assert.equal(elements.get('lvpmRunLink').href, 'https://github.com/owner/project/actions/workflows/build-labview-lvpm-image.yml');
  await hooks.dispatchLinuxWorker('build');
  assert.equal(postCount(progressStart), 1);
  assert.ok(requests.slice(progressStart).every(request => !/\/actions\/runs/.test(request.url)));
  assert.equal(runPoll, null);
  progressCases++;
}

for (const failure of [401,403,500,new Error('<img src=x onerror=alert(1)>')]) {
  progressStart = progressFixture();
  runResponse = failure instanceof Error ? failure : {response:{ok:false,status:failure}};
  await hooks.dispatchLinuxWorker('build');
  assert.match(elements.get('lvpmStatus').textContent, failure instanceof Error ? /<img/ : /Run status unavailable/);
  assert.equal(elements.get('lvpmStatus').innerHTML, '');
  assertLinuxProgress('Tracking stopped · check workflow runs', 'warn', false);
  assert.equal(elements.get('lvpmProgressPhase').innerHTML, '');
  assert.equal(runPoll, null);
  await hooks.dispatchLinuxWorker('build');
  assert.equal(postCount(progressStart), 1);
  progressCases++;
}
progressStart = progressFixture();
runResponse = {response:{ok:false,status:404}};
await hooks.dispatchLinuxWorker('build');
for (let attempt = 0; attempt < 12; attempt++) await runPoll();
assert.equal(runPoll, null);
assert.match(elements.get('lvpmStatus').textContent, /Run status unavailable/);
progressCases++;
for (const status of [403,404,500]) {
  progressStart = progressFixture();
  workflowMetadataStatus = status;
  await hooks.dispatchLinuxWorker('build');
  assert.equal(postCount(progressStart), 0);
  assert.match(elements.get('lvpmStatus').textContent, /Cannot verify workflow identity/);
  progressCases++;
}

for (const failure of [401,403]) {
  progressStart = progressFixture();
  actorStatus = failure;
  await hooks.dispatchLinuxWorker('build');
  assert.equal(postCount(progressStart), 0);
  assert.match(elements.get('lvpmStatus').textContent, /Cannot verify the authenticated Actions user/);
  assert.equal(elements.get('lvpmBuild').disabled, false);
  progressCases++;
}
progressStart = progressFixture();
workflowMetadata = {id:91, path:'.github/workflows/wrong.yml'};
await hooks.dispatchLinuxWorker('build');
assert.equal(postCount(progressStart), 0);
assert.match(elements.get('lvpmStatus').textContent, /Invalid user or workflow identity/);
progressCases++;
progressStart = progressFixture();
commitResponses = [revision, 'f'.repeat(40)];
await hooks.dispatchLinuxWorker('build');
assert.equal(postCount(progressStart), 0);
assert.match(elements.get('lvpmStatus').textContent, /default branch changed/);
progressCases++;

for (const change of [{platform:'windows'}, {revision:'historical'}, {dirty:true}, {}]) {
  for (const stage of ['actor','workflow','dispatch-json','run-json','poll']) {
    progressStart = progressFixture();
    let release;
    const barrier = new Promise(resolve => { release = resolve; });
    if (stage === 'actor' || stage === 'workflow') lookupBarrier = {
      match:url => stage === 'actor' ? url.endsWith('/user') : /\/actions\/workflows\/[^/]+\.yml$/.test(url), promise:barrier
    };
    if (stage === 'dispatch-json') dispatchJsonBarrier = barrier;
    if (stage === 'run-json') runJsonBarrier = barrier;
    const pending = hooks.dispatchLinuxWorker('build');
    await until(() => stage === 'actor' ? requests.slice(progressStart).some(request => request.url.endsWith('/user'))
      : stage === 'workflow' ? requests.slice(progressStart).some(request => /\/actions\/workflows\/[^/]+\.yml$/.test(request.url))
      : stage === 'dispatch-json' ? postCount(progressStart) === 1 : requests.slice(progressStart).some(request => /\/actions\/runs\/123$/.test(request.url)));
    if (stage === 'dispatch-json') {
      await until(() => elements.get('lvpmProgressPhase').textContent === 'Awaiting identified run');
      assertLinuxProgress('Awaiting identified run', '', true);
    }
    let polling;
    if (stage === 'poll') {
      await pending;
      runBarrier = barrier;
      polling = runPoll();
    }
    hooks.setState({candidate, vipcs:[], dragons:[], ...change});
    assertLinuxProgress('Tracking stopped · check workflow runs', 'warn', false);
    elements.get('lvpmStatus').textContent = 'New context unchanged';
    elements.get('lvpmRunLink').href = 'New link unchanged';
    elements.get('lvpmProgressPhase').textContent = 'New progress unchanged';
    release();
    await pending;
    if (polling) await polling;
    assert.equal(elements.get('lvpmStatus').textContent, 'New context unchanged', stage + JSON.stringify(change));
    assert.equal(elements.get('lvpmRunLink').href, 'New link unchanged');
    assert.equal(elements.get('lvpmProgressPhase').textContent, 'New progress unchanged');
    if (stage === 'actor' || stage === 'workflow') assert.equal(postCount(progressStart), 0);
    lookupBarrier = null;
    dispatchJsonBarrier = null;
    runJsonBarrier = null;
    runBarrier = null;
    progressCases++;
  }
}
hooks.invalidateLinuxOperation();
progressStart = progressFixture();
let releaseIdentity;
lookupBarrier = {match:url => url.endsWith('/user'), promise:new Promise(resolve => { releaseIdentity = resolve; })};
const changedTokenRequest = hooks.dispatchLinuxWorker('build');
await until(() => requests.slice(progressStart).some(request => request.url.endsWith('/user')));
context.localStorage = {getItem:() => 'different-token'};
elements.get('lvpmStatus').textContent = 'New auth context unchanged';
elements.get('lvpmProgressPhase').textContent = 'New auth progress unchanged';
releaseIdentity();
await changedTokenRequest;
assert.equal(postCount(progressStart), 0);
assert.equal(elements.get('lvpmStatus').textContent, 'New auth context unchanged');
assert.equal(elements.get('lvpmProgressPhase').textContent, 'New auth progress unchanged');
context.localStorage = savedStorage;
lookupBarrier = null;
hooks.invalidateLinuxOperation();
progressCases++;

progressStart = progressFixture();
await hooks.dispatchLinuxWorker('build');
let releaseOldPoll;
runBarrier = new Promise(resolve => { releaseOldPoll = resolve; });
const oldPoll = runPoll();
const requestsDuringPoll = requests.length;
await runPoll();
await hooks.dispatchLinuxWorker('build');
assert.equal(requests.length, requestsDuringPoll);
runBarrier = null;
hooks.setState({candidate, vipcs:[], dragons:[]});
dispatchDetails = {workflow_run_id:125,run_url:acceptedDetails.run_url.replace('/123','/125'),html_url:acceptedDetails.html_url.replace('/123','/125')};
runResponse = {...identifiedRun,id:125};
await hooks.dispatchLinuxWorker('build');
const replacementPoll = runPoll;
elements.get('lvpmStatus').textContent = 'New operation unchanged';
elements.get('lvpmProgressPhase').textContent = 'New operation progress unchanged';
releaseOldPoll();
await oldPoll;
assert.equal(elements.get('lvpmStatus').textContent, 'New operation unchanged');
assert.equal(elements.get('lvpmProgressPhase').textContent, 'New operation progress unchanged');
assert.equal(elements.get('lvpmRunLink').href, acceptedDetails.html_url.replace('/123','/125'));
assert.equal(runPoll, replacementPoll);
assert.equal(postCount(progressStart), 2);
hooks.invalidateLinuxOperation();
progressCases++;
let prerequisiteCases = 0;
function prerequisiteFixture(overrides = {}){
  progressFixture();
  dispatchStatus = 204;
  failMetadata = false;
  repoMetadata = {full_name:'owner/project', default_branch:'trunk', fork:false};
  workflowResponse = 'available';
  rawConfigStatus = 200;
  hooks.setState({ candidate, vipcs:[{ ...projectVipc, monitorOn:['windows'] }], dragons:[], columns:{}, ...overrides });
  installerView();
  hooks.renderLinuxPrerequisites();
  return requests.length;
}
const prerequisiteMarkup = () => elements.get('linuxPrerequisites').innerHTML;
function explicitMonitoring(vipcs = [{path:'project.vipc', monitorOn:['windows']}], dragons = []){
  const list = (name, files) => '    ' + name + ':' + (files.length ? '\n' + files.map(file =>
    '      - path: "' + file.path + '"\n        monitor: ' + (file.monitor !== false) + '\n        monitorOn: ' + ((file.monitorOn || []).join('+') || 'none') + '\n').join('') : ' []\n');
  return 'config:\n  container:\n' + list('vipc', vipcs) + list('dragon', dragons);
}
for (const tooling of ['vipm', 'lvpm']) {
  const start = prerequisiteFixture();
  await hooks.renderLinuxInstaller();
  elements.get('linuxInstaller').querySelectorAll().find(input => input.value === tooling).listener();
  hooks.setFileMonitor('vipc', 'project.vipc', 'linux', true);
  assert.match(prerequisiteMarkup(), /Save monitoring for review/);
  assert.doesNotMatch(prerequisiteMarkup(), /Next:|Build Linux LVPM container/);
  await hooks.renderUpdatePanel();
  assert.doesNotMatch(elements.get('updatePanel').innerHTML, /id="updRun"/);
  assert.equal((prerequisiteMarkup().match(/id="linuxPrimary"/g) || []).length, 1);
  assert.equal(postCount(start), 0);
  prerequisiteCases++;
}
let prerequisiteStart = prerequisiteFixture({dirty:true});
await hooks.runLinuxPrimaryAction();
assert.match(prerequisiteMarkup(), /Check committed monitoring/);
assert.match(prerequisiteMarkup(), /not proof of a proposal or merge/);
assert.match(prerequisiteMarkup(), /unattributed list/);
assert.doesNotMatch(prerequisiteMarkup(), /\/pull\/\d+|\/actions\/runs\/\d+/);
assert.equal(hooks.isMonitorDirty(), true);
await hooks.dispatchMonitor();
assert.equal(postCount(prerequisiteStart), 1);
assert.equal(elements.get('saveMonitor').disabled, true);
hooks.selectPlatform('windows');
hooks.selectPlatform('linux');
await new Promise(resolve => setImmediate(resolve));
assert.match(prerequisiteMarkup(), /Check committed monitoring/);
await hooks.dispatchMonitor();
assert.equal(postCount(prerequisiteStart), 1);
prerequisiteCases++;
for (const failure of [500, 502, 503, 504, 'transport']) {
  prerequisiteStart = prerequisiteFixture({dirty:true});
  if (failure === 'transport') dispatchError = new Error('network unavailable');
  else dispatchStatus = failure;
  await hooks.runLinuxPrimaryAction();
  await hooks.dispatchMonitor();
  assert.equal(postCount(prerequisiteStart), 1);
  assert.equal(elements.get('saveMonitor').disabled, true);
  assert.match(prerequisiteMarkup(), /pending or uncertain/);
  assert.doesNotMatch(prerequisiteMarkup(), /\/actions\/runs\/\d+/);
  prerequisiteCases++;
}
prerequisiteStart = prerequisiteFixture({dirty:true});
dispatchStatus = 422;
await hooks.runLinuxPrimaryAction();
assert.equal(elements.get('saveMonitor').disabled, false);
await hooks.runLinuxPrimaryAction();
assert.equal(postCount(prerequisiteStart), 2);
prerequisiteCases++;
let releaseMonitoringPost;
prerequisiteStart = prerequisiteFixture({dirty:true});
postBarrier = new Promise(resolve => { releaseMonitoringPost = resolve; });
const monitoringPost = hooks.runLinuxPrimaryAction();
await until(() => postCount(prerequisiteStart) === 1);
await hooks.dispatchMonitor();
assert.equal(postCount(prerequisiteStart), 1);
assert.equal(elements.get('linuxPrimary').disabled, true);
releaseMonitoringPost();
await monitoringPost;
postBarrier = null;
prerequisiteCases++;
for (const change of ['platform', 'revision', 'monitoring']) {
  prerequisiteStart = prerequisiteFixture({dirty:true});
  postBarrier = new Promise(resolve => { releaseMonitoringPost = resolve; });
  const pending = hooks.runLinuxPrimaryAction();
  await until(() => postCount(prerequisiteStart) === 1);
  if (change === 'platform') hooks.selectPlatform('windows');
  else if (change === 'revision') hooks.setState({candidate, revision:'e'.repeat(40), dirty:true});
  else hooks.setFileMonitor('vipc', 'project.vipc', 'linux', true);
  elements.get('monitorStatus').innerHTML = 'New context unchanged';
  releaseMonitoringPost();
  await pending;
  postBarrier = null;
  assert.equal(elements.get('monitorStatus').innerHTML, 'New context unchanged');
  assert.equal(postCount(prerequisiteStart), 1);
  prerequisiteCases++;
}
prerequisiteStart = prerequisiteFixture({dirty:true, revision:'e'.repeat(40)});
await hooks.runLinuxPrimaryAction();
assert.match(prerequisiteMarkup(), /also historical/);
activeConfig = explicitMonitoring();
const beforeInputs = plain(hooks.monitorInputs());
const beforeUrl = context.location.href;
const newHead = 'f'.repeat(40);
commitResponses = [newHead];
await hooks.runLinuxPrimaryAction();
assert.equal(postCount(prerequisiteStart), 1);
assert.equal(hooks.isMonitorDirty(), true);
assert.deepEqual(plain(hooks.monitorInputs()), beforeInputs);
assert.equal(context.location.href, beforeUrl);
assert.match(prerequisiteMarkup(), /Open matching committed settings/);
assert.match(prerequisiteMarkup(), /local edits stay here/);
assert.match(prerequisiteMarkup(), /target="_blank" rel="noopener"/);
assert.equal(hooks.headCheck().sha, newHead);
await hooks.runLinuxPrimaryAction();
assert.equal(postCount(prerequisiteStart), 1);
await assert.rejects(hooks.linuxWorkflowPayload('build'), /current default-branch revision/);
prerequisiteCases++;
for (const failure of ['network', 'invalid-head', 'mismatch', 'partial', 'malformed', 'duplicate', 'other-platform', 'worker-selection', 'permission', 'incomplete-file', 'duplicate-root', 'wrong-repository', 'wrong-root', 'inline-root']) {
  prerequisiteStart = prerequisiteFixture({dirty:true});
  await hooks.runLinuxPrimaryAction();
  activeConfig = explicitMonitoring();
  if (failure === 'network') failMetadata = true;
  if (failure === 'invalid-head') commitResponses = ['not-a-sha'];
  if (failure === 'mismatch') activeConfig = explicitMonitoring([{path:'different.vipc',monitorOn:['windows']}]);
  if (failure === 'other-platform') activeConfig = explicitMonitoring([{path:'project.vipc',monitorOn:['linux']}]);
  if (failure === 'partial') activeConfig = 'config:\n  container:\n    use: base\n';
  if (failure === 'malformed') activeConfig += '        monitor: maybe\n';
  if (failure === 'duplicate') activeConfig += '    vipc: []\n';
  if (failure === 'worker-selection') activeConfig += '    linuxUse: custom-worker\n';
  if (failure === 'permission') rawConfigStatus = 403;
  if (failure === 'incomplete-file') activeConfig = activeConfig.replace('        monitor: true\n', '');
  if (failure === 'duplicate-root') activeConfig += 'config:\n';
  if (failure === 'wrong-root') activeConfig = activeConfig.replace('config:', 'unrelated:') + 'config:\n  labviewVersion: "2026"\n';
  if (failure === 'inline-root') activeConfig += 'config: {}\n';
  if (failure === 'wrong-repository') repoMetadata.full_name = 'other/project';
  await hooks.runLinuxPrimaryAction();
  assert.equal(hooks.headCheck().sha, undefined, failure);
  assert.ok(['error','mismatch'].includes(hooks.headCheck().status), failure);
  assert.doesNotMatch(prerequisiteMarkup(), /Open matching committed settings/, failure);
  assert.equal(hooks.isMonitorDirty(), true);
  assert.equal(postCount(prerequisiteStart), 1);
  await hooks.dispatchMonitor();
  assert.equal(postCount(prerequisiteStart), 1);
  prerequisiteCases++;
}
for (const change of ['platform', 'revision', 'monitoring', 'tooling']) {
  prerequisiteStart = prerequisiteFixture({dirty:true});
  await hooks.renderLinuxInstaller();
  await hooks.runLinuxPrimaryAction();
  activeConfig = explicitMonitoring();
  let releaseHead;
  const promise = new Promise(resolve => { releaseHead = resolve; });
  lookupBarrier = {match:url => url.includes('/commits/'), promise};
  const pending = hooks.runLinuxPrimaryAction();
  await until(() => requests.slice(prerequisiteStart).some(request => request.url.includes('/commits/')));
  if (change === 'platform') hooks.selectPlatform('windows');
  else if (change === 'revision') hooks.setState({candidate, revision:'e'.repeat(40), dirty:true});
  else if (change === 'tooling') elements.get('linuxInstaller').querySelectorAll().find(input => input.value === 'lvpm').listener();
  else hooks.setFileMonitor('vipc', 'project.vipc', 'linux', true);
  const markup = prerequisiteMarkup();
  releaseHead();
  await pending;
  lookupBarrier = null;
  assert.equal(prerequisiteMarkup(), markup);
  assert.equal(postCount(prerequisiteStart), 1);
  prerequisiteCases++;
}
prerequisiteStart = prerequisiteFixture({revision:'e'.repeat(40)});
await hooks.runLinuxPrimaryAction();
assert.match(prerequisiteMarkup(), /Open current Linux settings/);
assert.equal(hooks.isMonitorDirty(), false);
assert.equal(postCount(prerequisiteStart), 0);
prerequisiteCases++;
const completeVipcs = [
  {path:'.github/labview/vipm/ci-tooling.vipc',monitorOn:['windows','linux'],monitored:true,role:'core',locked:true},
  {path:'project.vipc',monitorOn:['linux'],monitored:true},
  {path:'optout.vipc',monitorOn:[],monitored:false,monitor:false}
];
const completeDragons = [{path:'project.dragon',monitorOn:['windows'],monitored:true}];
prerequisiteStart = prerequisiteFixture({dirty:true, vipcs:completeVipcs, dragons:completeDragons,
  config:{use:'base',linuxUse:selection,actions:{masscompile:'custom/image:retained'}}});
const completeContext = hooks.linuxMonitoringContext();
const completeSelection = '  container:\n    use: base\n    linuxUse: "' + selection + '"\n    actions:\n      masscompile: custom/image:retained\n';
const completeYaml = explicitMonitoring(completeVipcs,completeDragons).replace('  container:\n',completeSelection);
assert.equal(hooks.committedMonitoringMatches(completeYaml,completeContext), true);
assert.equal(hooks.committedMonitoringMatches(explicitMonitoring(completeVipcs.toReversed(),completeDragons).replace('  container:\n',completeSelection),completeContext), true);
for (const changed of [
  completeYaml.replace('monitor: false', 'monitor: true').replace('monitorOn: none','monitorOn: linux'),
  completeYaml.replace('monitorOn: windows+linux','monitorOn: windows'),
  completeYaml.replace('monitorOn: windows\n','monitorOn: linux\n'),
  completeYaml.replace('custom/image:retained','base'),
  completeYaml.replace('    use: base','    use: custom'),
  completeYaml.replace('    linuxUse: "' + selection + '"','    linuxUse: base')
]) assert.equal(hooks.committedMonitoringMatches(changed,completeContext), false);
await hooks.runLinuxPrimaryAction();
activeConfig = completeYaml;
await hooks.runLinuxPrimaryAction();
assert.match(prerequisiteMarkup(), /Open matching committed settings/);
assert.deepEqual(plain(hooks.monitorInputs()), JSON.parse(completeContext.inputs));
assert.equal(postCount(prerequisiteStart), 1);
prerequisiteCases++;
for (const platforms of [['windows'],['windows','linux'],['linux'],[]]) {
  const file = {path:'.github/labview/antidoc/antidoc.vipc', monitorOn:platforms, monitor:platforms.length > 0, monitored:platforms.length > 0};
  prerequisiteStart = prerequisiteFixture({dirty:true,vipcs:[file]});
  await hooks.runLinuxPrimaryAction();
  const intended = plain(hooks.monitorInputs());
  activeConfig = explicitMonitoring([file]);
  await hooks.runLinuxPrimaryAction();
  if (platforms.join('+') === 'windows') assert.match(prerequisiteMarkup(), /Open matching committed settings/);
  else {
    assert.match(prerequisiteMarkup(), /Cannot verify the intended platform selection/);
    assert.doesNotMatch(prerequisiteMarkup(), /Open matching committed settings/);
    await hooks.dispatchMonitor();
  }
  assert.equal(hooks.isMonitorDirty(),true);
  assert.deepEqual(plain(hooks.monitorInputs()),intended);
  assert.equal(postCount(prerequisiteStart),1);
  prerequisiteCases++;
}
if (process.env.LVCI_MONITORING_WRITER_FIXTURE) {
  const fixtures = JSON.parse(process.env.LVCI_MONITORING_WRITER_FIXTURE);
  for (const fixture of fixtures) {
    const files = kind => fixture.inputs['container_' + kind].split(';').map(entry => {
      const [path, platforms] = entry.split('::');
      const monitorOn = platforms === 'none' ? [] : platforms.split('+');
      return {path,monitorOn,monitored:monitorOn.length > 0};
    });
    prerequisiteStart = prerequisiteFixture({dirty:true,vipcs:files('vipc'),dragons:files('dragon'),config:fixture.selection});
    const intended = hooks.linuxMonitoringContext();
    assert.equal(hooks.committedMonitoringMatches(fixture.yaml,intended),true);
    if (fixture.inputs.container_vipc.endsWith('::windows+linux')) {
      const explicit = fixture.yaml.replaceAll('        monitor: true\n','        monitor: true\n        monitorOn: windows+linux\n');
      assert.equal(hooks.committedMonitoringMatches(explicit,intended),true);
      assert.equal(hooks.committedMonitoringMatches(explicit.replaceAll('monitorOn: windows+linux','monitorOn: linux'),intended),false);
    }
    const mismatch = {...intended,inputs:JSON.stringify({...fixture.inputs,container_dragon:'project.dragon::' + (fixture.inputs.container_dragon.endsWith('::linux') ? 'windows' : 'linux')})};
    assert.equal(hooks.committedMonitoringMatches(fixture.yaml,mismatch),false);
    await hooks.runLinuxPrimaryAction();
    activeConfig = fixture.yaml;
    await hooks.runLinuxPrimaryAction();
    assert.match(prerequisiteMarkup(), /Open matching committed settings/);
    assert.equal(hooks.isMonitorDirty(),true);
    assert.equal(postCount(prerequisiteStart),1);
  }
  console.log('Real Reconfigure writer comparison: ' + fixtures.length + ' outputs passed');
}
for (const stage of ['head-json','settings-text']) {
  for (const change of ['token','monitoring','tooling']) {
    prerequisiteStart = prerequisiteFixture({dirty:true});
    await hooks.renderLinuxInstaller();
    await hooks.runLinuxPrimaryAction();
    activeConfig = explicitMonitoring();
    let releaseRead;
    const barrier = new Promise(resolve => { releaseRead = resolve; });
    if (stage === 'head-json') headJsonBarrier = barrier;
    else rawTextBarrier = barrier;
    const pending = hooks.runLinuxPrimaryAction();
    await until(() => requests.slice(prerequisiteStart).some(request => stage === 'head-json' ? request.url.includes('/commits/') : request.url.includes('raw.githubusercontent.com')));
    const oldToken = context.localStorage.getItem;
    if (change === 'token') context.localStorage.getItem = () => 'changed-token';
    else if (change === 'monitoring') hooks.setFileMonitor('vipc','project.vipc','linux',true);
    else elements.get('linuxInstaller').querySelectorAll().find(input => input.value === 'lvpm').listener();
    const markup = prerequisiteMarkup();
    releaseRead();
    await pending;
    headJsonBarrier = null;
    rawTextBarrier = null;
    assert.equal(prerequisiteMarkup(),markup);
    assert.equal(postCount(prerequisiteStart),1);
    context.localStorage.getItem = oldToken;
    prerequisiteCases++;
  }
}
for (const metadata of [
  {default_branch:'trunk',fork:true,parent:{full_name:'NI/LabVIEW-CI-CD-Toolkit'},source:{full_name:'ni/LabVIEW-CI-CD-Toolkit'}},
  {default_branch:'trunk'},
  {default_branch:'trunk',fork:true}
]) {
  prerequisiteStart = prerequisiteFixture({dirty:true});
  repoMetadata = {full_name:'owner/project', ...metadata};
  await hooks.runLinuxPrimaryAction();
  assert.equal(postCount(prerequisiteStart), 0);
  assert.match(prerequisiteMarkup(), /ancestry|fork of the tooling source/);
  prerequisiteCases++;
}
prerequisiteStart = prerequisiteFixture({dirty:true});
failMetadata = true;
await hooks.runLinuxPrimaryAction();
assert.equal(postCount(prerequisiteStart), 0);
prerequisiteCases++;
const sourceSource = source.replace("const repo = repoFromUrl();", "const repo = 'ni/LabVIEW-CI-CD-Toolkit';");
const sourceSandbox = vm.createContext({ ...context, fetch:async () => {throw new Error('Source Save must not perform network requests');} });
vm.runInContext(sourceSource.replace('    init();', 'globalThis.sourceSaveBlock = linuxSaveRestriction;'), sourceSandbox);
assert.match(await sourceSandbox.sourceSaveBlock(), /tooling source cannot/);
vm.runInContext(sourceSource.replace('    init();', `globalThis.sourceSave = () => {
  activePlatform = 'linux'; monitorDirty = true; currentVipcs = [{path:'project.vipc',monitorOn:['linux']}];
  selectedRevision = '${revision}'; defaultRevision = '${revision}';
  return runLinuxPrimaryAction();
};`), sourceSandbox);
await sourceSandbox.sourceSave();
assert.match(prerequisiteMarkup(), /tooling source repository/);
assert.equal(elements.get('linuxPrimary').disabled,true);
prerequisiteCases++;
const sourceReads = [];
let sourceHeadBarrier = null;
const sourceReadSandbox = vm.createContext({...context,fetch:async (url,options = {}) => {
  sourceReads.push({url,method:options.method || 'GET'});
  assert.notEqual(options.method,'POST');
  if (url.endsWith('/repos/ni/LabVIEW-CI-CD-Toolkit')) return {ok:true,json:async () => ({full_name:'ni/LabVIEW-CI-CD-Toolkit',default_branch:'main',fork:false})};
  if (url.includes('/commits/')) { if (sourceHeadBarrier) await sourceHeadBarrier; return {ok:true,json:async () => ({sha:newHead})}; }
  if (url.includes('raw.githubusercontent.com/ni/LabVIEW-CI-CD-Toolkit/')) return {ok:true,text:async () => explicitMonitoring()};
  throw new Error('Unexpected source read: ' + url);
}});
vm.runInContext(sourceSource.replace('    init();', `globalThis.sourceUi = {
  setup: status => {
    activePlatform = 'linux'; linuxToolingView = 'lvpm'; monitorDirty = true;
    selectedRevision = '${'e'.repeat(40)}'; defaultRevision = '${revision}'; selectedConfig = {};
    currentVipcs = [{path:'project.vipc',monitorOn:['windows'],packages:[]}]; currentDragons = [];
    linuxHeadCheck = null;
    linuxMonitoringRequest = status ? {...linuxMonitoringContext(),status} : null;
    renderLinuxPrerequisites();
  },
  run:runLinuxPrimaryAction,
  change:() => setFileMonitor('vipc','project.vipc','linux',true),
  dirty:() => monitorDirty,
  revision:() => selectedRevision
};`),sourceReadSandbox);
sourceReadSandbox.sourceUi.setup(null);
assert.equal(elements.get('linuxPrimary').disabled,true);
await sourceReadSandbox.sourceUi.run();
assert.equal(sourceReads.length,0);
prerequisiteCases++;
for (const status of ['accepted','uncertain']) {
  sourceReadSandbox.sourceUi.setup(status);
  assert.equal(elements.get('linuxPrimary').disabled,false);
  assert.match(prerequisiteMarkup(), /Check committed monitoring/);
  assert.match(prerequisiteMarkup(), /also historical/);
  await sourceReadSandbox.sourceUi.run();
  assert.match(prerequisiteMarkup(), /Open matching committed settings/);
  assert.match(prerequisiteMarkup(), new RegExp('sha=' + newHead));
  assert.equal(sourceReadSandbox.sourceUi.dirty(),true);
  assert.equal(sourceReadSandbox.sourceUi.revision(),'e'.repeat(40));
  assert.ok(sourceReads.every(request => request.method === 'GET'));
  prerequisiteCases++;
}
sourceReadSandbox.sourceUi.setup('accepted');
let releaseSourceHead;
sourceHeadBarrier = new Promise(resolve => { releaseSourceHead = resolve; });
const oldSourceReads = sourceReads.length;
const sourceCheck = sourceReadSandbox.sourceUi.run();
await until(() => sourceReads.length >= oldSourceReads + 2);
sourceReadSandbox.sourceUi.change();
const sourceStaleMarkup = prerequisiteMarkup();
releaseSourceHead();
await sourceCheck;
sourceHeadBarrier = null;
assert.equal(prerequisiteMarkup(),sourceStaleMarkup);
assert.doesNotMatch(prerequisiteMarkup(), /Open matching committed settings/);
assert.ok(sourceReads.every(request => request.method === 'GET'));
prerequisiteCases++;
prerequisiteStart = prerequisiteFixture({config:{linuxUse:selection}});
await hooks.renderLinuxInstaller();
await hooks.runLinuxPrimaryAction();
assert.equal(postCount(prerequisiteStart), 1);
assert.ok(requests.at(-1).url.endsWith('build-labview-lvpm-image.yml/dispatches'));
assert.match(prerequisiteMarkup(), /Build Linux LVPM container/);
assert.doesNotMatch(elements.get('lvpmReview').innerHTML, /Next:|id="lvpmBuild"/);
prerequisiteCases++;
prerequisiteStart = prerequisiteFixture({tooling:'lvpm'});
dispatchStatus = 200;
dispatchDetails = {...acceptedDetails};
runResponse = {...identifiedRun};
await hooks.renderLinuxInstaller();
elements.get('linuxInstaller').querySelectorAll().find(input => input.value === 'lvpm').listener();
await hooks.runLinuxPrimaryAction();
await hooks.runLinuxPrimaryAction();
assert.equal(postCount(prerequisiteStart),1);
assert.equal(elements.get('lvpmRunLink').href,'https://github.com/owner/project/actions/runs/' + identifiedRun.id);
assert.equal(hooks.getLinuxOperation().runId,identifiedRun.id);
assert.equal(elements.get('linuxPrimary').disabled,true);
hooks.invalidateLinuxOperation();
prerequisiteCases++;
let approved = 0;
prerequisiteStart = prerequisiteFixture();
hooks.setModal({querySelector:selector => modalFields.get(selector),classList:{remove(){approved++;},add(){}}});
await hooks.renderUpdatePanel();
await hooks.runLinuxPrimaryAction();
assert.equal(postCount(prerequisiteStart), 0);
assert.ok(approved > 0);
assert.doesNotMatch(elements.get('updatePanel').innerHTML, /id="updRun"/);
prerequisiteCases++;
let releaseVipmPost;
activeConfig = 'config:\n  container:\n    use: vipm-default\n';
postBarrier = new Promise(resolve => { releaseVipmPost = resolve; });
modalFields.get('#updConfirmApprove').onclick();
await until(() => postCount(prerequisiteStart) === 1);
assert.equal(elements.get('linuxPrimary').disabled,true);
hooks.renderLinuxPrerequisites();
assert.equal(elements.get('linuxPrimary').disabled,true);
await hooks.runLinuxPrimaryAction();
await hooks.dispatchContainerUpdate();
assert.equal(postCount(prerequisiteStart),1);
releaseVipmPost();
await until(() => elements.get('linuxPrimary').disabled === false);
postBarrier = null;
assert.match(prerequisiteMarkup(), /Review Linux VIPM build/);
prerequisiteCases++;
let dragonDetailCases = 0;
const actualDeclaration = fs.readFileSync(new URL('../example/Example.dragon', root), 'utf8');
assert.match(actualDeclaration, /oglib_boolean\s*=\s*"6\.0\.0\.9"/);
const detailedDragon = {inspection:{status:'parsed', reason:'Declarations parsed by trusted tooling.'}, dependencies:[{
  manager:'vipm', package_id:'oglib_boolean', declared_version:'6.0.0.9',
  platform_status:{windows:{status:'installed',installed_version:'6.0.0.9'},linux:{status:'missing',message:'Not present in the selected Linux worker.'}}
}]};
for (const platform of ['windows', 'linux']) {
  hooks.setState({candidate, platform, inventory:dragonInventory(detailedDragon)});
  hooks.renderDragonFiles([{...monitoredDragon, monitorOn:['windows','linux']}]);
  const markup = elements.get('dragonWrap').innerHTML;
  assert.match(markup, /File present · parsed/);
  assert.match(markup, /oglib_boolean/);
  assert.match(markup, /6\.0\.0\.9/);
  assert.match(markup, new RegExp(platform + ' worker evidence'));
  assert.match(markup, platform === 'windows' ? /Package satisfied/ : /Package missing/);
  assert.doesNotMatch(markup, /File absent/);
  dragonDetailCases++;
}
for (const testCase of [
  {file:{inspection:{status:'unknown',reason:'Declared Dragon file is missing.'}}, expected:/File absent/},
  {file:{error:'Bad TOML <ScRiPt>',inspection:{status:'unknown'}}, expected:/File parse error/},
  {file:{inspection:{status:'unknown',reason:'Backend unavailable'}}, available:false, expected:/File inspection unavailable/},
  {file:detailedDragon, revision:'e'.repeat(40), expected:/File inspection unavailable/},
  {file:{inspection:{status:'parsed'},dependencies:[]}, expected:/No package declarations/}
]) {
  hooks.setState({candidate, revision:testCase.revision, inventory:dragonInventory(testCase.file,testCase.available !== false)});
  hooks.renderDragonFiles([monitoredDragon]);
  const markup = elements.get('dragonWrap').innerHTML;
  assert.match(markup, testCase.expected);
  assert.doesNotMatch(markup, /<script>|Package satisfied/i);
  dragonDetailCases++;
}
for (const status of ['missing','wrong_version','unknown']) {
  const evidence = {...detailedDragon, dependencies:[{...detailedDragon.dependencies[0], platform_status:{linux:{status,message:'stale or unsupported native dependency <SCRIPT>'}}}]};
  hooks.setState({candidate, inventory:dragonInventory(evidence), columns:{linux:{ready:true,packages:new Set(['oglib_boolean-6.0.0.9'])}}});
  hooks.renderDragonFiles([monitoredDragon]);
  assert.match(elements.get('dragonWrap').innerHTML, status === 'missing' ? /Package missing/ : status === 'wrong_version' ? /Version mismatch/ : /Package unverified/);
  assert.doesNotMatch(elements.get('dragonWrap').innerHTML, /Package satisfied|<script>/i);
  dragonDetailCases++;
}
hooks.setState({candidate, inventory:dragonInventory(detailedDragon)});
hooks.renderDragonFiles([{...monitoredDragon, monitored:false, monitorOn:[]}]);
assert.match(elements.get('dragonWrap').innerHTML, /Not monitored on this platform/);
assert.doesNotMatch(elements.get('dragonWrap').innerHTML, /Package satisfied/);
dragonDetailCases++;
if (process.env.LVCI_DRAGON_UI_FIXTURE) {
  hooks.setState({candidate, inventory:{sha:revision, dragon:JSON.parse(process.env.LVCI_DRAGON_UI_FIXTURE)}});
  hooks.renderDragonFiles([{path:'project.dragon',monitorOn:['linux'],monitored:true}]);
  assert.match(elements.get('dragonWrap').innerHTML, /File present · parsed/);
  assert.match(elements.get('dragonWrap').innerHTML, /oglib_boolean/);
  assert.match(elements.get('dragonWrap').innerHTML, /6\.0\.0\.9/);
  assert.doesNotMatch(elements.get('dragonWrap').innerHTML, /Package satisfied/);
  dragonDetailCases++;
}
const integrate = fs.readFileSync(new URL('pages/integrate.html', root), 'utf8');
const sourceBlock = integrate.slice(integrate.indexOf('async function toolingSourceBlock(repo)'), integrate.indexOf('function toolingSourceMessage(repo'));
let sourceGuardReads = 0;
const sourceGuard = vm.createContext({canonical:{repo:'ni/LabVIEW-CI-CD-Toolkit'}, SOURCE_FALLBACK:{repo:'ni/LabVIEW-CI-CD-Toolkit'},
  gh:async () => { sourceGuardReads++; return {ok:true,json:async () => ({fork:true,parent:{full_name:'ni/LabVIEW-CI-CD-Toolkit'}})}; }});
vm.runInContext(sourceBlock + '\nglobalThis.block = toolingSourceBlock;', sourceGuard);
assert.match((await sourceGuard.block('ni/LabVIEW-CI-CD-Toolkit')).why, /tooling source repository/);
assert.equal(sourceGuardReads, 0);
assert.match((await sourceGuard.block('owner/toolkit-fork')).why, /fork of/);
assert.equal(sourceGuardReads, 1);
for (const functionName of ['installToRepo', 'proceedInstall']) {
  const body = integrate.slice(integrate.indexOf('async function ' + functionName + '('));
  assert.match(body.slice(0, 1000), /await toolingSourceBlock\(repo\)/);
}
console.log('Linux monitoring prerequisites: ' + prerequisiteCases + ' actual-function cases; Dragon file/package details: ' + dragonDetailCases + ' renderer cases passed.');
console.log('LVPM UX: existing dispatch/inventory/freshness/Dragon/OS-safety and monitoring regressions passed; ' + progressCases + ' dispatch identity/progress/error/duplicate/stale-context cases and ' + visualChecks + ' visual progress checks passed.');
