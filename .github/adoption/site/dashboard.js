'use strict';

const $ = (id) => document.getElementById(id);
const node = (name, text, css) => {
  const el = document.createElement(name);
  if (text !== undefined) el.textContent = text;
  if (css) el.className = css;
  return el;
};
const MS = 86400000;
const iso = (d) => d.toISOString().slice(0, 10);
const time = (day) => new Date(day + 'T00:00:00Z');
const shift = (day, n) => iso(new Date(+time(day) + n * MS));
const length = (a, b) => Math.round((time(b) - time(a)) / MS) + 1;
const fmt = new Intl.NumberFormat('en-US').format;
const date = (day, options = {month: 'short', day: 'numeric', year: 'numeric'}) =>
  new Intl.DateTimeFormat('en-US', {...options, timeZone: 'UTC'}).format(time(day));
const dates = (a, b) => a === b ? date(a) : date(a, {month:'short',day:'numeric', ...(a.slice(0,4) !== b.slice(0,4) ? {year:'numeric'} : {})}) + ' – ' + date(b);
const valid = (d) => typeof d === 'string' && /^\d{4}-\d{2}-\d{2}$/.test(d) &&
  Number.isFinite(+time(d)) && iso(time(d)) === d;

const METRICS = {
  clients: {name:'Verified public clients', icon:'◎', kind:'sample', desc:'Latest observed number of public repositories found using the toolkit in each period. Includes demo and scratch repositories; private clients and unindexed repositories are outside coverage.'},
  views: {name:'Repository views', icon:'◉', kind:'traffic', desc:'Total GitHub repository page views on observed days. Hatched periods have no observation; lighter bars have partial daily coverage.'},
  clones: {name:'Repository clones', icon:'⇄', kind:'traffic', desc:'Full Git clones on observed days. This may include CI, bots, and repeated clones, so the result cannot estimate installations or unique people.'},
  stars: {name:'Retained stars added', icon:'☆', kind:'event', desc:'Dates when accounts that still star the repository added their current star. Unstars and historical stars no longer present are unavailable.'},
  forks: {name:'Existing forks created', icon:'⑂', kind:'event', desc:'Creation dates of forks GitHub still lists. Deleted and detached forks are outside coverage.'},
  releases: {name:'Releases published', icon:'◆', kind:'event', desc:'Published releases GitHub still lists, including prereleases and backfilled releases on their publication dates.'},
  merges: {name:'Pull requests merged', icon:'⎇', kind:'event', desc:'Merged pull requests still visible in the GitHub repository, dated by their merge time.'},
};
let data, selected = 'all', metric, bucketSetting = 'auto', from, through, min, max;
const isTraffic = (k) => METRICS[k].kind === 'traffic';

function rows(k) {
  return k === 'clients' ? data.clients : isTraffic(k) ? data.traffic[k] : data.events[k];
}
function interval(k, a, b) {
  const series = rows(k);
  const entries = Object.keys(series).filter((day) => day >= a && day <= b).sort();
  if (METRICS[k].kind === 'sample') {
    const last = entries.at(-1);
    return {value:last ? series[last] : null, entries:entries.length, last};
  }
  const n = entries.reduce((sum, day) => sum + (isTraffic(k) ? series[day][0] : series[day]), 0);
  return {value:entries.length || !isTraffic(k) ? n : null, entries:entries.length};
}
function note(k, stats) {
  if (k === 'clients') return stats.last ? 'Observed ' + date(stats.last) : 'No observation in this window';
  if (isTraffic(k)) return stats.entries ? fmt(stats.entries) + ' of ' + fmt(length(from, through)) + ' days covered' : 'No traffic data in this window';
  return {stars:'Still starred today', forks:'Forks still listed', releases:'Includes prereleases', merges:'By merge date'}[k];
}
function renderCards() {
  const active = document.activeElement?.dataset?.metric;
  $('cards').replaceChildren(...Object.keys(METRICS).filter((k) => k !== 'merges').map((k) => {
    const stats = interval(k, from, through);
    const b = node('button', undefined, 'kpi' + (metric === k ? ' is-active' : ''));
    b.dataset.metric = k;
    b.setAttribute('aria-pressed', String(metric === k));
    const label = node('span', undefined, 'label');
    const icon = node('span', METRICS[k].icon, 'ico');
    icon.setAttribute('aria-hidden','true');
    label.append(icon, document.createTextNode(METRICS[k].name));
    b.append(label, node('span', stats.value === null ? '—' : fmt(stats.value), 'value'), node('small', note(k, stats)));
    b.addEventListener('click', () => { metric = k; render(); });
    return b;
  }));
  if (active) $('cards').querySelector('[data-metric="' + active + '"]')?.focus();
}
function nextBoundary(d, mode) {
  if (mode === 'day') return shift(d, 1);
  const x = time(d);
  if (mode === 'week') return shift(d, 7 - ((x.getUTCDay() + 6) % 7));
  if (mode === 'year') return String(x.getUTCFullYear() + 1) + '-01-01';
  return iso(new Date(Date.UTC(x.getUTCFullYear(), x.getUTCMonth() + 1, 1)));
}
function series() {
  const days = length(from, through);
  let mode = bucketSetting === 'auto' ? days > 1500 ? 'year' : days > 200 ? 'month' : days > 35 ? 'week' : 'day' : bucketSetting;
  // Keep very long custom selections readable and cap draw size.
  if (mode === 'day' && days > 730) mode = 'week';
  if (mode === 'week' && days > 5110) mode = 'month';
  const bins = [];
  for (let start = from; start <= through;) {
    const end = [shift(nextBoundary(start, mode), -1), through].sort()[0];
    bins.push({start, end, ...interval(metric, start, end), days:length(start, end)});
    start = shift(end, 1);
  }
  return {mode, bins};
}
const svgNS = 'http://www.w3.org/2000/svg';
function svg(name, attrs, value) {
  const el = document.createElementNS(svgNS, name);
  for (const [k, v] of Object.entries(attrs)) el.setAttribute(k, v);
  if (value !== undefined) el.textContent = value;
  return el;
}
function pointLabel(bin) {
  const value = bin.value === null ? 'No observation' : fmt(bin.value);
  const coverage = isTraffic(metric) ? ' · ' + bin.entries + '/' + bin.days + ' days covered'
    : bin.last ? ' · Observed ' + date(bin.last) : '';
  return dates(bin.start, bin.end) + ': ' + value + coverage;
}
function renderChart() {
  const {bins, mode} = series();
  const covering = bins.filter((b) => b.value !== null);
  const chart = $('chart');
  const defaultMessage = METRICS[metric].name + ' · ' + mode + ' grouping · UTC';
  const live = $('chart-status');
  live.textContent = defaultMessage;
  $('chart-description').textContent = METRICS[metric].desc;
  chart.replaceChildren();
  if (!covering.length) {
    chart.append(node('div', 'No observations in this range. Try a different time window or metric.', 'empty'));
  } else {
    const W = 1120, H = 280, L = 62, R = 18, T = 18, B = 42;
    const plotW = W - L - R, plotH = H - T - B;
    const chartMax = Math.max(1, ...covering.map((b) => b.value));
    const scale = 10 ** Math.floor(Math.log10(chartMax / 4));
    const step = Math.max(1, Math.ceil(chartMax / 4 / scale) * scale);
    const top = step * 4, bottom = H - B;
    const image = svg('svg', {viewBox:'0 0 1120 280',class:'trend-chart',role:'group','aria-label':defaultMessage});
    const pattern = svg('pattern', {id:'hatch',width:7,height:7,patternUnits:'userSpaceOnUse',patternTransform:'rotate(35)'});
    pattern.append(svg('rect',{width:7,height:7,fill:'#f4f7f5'}),svg('path',{d:'M0 0V7',stroke:'#dce8df','stroke-width':2}));
    const defs = svg('defs',{}); defs.append(pattern); image.append(defs);
    for (let i = 0; i <= 4; i++) {
      const y = bottom - i * plotH / 4;
      image.append(svg('line',{x1:L,x2:W-R,y1:y,y2:y,class:'rule'}),
        svg('text',{x:L-10,y:y+4,'text-anchor':'end'},fmt(i * step)));
    }
    const w = plotW / bins.length, stride = Math.max(1, Math.ceil(bins.length / 8));
    bins.forEach((b, i) => {
      const x = L + i * w;
      const h = b.value === null ? 0 : b.value / top * plotH;
      const margin = Math.min(w * .12, 4);
      const gw = Math.max(1, Math.min(w - 2 * margin, 62)), gx = x + (w-gw)/2;
      const g = svg('g',{class:'focusable',role:'img','aria-label':pointLabel(b),tabindex:-1});
      if (i === 0) g.setAttribute('tabindex','0');
      const engage = () => { live.textContent = pointLabel(b); };
      g.addEventListener('pointerenter',engage);
      g.addEventListener('pointerleave',() => { if (document.activeElement !== g) live.textContent = defaultMessage; });
      g.addEventListener('focus',engage);
      g.addEventListener('blur',() => { live.textContent = defaultMessage; });
      g.addEventListener('keydown',(e) => {
        const offset = e.key === 'ArrowLeft' ? -1 : e.key === 'ArrowRight' ? 1 : 0;
        if (!offset) return;
        e.preventDefault();
        const next = gs[i + offset];
        if (next) {g.setAttribute('tabindex','-1');next.setAttribute('tabindex','0');next.focus();}
      });
      g.append(svg('rect',{x,y:T,width:w,height:plotH,class:'target',fill:'transparent'}));
      if (b.value === null) {
        g.append(svg('rect',{x:x+margin,y:T,width:Math.max(1,w-2*margin),height:plotH,class:'missing','pointer-events':'none'}));
      } else if (b.value === 0) {
        g.append(svg('rect',{x:gx,y:bottom-3,width:gw,height:3,rx:1,class:'baseline'}));
      } else {
        g.append(svg('rect',{x:gx,y:bottom-h,width:gw,height:h,rx:Math.min(3,gw/3),class:'bar' + (isTraffic(metric) && b.entries < b.days ? ' partial' : '')}));
      }
      image.append(g);
      if (i % stride === 0) image.append(svg('text',{x:x+w/2,y:H-17,'text-anchor':'middle'},
        date(b.start, mode === 'year' ? {year:'numeric'} : mode === 'month' ? {month:'short',year:'2-digit'} : {month:'short',day:'numeric'})));
    });
    const gs = [...image.querySelectorAll('.focusable')];
    chart.append(image);
  }
  const table = $('chart-table');
  const head = document.createElement('tr');
  ['UTC period','Value','Coverage'].forEach((column) => head.append(node('th',column)));
  table.tHead.replaceChildren(head);
  table.tBodies[0].replaceChildren(...bins.map((b) => {
    const tr = document.createElement('tr');
    tr.append(node('td',dates(b.start,b.end)),node('td',b.value === null ? '—' : fmt(b.value)),
      node('td',isTraffic(metric) ? b.entries + ' / ' + b.days + ' days' : b.last ? date(b.last) : b.value === null ? 'No observation' : 'Dated events'));
    return tr;
  }));
}
function renderSources() {
  const trafficCoverage = (k) => {
    const days = Object.keys(rows(k)).sort();
    return days.length ? fmt(days.length) + ' observed days · ' + dates(days[0], days.at(-1)) : 'No days archived';
  };
  const clients = Object.keys(data.clients).sort();
  const sources = [
    ['Views & clones', 'Archived from GitHub', trafficCoverage('views') + ' for views. ' + trafficCoverage('clones') + ' for clones. GitHub provides rolling traffic only. Blank days are unknown; a zero appears only if GitHub supplied it. Daily unique counts cannot be summed to get unique people.'],
    ['Public client discovery', clients.length ? 'Observed' : 'Unavailable', clients.length ? `${fmt(clients.length)} observations from ${dates(clients[0], clients.at(-1))}. The legacy public registry was recovered from Pages history. Later topic searches validate each client against its public toolkit catalog. Private installs and clients without a public discovery signal are uncounted.` : 'This client discovery series applies to the canonical toolkit repository.'],
    ['Stars, forks, releases & PRs', 'Dated history', 'Reconstructed from records GitHub currently exposes, starting at repository creation. Removed records, unstars, or deleted forks are missing. This series can change as records disappear.'],
    ['Asset downloads', 'Current counter', 'Counters on uploaded assets still present. Auto-generated source ZIPs, tarballs, and container pulls are outside this measure. GitHub does not supply historical dates for these downloads.'],
  ];
  $('sources').replaceChildren(...sources.map(([name, status, desc]) => {
    const row = node('div',undefined,'source-row');
    const h = node('div',undefined,'source-head');
    h.append(node('strong',name),node('span',status,'tag')); row.append(h,node('p',desc)); return row;
  }));
  const source = node('a','GitHub traffic API');
  source.href = 'https://docs.github.com/en/rest/metrics/traffic';
  const reference = node('p');
  reference.append(source);
  $('sources').firstElementChild.append(reference);
  const c = data.current;
  $('snapshot').replaceChildren(...[
    ['Repository created',date(min)],['Current stars',fmt(c.stars)],['Current forks',fmt(c.forks)],
    ['Watchers',fmt(c.watchers)],['Latest published release',data.latest_release?.tag ?? '—'],
    ['Uploaded release assets',fmt(c.assets)],['Asset downloads',c.assets ? fmt(c.downloads) : 'No uploaded assets'],
  ].map(([key,value]) => {const row=node('div');row.append(node('dt',key),node('dd',value));return row;}));
  const ctx = $('traffic-note');
  ctx.replaceChildren(node('strong','Latest GitHub traffic windows'));
  for (const k of ['views','clones']) {
    const w = data.traffic_windows[k];
    ctx.append(node('p',w ? `${METRICS[k].name}: ${fmt(w.count)} total / ${fmt(w.uniques)} unique, ${dates(w.from,w.to)}.`
      : `No deduplicated snapshot for ${k}.`));
  }
}
function updateUrl() {
  const params = new URLSearchParams();
  if (location.protocol === 'file:') return;
  if (selected !== 'all') params.set('range',selected);
  if (selected === 'custom') {params.set('from',from);params.set('through',through);}
  if (metric !== defaultMetric()) params.set('metric',metric);
  if (bucketSetting !== 'auto') params.set('bucket',bucketSetting);
  const query = params.toString();
  history.replaceState(null,'',location.pathname + (query ? '?' + query : '') + location.hash);
}
function defaultMetric() {return Object.keys(data.clients).length ? 'clients' : 'stars';}
function render() {
  $('window-label').textContent = dates(from,through) + ' · UTC';
  $('metric').value = metric;
  $('bucket').value = bucketSetting;
  for (const btn of $('presets').querySelectorAll('button')) btn.setAttribute('aria-pressed',btn.dataset.range === selected);
  renderCards();
  renderChart();
  updateUrl();
}
function preset(k) {
  selected = k; through = max;
  from = k === 'all' ? min : [shift(max,1-Number(k)),min].sort().at(-1);
}
function csv() {
  const out = [['date_utc','verified_public_clients_observed','repository_views','daily_unique_views','repository_clones','daily_unique_cloners','retained_stars_added','existing_forks_created','releases_published','pulls_merged']];
  for (let d=from; d<=through; d=shift(d,1)) {
    out.push([d,data.clients[d] ?? '',...(data.traffic.views[d] ?? ['','']),...(data.traffic.clones[d] ?? ['','']),
      ...['stars','forks','releases','merges'].map(k => data.events[k][d] ?? 0)]);
  }
  const url = URL.createObjectURL(new Blob([out.map(r=>r.join(',')).join('\r\n') + '\r\n'], {type:'text/csv;charset=utf-8'}));
  const a = node('a'); a.href = url; a.download = `adoption-${from}-to-${through}.csv`; a.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}
async function init() {
  const embedded = $('embedded-snapshot');
  if (embedded) data = JSON.parse(embedded.textContent);
  else {
    const response = await fetch('data.json', {cache:'no-cache'});
    if (!response.ok) throw new Error('Snapshot unavailable');
    data = await response.json();
  }
  min = data.created;
  max = data.updated?.slice(0,10);
  if (data.schema !== 1 || !valid(min) || !valid(max) || min > max || !/^[\w.-]+\/[\w.-]+$/.test(data.repo)) throw new Error('Unsupported snapshot');
  $('repo-name').textContent = data.repo.toLowerCase() === 'ni/labview-ci-cd-toolkit'
    ? 'LabVIEW CI/CD Toolkit' : data.repo;
  $('repo-link').href = 'https://github.com/' + data.repo;
  $('updated').textContent = date(max) + ' · ' + data.updated.slice(11,16) + ' UTC';
  const p = new URLSearchParams(location.search);
  const desired = p.get('range');
  preset(['14','30','90','365'].includes(desired) ? desired : 'all');
  if (desired === 'custom' && valid(p.get('from')) && valid(p.get('through')) &&
      p.get('from') >= min && p.get('through') <= max && p.get('from') <= p.get('through')) {
    selected = 'custom'; from = p.get('from'); through = p.get('through');
    $('custom').hidden = false;
  }
  for (const id of ['from','through']) {$(id).min=min; $(id).max=max; $(id).value=id==='from'?from:through;}
  metric = Object.hasOwn(METRICS,p.get('metric')) ? p.get('metric') : defaultMetric();
  if (['day','week','month','year'].includes(p.get('bucket'))) bucketSetting=p.get('bucket');
  $('metric').replaceChildren(...Object.entries(METRICS).map(([id,m]) => new Option(m.name,id)));
  $('metric').addEventListener('change', (e) => {metric=e.target.value;render();});
  $('bucket').addEventListener('change', (e) => {bucketSetting=e.target.value;render();});
  $('presets').addEventListener('click', (e) => {
    const btn=e.target.closest('button');if (!btn) return;
    if (btn.dataset.range==='custom') {$('custom').hidden=false;$('from').value=from;$('through').value=through;$('from').focus();}
    else { $('custom').hidden=true; preset(btn.dataset.range);render(); }
  });
  for (const id of ['from','through']) $(id).addEventListener('input',() => $('through').setCustomValidity(''));
  $('custom').addEventListener('submit',(e)=>{
    e.preventDefault();
    const f=$('from').value,t=$('through').value;
    if (f>t) {$('through').setCustomValidity('Choose an end date on or after the start date.');$('through').reportValidity();return;}
    selected='custom';from=f;through=t;render();
  });
  $('export').addEventListener('click',csv);
  renderSources();
  render();
  $('dashboard').hidden = false;
}
init().catch(() => {
  $('updated').textContent='Snapshot unavailable';
  $('load-message').className='load-error';
  $('load-message').textContent='The data snapshot could not be loaded. Reload the page, or check the repository’s adoption workflow.';
});
