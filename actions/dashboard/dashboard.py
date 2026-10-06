#!/usr/bin/env python3
import html, json, os, re, sys, time, urllib.request, urllib.error, zipfile
from urllib.parse import quote
from xml.etree import ElementTree as ET

token    = os.environ['GH_TOKEN']
repo     = os.environ['REPO']
pages_url = os.environ['PAGES_URL']

# API health: a persistent failure while fetching commit history must NOT
# blank the good dashboard (see the fetch_commits guard). gh_get sets this
# when a transient failure (rate limit / 5xx / network) survives its retries.
_API = {'degraded': False}

def _retry_delay(err, attempt):
    # Prefer the server's own guidance: Retry-After (seconds), or seconds until
    # X-RateLimit-Reset for a primary rate limit; else capped exponential
    # backoff. Bounded so a build never hangs on a long reset window.
    hdrs = getattr(err, 'headers', None)
    if hdrs is not None:
        ra = hdrs.get('Retry-After')
        if ra and str(ra).strip().isdigit():
            return min(30, max(1, int(ra)))
        if str(hdrs.get('X-RateLimit-Remaining', '')).strip() == '0':
            reset = hdrs.get('X-RateLimit-Reset')
            if reset and str(reset).strip().isdigit():
                return min(30, max(1, int(reset) - int(time.time())))
    return min(30, 2 ** attempt)

def gh_get(path, _tries=4):
    # NOTE: an empty path must hit the bare repo endpoint (…/repos/{repo}); a
    # trailing slash (…/repos/{repo}/) makes GitHub return 404, which silently
    # broke get_default_branch() below. Only join the '/' when there IS a path.
    url = f"https://api.github.com/repos/{repo}" + (f"/{path}" if path else "")
    req = urllib.request.Request(url, headers={
        'Authorization': f'token {token}',
        'Accept': 'application/vnd.github+json',
        'X-GitHub-Api-Version': '2022-11-28',
    })
    # Rate limits (primary 403 with x-ratelimit-remaining:0, secondary 403/429)
    # and 5xx are TRANSIENT: a burst of CI activity easily trips a secondary
    # rate limit, and a single flaky call used to return None and silently
    # blank the dashboard. Retry those with backoff (honoring Retry-After /
    # X-RateLimit-Reset); a dropped connection / timeout is retried too. Only a
    # PERSISTENT failure returns None AND marks the API degraded, which makes
    # the build refuse to publish a truncated dashboard (see fetch_commits).
    # Once degraded, further calls try once (the limit will not clear in 30s).
    n_tries = 1 if _API['degraded'] else _tries
    for attempt in range(n_tries):
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            transient = e.code in (403, 429) or 500 <= e.code < 600
            if transient and attempt < n_tries - 1:
                delay = _retry_delay(e, attempt)
                print(f"  HTTP {e.code} for {path}; retrying in {delay}s "
                      f"(attempt {attempt + 1}/{n_tries})", file=sys.stderr)
                time.sleep(delay)
                continue
            print(f"  HTTP {e.code} for {path}", file=sys.stderr)
            if transient:
                _API['degraded'] = True
            return None
        except (urllib.error.URLError, OSError) as e:
            if attempt < n_tries - 1:
                time.sleep(2 ** attempt)
                continue
            print(f"  network error for {path}: {e}", file=sys.stderr)
            _API['degraded'] = True
            return None

# ── Resolve the repo's default branch (NOT hard-coded 'main') ─────
# Consumers commonly use 'master' or other default branches. Hard-coding 'main'
# made fetch_commits() request commits from a branch that doesn't exist, so the
# dashboard came up EMPTY on every non-'main' repo. Query the repo's real default
# branch once (cached); fall back to 'main' only if the lookup fails.
_default_branch = None

def get_default_branch():
    global _default_branch
    if _default_branch is not None:
        return _default_branch
    repo_info = gh_get('')
    _default_branch = repo_info.get('default_branch', 'main') if repo_info else 'main'
    return _default_branch

# ── Fetch a JSON file straight from the deployed Pages site ─────
# Reads static gallery/report JSON deployed alongside the dashboard (the snapshot
# blob index, masscompile summaries, …). Returns None on any error (missing file,
# propagation lag, etc.).
def http_json(url):
    req = urllib.request.Request(url, headers={'Accept': 'application/json'})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.load(r)
    except Exception:
        return None

# ── Snapshot coverage: which of a revision's VIs have a rendered snapshot ─────
# Snapshots are content-addressed by git blob SHA under vi-snapshots/by-blob/, so
# whether a revision "has snapshots" is answered by the snapshot store itself —
# NOT by VIDiff (which only renders CHANGED VIs and is what this column used to
# read, leaving it blank for every revision that had snapshots but no diff run).
# The set of blobs that have a deployed snapshot is read ONCE: a single blobs.json
# index when build-snapshots.ps1 has written one, else the union of every manifest
# listed in commits.json (also exact — build-gallery writes a full-tree manifest
# per commit and the deploy keeps every by-blob file). A revision's coverage is
# then its VI blobs intersected with that set, which also reveals partial coverage
# (older revisions whose changed VIs were never rendered).
_rendered_blobs_cache = {'set': None}
def rendered_blobs():
    if _rendered_blobs_cache['set'] is not None:
        return _rendered_blobs_cache['set']
    blobs = set()
    idx = http_json(f"{pages_url}/vi-snapshots/blobs.json")
    if isinstance(idx, list):
        blobs = {b for b in idx if isinstance(b, str)}
    elif isinstance(idx, dict) and isinstance(idx.get('blobs'), list):
        blobs = {b for b in idx['blobs'] if isinstance(b, str)}
    else:
        commits = http_json(f"{pages_url}/vi-snapshots/commits.json")
        if isinstance(commits, list):
            for c in commits:
                csha = c.get('sha') if isinstance(c, dict) else None
                if not csha:
                    continue
                man = http_json(f"{pages_url}/vi-snapshots/{csha}/manifest.json")
                if isinstance(man, list):
                    for e in man:
                        if isinstance(e, dict) and e.get('blob'):
                            blobs.add(e['blob'])
    _rendered_blobs_cache['set'] = blobs
    return blobs

_snap_cov_cache = {}
def snapshot_coverage(sha):
    # (have, total) for a revision: how many of its VIs have a rendered snapshot
    # vs how many VIs it has. total = vi_tree (the same VI set the browser shows);
    # have = those whose content blob is in the rendered set.
    if sha in _snap_cov_cache:
        return _snap_cov_cache[sha]
    vis = vi_tree(sha)
    total = len(vis)
    have = 0
    if total:
        rb = rendered_blobs()
        if rb:
            have = sum(1 for v in vis if v.get('blob') in rb)
    res = (have, total)
    _snap_cov_cache[sha] = res
    return res

_json_blobs_cache = {}
def rendered_json_blobs(platform):
    key = 'windows' if platform == 'windows' else 'linux'
    if key in _json_blobs_cache:
        return _json_blobs_cache[key]
    name = 'json-blobs.windows.json' if key == 'windows' else 'json-blobs.json'
    data = http_json(f"{pages_url}/vi-snapshots/{name}")
    blobs = set()
    if isinstance(data, list):
        blobs = {b for b in data if isinstance(b, str)}
    _json_blobs_cache[key] = blobs
    return blobs

_snap2_cov_cache = {}
def snapshot2_coverage(sha, platform):
    key = (sha, platform)
    if key in _snap2_cov_cache:
        return _snap2_cov_cache[key]
    vis = [v for v in vi_tree(sha) if str(v.get('vi_rel', '')).lower().endswith('.vi')]
    total = len(vis)
    have = 0
    if total:
        rb = rendered_json_blobs(platform)
        if rb:
            have = sum(1 for v in vis if v.get('blob') in rb)
    res = (have, total)
    _snap2_cov_cache[key] = res
    return res

_mc_cache = {}
def masscompile_summary(sha, platform='windows'):
    # {total, ok, bad, percent, status, exit, duration} written by masscompile.ps1
    # and deployed alongside the report at masscompile/<sha>/summary.json (Windows)
    # and masscompile/<sha>/linux/summary.json (Linux). Lets the Mass Compile
    # column show the % of project VIs that compiled instead of a binary pass/fail
    # (most VIs compile even when a few depend on libraries absent from the CI
    # image), and lets the cell report Windows and Linux separately since the two
    # platforms can compile a different set of VIs.
    key = (sha, platform)
    if key in _mc_cache:
        return _mc_cache[key]
    sub = 'linux/' if platform == 'linux' else ''
    data = http_json(f"{pages_url}/masscompile/{sha}/{sub}summary.json")
    _mc_cache[key] = data if isinstance(data, dict) else None
    return _mc_cache[key]

_builds_cache = {}
def builds_summary(sha, platform='windows'):
    # {platform, status, total, built, skipped, failed, duration, labview_version,
    # specs[...]} written by build-binaries.ps1 / build-binaries.sh and deployed
    # alongside the Builds report at builds/<sha>/summary.json (Windows) and
    # builds/<sha>/linux/summary.json (Linux). Lets the Builds column show a split
    # pill of succeeded vs failed build specifications, per platform.
    key = (sha, platform)
    if key in _builds_cache:
        return _builds_cache[key]
    sub = 'linux/' if platform == 'linux' else ''
    data = http_json(f"{pages_url}/builds/{sha}/{sub}summary.json")
    _builds_cache[key] = data if isinstance(data, dict) else None
    return _builds_cache[key]

_ut_cache = {}
def unit_tests_summary(sha, platform='windows'):
    # build-unittest-report.py deploys the same model that renders the friendly
    # report. Reading it lets the dashboard show the actual failure percentage
    # instead of a binary pass/fail status. Linux results (when present) live under
    # unit-tests/<sha>/linux/ so the cell can report both platforms.
    key = (sha, platform)
    if key in _ut_cache:
        return _ut_cache[key]
    sub = 'linux/' if platform == 'linux' else ''
    data = http_json(f"{pages_url}/unit-tests/{sha}/{sub}results.json")
    summary = (data or {}).get('summary') if isinstance(data, dict) else None
    _ut_cache[key] = summary if isinstance(summary, dict) else None
    return _ut_cache[key]

_via_cache = {}
def vi_analyzer_summary(sha, platform='windows'):
    # {meta, summary:{vis_analyzed, tests_run, passed, failed, skipped, findings,
    # vis_with_findings}, errors:{vi_not_loadable, ...}, rule_counts[...]} written by
    # build-analyzer-report.py and deployed alongside the report at
    # vi-analyzer/<sha>/summary.json (Windows) and vi-analyzer/<sha>/linux/summary.json
    # (Linux). Lets the VI Analyzer column show the finding count per platform (and
    # flag a degraded analysis) instead of a binary pass/fail, and report Windows and
    # Linux separately since the two containers can analyze a different set of VIs.
    key = (sha, platform)
    if key in _via_cache:
        return _via_cache[key]
    sub = 'linux/' if platform == 'linux' else ''
    data = http_json(f"{pages_url}/vi-analyzer/{sha}/{sub}summary.json")
    _via_cache[key] = data if isinstance(data, dict) else None
    return _via_cache[key]

def _pct_threshold(value, default):
    try:
        return max(0, min(100, int(float(str(value).strip()))))
    except Exception:
        return default

def unit_test_thresholds():
    # Quality gates are pass-rate thresholds, which is the common way to express
    # test health even though the badge text itself reports the failed-test rate.
    # Defaults: green at 100% passed, yellow at >=80%, red below 80%.
    vals = {'greenAtLeast': 100, 'yellowAtLeast': 80, 'redBelow': 80}
    try:
        in_ut = in_thresholds = in_passed = False
        for raw in open('.github/labview-ci.yml', encoding='utf-8'):
            line = raw.replace('\t', '    ').rstrip('\n')
            if re.match(r'^unitTests:\s*$', line):
                in_ut = True; in_thresholds = False; in_passed = False; continue
            if in_ut and re.match(r'^\S', line):
                break
            if not in_ut:
                continue
            if re.match(r'^\s{2}thresholds:\s*$', line):
                in_thresholds = True; in_passed = False; continue
            if in_thresholds and re.match(r'^\s{4}(passedPercent|passPercent|passRate):\s*$', line):
                in_passed = True; continue
            if in_passed:
                m = re.match(r'^\s{6}(greenAtLeast|yellowAtLeast|redBelow):\s*([^#\s]+)', line)
                if m:
                    vals[m.group(1)] = _pct_threshold(m.group(2), vals[m.group(1)])
                    continue
                if re.match(r'^\s{0,5}\S', line):
                    in_passed = False
    except Exception:
        pass
    vals['greenAtLeast'] = max(vals['greenAtLeast'], vals['yellowAtLeast'])
    vals['redBelow'] = min(vals['redBelow'], vals['yellowAtLeast'])
    return vals

UNIT_TEST_THRESHOLDS = unit_test_thresholds()

def unit_test_badge_kind(pass_pct):
    if pass_pct >= UNIT_TEST_THRESHOLDS['greenAtLeast']:
        return 'pass'
    if pass_pct < UNIT_TEST_THRESHOLDS['redBelow']:
        return 'fail'
    return 'warn'

def masscompile_thresholds():
    # The Mass Compile badge shows the % of project VIs that compiled. Colour it
    # with configurable thresholds (like the Unit Tests column) instead of a
    # near-flat amber. Defaults: green >= 95% compiled, yellow >= 80%, red below
    # 80%. Read from .github/labview-ci.yml under massCompile.thresholds. A true
    # compile failure (LabVIEW could not finish the compile) is handled
    # separately and always renders as a solid bright-red pill.
    vals = {'greenAtLeast': 95, 'yellowAtLeast': 80, 'redBelow': 80}
    try:
        in_mc = in_thresholds = in_pct = False
        for raw in open('.github/labview-ci.yml', encoding='utf-8'):
            line = raw.replace('\t', '    ').rstrip('\n')
            if re.match(r'^massCompile:\s*$', line):
                in_mc = True; in_thresholds = False; in_pct = False; continue
            if in_mc and re.match(r'^\S', line):
                break
            if not in_mc:
                continue
            if re.match(r'^\s{2}thresholds:\s*$', line):
                in_thresholds = True; in_pct = False; continue
            if in_thresholds and re.match(r'^\s{4}(compiledPercent|compilePercent|percent):\s*$', line):
                in_pct = True; continue
            if in_pct:
                m = re.match(r'^\s{6}(greenAtLeast|yellowAtLeast|redBelow):\s*([^#\s]+)', line)
                if m:
                    vals[m.group(1)] = _pct_threshold(m.group(2), vals[m.group(1)])
                    continue
                if re.match(r'^\s{0,5}\S', line):
                    in_pct = False
    except Exception:
        pass
    vals['greenAtLeast'] = max(vals['greenAtLeast'], vals['yellowAtLeast'])
    vals['redBelow'] = min(vals['redBelow'], vals['yellowAtLeast'])
    return vals

MASSCOMPILE_THRESHOLDS = masscompile_thresholds()

def masscompile_badge_kind(percent, status=None):
    # Returns (chip-kind, solid) for a Mass Compile percentage. A true failure
    # (LabVIEW could not complete the compile) is always a solid, bright-red pill
    # regardless of the numeric value; otherwise colour by the configurable
    # compiled-percentage thresholds (green/yellow/red).
    if status == 'failed':
        return 'fail', True
    if percent >= MASSCOMPILE_THRESHOLDS['greenAtLeast']:
        return 'pass', False
    if percent >= MASSCOMPILE_THRESHOLDS['yellowAtLeast']:
        return 'warn', False
    return 'fail', False

# LabVIEW / NI source-file extensions. A revision that touches one of
# these (outside the CI tooling — see below) is a change to the actual
# project, as opposed to a CI/docs/tooling revision.
LV_SOURCE_EXTS = (
    '.vi', '.vit', '.ctl', '.ctt', '.lvclass', '.lvlib', '.lvlibp',
    '.lvproj', '.xctl', '.xnode', '.vipc', '.vip', '.llb', '.mnu', '.lvtest',
)

# Directories that hold CI TOOLING (not the LabVIEW project): vendored workflows
# + scripts under .github/, the shared composite actions (whose helper VIs, e.g.
# actions/vidiff/PrintToSingleFileHtml/*.vi, must NOT count as project work), the
# runtime tooling checkout (_lvci/), and build output. A VI under any of these is
# tooling, so a commit touching only these is a CI revision — keeping the
# CI-vs-project split correct even though the tooling itself ships VIs.
TOOLING_PREFIXES = ('.github/', 'actions/', '_lvci/', 'ci-out/', 'build/')

# External dependency manifests (VI Package Configuration / VI Package). A
# revision whose ONLY project-source change is to these touches no actual VIs
# or LabVIEW code — it just bumps the add-on dependencies the project pulls in.
# These can be filtered out of the dashboard (a dependency-only commit changes no
# VIs, so it never produces per-VI CI results).
DEP_EXTS = ('.vipc', '.vip')

# ── Classify a commit: does it touch project LabVIEW source? ─────
# Cached so the paged fetch below and the row loop share ONE detail call per
# commit (the file list comes from the per-commit endpoint).
_classify_cache = {}
def classify_commit(sha):
    if sha in _classify_cache:
        return _classify_cache[sha]
    detail = gh_get(f'commits/{sha}') or {}
    files = [f['filename'] for f in (detail.get('files') or [])]
    # Project LabVIEW source touched by this commit (excluding the CI tooling's
    # own helper VIs under .github/, actions/, etc.).
    proj_src = [f for f in files
                if f.lower().endswith(LV_SOURCE_EXTS) and not f.startswith(TOOLING_PREFIXES)]
    is_proj = bool(proj_src)
    # A "dependency-only" revision is a project revision whose every project-source
    # change is an external dependency manifest (.vipc/.vip) — i.e. no real VI,
    # control, class, library or project file changed.
    is_dep_only = is_proj and all(f.lower().endswith(DEP_EXTS) for f in proj_src)
    info = {'files': files, 'is_project': is_proj, 'is_dep_only': is_dep_only}
    _classify_cache[sha] = info
    return info

# ── Fetch commits, deep enough to surface the project's own history ─────
# The status table needs RECENT commits (for badges), but the project's revisions
# must stay visible even when a long CI/tooling sprint fills the most-recent slots
# on main. So fetch the recent window, then keep paging — classifying each commit —
# until enough project revisions are collected (or a hard scan cap is hit). Beyond
# the recent window only PROJECT revisions are kept, so the deeper scan surfaces
# project history without flooding the table with old CI commits.
#
# The cap must stay well ahead of the tooling-to-project commit ratio: EVERY CI
# run (dashboard rebuilds, catalog bumps, snapshot backfills, …) adds tooling
# commits to main, so the project's earliest revisions sink deeper over time. If
# the cap is too tight it silently truncates the OLDEST project revisions off the
# bottom of the table (the table appears to "get shorter" as CI activity grows).
# The paging still stops early once _PROJECT_TARGET project revisions are found or
# history is exhausted, so the cap only ever engages on a genuinely huge history.
_RECENT_WINDOW  = 100   # always keep at least this many most-recent commits
_PROJECT_TARGET = 30    # keep paging until this many project revisions are found
_SCAN_CAP       = 3000  # never classify more than this many commits (cost guard)
def fetch_commits():
    out, n_proj, n_scanned, page = [], 0, 0, 1
    branch = get_default_branch()
    while n_scanned < _SCAN_CAP and not _API['degraded']:
        batch = gh_get(f'commits?sha={branch}&per_page=100&page={page}') or []
        if not batch:
            break
        for c in batch:
            n_scanned += 1
            info = classify_commit(c['sha'])
            if _API['degraded']:
                break
            if n_scanned <= _RECENT_WINDOW or info['is_project']:
                out.append(c)
            if info['is_project']:
                n_proj += 1
            if n_scanned >= _SCAN_CAP:
                break
        if n_scanned >= _RECENT_WINDOW and n_proj >= _PROJECT_TARGET:
            break
        page += 1
    return out
commits_data = fetch_commits()

# Refuse to publish a degraded dashboard. If a GitHub API call failed
# (rate-limit / outage / network) while LISTING or CLASSIFYING commits - even
# after gh_get's retries - the revision history is incomplete. Publishing now
# would overwrite the good dashboard with a blank or truncated one (exactly
# what a burst of concurrent CI activity used to cause, since the project's
# own revisions can sit hundreds of tooling commits deep and the deep-scan
# that surfaces them is the first thing a rate limit cuts off). Fail the build
# instead: the deploy step is skipped, the previous good dashboard stays live,
# and the next trigger rebuilds once the API recovers.
if _API['degraded']:
    sys.exit("::error::GitHub API was rate-limited or unavailable while "
             "reading commit history, so the revision data is incomplete. "
             "Refusing to publish a blank or truncated dashboard over the "
             "good one - the previous dashboard stays live and rebuilds "
             "automatically once the API recovers.")

# ── List the VI files present at a revision ─────────────────
# Powers the VI Browser's file tree INDEPENDENTLY of whether snapshots have
# been rendered: the browser builds its sidebar from this list, so a revision's
# hierarchy is always visible (a missing snapshot just shows a placeholder + a
# "Generate snapshots" prompt). The git tree API returns each blob's SHA, which
# is exactly the content-address the snapshot store keys on
# (vi-snapshots/by-blob/<ab>/<blob>.html), so the browser can map every VI to
# its snapshot (or detect its absence) with no extra index. Filter mirrors
# build-snapshots.ps1 (*.vi/*.ctl, excluding CI/build dirs).
_tree_cache = {}
def vi_tree(sha):
    if sha in _tree_cache:
        return _tree_cache[sha]
    data = gh_get(f'git/trees/{sha}?recursive=1')
    vis = []
    if data and isinstance(data.get('tree'), list):
        for t in data['tree']:
            if t.get('type') != 'blob':
                continue
            p = t.get('path', '')
            pl = p.lower()
            if not (pl.endswith('.vi') or pl.endswith('.ctl')):
                continue
            if p.startswith(TOOLING_PREFIXES):
                continue
            vis.append({'vi_rel': p, 'blob': t.get('sha', '')})
        vis.sort(key=lambda e: e['vi_rel'].lower())
    _tree_cache[sha] = vis
    return vis

# ── VIDiff shape: how many VIs a revision changed vs its parent ──────────────
# (different, new, deleted) for the VI/CTL set, keyed by file path with the git
# blob SHA as the content fingerprint: different = same path, changed blob; new =
# a path the parent did not have; deleted = a path the parent had and this
# revision dropped. A rename reads as one new + one deleted (path-based, like a
# plain git diff). Both trees come from vi_tree(), which is cached, so a parent
# that is itself a row in the window costs no extra API call. A root commit (no
# parent) counts everything present as new.
_diff_counts_cache = {}
def vidiff_counts(sha, parent):
    key = (sha, parent)
    if key in _diff_counts_cache:
        return _diff_counts_cache[key]
    cur = {v['vi_rel']: v.get('blob', '') for v in vi_tree(sha)}
    if not parent:
        res = (0, len(cur), 0)
    else:
        prev = {v['vi_rel']: v.get('blob', '') for v in vi_tree(parent)}
        different = sum(1 for p, b in cur.items() if p in prev and prev[p] != b)
        new       = sum(1 for p in cur if p not in prev)
        deleted   = sum(1 for p in prev if p not in cur)
        res = (different, new, deleted)
    _diff_counts_cache[key] = res
    return res

# Accumulates one entry per project revision for vi-snapshots/files.json, the
# VI Browser's snapshot-independent source of commits + file trees.
file_commits = []

# ── "Run" targets for empty cells ───────────────────────────────
# Each capability column maps to the consumer-repo workflow(s) that re-run it
# for one commit, so an empty cell can offer a one-click "run". Entries with
# both 'windows' and 'linux' drive the platform picker; a lone 'all' key is a
# single-target capability (no picker). The inputs are the workflow_dispatch
# fields, with {sha}/{parent} placeholders filled per row in the browser. These
# follow the standard installed workflow names, so they resolve in the consumer
# repo (source + hybrid installs); a thin consumer without them simply gets a
# link that 404s, which is why the affordance is unobtrusive.
RUN_TARGETS = {
    # 'batch' (Windows only for now) is the OPT-IN warm-container path: the
    # "Populate history" dialog's "keep one container warm across revisions" option
    # dispatches this ONE workflow with a space-separated SHA list, which compiles
    # every revision inside a single long-lived container (docker exec per commit)
    # instead of one fresh container per revision. 'shas_input' names the
    # workflow_dispatch input that carries the list. Absent 'batch' (e.g. Linux),
    # the option falls back to the normal per-revision dispatch.
    'masscompile': {'label': 'Mass Compile', 'platforms': {
        'windows': {'wf': 'masscompile-windows-container.yml', 'inputs': {'commit_sha': '{sha}'},
                    'batch': {'wf': 'masscompile-backfill-windows.yml', 'shas_input': 'shas'}},
        'linux':   {'wf': 'masscompile-linux-container.yml',   'inputs': {'commit_sha': '{sha}'}}}},
    'vi-analyzer': {'label': 'VI Analyzer', 'platforms': {
        'windows': {'wf': 'run-vi-analyzer-windows-container.yml', 'inputs': {'commit_sha': '{sha}'},
                    'batch': {'wf': 'vi-analyzer-backfill-windows.yml', 'shas_input': 'shas'}},
        'linux':   {'wf': 'run-vi-analyzer-linux-container.yml', 'inputs': {'commit_sha': '{sha}'}}}},
    'vidiff': {'label': 'VIDiff', 'platforms': {
        'windows': {'wf': 'vidiff-windows-container.yml', 'inputs': {'head_sha': '{sha}', 'base_sha': '{parent}'}},
        'linux':   {'wf': 'vidiff-linux-container.yml',   'inputs': {'head_sha': '{sha}', 'base_sha': '{parent}'}}}},
    'snapshots': {'label': 'VI Snapshots', 'platforms': {
        # Snapshots are content-addressed and deduped, so a single backfill renders
        # exactly the MISSING VIs across all history (already-rendered blobs are
        # skipped instantly). Dispatching backfill — not head — means clicking Run
        # on an empty snapshot cell actually fills that revision (head would only
        # ever re-render the current HEAD). 'backfill' is a long-standing input on
        # every consumer's vi-snapshots.yml (the "Populate history" card uses it),
        # so this needs no workflow-file change to reach existing installs.
        'all': {'wf': 'vi-snapshots.yml', 'inputs': {'mode': 'backfill'}}}},
      'snapshots2': {'label': 'VI Browser 2.0 Snapshots', 'platforms': {
        'windows': {'wf': 'vi-snapshots-json-windows.yml', 'inputs': {'target_sha': '{sha}'}},
        'linux':   {'wf': 'vi-snapshots-json.yml',         'inputs': {'mode': 'head', 'target_sha': '{sha}'}}}},
    # Unit tests run in the Windows worker only: Caraya, VI Tester and LUnit are
    # VIPM packages (Windows-only) and NI UTF runs via the native LabVIEW CLI. The
    # runner emits JUnit that build-unittest-report.py normalises into one report.
    'unit-tests': {'label': 'Unit Tests', 'platforms': {
        'windows': {'wf': 'unit-tests-windows-container.yml', 'inputs': {'commit_sha': '{sha}'},
                    'batch': {'wf': 'unit-tests-backfill-windows.yml', 'shas_input': 'shas'}}}},
    # Antidoc (Wovalab) documentation generation runs in the Windows worker only
    # (the Antidoc CLI is a VIPM package baked into the custom image). Doc-gen is
    # heavier than the per-VI checks, so it is on-demand / push-to-default-branch.
    'antidoc': {'label': 'Antidoc', 'platforms': {
        'windows': {'wf': 'run-antidoc-windows-container.yml', 'inputs': {'commit_sha': '{sha}'},
                    'batch': {'wf': 'antidoc-backfill-windows.yml', 'shas_input': 'shas'}}}},
    # Builds: run the project's LabVIEW build specifications (EXE, PPL, shared
    # library, source distribution, ...) headlessly. Cross-platform where the
    # spec type allows; Installer / .NET Interop are Windows-only (skipped on
    # Linux, never failed).
    'builds': {'label': 'Builds', 'platforms': {
        'windows': {'wf': 'build-binaries-windows-container.yml', 'inputs': {'commit_sha': '{sha}'}},
        'linux':   {'wf': 'build-binaries-linux-container.yml',   'inputs': {'commit_sha': '{sha}'}}}},
    # SBOM generation runs in the Windows worker because VIPM supplies the
    # dependency inventory. The generated CycloneDX report is per revision.
    'sbom-generation': {'label': 'SBOM Generation', 'platforms': {
        'windows': {'wf': 'generate-sbom-windows-container.yml', 'inputs': {'commit_sha': '{sha}'}}}},
}

# Gate run targets to the workflows ACTUALLY installed in this repo, so the
# dashboard never offers a Run / Populate-history action whose workflow file is
# absent (which would 404 on dispatch). The dashboard is built inside the consumer
# repo, so the workflows directory on disk is this repo's installed set: a platform
# whose workflow is missing is dropped, and a capability left with no platform is
# removed entirely (its cells then render a plain dash, and the dialog omits it).
# Fail open -- if the directory can't be read, keep every target (better a rare
# 404 than hiding a working action).
def _installed_workflow_files():
    import glob as _glob
    for _base in [p for p in (os.environ.get('GITHUB_WORKSPACE'), '.') if p]:
        _wfdir = os.path.join(_base, '.github', 'workflows')
        if os.path.isdir(_wfdir):
            return {os.path.basename(_p)
                    for _ext in ('*.yml', '*.yaml')
                    for _p in _glob.glob(os.path.join(_wfdir, _ext))}
    return None

_installed_wf = _installed_workflow_files()
if _installed_wf is not None:
    _gated_targets = {}
    for _cap, _cdef in RUN_TARGETS.items():
        _plats = {}
        for _k, _v in (_cdef.get('platforms') or {}).items():
            if _v.get('wf') not in _installed_wf:
                continue
            # Drop a warm-container 'batch' sub-target whose backfill workflow is
            # not installed on this repo, so the dialog's reuse option falls back
            # to per-revision dispatch instead of 404-ing on a missing workflow.
            _b = _v.get('batch')
            if _b and _b.get('wf') not in _installed_wf:
                _v = {_kk: _vv for _kk, _vv in _v.items() if _kk != 'batch'}
            _plats[_k] = _v
        if _plats:
            _ncdef = dict(_cdef); _ncdef['platforms'] = _plats
            _gated_targets[_cap] = _ncdef
    RUN_TARGETS = _gated_targets

import json as _json
run_targets_json = _json.dumps(RUN_TARGETS)

# ── Warm-batch time estimate: per cap+platform median {total, work} seconds ──
# Feeds the "Populate history" dialog's live Fresh-vs-Warm-batch estimate. Fully
# automatic from history: 'work' is the median in-container compile time the
# reports already record (masscompile summary.json 'duration'); 'total' is the
# median wall-clock of recent completed runs of the per-commit workflow (the GH
# Actions API run_started_at→updated_at). The client derives startup = total-work
# and shows Fresh = N×total vs Warm = startup + N×work, falling back to built-in
# defaults when a repo has no history yet. Bounded, best-effort, and never fatal.
def _median(vals):
    vs = sorted(v for v in vals if isinstance(v, (int, float)) and v > 0)
    if not vs:
        return None
    n = len(vs); m = n // 2
    return vs[m] if n % 2 else (vs[m - 1] + vs[m]) / 2.0

_wf_total_cache = {}
def _workflow_total_median(wf):
    if wf in _wf_total_cache:
        return _wf_total_cache[wf]
    import datetime as _dt2
    durs = []
    data = gh_get(f'actions/workflows/{quote(wf, safe="")}/runs?status=completed&per_page=20')
    for run in ((data or {}).get('workflow_runs') or []):
        if run.get('conclusion') not in ('success', 'failure'):
            continue
        s = run.get('run_started_at') or run.get('created_at')
        e = run.get('updated_at')
        if not s or not e:
            continue
        try:
            ds = _dt2.datetime.fromisoformat(str(s).replace('Z', '+00:00'))
            de = _dt2.datetime.fromisoformat(str(e).replace('Z', '+00:00'))
            sec = (de - ds).total_seconds()
        except Exception:
            continue
        if 0 < sec < 6 * 3600:
            durs.append(sec)
    med = _median(durs)
    _wf_total_cache[wf] = med
    return med

def build_run_timing():
    # 'work' medians we can read cheaply from reports already loaded for the table.
    mc_work = _median([(v or {}).get('duration') for v in _mc_cache.values()])
    work_by_cap = {'masscompile': mc_work}
    timing = {}
    for cap, cdef in RUN_TARGETS.items():
        if cap == 'snapshots':
            continue
        plats = {}
        for pk, pv in (cdef.get('platforms') or {}).items():
            wf = pv.get('wf')
            if not wf:
                continue
            plats[pk] = {'total': _workflow_total_median(wf), 'work': work_by_cap.get(cap)}
        if plats:
            timing[cap] = plats
    return timing


# ── Active CI runs (queued / in progress) keyed by the commit they ran on ────
# A freshly pushed revision starts its CI before any check posts a commit status,
# so a brand-new row would otherwise show idle "run" arrows for activities that
# are ALREADY queued on GitHub. Ask the Actions API which runs are queued or in
# progress right now and key them by head_sha + capability, so each affected cell
# reads "Queued"/"Running" the moment its run is created - well before that run
# posts its first commit status (which only lands part-way through the job).
_WF_TO_CAP = {}
for _cap, _d in RUN_TARGETS.items():
    for _p in (_d.get('platforms') or {}).values():
        if _p.get('wf'):
            _WF_TO_CAP[_p['wf']] = _cap
        # Warm-container batch backfills belong to the same capability column —
        # without this mapping a queued/running/dead batch run is invisible here.
        if _p.get('batch') and _p['batch'].get('wf'):
            _WF_TO_CAP[_p['batch']['wf']] = _cap
_CAP_RUN_LABEL = {'masscompile': 'compile', 'vi-analyzer': 'analyze', 'vidiff': 'diff',
                  'snapshots': 'snapshots', 'snapshots2': '2.0 snapshots',
                  'unit-tests': 'tests', 'antidoc': 'docs',
                  'sbom-generation': 'SBOM'}
_WAITING_RUN_STATUSES = {'queued', 'requested', 'waiting', 'pending'}

def _target_shas_for_run(run, cap):
    # Which revision row(s) a run belongs to. A snapshots-gallery dispatch covers
    # every row ('*'). Any other workflow_dispatch run is matched by the full
    # 40-hex SHA(s) embedded in its run-name — a warm-container backfill lists
    # several, so it maps onto every revision it walks. Push/PR runs key off
    # head_sha. An unattributable run maps to no row (empty list).
    if cap == 'snapshots' and run.get('event') == 'workflow_dispatch':
        return ['*']
    if run.get('event') == 'workflow_dispatch':
        text = ' '.join(str(run.get(k) or '') for k in ('display_title', 'name'))
        return [s.lower() for s in re.findall(r'\b[0-9a-f]{40}\b', text, re.I)]
    return [run['head_sha']] if run.get('head_sha') else []

def fetch_active_runs():
    by_sha = {}
    for _st in ('in_progress', 'pending', 'waiting', 'requested', 'queued'):   # in_progress first so it wins over a stale queued state
        data = gh_get(f'actions/runs?status={_st}&per_page=100')
        for run in ((data or {}).get('workflow_runs') or []):
            wf = (run.get('path') or '').rsplit('/', 1)[-1]
            cap = _WF_TO_CAP.get(wf)
            if not cap:
                continue
            for sha_ in _target_shas_for_run(run, cap):
                by_sha.setdefault(sha_, {}).setdefault(cap, run)
    return by_sha

active_runs = fetch_active_runs()

# ── Recently-dead runs: failed WITHOUT ever posting a commit status ──────────
# A dispatch that dies before its first status step (startup failure, an Actions
# spending-limit / billing refusal, cancelled while still queued) used to leave
# NO trace: no active run, no status, so its cell silently reverted to an idle
# run arrow — to anyone watching, the queued work just evaporated. Key the
# FAILED ones by row + capability so run_cell can show a red badge instead.
# Only the newest completed run per (sha, cap) counts — a later success (which
# posts statuses and renders through the normal result path) supersedes an
# older failure — and only within a 24h window, so a stale incident ages back
# to a clean idle cell instead of haunting the row forever.
_FAILED_CONCLUSIONS = {'failure', 'startup_failure', 'timed_out', 'cancelled'}

def fetch_failed_runs():
    cutoff = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(time.time() - 24 * 3600))
    by_sha = {}
    seen = set()
    data = gh_get('actions/runs?status=completed&per_page=100')
    for run in ((data or {}).get('workflow_runs') or []):   # newest first
        wf = (run.get('path') or '').rsplit('/', 1)[-1]
        cap = _WF_TO_CAP.get(wf)
        if not cap:
            continue
        for sha_ in _target_shas_for_run(run, cap):
            if (sha_, cap) in seen:
                continue
            seen.add((sha_, cap))
            if (run.get('conclusion') in _FAILED_CONCLUSIONS
                    and (run.get('created_at') or '') >= cutoff):
                by_sha.setdefault(sha_, {})[cap] = run
    return by_sha

failed_runs = fetch_failed_runs()

# Small "image" glyph (GitHub octicon) shown beside a commit message when that
# revision has rendered VI snapshots, so snapshot coverage is discoverable from
# the main table — not just the Snapshots column.
SNAP_ICON = ('<svg viewBox="0 0 16 16" width="12" height="12" fill="currentColor" '
             'aria-hidden="true" style="vertical-align:text-bottom;flex:0 0 auto">'
             '<path d="M1.75 2.5a.25.25 0 0 0-.25.25v10.5c0 .138.112.25.25.25h.94l8.5-8.5a.25.25 0 0 1 '
             '.354 0l1.756 1.757V2.75a.25.25 0 0 0-.25-.25H1.75ZM14.5 9.232 11.06 5.79 3.852 13h9.898a.25.25 '
             '0 0 0 .25-.25V9.232ZM1.75 1h12.5c.966 0 1.75.784 1.75 1.75v10.5A1.75 1.75 0 0 1 14.25 15H1.75A1.75 '
             '1.75 0 0 1 0 13.25V2.75C0 1.784.784 1 1.75 1ZM5.5 6a1.5 1.5 0 1 1-3 0 1.5 1.5 0 0 1 3 0Z"/></svg>')

# ── Status chips ──────────────────────────────────────────────────────
# Every result cell renders a small pill "chip" (subtle tinted background, a
# coloured state glyph, and the label) instead of a solid block with an emoji,
# so the table reads as a clean status board. The kind drives both colour and
# glyph: pass (green check), fail (red x), warn (amber alert), info (neutral
# blue, no glyph - used for counts like snapshot coverage).
_CHIP_ICON = {
    'pass': '<svg viewBox="0 0 16 16" fill="currentColor" aria-hidden="true"><path d="M13.78 4.22a.75.75 0 0 1 0 1.06l-7.25 7.25a.75.75 0 0 1-1.06 0L1.22 9.28a.75.75 0 1 1 1.06-1.06L6 11.94l6.72-6.72a.75.75 0 0 1 1.06 0Z"/></svg>',
    'fail': '<svg viewBox="0 0 16 16" fill="currentColor" aria-hidden="true"><path d="M3.72 3.72a.75.75 0 0 1 1.06 0L8 6.94l3.22-3.22a.75.75 0 1 1 1.06 1.06L9.06 8l3.22 3.22a.75.75 0 1 1-1.06 1.06L8 9.06l-3.22 3.22a.75.75 0 0 1-1.06-1.06L6.94 8 3.72 4.78a.75.75 0 0 1 0-1.06Z"/></svg>',
    'warn': '<svg viewBox="0 0 16 16" fill="currentColor" aria-hidden="true"><path d="M6.46 1.05c.66-1.23 2.43-1.23 3.08 0l6.08 11.38A1.75 1.75 0 0 1 14.08 15H1.92a1.75 1.75 0 0 1-1.54-2.57ZM8.75 5.75a.75.75 0 0 0-1.5 0v3a.75.75 0 0 0 1.5 0ZM9 11a1 1 0 1 1-2 0 1 1 0 0 1 2 0Z"/></svg>',
    'info': '',
}
_STATE_KIND = {'success': 'pass', 'failure': 'fail', 'error': 'fail', 'pending': 'warn'}

def _chip(kind, label, url='', title='', solid=False):
    """Render a status pill. ``kind`` in {pass,fail,warn,info}; ``label`` is the
    text; ``url`` makes it a link; ``title`` is an optional tooltip. ``solid``
    fills the pill with its state colour (used for a hard failure -- e.g. a Mass
    Compile run LabVIEW could not complete -- so it reads as bright, filled red)."""
    icon = _CHIP_ICON.get(kind, '')
    lab  = f'<a href="{url}" style="color:inherit">{label}</a>' if url else label
    ttl  = f' title="{title}"' if title else ''
    cls  = f'cc-{kind}' + (' cc-solid' if solid else '')
    return f'<span class="cidash-chip {cls}"{ttl}>{icon}{lab}</span>'

def _chip_split(segments, url='', title=''):
    """Render a neutral status pill whose individual values carry their own
    colour, instead of tinting the whole pill one (worst-wins) colour. Use it
    when one cell reports two values that can legitimately differ in state -
    e.g. the Windows vs Linux mass-compile percentages - so each value's colour
    tells its own story. ``segments`` is a list of ``(kind, text, label)`` or
    ``(kind, text, label, solid)`` tuples joined by a muted separator; a
    ``solid`` segment is filled with its state colour (used for a hard compile
    failure). ``label`` is shown as an instant
    (CSS-driven, no native-tooltip delay) mouse-over naming the value, e.g.
    "Windows" or "Linux". ``url`` makes it a link; ``title`` is an optional
    pill-level tooltip. A failing segment keeps the ``cc-fail`` class so the
    table's row-status filter still rolls the row up to "failed"."""
    parts = []
    for seg in segments:
        _k, _t = seg[0], seg[1]
        _lab = seg[2] if len(seg) > 2 else ''
        _solid = seg[3] if len(seg) > 3 else False
        _tip = f' data-tip="{_lab}"' if _lab else ''
        _cls = f'cc-{_k}' + (' cc-solid' if _solid else '')
        parts.append(f'<span class="cidash-vseg {_cls}"{_tip}>{_t}</span>')
    inner = '<span class="cidash-vseg-sep">/</span>'.join(parts)
    body  = f'<a href="{url}" style="color:inherit">{inner}</a>' if url else inner
    ttl   = f' title="{title}"' if title else ''
    return f'<span class="cidash-chip cc-split"{ttl}>{body}</span>'

# ── Framed per-revision reports ─────────────────────────────────────────────────────
# Per-revision reports (Mass Compile, VI Analyzer) open INSIDE the dashboard
# chrome via report-viewer.html (deployed at report/index.html), which frames
# the report under the shared site header — same nav, a revision picker, and a
# Regenerate / Re-run button. So the header no longer "goes away" when a report
# is opened, and reports that predate the header (or carry none of their own)
# still appear inside the chrome. Diff/Snapshots already open the VI Browser (its
# own headered page), so only these two doctypes are wrapped here.
DOC_LABELS = {'vi-analyzer-report': 'VI Analyzer', 'masscompile-report': 'Mass Compile', 'unit-tests-report': 'Unit Tests', 'antidoc-report': 'Antidoc', 'builds-report': 'Builds', 'sbom-report': 'SBOM'}

def viewer_url(report_url, doctype, sha, short, platform=''):
    """Wrap a deployed report's absolute Pages URL so it opens framed under the
    shared header. ``report_url`` is e.g. ``{pages_url}/masscompile/<sha>/index.html``;
    the viewer lives at ``{pages_url}/report/index.html`` so the embedded ``src``
    is made relative to it (``../<prefix>/<sha>/index.html``)."""
    rel = report_url[len(pages_url):].lstrip('/') if report_url.startswith(pages_url) else report_url
    src = '../' + rel
    title = f'{DOC_LABELS.get(doctype, "CI Report")} \u00b7 {short or sha[:7]}'
    q = ('type=' + quote(doctype, safe='')
         + '&sha=' + quote(sha, safe='')
         + ('&short=' + quote(short, safe='') if short else '')
         + ('&platform=' + quote(platform, safe='') if platform else '')
         + '&src=' + quote(src, safe='')
         + '&title=' + quote(title, safe=''))
    return f'{pages_url}/report/index.html?{q}'

def maybe_frame(url, doctype, prefix, sha, short):
    """Frame ``url`` only when it points at a real deployed report
    (``…/<prefix>/<sha>/index.html``). A failed run whose status links to the
    Actions run page (not a report) is returned unchanged."""
    if url and f'/{prefix}/{sha}/index.html' in url:
        return viewer_url(url, doctype, sha, short)
    return url

import datetime as _dt
def _stale_pending(s):
    # A 'pending' status older than 2h is almost certainly orphaned
    # (a cancelled/abandoned run) rather than one that is still running.
    if not s or s.get('state') != 'pending':
        return False
    try:
        ts = s.get('created_at', '').replace('Z', '+00:00')
        age = _dt.datetime.now(_dt.timezone.utc) - _dt.datetime.fromisoformat(ts)
        return age.total_seconds() > 2 * 3600
    except Exception:
        return False

# A 'pending' commit status only means a run STARTED. If that run later
# crashed or was cancelled WITHOUT posting a terminal status, the pending
# lingers and the dashboard would otherwise show a perpetual "running" spinner
# for a job that has actually stopped (often with an error). Verify the linked
# workflow run is genuinely still active before trusting a pending as live.
_run_active_cache = {}
def run_is_active(url):
    if not url:
        return False
    m = re.search(r'/actions/runs/(\d+)', url)
    if not m:
        return False
    rid = m.group(1)
    if rid in _run_active_cache:
        return _run_active_cache[rid]
    data = gh_get(f'actions/runs/{rid}')
    active = bool(data) and data.get('status') in (
        'queued', 'in_progress', 'requested', 'waiting', 'pending')
    _run_active_cache[rid] = active
    return active

# Set as soon as any cell renders an actively-running activity; drives the
# faster page auto-refresh so a live run (and its result) surfaces promptly.
running_flag = {'on': False}
# Together these gate the "Run all history" backfill card (built after the row
# loop): `any_output` flips True the moment ANY terminal CI result is rendered,
# and `run_count` counts the empty cells that offer a one-click run. The card is
# emitted only on a fresh dashboard — no results, nothing running — that still
# has revisions left to run, so it never appears once CI output exists.
any_output = {'on': False}
run_count  = {'n': 0}
# Caps (activities) that have at least one rendered result or active run anywhere
# in history. The "Populate history" dialog uses this to offer an "only workers
# that have never run" filter — a worker absent from this set is brand-new.
caps_ran = set()

# Project revisions (newest first), fed to the "Populate history" dialog's
# "start from" picker. Only project revisions carry run glyphs (badge() returns
# EMPTY_CELL for non-project commits), so these are exactly the revisions the
# dialog can queue activities for. json.dumps escapes the messages safely for the
# JS string context; the dialog renders them via textContent (no HTML injection).
hist_revs = []

rows_html = []
for c in commits_data:
    sha     = c['sha']
    short   = sha[:7]
    msg     = c['commit']['message'].splitlines()[0][:80]
    author  = c['commit']['author']['name']
    date    = c['commit']['author']['date']
    parent  = (c.get('parents') or [{}])[0].get('sha', '')

    # Classify the revision by scope (cached above): a "project change" touches
    # at least one LabVIEW source file in the project itself — NOT the helper VIs
    # that ship with the CI tooling (.github/, actions/, _lvci/, ci-out/, build/).
    # Everything else (workflows, scripts, docs, metadata, merges) is a non-project
    # revision, hidden by default.
    _info = classify_commit(sha)
    files = _info['files']
    is_project = _info['is_project']
    proj_flag = 'true' if is_project else 'false'
    # Dependency-only revisions (only .vipc/.vip changed) can be filtered out of
    # the dashboard via the "Include dependency-only revisions" toggle.
    dep_only_flag = 'true' if _info.get('is_dep_only') else 'false'

    # Record this revision's VI file tree for the VI Browser (project revisions
    # only — CI/tooling commits don't change the VI set, so they would just
    # duplicate a neighbour's tree). Done here so the browser can render the
    # hierarchy before — or without — any snapshots existing.
    if is_project:
        file_commits.append({
            'sha': sha,
            'short': short,
            'message': msg,
            'author': author,
            'date': date,
            'dep_only': bool(_info.get('is_dep_only')),
            'vis': vi_tree(sha),
        })
        # Newest-first list of revisions the history dialog can populate.
        _s2w_have, _s2w_total = snapshot2_coverage(sha, 'windows')
        _s2l_have, _s2l_total = snapshot2_coverage(sha, 'linux')
        if _s2w_have > 0 or _s2l_have > 0:
            caps_ran.add('snapshots2')
        hist_revs.append({
            'sha': sha,
            'short': short,
            'msg': msg,
            'dep_only': bool(_info.get('is_dep_only')),
            'snapshots2': {
                'windows': {'have': _s2w_have, 'total': _s2w_total},
                'linux': {'have': _s2l_have, 'total': _s2l_total},
            },
        })

    # Fetch commit statuses
    statuses_data = gh_get(f'commits/{sha}/statuses') or []
    status_map = {}
    for s in statuses_data:
        ctx = s['context']
        if ctx not in status_map:          # keep latest per context
            status_map[ctx] = s

    def pick_status(*contexts):
        # Gather the latest status for each candidate context (in priority order).
        cands = [status_map[c] for c in contexts if c in status_map]
        if not cands:
            return None
        # Prefer a terminal state (success/failure/error) over 'pending', then
        # take the MOST RECENT one. This means a fresh Linux success wins over a
        # stale Windows failure for the same logical check, and an orphaned
        # 'pending' from a cancelled run never masks a completed result.
        terminal = [c for c in cands if c['state'] in ('success', 'failure', 'error')]
        pool = terminal if terminal else cands
        chosen = max(pool, key=lambda s: s.get('created_at', ''))
        # A 'pending' older than 2h is almost certainly orphaned (cancelled run) —
        # show it as no-status rather than a perpetual spinner.
        if _stale_pending(chosen):
            return None
        return chosen

    EMPTY_CELL = '<td style="text-align:center;color:var(--fg-muted);font-size:.75em">—</td>'

    def run_cell(cap):
        # An empty *project* cell offers a one-click "run": a subtle play glyph
        # that opens the dispatch dialog for this capability + commit. Columns
        # with no re-run workflow (or unknown caps) fall back to a plain dash.
        if cap not in RUN_TARGETS:
            return EMPTY_CELL
        # Already auto-running? If this activity has a queued / in-progress run for
        # THIS commit (it just auto-started on push), surface it as Queued/Running
        # instead of an idle run arrow - it posts its first commit status only
        # part-way through, so the live run fills that gap until then.
        ar = active_runs.get(sha, {}).get(cap) or active_runs.get('*', {}).get(cap)
        if ar is not None:
            caps_ran.add(cap)
            if ar.get('status') in _WAITING_RUN_STATUSES:
                return queued_cell(ar.get('html_url', ''), cap=cap)
            return running_cell(_CAP_RUN_LABEL.get(cap, cap), ar.get('html_url', ''), cap=cap)
        # No live run and no status: before falling back to an idle arrow, check
        # whether a recent run for this cell DIED without reporting (see
        # fetch_failed_runs) — that must stay visible, not silently reset.
        fr = failed_runs.get(sha, {}).get(cap) or failed_runs.get('*', {}).get(cap)
        if fr is not None:
            return failed_cell(fr, cap=cap)
        run_count['n'] += 1
        return ('<td style="text-align:center">'
                f'<a href="#" class="cidash-run" data-cap="{cap}" data-sha="{sha}" '
                f'data-parent="{parent}" data-short="{short}" '
                f'title="Run {RUN_TARGETS[cap]["label"]} for commit {short}">&#9655;</a></td>')

    def fresh_pending(*contexts):
        # Return the newest actively-running (fresh 'pending') status among the
        # candidate contexts, or None. Detected separately from pick_status so a
        # re-run in progress reads as "running" even when an older terminal status
        # for the same logical check still exists.
        best = None
        for ctx in contexts:
            s = status_map.get(ctx)
            if not s or s.get('state') != 'pending' or _stale_pending(s):
                continue
            if best is None or s.get('created_at', '') > best.get('created_at', ''):
                best = s
        # A pending whose workflow run has already finished is stale (the run
        # stopped without posting its terminal status) — don't render it live.
        if best is not None and not run_is_active(best.get('target_url', '')):
            return None
        return best

    def _active_attrs(cap):
        # A queued/running cell carries no result yet, but it IS for a re-runnable
        # capability + this commit. Tag it (cidash-active-cell + cap/sha) so the
        # "Populate history" dialog can offer Re-run for an activity that is merely
        # in-flight, instead of greying it out as "none in scope". The optimistic
        # overlay (cidash-cap-cell) is left untouched, so the cell keeps its live
        # Queued/Running badge.
        if not cap or cap not in RUN_TARGETS:
            return ''
        return (f' class="cidash-active-cell" data-cap="{cap}" data-sha="{sha}"'
                f' data-parent="{parent}" data-short="{short}" data-active="true"')

    def running_cell(label, url, cap=None):
        # A spinning "running" indicator linking straight to the live workflow run
        # so the user can jump to it and see where it is.
        running_flag['on'] = True
        inner = f'<span class="run-spin"></span>{label}'
        body  = (f'<a href="{url}" style="color:#fff;text-decoration:none;display:inline-flex;align-items:center;gap:5px">{inner}</a>'
                 if url else f'<span style="display:inline-flex;align-items:center;gap:5px">{inner}</span>')
        return (f'<td style="text-align:center"{_active_attrs(cap)}>'
                '<span class="run-badge" title="Running — click to view progress">'
                f'{body}</span></td>')

    def queued_cell(url, cap=None):
        # A run that is QUEUED on GitHub Actions but has not started yet, so it has
        # posted no commit status. Mirrors running_cell but reads "Queued", so a
        # brand-new revision shows its activities are already on their way rather
        # than an idle run arrow.
        running_flag['on'] = True
        inner = '<span class="run-spin"></span>Queued'
        body  = (f'<a href="{url}" style="color:#fff;text-decoration:none;display:inline-flex;align-items:center;gap:5px">{inner}</a>'
                 if url else f'<span style="display:inline-flex;align-items:center;gap:5px">{inner}</span>')
        return (f'<td style="text-align:center"{_active_attrs(cap)}>'
                '<span class="run-badge" title="Queued on GitHub Actions - waiting for a runner">'
                f'{body}</span></td>')

    def failed_cell(run, cap=None):
        # A run for this cell that completed WITHOUT ever posting a commit status
        # (startup failure, spending-limit refusal, cancelled while queued). An
        # idle run arrow here would erase all evidence the run existed — the
        # "queued pills silently disappear" failure mode — so render a sticky red
        # badge linking to the dead run (same look as the browser-local overlay's
        # confirmed-failure bang, and the row-status filter already counts
        # .run-badge.cidash-failed as a failed row). Tagged as a cap-cell with
        # data-done="false" so the Populate-history dialog still counts the cell
        # as MISSING (Fill re-queues it) and the optimistic overlay can paint
        # over it — and restore it — on a re-run.
        url   = run.get('html_url', '')
        concl = run.get('conclusion') or 'failure'
        inner = '<span class="cidash-bang" aria-hidden="true">!</span>Failed'
        body  = (f'<a href="{url}" style="color:inherit;text-decoration:none;display:inline-flex;align-items:center;gap:5px">{inner}</a>'
                 if url else f'<span style="display:inline-flex;align-items:center;gap:5px">{inner}</span>')
        attrs = ''
        if cap and cap in RUN_TARGETS:
            attrs = (f' class="cidash-cap-cell" data-cap="{cap}" data-sha="{sha}"'
                     f' data-parent="{parent}" data-short="{short}"'
                     f' data-ts="{run.get("created_at", "")}" data-done="false"')
        return (f'<td style="text-align:center"{attrs}>'
                f'<span class="run-badge cidash-failed" title="This run ended ({concl}) before reporting a result — click to see why on GitHub, then re-queue it from Populate history">'
                f'{body}</span></td>')

    def active_run_cell(cap, label):
        # If a run for THIS commit + capability is queued or in progress on GitHub
        # right now (per the Actions API), return its Queued/Running cell — else
        # None. Checked BEFORE a result chip so that when a capability ran on two
        # platforms and the fast one (e.g. Linux) already posted its result, the
        # cell still shows the OTHER platform's run is live instead of silently
        # reverting to a finished-looking result. Matches the empty-cell behaviour
        # in run_cell(), which already prefers an active run over an idle arrow.
        ar = active_runs.get(sha, {}).get(cap)
        if ar is None:
            return None
        caps_ran.add(cap)
        if ar.get('status') == 'queued':
            return queued_cell(ar.get('html_url', ''), cap=cap)
        return running_cell(label, ar.get('html_url', ''), cap=cap)

    def badge(label, *contexts, url_override=None, cap=None, doc=None):
        if not is_project:
            return EMPTY_CELL
        run = fresh_pending(*contexts)
        if run is not None:
            if cap: caps_ran.add(cap)
            return running_cell(label, run.get('target_url', ''), cap=cap)
        # A sibling-platform run for this cap may still be live even though one
        # platform already posted a terminal commit status — surface it as running.
        if cap:
            _live = active_run_cell(cap, label)
            if _live is not None:
                return _live
        s = pick_status(*contexts)
        if not s:
            return run_cell(cap) if cap else EMPTY_CELL
        any_output['on'] = True
        if cap: caps_ran.add(cap)
        url    = url_override or s.get('target_url','')
        # Open a per-revision report inside the dashboard chrome (framed under
        # the shared header) rather than as a bare page — but only when the
        # status actually links to a deployed report (a failed run links to its
        # Actions page, which is left as-is).
        if doc and not url_override:
            url = maybe_frame(url, doc[0], doc[1], sha, short)
        # When this column has a re-run workflow, tag the result cell with its
        # cap/sha (+ the result's timestamp) so a re-run dispatched from elsewhere
        # (the report's "Re-run analysis" button) can overlay a "Queued" spinner on
        # it via the same lvci_queued_runs bridge the empty-cell Run now uses. The
        # data-ts lets the overlay self-clear once a NEWER result lands.
        cap_attrs = ''
        if cap:
            cap_attrs = (f' class="cidash-cap-cell" data-cap="{cap}" data-sha="{sha}"'
                         f' data-parent="{parent}" data-short="{short}" data-ts="{s.get("created_at","")}"')
        return (f'<td style="text-align:center"{cap_attrs}>'
                f'{_chip(_STATE_KIND.get(s["state"], "warn"), label, url)}</td>')

    # Mass Compile column: show the % of project VIs that compiled (most VIs
    # compile even when a few depend on libraries absent from the CI image),
    # sourced from the run's summary.json. Falls back to the plain status badge
    # for older runs that predate summary.json.
    if not is_project:
        mc_badge = EMPTY_CELL
    else:
        _mc_win = masscompile_summary(sha, 'windows')
        _mc_lin = masscompile_summary(sha, 'linux')
        _mc_live = active_run_cell('masscompile', 'compile')
        def _mc_kind(_s):
            return masscompile_badge_kind(_s.get('percent') or 0, _s.get('status'))
        _present = [(plat, s) for plat, s in (('windows', _mc_win), ('linux', _mc_lin))
                    if s and isinstance(s.get('percent'), int)]
        if _mc_live is not None:
            mc_badge = _mc_live
        elif _present:
            any_output['on'] = True
            caps_ran.add('masscompile')
            # The Mass Compile report opens framed inside the dashboard chrome
            # (report-viewer.html), so the header stays put and even older
            # reports that carry no header of their own still appear under it,
            # with a Regenerate button.
            _url = viewer_url(f'{pages_url}/masscompile/{sha}/index.html', 'masscompile-report', sha, short)
            # Tag the result cell exactly like badge() does (cidash-cap-cell +
            # data-cap/sha/parent/ts). This is what lets the Populate-history dialog
            # see Mass Compile as "done" (so its Re-run is offered, not greyed out)
            # and lets the optimistic "Queued" overlay land on the cell on a re-run.
            _mc_ts = (pick_status('CI / Mass Compile') or pick_status('CI / Mass Compile (Linux)') or {}).get('created_at', '')
            _gt = MASSCOMPILE_THRESHOLDS
            _gate = (f'green >= {_gt["greenAtLeast"]}%, yellow >= {_gt["yellowAtLeast"]}%, '
                     f'red < {_gt["redBelow"]}% (a compile failure is solid red)')
            if len(_present) == 2:
                # Windows and Linux can compile a different set of VIs, so show
                # both as a two-number pill (Windows / Linux). Their results can
                # diverge sharply (e.g. all VIs compile on Windows but a missing
                # Linux dependency fails the whole project), so colour each
                # percentage by its own state instead of tinting the entire pill
                # the single worst colour. Each value also carries an instant
                # mouse-over naming its platform (Windows / Linux).
                _wp, _lp = _mc_win['percent'], _mc_lin['percent']
                _wk, _ws = _mc_kind(_mc_win)
                _lk, _ls = _mc_kind(_mc_lin)
                _tip = (f'Windows {_wp}% ({_mc_win.get("ok",0)}/{_mc_win.get("total",0)}) · '
                        f'Linux {_lp}% ({_mc_lin.get("ok",0)}/{_mc_lin.get("total",0)}) project VIs compiled; '
                        f'{_gate}')
                _chip_html = _chip_split([(_wk, f'{_wp}%', 'Windows', _ws),
                                          (_lk, f'{_lp}%', 'Linux', _ls)], _url, _tip)
            else:
                _plat, _mc = _present[0]
                _kind, _solid = _mc_kind(_mc)
                _label = f'{_mc["percent"]}%'
                _tip = (f'{_plat.capitalize()}: {_mc.get("ok",0)}/{_mc.get("total",0)} project VIs compiled; '
                        f'{_gate}')
                _chip_html = _chip(_kind, _label, _url, _tip, solid=_solid)
            mc_badge = (f'<td style="text-align:center" class="cidash-cap-cell" data-cap="masscompile" '
                        f'data-sha="{sha}" data-parent="{parent}" data-short="{short}" data-ts="{_mc_ts}">'
                        f'{_chip_html}</td>')
        else:
            mc_badge = badge('compile', 'CI / Mass Compile', cap='masscompile', doc=('masscompile-report', 'masscompile'))
    # VI Analyzer column: show the number of findings (failed-test instances) per
    # platform, sourced from the run's summary.json, so the table conveys the
    # analysis outcome at a glance and reports Windows and Linux separately (the two
    # containers can analyze a different set of VIs). A degraded analysis (un-loadable
    # VIs, or zero VIs analyzed) is flagged solid red. Falls back to the plain status
    # badge for older runs that predate summary.json.
    if not is_project:
        via_badge = EMPTY_CELL
    else:
        _via_win = vi_analyzer_summary(sha, 'windows')
        _via_lin = vi_analyzer_summary(sha, 'linux')
        _via_live = active_run_cell('vi-analyzer', 'analyze')
        def _via_findings(_d):
            _s = _d.get('summary') or {}
            _f = _s.get('findings')
            return (_s.get('failed') or 0) if _f is None else _f
        def _via_seg(_d):
            _s = _d.get('summary') or {}
            _e = _d.get('errors') or {}
            _find = _via_findings(_d)
            _degraded = (_s.get('vis_analyzed') or 0) == 0 or any(
                (_e.get(k) or 0) > 0 for k in
                ('vi_not_loadable', 'test_not_loadable', 'test_not_runnable', 'test_error_out'))
            if _degraded:
                return ('fail', str(_find), True)
            if _find > 0:
                return ('warn', str(_find), False)
            return ('pass', '0', False)
        def _via_tip(_plat, _d):
            _s = _d.get('summary') or {}
            _f = _via_findings(_d)
            return (f'{_plat}: {_f} finding{"" if _f == 1 else "s"} across '
                    f'{_s.get("vis_with_findings", 0)} VI(s), {_s.get("vis_analyzed", 0)} analyzed')
        _via_present = [(plat, s) for plat, s in (('windows', _via_win), ('linux', _via_lin))
                        if s and isinstance(s.get('summary'), dict)]
        if _via_live is not None:
            via_badge = _via_live
        elif _via_present:
            any_output['on'] = True
            caps_ran.add('vi-analyzer')
            _via_ts = (pick_status('CI / VI Analyzer') or pick_status('CI / VI Analyzer (Linux)') or {}).get('created_at', '')
            if len(_via_present) == 2:
                _via_url = viewer_url(f'{pages_url}/vi-analyzer/{sha}/index.html', 'vi-analyzer-report', sha, short)
                _wk, _wt, _ws = _via_seg(_via_win)
                _lk, _lt, _ls = _via_seg(_via_lin)
                _via_tt = _via_tip('Windows', _via_win) + ' \u00b7 ' + _via_tip('Linux', _via_lin)
                _via_chip = _chip_split([(_wk, _wt, 'Windows', _ws), (_lk, _lt, 'Linux', _ls)], _via_url, _via_tt)
            else:
                _via_plat, _via_d = _via_present[0]
                _via_sub = 'linux/' if _via_plat == 'linux' else ''
                _via_url = viewer_url(f'{pages_url}/vi-analyzer/{sha}/{_via_sub}index.html',
                                      'vi-analyzer-report', sha, short, _via_plat)
                _vk, _vt, _vsolid = _via_seg(_via_d)
                _via_tt = _via_tip(_via_plat.capitalize(), _via_d)
                _via_chip = _chip(_vk, _vt, _via_url, _via_tt, solid=_vsolid)
            via_badge = (f'<td style="text-align:center" class="cidash-cap-cell" data-cap="vi-analyzer" '
                         f'data-sha="{sha}" data-parent="{parent}" data-short="{short}" data-ts="{_via_ts}">'
                         f'{_via_chip}</td>')
        else:
            via_badge = badge('analyze', 'CI / VI Analyzer', 'CI / VI Analyzer (Linux)', cap='vi-analyzer',
                              doc=('vi-analyzer-report', 'vi-analyzer'))
    # VIDiff column: rather than a single "diff" badge, show the SHAPE of the
    # revision — how many VIs are different / new / deleted versus its parent —
    # so the table conveys at a glance what each revision did, not just that a
    # diff ran. The three counts are computed from the VI file trees (content
    # blob per path) and are coloured modified-amber / added-green / deleted-red,
    # with a tip strip spelling each out. The whole cell still links into the VI
    # Browser filtered to this revision's changed VIs (each VI linking to its
    # side-by-side report). When VIDiff has not produced a result yet the cell
    # falls back to the same running / queued / one-click-run affordances as the
    # other columns.
    def diff_cell():
        if not is_project:
            return EMPTY_CELL
        _ctxs = ('CI / VIDiff (windows)', 'CI / VIDiff (linux)')
        _run = fresh_pending(*_ctxs)
        if _run is not None:
            caps_ran.add('vidiff')
            return running_cell('diff', _run.get('target_url', ''), cap='vidiff')
        _live = active_run_cell('vidiff', 'diff')
        if _live is not None:
            return _live
        _s = pick_status(*_ctxs)
        if not _s:
            return run_cell('vidiff')
        any_output['on'] = True
        caps_ran.add('vidiff')
        _d, _n, _del = vidiff_counts(sha, parent)
        _url = f'{pages_url}/vi-snapshots/index.html?sha={sha}&changed=1'
        # Spell the counts out for the tip strip; "0" parts stay quiet (greyed)
        # so the eye lands on what actually changed.
        _parts = (f'{_d} different', f'{_n} new', f'{_del} deleted')
        _tip = 'VIs changed in this revision: ' + ' · '.join(_parts) + ' — click to view the diffs'
        def _num(val, cls):
            z = '' if val else ' ds-zero'
            return f'<span class="{cls}{z}">{val}</span>'
        _stat = (f'{_num(_d, "ds-mod")}<span class="ds-sep">/</span>'
                 f'{_num(_n, "ds-add")}<span class="ds-sep">/</span>'
                 f'{_num(_del, "ds-del")}')
        return ('<td style="text-align:center" class="cidash-cap-cell" data-cap="vidiff" '
                f'data-sha="{sha}" data-parent="{parent}" data-short="{short}" '
                f'data-ts="{_s.get("created_at","")}">'
                f'<a href="{_url}" class="cidash-diffstat" title="{_tip}">{_stat}</a></td>')
    diff_badge = diff_cell()
    # Snapshots column: how many of this revision's VIs have a rendered snapshot
    # (content-addressed by git blob), linking into the VI Browser for the commit.
    # Snapshots are a single content-addressed gallery (not per-platform) and are
    # independent of VIDiff. None yet -> one-click run; partial coverage is flagged
    # amber so the missing VIs are visible (fill them by running this revision, or
    # via "Populate history" — a backfill renders all history oldest -> newest).
    if not is_project:
        snap_badge = '<td style="text-align:center;color:var(--fg-muted);font-size:.75em">—</td>'
    else:
        _snap_run = fresh_pending('CI / VI Snapshots')
        _have, _total = snapshot_coverage(sha)
        _snap_live = active_run_cell('snapshots', 'snapshots')
        if _snap_run is not None:
            caps_ran.add('snapshots')
            snap_badge = running_cell('snapshots', _snap_run.get('target_url', ''), cap='snapshots')
        elif _snap_live is not None:
            snap_badge = _snap_live
        elif _have <= 0:
            snap_badge = run_cell('snapshots')
        else:
            any_output['on'] = True
            caps_ran.add('snapshots')
            _href = f'{pages_url}/vi-snapshots/index.html?sha={sha}'
            if _have >= _total:
                _kind, _txt = 'info', str(_total)
                _tip = f'Snapshots rendered for all {_total} VIs in this revision'
            else:
                _kind, _txt = 'warn', f'{_have}/{_total}'
                _tip = (f'{_have} of {_total} VIs have snapshots; {_total - _have} missing '
                        f'- run this revision or use Populate history to backfill')
            _snap_ts = (pick_status('CI / VI Snapshots') or {}).get('created_at', '')
            _snap_done = 'true' if _have >= _total else 'false'
            snap_badge = (f'<td style="text-align:center" class="cidash-cap-cell" data-cap="snapshots" '
                    f'data-sha="{sha}" data-parent="{parent}" data-short="{short}" data-ts="{_snap_ts}" data-done="{_snap_done}">'
                          f'{_chip(_kind, _txt, _href, _tip)}</td>')

    # Unit Tests column: show the percentage of tests that failed, sourced from
    # the deployed report model. Colour is controlled by configurable pass-rate
    # thresholds (green/yellow/red), while the label stays failure-focused.
    if not is_project:
        unit_badge = EMPTY_CELL
    else:
        _ut_run = fresh_pending('CI / Unit Tests')
        _ut_win = unit_tests_summary(sha, 'windows')
        _ut_lin = unit_tests_summary(sha, 'linux')
        _ut_present = [(plat, s) for plat, s in (('windows', _ut_win), ('linux', _ut_lin))
                       if s and 'tests' in s]
        _ut_live = active_run_cell('unit-tests', 'tests')
        if _ut_run is not None:
            caps_ran.add('unit-tests')
            unit_badge = running_cell('tests', _ut_run.get('target_url', ''), cap='unit-tests')
        elif _ut_live is not None:
            unit_badge = _ut_live
        elif _ut_present:
            any_output['on'] = True
            caps_ran.add('unit-tests')
            def _ut_fail_pct(_s):
                _t = max(0, int(_s.get('tests') or 0))
                _fl = max(0, int(_s.get('failed') or 0) + int(_s.get('errored') or 0))
                return round(100 * _fl / _t) if _t else 0
            def _ut_pass_pct(_s):
                _t = max(0, int(_s.get('tests') or 0))
                _p = max(0, int(_s.get('passed') or 0))
                return round(100 * _p / _t) if _t else 100
            _url = viewer_url(f'{pages_url}/unit-tests/{sha}/index.html', 'unit-tests-report', sha, short)
            _ut_ts = (pick_status('CI / Unit Tests') or pick_status('CI / Unit Tests (Linux)') or {}).get('created_at', '')
            if len(_ut_present) == 2:
                # Windows and Linux can run a different set of tests, so show both
                # failure rates as a two-number pill (Windows / Linux); worst wins.
                _wf, _lf = _ut_fail_pct(_ut_win), _ut_fail_pct(_ut_lin)
                _kind = unit_test_badge_kind(min(_ut_pass_pct(_ut_win), _ut_pass_pct(_ut_lin)))
                _label = f'{_wf}% / {_lf}% failed'
                _tip = f'Windows {_wf}% failed · Linux {_lf}% failed (green pass rate >= {UNIT_TEST_THRESHOLDS["greenAtLeast"]}%)'
            else:
                _plat, _ut = _ut_present[0]
                _failed_pct = _ut_fail_pct(_ut)
                _pass_pct = _ut_pass_pct(_ut)
                _kind = unit_test_badge_kind(_pass_pct)
                _label = f'{_failed_pct}% failed'
                _total = max(0, int(_ut.get('tests') or 0))
                _failed = max(0, int(_ut.get('failed') or 0) + int(_ut.get('errored') or 0))
                _tip = (f'{_plat.capitalize()}: {_failed} of {_total} unit tests failed; pass rate {_pass_pct}%. '
                        f'Green >= {UNIT_TEST_THRESHOLDS["greenAtLeast"]}% passed, '
                        f'yellow >= {UNIT_TEST_THRESHOLDS["yellowAtLeast"]}%, '
                        f'red < {UNIT_TEST_THRESHOLDS["redBelow"]}%.') if _total else 'No unit tests found; 0% failed.'
            unit_badge = (f'<td style="text-align:center" class="cidash-cap-cell" data-cap="unit-tests" '
                          f'data-sha="{sha}" data-parent="{parent}" data-short="{short}" data-ts="{_ut_ts}">'
                          f'{_chip(_kind, _label, _url, _tip)}</td>')
        else:
            unit_badge = badge('tests', 'CI / Unit Tests', cap='unit-tests',
                               doc=('unit-tests-report', 'unit-tests'))
    # Antidoc column: links to the generated documentation, framed in the
    # dashboard chrome via report-viewer. An empty project cell offers a
    # one-click run (cap='antidoc' dispatches run-antidoc-windows-container.yml).
    antidoc_badge = badge('docs', 'CI / Antidoc', cap='antidoc',
                          doc=('antidoc-report', 'antidoc'))

    # Builds column: a split pill of build specifications that SUCCEEDED vs FAILED
    # for this revision (e.g. "2 / 1"), aggregated across the Windows and Linux
    # build runs and sourced from each run's summary.json. An empty project cell
    # offers a one-click run; a project with no build specifications renders a
    # plain dash.
    def builds_cell():
        if not is_project:
            return EMPTY_CELL
        _bw = builds_summary(sha, 'windows')
        _bl = builds_summary(sha, 'linux')
        _live = active_run_cell('builds', 'build')
        if _live is not None:
            caps_ran.add('builds')
            return _live
        _present = [(plat, s) for plat, s in (('windows', _bw), ('linux', _bl))
                    if s and isinstance(s.get('total'), int)]
        if not _present:
            return run_cell('builds')
        _built = sum(int(s.get('built') or 0) for _, s in _present)
        _failed = sum(int(s.get('failed') or 0) for _, s in _present)
        _skipped = sum(int(s.get('skipped') or 0) for _, s in _present)
        _total = sum(int(s.get('total') or 0) for _, s in _present)
        if _total <= 0:
            # A project with no build specifications: nothing to show.
            return EMPTY_CELL
        any_output['on'] = True
        caps_ran.add('builds')
        _ts = (pick_status('CI / Builds') or pick_status('CI / Builds (Linux)') or {}).get('created_at', '')
        # Open the Builds report framed in the dashboard chrome (report-viewer),
        # so it carries the shared header, a revision picker and a Rebuild button
        # like the other report columns.
        _url = viewer_url(f'{pages_url}/builds/{sha}/index.html', 'builds-report', sha, short)
        _tip = (f'{_built} built, {_failed} failed'
                + (f', {_skipped} skipped' if _skipped else '')
                + f' of {_total} build specification(s)')
        # Split pill: succeeded (green) / failed (red when > 0, muted grey at 0 so
        # a clean run does not roll the row up to "failed").
        _fail_kind = 'fail' if _failed > 0 else 'muted'
        _chip_html = _chip_split([('pass', str(_built), 'Succeeded'),
                                  (_fail_kind, str(_failed), 'Failed')], _url, _tip)
        return (f'<td style="text-align:center" class="cidash-cap-cell" data-cap="builds" '
                f'data-sha="{sha}" data-parent="{parent}" data-short="{short}" data-ts="{_ts}">'
                f'{_chip_html}</td>')
    builds_badge = builds_cell()

    # New runs publish the catalog-declared context. Keep the legacy context as
    # a fallback so reports generated before this wiring remain discoverable.
    sbom_badge = badge('SBOM', 'CI / SBOM Generation', 'CI / SBOM',
                       cap='sbom-generation', doc=('sbom-report', 'sbom'))

    # Small camera/image glyph beside the commit message whenever this revision
    # has any rendered VI snapshots, so snapshot coverage is discoverable straight
    # from the main table (tooltip per request). Coverage is cached, so reusing it
    # here costs nothing.
    snap_glyph = ''
    if is_project:
        _gh, _gt = snapshot_coverage(sha)
        if _gh > 0:
            _gtip = ('Snapshots exist for this revision'
                     if _gh >= _gt else
                     f'Snapshots exist for this revision ({_gh} of {_gt} VIs)')
            snap_glyph = (f'<a href="{pages_url}/vi-snapshots/index.html?sha={sha}" '
                          f'class="snap-glyph" title="{_gtip}" aria-label="{_gtip}">{SNAP_ICON}</a>')

    # Avatar for the rich Revision cell: a deterministic initial-circle (no
    # network-avatar dependency - the commits API often has no linked GitHub
    # user). The hue is derived from the author name so each author keeps a
    # stable colour; white text on a mid-tone fill reads in both themes.
    _av_name = (author or '').strip()
    _av_initial = next((ch for ch in _av_name if ch.isalnum()), '?').upper()
    _av_hue = (sum(ord(ch) for ch in _av_name) % 360) if _av_name else 210
    _av_bg = f'hsl({_av_hue},42%,45%)'
    _browse = f'{pages_url}/vi-snapshots/index.html?sha={sha}'

    rows_html.append(f"""
    <tr data-project="{proj_flag}" data-deponly="{dep_only_flag}">
      <td class="cidash-rev">
        <div class="cidash-rev-wrap">
          <span class="cidash-avatar" style="background:{_av_bg}" title="{html.escape(author)}" aria-hidden="true">{html.escape(_av_initial)}</span>
          <div class="cidash-rev-body">
            <div class="cidash-rev-line1"><a href="{_browse}" class="cidash-rev-msg" title="{html.escape(msg)}">{html.escape(msg)}</a>{snap_glyph}</div>
            <div class="cidash-rev-meta"><a href="{_browse}" class="cidash-rev-sha" title="Browse this revision's VIs in the VI Browser">{short}</a><span class="cidash-rev-dot">&middot;</span>{html.escape(author)}<span class="cidash-rev-dot">&middot;</span>{date[:10]}</div>
          </div>
        </div>
      </td>
      {mc_badge}
      {via_badge}
      {diff_badge}
      {snap_badge}
      {unit_badge}
      {antidoc_badge}
      {builds_badge}
      {sbom_badge}
    </tr>""")

rows = '\n'.join(rows_html)
hist_json = _json.dumps(hist_revs)
caps_ran_json = _json.dumps({c: 1 for c in sorted(caps_ran)})
# Best-effort: the run-time estimate is a convenience. A timing/API hiccup must
# never break dashboard generation - the client falls back to TIMING_DEFAULT
# whenever a capability is absent from RT_TIMING.
try:
    run_timing_json = _json.dumps(build_run_timing())
except Exception:
    run_timing_json = '{}'
now  = __import__('datetime').datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')

# The "Include CI-only revisions" toggle is de-selected by default, so the
# dashboard opens on project revisions (those that change LabVIEW source).
# The toggle is honored strictly: while it is de-selected, CI-only revisions
# stay hidden — there is no automatic override. If that leaves the table
# empty (e.g. during a long CI/tooling sprint where the recent commits don't
# touch VIs), an inline empty-state prompt invites enabling the toggle, so
# the page is never a silently blank table (see the filter script below).

# While something is actively running, poll faster so the live indicator
# (and its eventual result) surfaces promptly; otherwise refresh lazily.
refresh_secs = 60 if running_flag['on'] else 900
refresh_note = ('Live — refreshing every 60 s while CI runs'
                if running_flag['on'] else 'Auto-refreshes every 15 min')

# Title/header brand from the repo at runtime (just the repo name, no owner)
# so a client repo shows ITS own name — not the source repo's — and stays
# correct across tooling updates without any rebrand substitution.
repo_name = repo.split('/')[-1]

# ── Version badge + update notification ─────────────────────
# Read this repo's installed CI tooling version and the source repo it
# pulls tooling from. The badge (left of the toolbar) shows the version.
# Client repos (whose source repo differs from this repo) get a live
# check against the source's catalog and a notification glyph + What's
# New dialog when a newer release exists. The source repo itself always
# runs the latest version, so it needs no check.
_cat = {}
try:
    with open(os.environ.get('CATALOG_PATH', '.github/labview-ci/catalog.json'), encoding='utf-8') as _cf:
        _cat = json.load(_cf)
except Exception:
    _cat = {}
lvci_version   = str(_cat.get('version', '') or '')
_src           = _cat.get('source', {}) or {}
lvci_src_repo  = str(_src.get('repo', '') or '')
lvci_src_ref   = str(_src.get('ref', 'main') or 'main')
# Thin consumers have no catalog.json — fall back to the install manifest
# (.github/labview-ci.yml) for the installed version + source pointer so the
# version badge still works.
if not lvci_version:
    try:
        import re as _re
        _in_src = False
        for _line in open('.github/labview-ci.yml', encoding='utf-8'):
            _m = _re.match(r'^\s*installedVersion:\s*(\S+)', _line)
            if _m: lvci_version = _m.group(1).strip()
            if _re.match(r'^\s*source:\s*$', _line): _in_src = True; continue
            if _in_src:
                _m = _re.match(r'^\s*repo:\s*(\S+)', _line)
                if _m and not lvci_src_repo: lvci_src_repo = _m.group(1).strip()
                _m = _re.match(r'^\s*ref:\s*(\S+)', _line)
                if _m: lvci_src_ref = _m.group(1).strip()
                if _line and not _line[0].isspace(): _in_src = False
    except Exception:
        pass

# Per-repo concurrency cap (config.concurrency.maxParallel in .github/labview-ci.yml,
# default 5). Parsed for config compatibility, but NOT currently enforced for the
# workflow_dispatch backfills (each is an independent run paced only by GitHub's
# account-wide runner concurrency), so the backfill card no longer advertises it.
lvci_max_parallel = 5
try:
    for _cline in open('.github/labview-ci.yml', encoding='utf-8'):
        _cs = _cline.strip()
        if _cs.startswith('maxParallel:'):
            _cv = _cs.split(':', 1)[1].split('#', 1)[0].strip()
            if _cv.isdigit():
                lvci_max_parallel = int(_cv)
            break
except Exception:
    pass

# Dependency-only revisions (whose only project change is a .vipc/.vip bump) never
# get per-VI CI results, so they are filtered out of the dashboard by default. The
# user can still reveal them with the "Include dependency-only revisions" toggle.
lvci_dep_ci_on = False
lvci_is_source = (not lvci_src_repo) or (lvci_src_repo.lower() == repo.lower())
lvci_cfg_json  = json.dumps({
    'version': lvci_version, 'sourceRepo': lvci_src_repo,
    'sourceRef': lvci_src_ref, 'isSource': bool(lvci_is_source), 'repo': repo,
})
version_badge_html = ''
version_check_script = ''
if lvci_version:
    _badge_title = ('Latest LabVIEW CI tooling version' if lvci_is_source
                    else 'Installed LabVIEW CI tooling version')
    version_badge_html = (
        '<div id="lvci-version" title="' + _badge_title + '" '
        'style="position:relative;display:inline-flex;align-items:center;gap:6px;'
        'background:rgba(110,118,129,.15);color:var(--fg-muted);'
        'border:1px solid rgba(110,118,129,.35);padding:8px 12px;border-radius:6px;'
        'font-size:.78em;font-weight:600;font-family:ui-monospace,SFMono-Regular,Menlo,monospace;'
        'user-select:none"><span>v' + lvci_version + '</span>'
        '<span id="lvci-update-dot" aria-hidden="true"></span></div>'
    )
    version_check_script = (
        '<style>'
        '#lvci-version.lvci-clickable{cursor:pointer;color:var(--fg)}'
        '#lvci-version.lvci-clickable:hover{background:rgba(110,118,129,.25)}'
        '#lvci-version.lvci-has-update{cursor:pointer;color:var(--fg);'
        'border-color:#d29922;background:rgba(210,153,34,.13)}'
        '#lvci-version.lvci-has-update:hover{background:rgba(210,153,34,.22)}'
        '#lvci-version.lvci-updating{cursor:pointer;color:var(--fg);'
        'border-color:#1f6feb;background:rgba(31,111,235,.14)}'
        '#lvci-version.lvci-updating:hover{background:rgba(31,111,235,.24)}'
        '.lvci-spin{width:10px;height:10px;border:2px solid rgba(88,166,255,.4);'
        'border-top-color:#58a6ff;border-radius:50%;display:inline-block;'
        'animation:lvci-spin-kf .7s linear infinite}'
        '@keyframes lvci-spin-kf{to{transform:rotate(360deg)}}'
        '#lvci-update-dot{display:none}'
        '#lvci-update-dot.on{display:block;position:absolute;top:-5px;right:-5px;'
        'width:11px;height:11px;border-radius:50%;background:#d29922;'
        'box-shadow:0 0 0 2px var(--bg);animation:lvci-pulse 1.7s ease-out infinite}'
        '@keyframes lvci-pulse{0%{box-shadow:0 0 0 2px var(--bg),0 0 0 0 rgba(210,153,34,.5)}'
        '70%{box-shadow:0 0 0 2px var(--bg),0 0 0 7px rgba(210,153,34,0)}'
        '100%{box-shadow:0 0 0 2px var(--bg),0 0 0 0 rgba(210,153,34,0)}}'
        '</style>'
        '<script>(function(){'
        'var C=' + lvci_cfg_json + ';'
        'function cmp(a,b){var p=String(a||"0").split("."),q=String(b||"0").split(".");'
        'for(var i=0;i<Math.max(p.length,q.length);i++){var d=(parseInt(p[i],10)||0)-(parseInt(q[i],10)||0);if(d)return d;}return 0;}'
        'if(!C.version)return;'
        'var b=document.getElementById("lvci-version");if(!b)return;'
        # The badge always opens the release-notes / What's New dialog, whether or
        # not an update exists. A single reassignable action ("act") lets the in-flight
        # branch repoint the click to the running upgrade action without stacking listeners.
        'var src=C.sourceRepo||C.repo;'
        'var go=function(){lvciOpen("whats-new.html?repo="+encodeURIComponent(C.repo)+"&from="+encodeURIComponent(C.version)+"&src="+encodeURIComponent(src)+"&ref="+encodeURIComponent(C.sourceRef),"What\\u2019s New");};'
        'var act=go;'
        'b.classList.add("lvci-clickable");'
        'b.setAttribute("role","button");b.setAttribute("tabindex","0");'
        'b.title="LabVIEW CI v"+C.version+" \\u2014 click for release notes";'
        'b.addEventListener("click",function(){act();});'
        'b.addEventListener("keydown",function(e){if(e.key==="Enter"||e.key===" "){e.preventDefault();act();}});'
        # GitHub API helper. Reuse the Run-now dispatch token if present (higher rate
        # limit); otherwise unauthenticated, which works for public repos.
        'function tok(){try{return localStorage.getItem("lvci_dispatch_token")||"";}catch(e){return "";}}'
        'function api(p){var h={"Accept":"application/vnd.github+json","X-GitHub-Api-Version":"2022-11-28"};var t=tok();if(t)h.Authorization="Bearer "+t;'
        'return fetch("https://api.github.com/repos/"+C.repo+p,{headers:h,cache:"no-store"}).then(function(r){return r.ok?r.json():null;}).catch(function(){return null;});}'
        'function active(runs){var S=["queued","in_progress","requested","waiting","pending"];'
        'return (runs||[]).filter(function(r){return S.indexOf(r.status)>=0&&Date.parse(r.created_at)>Date.now()-2*3600*1000;})[0];}'
        # Replace the version pill with an in-flight "Updating" indicator (spinner +
        # blue) that opens the running action. Takes priority over the update dot.
        'function inflight(label,url,tip){b.classList.remove("lvci-clickable","lvci-has-update");b.classList.add("lvci-updating");'
        'b.textContent="";var sp=document.createElement("span");sp.className="lvci-spin";'
        'var tx=document.createElement("span");tx.textContent=label;b.appendChild(sp);b.appendChild(tx);'
        'b.title=tip;act=function(){window.open(url,"_blank","noopener");};}'
        # Optimistic "Updating" indicator. The apply-tooling-update workflow runs for
        # only a few seconds, so polling for it on a 15-min auto-refresh almost never
        # catches it. When the What's New dialog dispatches the update it records a flag
        # in localStorage (and calls window.lvciMarkUpdating below), so we paint the
        # badge immediately and keep it across refreshes until this build reaches the
        # target version or a 30-min TTL elapses.
        'function updGet(){try{var s=localStorage.getItem("lvci_updating");if(!s)return null;var o=JSON.parse(s);'
        # Dashboards for different repos share one origin (user.github.io) = one
        # localStorage. Ignore (don't clear) a flag written by another repo's dashboard.
        'if(o&&o.repo&&o.repo!==C.repo)return null;'
        'if(!o||!o.v||Date.now()-o.ts>18e5||cmp(C.version,o.v)>=0){localStorage.removeItem("lvci_updating");return null;}return o;}catch(e){return null;}}'
        'function paintUpd(v){inflight("Updating\\u2026","https://github.com/"+C.repo+"/pulls","Updating to v"+v+" \\u2014 click to review or merge the update pull request");}'
        'window.lvciMarkUpdating=function(v){try{localStorage.setItem("lvci_updating",JSON.stringify({v:v,ts:Date.now(),repo:C.repo}));}catch(e){}paintUpd(v);};'
        # The source-of-truth repo never "updates": it ORIGINATES versions. Cutting a
        # release bumps its own catalog ahead of the just-built dashboard, which would
        # otherwise trip the "Updating to vX" redeploy hint below. Skip all updating
        # chrome so the source badge stays a plain release-notes link.
        'if(C.isSource||!C.sourceRepo)return;'
        'var _upd=updGet();if(_upd){paintUpd(_upd.v);return;}'
        # 1) Is the update workflow itself running (just clicked Update now / the PR is
        #    being opened)? -> "Updating..." linking to that run.
        'api("/actions/workflows/apply-tooling-update.yml/runs?per_page=5").then(function(j){'
        'var a=active(j&&j.workflow_runs);'
        'if(a){inflight("Updating\\u2026",a.html_url,"LabVIEW CI update in progress \\u2014 click to view the run");return true;}return false;'
        '}).then(function(done){if(done)return done;'
        # 2) Was an update already merged (this repo\u2019s committed catalog version is newer
        #    than the version THIS page was built at)? Then the dashboard is rebuilding/
        #    deploying \u2014 the exact window where the badge used to silently lag. Show
        #    "Updating to vX..." linking to the in-flight (or latest) dashboard build.
        'return fetch("https://raw.githubusercontent.com/"+C.repo+"/HEAD/.github/labview-ci/catalog.json",{cache:"no-store"})'
        '.then(function(r){return r.ok?r.json():null;}).then(function(cat){'
        'if(!cat||!cat.version||cmp(cat.version,C.version)<=0)return false;'
        'return api("/actions/workflows/dashboard-pages.yml/runs?per_page=3").then(function(j){'
        'var runs=(j&&j.workflow_runs)||[];var r=active(runs)||runs[0];var url=r?r.html_url:("https://github.com/"+C.repo+"/actions");'
        'inflight("Updating to v"+cat.version+"\\u2026",url,"Update to v"+cat.version+" is being deployed \\u2014 click to watch the build");return true;});});'
        '}).then(function(done){if(done)return done;'
        # 3) Otherwise the normal "update available" check (clients only): flag the
        #    badge amber + pulsing dot when the source publishes a newer release.
        #    First follow the relocation pointer (source.json) so a moved project is
        #    compared against its new official home; the badge links to whats-new.html,
        #    which shows the move and re-points this repo on update.
        'if(C.isSource||!C.sourceRepo)return;'
        'var sR=C.sourceRepo,sF=C.sourceRef;'
        'fetch("https://raw.githubusercontent.com/"+sR+"/"+sF+"/.github/labview-ci/source.json",{cache:"no-store"})'
        '.then(function(r){return r.ok?r.json():null;}).then(function(p){'
        'if(p&&p.repo&&p.repo.toLowerCase()!==sR.toLowerCase()){sR=p.repo;sF=p.ref||sF;}'
        'return fetch("https://raw.githubusercontent.com/"+sR+"/"+sF+"/.github/labview-ci/catalog.json",{cache:"no-store"});'
        '}).then(function(r){return r.ok?r.json():null;}).then(function(cat){'
        'if(!cat||!cat.version||cmp(cat.version,C.version)<=0)return;'
        'var d=document.getElementById("lvci-update-dot");'
        'b.classList.remove("lvci-clickable");b.classList.add("lvci-has-update");if(d)d.classList.add("on");'
        'b.title="Update available: v"+C.version+" \\u2192 v"+cat.version+" \\u2014 click to see what\\u2019s new";'
        '}).catch(function(){});});})();</scr' + 'ipt>'
    )

# ── "Run this cell" dialog ──────────────────────────────────
# Styling for the subtle play glyph shown in empty project cells.
run_dialog_css = (
    '.cidash-run{display:inline-block;color:var(--fg-muted);font-size:.95em;'
    'line-height:1;text-decoration:none;opacity:.5;transition:opacity .12s,color .12s}'
    '.cidash-run:hover{opacity:1;color:var(--link);text-decoration:none}'
    'tr:hover .cidash-run{opacity:.85}'
    '.snap-glyph{display:inline-flex;align-items:center;color:var(--fg-muted);'
    'opacity:.6;text-decoration:none;flex:0 0 auto;transition:opacity .12s,color .12s}'
    '.snap-glyph:hover{opacity:1;color:var(--link)}'
    'tr:hover .snap-glyph{opacity:.85}'
    '.cidash-btn{border:1px solid var(--border);border-radius:6px;padding:8px 14px;'
    'font-size:.85em;font-weight:600;cursor:pointer;font-family:inherit}'
    '.cidash-go{background:#238636;color:#fff;border-color:transparent}'
    '.cidash-go:hover{background:#2ea043}.cidash-go:disabled{opacity:.6;cursor:default}'
    '.cidash-ghost{background:var(--bg);color:var(--fg)}'
    '.cidash-ghost:hover{border-color:var(--link)}'
    # Danger button used to cancel a queued run.
    '.cidash-danger{background:#da3633;color:#fff;border-color:transparent}'
    '.cidash-danger:hover{background:#f85149}.cidash-danger:disabled{opacity:.6;cursor:default}'
    # The Queued badge is now a button that opens the manage dialog (view / cancel).
    '.cidash-queued{cursor:pointer}'
    '.cidash-queued:hover{filter:brightness(1.13)}'
    '.cidash-queued:focus-visible{outline:2px solid var(--link);outline-offset:1px}'
    # "#N" place-in-queue chip shown inside the Queued badge when several are waiting.
    '.cidash-qpos{margin-left:5px;font-weight:700;opacity:.92;font-variant-numeric:tabular-nums}'
    '.cidash-qpos:empty{display:none}'
    # "Run all history" backfill card shown above the table on a fresh install.
    '.lvci-backfill{border:1px solid var(--border);border-left:3px solid #1f6feb;'
    'background:var(--surface);border-radius:10px;padding:14px 16px;margin:0 0 18px}'
    '.lvci-bf-main{display:flex;align-items:center;gap:14px;flex-wrap:wrap}'
    '.lvci-bf-icon{font-size:1.5em;line-height:1}'
    '.lvci-bf-text{flex:1 1 320px;min-width:240px;font-size:.9em;line-height:1.5}'
    '.lvci-bf-text strong{display:block;font-size:1.03em;margin-bottom:2px}'
    '.lvci-bf-text span{color:var(--fg-muted)}'
    '.lvci-bf-actions{display:flex;gap:8px;flex-wrap:wrap;align-items:center}'
    '.lvci-bf-tok{margin-top:12px;border-top:1px solid var(--border);padding-top:12px;font-size:.84em;color:var(--fg-muted)}'
    '.lvci-bf-status{font-size:.82em;margin-top:10px}'
    '.lvci-bf-status:empty{display:none}'
    # Concurrency note + the "How concurrency works" disclosure on the backfill card.
    '.lvci-bf-note{display:block;margin-top:8px}'
    '.lvci-bf-info{margin-top:8px;font-size:.92em}'
    '.lvci-bf-info>summary{display:inline-block;cursor:pointer;color:var(--link);list-style:none;font-weight:600}'
    '.lvci-bf-info>summary::-webkit-details-marker{display:none}'
    '.lvci-bf-info>summary:hover{text-decoration:underline}'
    '.lvci-bf-info[open]>summary{margin-bottom:8px}'
    '.lvci-bf-infobody{color:var(--fg-muted);line-height:1.55;border-left:2px solid #1f6feb;padding-left:12px}'
    '.lvci-bf-cfg{display:inline-block;margin-top:8px;color:var(--link);font-weight:600}'
    # "Populate history" dialog — form controls (start-from picker, activity
    # checkboxes, diff-based toggle). Reuses the run-modal chrome + cidash-btn.
    '.cidash-hist-sec{margin:0 0 16px}'
    '.cidash-hist-lbl{display:block;font-size:.72em;font-weight:700;text-transform:uppercase;letter-spacing:.05em;color:var(--fg-muted);margin:0 0 7px}'
    '.cidash-hist-scope{display:flex;flex-direction:column;gap:9px}'
    '.cidash-hist-radio{display:flex;align-items:center;gap:9px;font-size:.9em;cursor:pointer;user-select:none}'
    '.cidash-hist-radio input{accent-color:var(--link);width:15px;height:15px;margin:0;flex:0 0 auto}'
    '.cidash-hist-radio .sub{color:var(--fg-muted);font-size:.88em}'
    '.cidash-hist-rangerow{display:flex;flex-wrap:wrap;gap:10px 14px;align-items:center;padding:2px 0 4px 25px}'
    '.cidash-hist-rangerow label{display:flex;align-items:center;gap:7px;font-size:.84em;color:var(--fg-muted)}'
    '#cidash-hist-from,#cidash-hist-to{padding:7px 9px;background:var(--bg);color:var(--fg);border:1px solid var(--border);border-radius:6px;font-size:.85em;font-family:inherit;max-width:248px}'
    '.cidash-hist-spectools{display:flex;gap:14px;margin:2px 0 6px 25px;font-size:.8em}'
    '.cidash-hist-spectools a{color:var(--link);cursor:pointer}.cidash-hist-spectools a:hover{text-decoration:underline}'
    '.cidash-hist-speclist{display:flex;flex-direction:column;gap:1px;max-height:210px;overflow:auto;margin-left:25px;padding:5px;border:1px solid var(--border);border-radius:7px;background:var(--bg)}'
    '.cidash-hist-specitem{display:flex;align-items:center;gap:9px;padding:5px 7px;border-radius:5px;font-size:.84em;cursor:pointer;min-width:0}'
    '.cidash-hist-specitem:hover{background:var(--surface)}'
    '.cidash-hist-specitem input{accent-color:var(--link);width:14px;height:14px;margin:0;flex:0 0 auto}'
    '.cidash-hist-specitem .sh{font-family:ui-monospace,Menlo,monospace;color:var(--fg-muted);flex:0 0 auto}'
    '.cidash-hist-specitem .ms{overflow:hidden;text-overflow:ellipsis;white-space:nowrap;color:var(--fg)}'
    '.cidash-hist-revfilter{display:flex;flex-wrap:wrap;align-items:center;gap:10px;margin:0 0 10px}'
    '.cidash-hist-search{flex:1 1 220px;min-width:150px;padding:7px 10px;background:var(--bg);color:var(--fg);border:1px solid var(--border);border-radius:6px;font-size:.85em;font-family:inherit}'
    '.cidash-hist-inclchk{display:inline-flex;align-items:center;gap:6px;font-size:.82em;color:var(--fg-muted);cursor:pointer;white-space:nowrap}'
    '.cidash-hist-inclchk input{accent-color:var(--link);width:14px;height:14px;margin:0}'
    '.cidash-hist-specitem .cidash-hist-deptag{margin-left:auto;font-size:.7em;color:var(--fg-muted);border:1px solid var(--border);border-radius:4px;padding:0 5px;flex:0 0 auto}'
    '.cidash-hist-specempty{color:var(--fg-muted);font-size:.84em;padding:8px}'
    # Intro line + the Activities header row (label on the left, quick-preset chips
    # on the right).
    '.cidash-hist-intro{margin:0 0 16px;color:var(--fg-muted);font-size:.86em;line-height:1.55}'
    '.cidash-hist-actshead{display:flex;align-items:center;justify-content:space-between;flex-wrap:wrap;gap:8px 10px;margin:0 0 9px}'
    '.cidash-hist-quick{display:flex;flex-wrap:wrap;gap:6px}'
    '.cidash-hist-chip{border:1px solid var(--border);background:var(--bg);color:var(--fg);border-radius:999px;padding:4px 11px;font-size:.76em;font-weight:600;cursor:pointer;font-family:inherit}'
    '.cidash-hist-chip:hover{border-color:var(--link);color:var(--link)}'
    # One row per activity. The name + description sit on the left above a meta
    # line (counts + optional Windows/Linux platform toggles); the Skip / Fill /
    # Re-run segmented control is pinned to the right and stays vertically
    # centred, so every row's primary control lines up no matter how much
    # metadata it carries - and the control can never be clipped by the dialog
    # edge. This scales cleanly as activities and platforms grow.
    '.cidash-hist-acts{display:flex;flex-direction:column;gap:7px}'
    '.cidash-hist-actsempty{color:var(--fg-muted);font-size:.85em;padding:4px 2px}'
    '.cidash-hist-act{display:grid;grid-template-columns:minmax(0,1fr) auto;grid-template-areas:"info controls" "meta controls";gap:3px 16px;align-items:center;padding:10px 14px;border:1px solid var(--border);border-radius:8px;background:var(--bg)}'
    # A skipped activity dims only its name + meta (so it reads as "off") while its
    # Skip / Fill / Re-run control stays full-strength and clearly clickable -
    # dimming the whole row made "Clear" look like nothing could be selected.
    '.cidash-hist-act.skip .cidash-hist-actinfo,.cidash-hist-act.skip .cidash-hist-actmeta{opacity:.5}'
    '.cidash-hist-actinfo{grid-area:info;min-width:0}'
    '.cidash-hist-actname{font-size:.9em;font-weight:600}'
    '.cidash-hist-actsub{color:var(--fg-muted);font-size:.82em;margin-top:1px;line-height:1.4}'
    # Meta line under the name: revision counts plus any platform toggles, wrapping
    # gracefully instead of pushing the control off-screen.
    '.cidash-hist-actmeta{grid-area:meta;display:flex;align-items:center;flex-wrap:wrap;gap:4px 14px;min-width:0;font-size:.76em;color:var(--fg-muted)}'
    '.cidash-hist-actcount{white-space:nowrap;font-variant-numeric:tabular-nums}'
    '.cidash-hist-actcount:empty{display:none}'
    '.cidash-hist-plats{display:flex;align-items:center;gap:12px;flex-wrap:wrap}'
    '.cidash-hist-plats label{display:inline-flex;align-items:center;gap:5px;cursor:pointer;user-select:none}'
    '.cidash-hist-plats input{accent-color:var(--link);width:13px;height:13px;margin:0}'
    '.cidash-seg{grid-area:controls;justify-self:end;align-self:center;display:inline-flex;flex:0 0 auto;border:1px solid var(--border);border-radius:7px;overflow:hidden}'
    '.cidash-seg button{border:0;background:transparent;color:var(--fg-muted);padding:6px 13px;font-size:.78em;font-weight:600;cursor:pointer;font-family:inherit;line-height:1.5}'
    '.cidash-seg button+button{border-left:1px solid var(--border)}'
    '.cidash-seg button.on{background:var(--link);color:#fff}'
    '.cidash-seg button:disabled{opacity:.34;cursor:default}'
    '.cidash-seg button:not(.on):not(:disabled):hover{background:var(--surface);color:var(--fg)}'
    # On narrow viewports the row collapses to a single column: name, meta, then
    # the control on its own line, all left-aligned.
    '@media(max-width:560px){.cidash-hist-act{grid-template-columns:1fr;grid-template-areas:"info" "meta" "controls";gap:7px 0}.cidash-seg{justify-self:start}}'
    '.cidash-hist-summary{font-size:.84em;color:var(--fg-muted);margin:0 0 12px}'
    '.cidash-hist-summary b{color:var(--fg)}'
)
# Modal + controller. Clicking a cell's play glyph opens this; clicking "Run now"
# DISPATCHES the workflow(s) straight to GitHub Actions via the REST API
# (browser -> api.github.com), so the run actually starts — no terminal, no
# copy-paste. Dispatch needs a credential the browser can send, so the user pastes
# a fine-grained token ONCE; it lives only in this browser's localStorage and is
# sent only to api.github.com (never to the repo, the page, or any third party).
# A "Run on GitHub" link is offered as a no-token fallback, and the equivalent
# `gh` command is tucked away for CLI users.
run_dialog = (r"""
  <div id="cidash-run-modal" onclick="if(event.target===this)cidashRunClose()" style="display:none;position:fixed;inset:0;z-index:310;background:rgba(0,0,0,.55)">
    <div role="dialog" aria-modal="true" aria-labelledby="cidash-run-title" style="position:absolute;left:50%;top:50%;transform:translate(-50%,-50%);width:min(560px,calc(100% - 32px));max-height:calc(100% - 48px);overflow:auto;background:var(--bg);border:1px solid var(--border);border-radius:10px;box-shadow:0 10px 48px rgba(0,0,0,.5)">
      <div style="display:flex;align-items:center;justify-content:space-between;padding:12px 16px;border-bottom:1px solid var(--border);background:var(--surface)">
        <strong id="cidash-run-title" style="font-size:.95em">Run</strong>
        <button onclick="cidashRunClose()" style="background:transparent;border:1px solid var(--border);color:var(--fg);padding:5px 12px;border-radius:6px;cursor:pointer;font-size:.82em">&#10005; Close</button>
      </div>
      <div id="cidash-run-body" style="padding:16px"></div>
    </div>
  </div>
  <div id="cidash-q-modal" onclick="if(event.target===this)cidashQClose()" style="display:none;position:fixed;inset:0;z-index:311;background:rgba(0,0,0,.55)">
    <div role="dialog" aria-modal="true" aria-labelledby="cidash-q-title" style="position:absolute;left:50%;top:50%;transform:translate(-50%,-50%);width:min(480px,calc(100% - 32px));max-height:calc(100% - 48px);overflow:auto;background:var(--bg);border:1px solid var(--border);border-radius:10px;box-shadow:0 10px 48px rgba(0,0,0,.5)">
      <div style="display:flex;align-items:center;justify-content:space-between;padding:12px 16px;border-bottom:1px solid var(--border);background:var(--surface)">
        <strong id="cidash-q-title" style="font-size:.95em">Queued run</strong>
        <button onclick="cidashQClose()" style="background:transparent;border:1px solid var(--border);color:var(--fg);padding:5px 12px;border-radius:6px;cursor:pointer;font-size:.82em">&#10005; Close</button>
      </div>
      <div id="cidash-q-body" style="padding:16px"></div>
    </div>
  </div>
  <div id="cidash-hist-modal" onclick="if(event.target===this)cidashHistClose()" style="display:none;position:fixed;inset:0;z-index:310;background:rgba(0,0,0,.55)">
    <div role="dialog" aria-modal="true" aria-labelledby="cidash-hist-title" style="position:absolute;left:50%;top:50%;transform:translate(-50%,-50%);width:min(640px,calc(100% - 32px));max-height:calc(100% - 48px);overflow:auto;background:var(--bg);border:1px solid var(--border);border-radius:10px;box-shadow:0 10px 48px rgba(0,0,0,.5)">
      <div style="display:flex;align-items:center;justify-content:space-between;padding:12px 16px;border-bottom:1px solid var(--border);background:var(--surface)">
        <strong id="cidash-hist-title" style="font-size:.95em">Populate dashboard history</strong>
        <button onclick="cidashHistClose()" style="background:transparent;border:1px solid var(--border);color:var(--fg);padding:5px 12px;border-radius:6px;cursor:pointer;font-size:.82em">&#10005; Close</button>
      </div>
      <div id="cidash-hist-body" style="padding:16px"></div>
    </div>
  </div>
  <script>
  (function(){
    var RT = __RUN_TARGETS__;
    var RT_TIMING = __RUN_TIMING__;
    var REPO = "__REPO__";
    var BRANCH = "__BRANCH__";
    var TOK_KEY = "lvci_dispatch_token";
    var state = {cap:null, sha:'', parent:'', short:''};
    function $(id){ return document.getElementById(id); }
    function esc(s){ return String(s==null?'':s).replace(/[&<>"']/g,function(c){return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]; }); }
    function fill(t){ return String(t).replace(/\{sha\}/g, state.sha).replace(/\{parent\}/g, state.parent||''); }
    // The dashboard's own dispatch token; if it isn't set, fall back to the token
    // saved during "Apply to New Repo" (lvci_install_token — same browser, same
    // origin) so the SAME token used to set the dashboard up also queues runs here
    // with no second token to create. It still needs Actions: Read and write to
    // dispatch; if it lacks that, the 403 hint explains the one box to add.
    function getTok(){ try{ return localStorage.getItem(TOK_KEY)||localStorage.getItem("lvci_install_token")||''; }catch(e){ return ''; } }
    function setTok(v){ try{ localStorage.setItem(TOK_KEY, v); }catch(e){} }
    function clearTok(){ try{ localStorage.removeItem(TOK_KEY); }catch(e){} }
    function ghCmd(wf, inputs){
      var s = "gh workflow run " + wf + " --ref " + BRANCH;
      Object.keys(inputs).forEach(function(k){ s += " -f " + k + "=" + fill(inputs[k]); });
      return s;
    }
    function actionsUrl(wf){ return "https://github.com/" + REPO + "/actions/workflows/" + wf; }
    // Pre-fill the fine-grained token form: name, resource owner, and the one
    // permission a workflow_dispatch needs (Actions: write). GitHub provides no
    // URL param to pre-select the specific repo, so "Repository access" stays a
    // manual step. Same token (lvci_dispatch_token) the report header + What's New use.
    function tokenSetupUrl(){
      var owner = (REPO.split("/")[0]) || "";
      var qp = "name=" + encodeURIComponent("LabVIEW CI dispatch")
        + "&description=" + encodeURIComponent("Queue LabVIEW CI runs for " + REPO + " (Run now / Re-run / Update).")
        + (owner ? "&target_name=" + encodeURIComponent(owner) : "")
        + "&actions=write";
      return "https://github.com/settings/personal-access-tokens/new?" + qp;
    }
    function cap(s){ return s.charAt(0).toUpperCase() + s.slice(1); }
    function selectedPlats(def){
      var keys = Object.keys(def.platforms);
      if (keys.length === 1) return keys;
      var boxes = keys.map(function(k){ return $("cidash-plat-"+k); });
      if (boxes.every(function(b){ return !b; })) return keys.slice();
      return keys.filter(function(k){ var b = $("cidash-plat-"+k); return b && b.checked; });
    }
    function setStatus(html, kind){
      var s = $("cidash-run-status"); if(!s) return;
      var col = kind==='ok' ? '#3fb950' : (kind==='err' ? '#f85149' : (kind==='warn' ? '#d29922' : 'var(--fg-muted)'));
      s.style.color = col; s.innerHTML = html || '';
    }
    function filledInputs(p){ var o={}; Object.keys(p.inputs).forEach(function(k){ o[k]=fill(p.inputs[k]); }); return o; }
    function dispatchOne(wf, inputs){
      return fetch('https://api.github.com/repos/'+REPO+'/actions/workflows/'+encodeURIComponent(wf)+'/dispatches', {
        method:'POST',
        headers:{ 'Authorization':'Bearer '+getTok(), 'Accept':'application/vnd.github+json',
                  'X-GitHub-Api-Version':'2022-11-28', 'Content-Type':'application/json' },
        body: JSON.stringify({ ref: BRANCH, inputs: inputs })
      }).then(function(r){ return { wf:wf, ok:r.status===204, status:r.status }; })
        .catch(function(e){ return { wf:wf, ok:false, status:0, err:String(e&&e.message||e) }; });
    }
    // ── Optimistic "queued" overlay ──────────────────────────────
    // A successful dispatch returns 204 with no body, and the server-rendered
    // "running" badge only appears once the run posts a pending commit status AND
    // the Pages site rebuilds — minutes later. So the dispatched cell would keep
    // showing its empty run-glyph, making it look like nothing happened. Paint the
    // cell as "Queued" immediately and remember it per-browser (localStorage) so it
    // survives the page's auto-refresh until the real status takes over. Entries
    // self-expire so a run that never reports a status can't wedge a fake spinner.
    var QKEY = "lvci_queued_runs";
    var QTTL = 20*60*1000;   // anti-phantom: drop an entry we could NEVER tie to a real run (no run id) after 20 min
    var QDONE = 5*60*1000;   // after a run finishes OK, keep the cell up to this long while the server result rebuilds
    var QFAST = 60*1000;     // re-check this often while a queued run is unconfirmed
    var QPOSTTL = 5*60*1000; // how long a fetched queue snapshot stays trustworthy for numbering
    var qReloadArmed = false;
    var qCapturing = {};     // key -> [callbacks] while a run-id lookup is in flight
    // The REAL place-in-line of every run still waiting for a runner on this repo,
    // built by qSync from the live Actions API: { runId(str): position(1-based) }.
    // null until the first fetch; qQueueTotal = how many runs are waiting in all.
    var qQueuePos = null, qQueueTotal = 0, qQueueAt = 0;
    function qLoad(){ try{ return JSON.parse(localStorage.getItem(QKEY)||"{}")||{}; }catch(e){ return {}; } }
    function qSave(o){ try{ localStorage.setItem(QKEY, JSON.stringify(o)); }catch(e){} }
    function resultCell(c, sha){
      // The cell carrying a re-runnable activity's result OR its in-flight run - both
      // are valid targets to overlay an optimistic "Queued" spinner onto. A finished
      // result is a cidash-cap-cell; a queued/running auto-run is a cidash-active-cell
      // (tagged so the Populate-history dialog can re-run an in-flight activity). Matching
      // both is what lets a re-run of an in-flight cell show its Queued badge, not just
      // re-runs of finished ones (the latest revision's activity is usually still in flight).
      return document.querySelector('td.cidash-cap-cell[data-cap="'+c+'"][data-sha="'+sha+'"]')
          || document.querySelector('td.cidash-active-cell[data-cap="'+c+'"][data-sha="'+sha+'"]');
    }
    function qPaint(td, c, sha){
      // Overlay the spinning "Queued" badge onto the cell, replacing its run glyph.
      // The badge is now a BUTTON (not a link): clicking it opens the manage dialog
      // where the run can be viewed on GitHub or cancelled. A "#N" chip (filled in
      // by qRenumber) shows the run's place in the queue when several are waiting.
      if(!td) return;
      td.classList.add('cidash-queued-cell');
      td.setAttribute('data-qcap', c); td.setAttribute('data-qsha', sha);
      td.innerHTML = '<span class="run-badge cidash-queued" role="button" tabindex="0" '
        + 'title="Queued from this browser \u2014 click to view it on GitHub or cancel it">'
        + '<span class="run-spin"></span>Queued<span class="cidash-qpos"></span></span>';
    }
    function qPaintFailed(td, c, sha){
      // A run we CONFIRMED failed on GitHub: replace the spinner with a sticky red
      // "bang" badge (same button affordance as Queued, so clicking opens the
      // manage dialog to view the failed run or dismiss it). Never reverts on its own.
      if(!td) return;
      td.classList.add('cidash-queued-cell');
      td.setAttribute('data-qcap', c); td.setAttribute('data-qsha', sha);
      td.innerHTML = '<span class="run-badge cidash-queued cidash-failed" role="button" tabindex="0" '
        + 'title="This run failed \u2014 click to see the error on GitHub or dismiss it">'
        + '<span class="cidash-bang" aria-hidden="true">!</span>Failed</span>';
    }
    function qArmReload(){
      // While a queued run is still unconfirmed, reload sooner than the lazy
      // auto-refresh so the real status surfaces promptly (mirrors the server's
      // own faster cadence while CI is live). Never reload while a dialog (Apply to
      // New Repo / Configure / ...) is open -- that would destroy the in-progress
      // iframe; defer and reload once it is closed.
      if(qReloadArmed) return; qReloadArmed = true;
      function fire(){
        if(typeof lvciModalOpen==='function' && lvciModalOpen()){ setTimeout(fire, 4000); return; }
        location.reload();
      }
      setTimeout(fire, QFAST);
    }
    function qLiveEntries(o){
      // The still-queued entries, oldest first - this IS the queue order. A failed
      // or finished entry is terminal so it is no longer "in line". An entry stays
      // while we can still reconcile its real run (a captured run id + a token),
      // while inside the optimistic window, OR while qSync still sees a run active
      // (e.activeAt), so a run held behind GitHub's concurrency cap keeps its place.
      var now = Date.now(); var tok = getTok();
      return Object.keys(o)
        .filter(function(k){ var e=o[k]; if(!e || e.failed || e.done) return false;
          var hasRun = (e.runs||[]).some(function(r){ return r && r.id; });
          return (hasRun && tok) || (now-(e.ts||0))<=QTTL || (e.activeAt && (now-e.activeAt)<=QTTL); })
        .map(function(k){ return { key:k, ts:o[k].ts||0 }; })
        .sort(function(a,b){ return a.ts - b.ts; });
    }
    function qRenumber(){
      // Stamp each optimistic queued cell with its place in line. Once qSync has
      // fetched the live queue, use the cell's REAL position there - counting every
      // run waiting for a runner on this repo, from any activity, push, or person -
      // so the numbers reflect reality (e.g. #9/#10 when 8 runs are already ahead),
      // not just how many this browser queued. Before that first fetch (or for a
      // cell whose run id hasn't been captured yet) fall back to this browser's own
      // order (oldest = #1). The chip is hidden unless more than one run is in line.
      var o = qLoad(); var live = qLiveEntries(o);
      var havePos = !!qQueuePos && (Date.now() - qQueueAt) <= QPOSTTL;
      var total = havePos ? qQueueTotal : live.length;
      var show = total > 1;
      live.forEach(function(en, idx){
        var i = en.key.indexOf('|'); var c = en.key.slice(0,i); var sha = en.key.slice(i+1);
        var td = document.querySelector('td.cidash-queued-cell[data-qcap="'+c+'"][data-qsha="'+sha+'"]');
        if(!td) return;
        var posEl = td.querySelector('.cidash-qpos'); if(!posEl) return;
        var real = 0;
        if(havePos){
          // The cell starts when its earliest-in-line platform run starts, so take
          // the best (lowest) real position among its still-waiting runs.
          ((o[en.key]||{}).runs||[]).forEach(function(r){
            var p = r && r.id && qQueuePos[String(r.id)];
            if(p && (!real || p < real)) real = p;
          });
        }
        var txt = '';
        if(show){
          if(real) txt = '#'+real;
          else if(!havePos) txt = '#'+(idx+1);   // pre-fetch: this browser's own order
          // else: real queue known but this cell isn't placed yet - leave it blank
          // until qSync resolves its run id, rather than show a misleading number.
        }
        posEl.textContent = txt;
      });
    }
    function markQueued(c, sha, plats, parent, ts){
      try {
        var o = qLoad(); var key = c+'|'+sha; var prev = o[key] || {};
        // Re-marking a cell (e.g. once its dispatch confirms which platforms took)
        // must NOT wipe a run id already captured for it, so carry runs/ts forward.
        o[key] = { ts: ts || prev.ts || Date.now(), plats: plats || prev.plats || [], parent: parent || prev.parent || '',
                   short: (sha||'').slice(0,7), runs: prev.runs || [] };
        qSave(o);
      } catch(e){}
      try {
        var a = document.querySelector('a.cidash-run[data-cap="'+c+'"][data-sha="'+sha+'"]');
        var td = a ? (a.closest ? a.closest('td') : null)
                   : document.querySelector('td.cidash-queued-cell[data-qcap="'+c+'"][data-qsha="'+sha+'"]');
        if(!td){
          // Re-run of a cell that already HAS a result: overlay the spinner onto the
          // result cell and remember its original badge so a failed/cancelled dispatch
          // can restore it. Same bridge the report viewer's "Re-run" uses (see qSync),
          // now reachable from the Populate-history dialog's per-activity "Re-run".
          var rc = resultCell(c, sha);
          if(rc){
            var oR = qLoad(); var kR = c+'|'+sha;
            if(oR[kR] && !oR[kR].orig){ oR[kR].orig = rc.innerHTML; qSave(oR); }
            td = rc;
          }
        }
        qPaint(td, c, sha);
        qArmReload();
        qRenumber();
      } catch(e){}
    }
    function qForget(c, sha){
      // Drop the optimistic entry and restore the cell's run glyph so the cell is
      // immediately actionable again (re-run) without waiting for a reload.
      var o = qLoad(); var entry = o[c+'|'+sha];
      delete o[c+'|'+sha]; qSave(o);
      var td = document.querySelector('td.cidash-queued-cell[data-qcap="'+c+'"][data-qsha="'+sha+'"]');
      if(td){
        td.classList.remove('cidash-queued-cell');
        td.removeAttribute('data-qcap'); td.removeAttribute('data-qsha');
        if(entry && entry.orig){
          // Result cell (re-run overlay) — restore its original status badge.
          td.innerHTML = entry.orig;
        } else {
          var parent = (entry && entry.parent) || '';
          var short = (entry && entry.short) || sha.slice(0,7);
          var label = (RT[c] && RT[c].label) || c;
          td.innerHTML = '<a href="#" class="cidash-run" data-cap="'+esc(c)+'" data-sha="'+esc(sha)+'" '
            + 'data-parent="'+esc(parent)+'" data-short="'+esc(short)+'" '
            + 'title="Run '+esc(label)+' for commit '+esc(short)+'">\u25B7</a>';
        }
      }
      qRenumber();
    }
    function entryItems(c, entry){
      // The (workflow, platform) pairs this queued entry dispatched.
      var def = RT[c]; if(!def || !entry) return [];
      return (entry.plats||[]).map(function(plat){
        var p = def.platforms[plat]; return p ? { wf: p.wf, plat: plat } : null;
      }).filter(function(x){ return x; });
    }
    function claimedRunIds(){
      // Run ids already attributed to some queued cell, so a lookup never grabs a
      // run that belongs to a different cell.
      var o = qLoad(); var s = {};
      Object.keys(o).forEach(function(k){ ((o[k]||{}).runs||[]).forEach(function(r){ if(r && r.id) s[r.id]=1; }); });
      return s;
    }
    function captureRuns(c, sha, attempt, cb){
      // A workflow_dispatch returns 204 with no run id, so to later CANCEL the run
      // (and link straight to it) we look it up: poll the workflow's recent
      // dispatch runs and claim the freshest active one we have not already
      // attributed to another cell. Best-effort, non-blocking, and self-limiting.
      // Concurrent lookups for the SAME cell (e.g. the manage dialog opening AND a
      // Cancel click) are coalesced so they share one network pass and both get
      // their callback when it resolves — no duplicate claims, no stalled retries.
      var key = c+'|'+sha;
      attempt = attempt || 1;
      if(attempt === 1){
        if(qCapturing[key]){ if(cb) qCapturing[key].push(cb); return; }
        qCapturing[key] = cb ? [cb] : [];
      }
      function done(){ var cbs = qCapturing[key] || []; delete qCapturing[key]; cbs.forEach(function(fn){ try{ fn(); }catch(e){} }); }
      var tok = getTok(); if(!tok){ done(); return; }
      var entry = qLoad()[key];
      if(!entry){ done(); return; }
      var need = entryItems(c, entry).filter(function(it){
        return !((entry.runs||[]).some(function(r){ return r.plat===it.plat; }));
      });
      if(!need.length){ done(); return; }
      var t0 = entry.ts || Date.now();
      var byWf = {}; need.forEach(function(it){ (byWf[it.wf]=byWf[it.wf]||[]).push(it.plat); });
      Promise.all(Object.keys(byWf).map(function(wf){
        return fetch('https://api.github.com/repos/'+REPO+'/actions/workflows/'+encodeURIComponent(wf)+'/runs?event=workflow_dispatch&per_page=20',
          { headers:{ 'Authorization':'Bearer '+tok, 'Accept':'application/vnd.github+json', 'X-GitHub-Api-Version':'2022-11-28' } })
          .then(function(r){ return r.ok ? r.json() : null; })
          .then(function(j){ return { wf: wf, runs: (j&&j.workflow_runs)||[] }; })
          .catch(function(){ return { wf: wf, runs: [] }; });
      })).then(function(resps){
        var claim = claimedRunIds();
        var o = qLoad(); var e = o[key]; if(!e){ done(); return; }
        e.runs = e.runs || [];
        var have = {}; e.runs.forEach(function(r){ have[r.plat]=1; });
        var claimedDone = false;
        resps.forEach(function(rp){
          var cands = rp.runs.filter(function(run){
            var created = Date.parse(run.created_at || run.run_started_at || 0);
            // Claim recently-COMPLETED runs too: a dispatch that dies within
            // seconds (startup failure, spending limit, cancelled while queued)
            // is already 'completed' by the time this lookup runs, and skipping
            // it left the cell an unconfirmable spinner that silently timed out
            // at QTTL. Claiming it lets qSync turn the cell into the sticky red
            // "Failed" bang instead. The t0 window keeps unrelated old runs out.
            var live = ['queued','in_progress','requested','waiting','pending','action_required','completed'].indexOf(run.status) >= 0;
            return live && created >= (t0 - 12000) && !claim[run.id];
          }).sort(function(a,b){ return Date.parse(b.created_at) - Date.parse(a.created_at); });
          (byWf[rp.wf]||[]).forEach(function(plat){
            if(have[plat]) return;
            var run = cands.shift(); if(!run) return;
            e.runs.push({ wf: rp.wf, plat: plat, id: run.id, url: run.html_url, status: run.status });
            if(run.status === 'completed') claimedDone = true;
            claim[run.id] = 1; have[plat] = 1;
          });
        });
        o[key] = e; qSave(o);
        // A claimed run has already finished — judge it now (red bang / done
        // handoff) instead of leaving a spinner up until the next lazy sync.
        if(claimedDone) qRecheck();
        var gotAll = entryItems(c, e).every(function(it){ return (e.runs||[]).some(function(r){ return r.plat===it.plat; }); });
        if(!gotAll && attempt < 5){ setTimeout(function(){ captureRuns(c, sha, attempt+1, null); }, 2000); }
        else { done(); }
      });
    }
    function captureSnapshotRun(t0, attempt){
      // The "Populate history" backfill is ONE snapshots run that walks the whole
      // history, so every "snapshots|<sha>" entry shares it. captureRuns' per-cell
      // claim would hand the single run to just the first sha, so look it up once
      // and write it into EVERY snapshot entry still missing a run id.
      var tok = getTok(); if(!tok) return;
      t0 = t0 || Date.now(); attempt = attempt || 1;
      fetch('https://api.github.com/repos/'+REPO+'/actions/workflows/'+encodeURIComponent('vi-snapshots.yml')+'/runs?event=workflow_dispatch&per_page=20',
        { headers:{ 'Authorization':'Bearer '+tok, 'Accept':'application/vnd.github+json', 'X-GitHub-Api-Version':'2022-11-28' } })
        .then(function(r){ return r.ok ? r.json() : null; })
        .then(function(j){
          var runs = (j && j.workflow_runs) || [];
          var run = runs.filter(function(rn){
            return Date.parse(rn.created_at || rn.run_started_at || 0) >= (t0 - 12000);
          }).sort(function(a,b){ return Date.parse(b.created_at) - Date.parse(a.created_at); })[0];
          if(!run){ if(attempt < 4){ setTimeout(function(){ captureSnapshotRun(t0, attempt+1); }, 2500); } return; }
          var o = qLoad(); var changed = false;
          Object.keys(o).forEach(function(key){
            if(key.indexOf('snapshots|') !== 0) return;
            var e = o[key]; if(!e || (e.runs||[]).length) return;
            e.runs = [{ wf:'vi-snapshots.yml', plat:'all', id:run.id, url:run.html_url, status:run.status }];
            changed = true;
          });
          if(changed) qSave(o);
        }).catch(function(){});
    }
    function captureBatchRun(wf, t0, attempt){
      // A warm-container batch is ONE run of the backfill workflow covering every
      // SHA in its list — captureRuns can never attribute it (it queries the
      // per-revision workflow, and its per-cell claim would hand a single run to
      // just the first sha anyway; the exact gap captureSnapshotRun closes for
      // snapshots). Look the batch run up once and write it into EVERY entry
      // stamped with this backfill workflow (dispatchCells sets entry.batchWf)
      // that still lacks a run id, so the whole group outlives the optimistic
      // window — or turns into the sticky red bang together if the run dies
      // before starting.
      var tok = getTok(); if(!tok) return;
      t0 = t0 || Date.now(); attempt = attempt || 1;
      fetch('https://api.github.com/repos/'+REPO+'/actions/workflows/'+encodeURIComponent(wf)+'/runs?event=workflow_dispatch&per_page=20',
        { headers:{ 'Authorization':'Bearer '+tok, 'Accept':'application/vnd.github+json', 'X-GitHub-Api-Version':'2022-11-28' } })
        .then(function(r){ return r.ok ? r.json() : null; })
        .then(function(j){
          var runs = (j && j.workflow_runs) || [];
          var run = runs.filter(function(rn){
            return Date.parse(rn.created_at || rn.run_started_at || 0) >= (t0 - 12000);
          }).sort(function(a,b){ return Date.parse(b.created_at) - Date.parse(a.created_at); })[0];
          if(!run){ if(attempt < 4){ setTimeout(function(){ captureBatchRun(wf, t0, attempt+1); }, 2500); } return; }
          var o = qLoad(); var changed = false;
          Object.keys(o).forEach(function(key){
            var e = o[key];
            if(!e || e.batchWf !== wf || (e.runs||[]).length) return;
            e.runs = [{ wf: wf, plat: (e.plats||[])[0] || 'windows', id: run.id, url: run.html_url, status: run.status }];
            changed = true;
          });
          if(changed){ qSave(o); if(run.status === 'completed') qRecheck(); }
        }).catch(function(){});
    }
    function qSync(){
      // Reconcile the optimistic "Queued" overlays against the REAL run status on
      // GitHub so a cell never silently reverts to its run glyph: a run still
      // queued/running keeps its badge alive (so a run held behind GitHub's
      // concurrency cap keeps its badge for as long as it takes), a run that
      // FAILED becomes a sticky red "bang" badge (an early failure may never post
      // a commit status for the server to render), and a finished-OK run is marked
      // done so the server result can take over. Runs are matched by their captured
      // id and looked up directly when too old to appear in the recent activity
      // list, so this still works when you come back to the dashboard much later.
      var tok = getTok(); if(!tok) return;
      var o = qLoad();
      var live = Object.keys(o).filter(function(k){ return o[k] && !o[k].failed && !o[k].done; });
      if(!live.length) return;
      var snapTs = 0, snapNeedsId = false;
      live.forEach(function(k){
        if(k.indexOf('snapshots|')===0){ var ts=o[k].ts||0; if(!snapTs||ts<snapTs) snapTs=ts; if(!((o[k].runs||[]).length)) snapNeedsId=true; }
      });
      if(snapNeedsId) captureSnapshotRun(snapTs);
      // Batch entries share ONE backfill run, so route their id lookup through
      // captureBatchRun (once per workflow, from the earliest entry ts) —
      // captureRuns would query the per-revision workflow and find nothing.
      var batchNeed = {};
      live.forEach(function(k){
        var eB = o[k];
        if(eB.batchWf && !((eB.runs||[]).length)){
          var bts = eB.ts || 0;
          if(!(eB.batchWf in batchNeed) || bts < batchNeed[eB.batchWf]) batchNeed[eB.batchWf] = bts;
        }
      });
      Object.keys(batchNeed).forEach(function(bwf){ captureBatchRun(bwf, batchNeed[bwf]); });
      live.forEach(function(k){
        var c = k.slice(0, k.indexOf('|')); var sha = k.slice(k.indexOf('|')+1);
        if(c!=='snapshots' && !o[k].batchWf && !((o[k].runs||[]).length)) captureRuns(c, sha);
      });
      // Every run id we still need a verdict on, across all non-terminal entries.
      var wantIds = {};
      live.forEach(function(k){ ((o[k]||{}).runs||[]).forEach(function(r){ if(r && r.id) wantIds[r.id]=1; }); });
      fetch('https://api.github.com/repos/'+REPO+'/actions/runs?per_page=100',
        { headers:{ 'Authorization':'Bearer '+tok, 'Accept':'application/vnd.github+json', 'X-GitHub-Api-Version':'2022-11-28' } })
        .then(function(r){ return r.ok ? r.json() : null; })
        .then(function(j){
          var byId = {}; if(j && j.workflow_runs){ j.workflow_runs.forEach(function(run){ byId[run.id] = run; }); }
          // A run from an earlier session can be too old for the recent list; fetch
          // those directly by id so a long-finished (or still-queued) run is still
          // reconciled. Capped so a big backlog can't fan out into many requests.
          var miss = Object.keys(wantIds).filter(function(id){ return !byId[id]; }).slice(0, 12);
          return Promise.all(miss.map(function(id){
            return fetch('https://api.github.com/repos/'+REPO+'/actions/runs/'+id,
              { headers:{ 'Authorization':'Bearer '+tok, 'Accept':'application/vnd.github+json', 'X-GitHub-Api-Version':'2022-11-28' } })
              .then(function(r){ return r.ok ? r.json() : null; })
              .then(function(run){ if(run && run.id) byId[run.id] = run; })
              .catch(function(){});
          })).then(function(){ return byId; });
        })
        .then(function(byId){
          if(!byId) return;
          // Build the REAL repo-wide queue order from the live run list: every run
          // still WAITING for a runner (not yet in_progress = running) is one slot,
          // oldest first. This counts runs queued by any activity, push, or person
          // on this repo - so the user's cells number behind whatever is ahead of
          // them. (A repo-scoped token can't see OTHER repos sharing the runner
          // pool, so account-wide contention beyond this repo isn't reflected.)
          var WAIT = ['queued','requested','waiting','pending'];
          var inLine = [];
          Object.keys(byId).forEach(function(id){
            var run = byId[id];
            if(run && WAIT.indexOf(run.status) >= 0){
              inLine.push({ id:String(run.id), at:Date.parse(run.created_at || run.run_started_at || '') || 0 });
            }
          });
          inLine.sort(function(a,b){ return (a.at - b.at) || (a.id < b.id ? -1 : 1); });
          qQueuePos = {}; inLine.forEach(function(r, i){ qQueuePos[r.id] = i + 1; });
          qQueueTotal = inLine.length; qQueueAt = Date.now();
          var o2 = qLoad(); var changed = false; var now = Date.now();
          var FAIL = ['failure','timed_out','startup_failure','cancelled'];
          var ACTIVE = ['queued','in_progress','requested','waiting','pending','action_required'];
          Object.keys(o2).forEach(function(key){
            var e = o2[key]; if(!e || e.failed || e.done) return;
            var ids = (e.runs||[]).map(function(r){ return r.id; }).filter(Boolean);
            if(!ids.length) return;
            var anyActive = false, anyFail = false, allKnown = true, allDone = true, failUrl = '';
            ids.forEach(function(id){
              var run = byId[id]; if(!run){ allKnown = false; allDone = false; return; }
              if(ACTIVE.indexOf(run.status) >= 0){ anyActive = true; allDone = false; }
              else if(run.status === 'completed'){ if(FAIL.indexOf(run.conclusion) >= 0){ anyFail = true; failUrl = failUrl || run.html_url || ''; } }
              else { allDone = false; }
            });
            if(anyActive){ e.activeAt = now; changed = true; }
            else if(anyFail && allKnown){ e.failed = { url: failUrl }; changed = true; }
            else if(allKnown && allDone){ e.done = now; changed = true; }
          });
          // applyQueued re-numbers as it repaints; otherwise renumber directly so
          // the freshly fetched real positions are applied even when nothing else
          // about this browser's entries changed (e.g. only the queue ahead moved).
          if(changed){ qSave(o2); applyQueued(); }
          else { qRenumber(); }
        }).catch(function(){});
    }
    function applyQueued(){
      // Re-apply remembered queued badges after each (auto-)reload. A confirmed
      // failure stays a sticky red "bang" badge; a confirmed-finished run hands off
      // to the server result; an entry whose run we can still reconcile (a captured
      // run id + a token) is kept alive until it actually finishes, even past the
      // optimistic window, so a run queued behind GitHub's concurrency cap never
      // silently vanishes. Entries we could never tie to a real run still age out.
      var o = qLoad(); var now = Date.now(); var changed = false; var live = 0; var tok = getTok();
      Object.keys(o).forEach(function(key){
        var e = o[key]; var i = key.indexOf('|'); var c = key.slice(0,i); var sha = key.slice(i+1);
        if(!e){ delete o[key]; changed = true; return; }
        var painted = document.querySelector('td.cidash-queued-cell[data-qcap="'+c+'"][data-qsha="'+sha+'"]');
        var a = document.querySelector('a.cidash-run[data-cap="'+c+'"][data-sha="'+sha+'"]');
        var rc = resultCell(c, sha);
        if(e.failed){
          if(rc){
            var frts = Date.parse(rc.getAttribute('data-ts')||'') || 0;
            if(frts && frts > (e.ts||0)){ delete o[key]; changed = true; return; }
          }
          var ftd = painted || (a ? a.closest('td') : null) || rc;
          if(ftd){ if(rc && !e.orig){ e.orig = rc.innerHTML; changed = true; } qPaintFailed(ftd, c, sha); }
          return;
        }
        if(e.done){
          // The run finished OK - hand off to the server-rendered result: forget as
          // soon as a real result cell is present, or after a short grace for the
          // Pages site to rebuild; until then it keeps showing the spinner below.
          if(rc){ delete o[key]; changed = true; return; }
          if((now - (e.done||0)) > QDONE){ delete o[key]; changed = true; return; }
        }
        var hasRun = (e.runs||[]).some(function(r){ return r && r.id; });
        var alive = (hasRun && tok) || (now - (e.ts||0)) <= QTTL || (e.activeAt && (now - e.activeAt) <= QTTL);
        if(!alive){ delete o[key]; changed = true; return; }
        if(painted){ live++; }
        else if(a){ qPaint(a.closest('td'), c, sha); live++; }
        else if(rc){
          var rts = Date.parse(rc.getAttribute('data-ts')||'') || 0;
          if(rts && rts > (e.ts||0)){ delete o[key]; changed = true; }
          else { if(!e.orig){ e.orig = rc.innerHTML; changed = true; } qPaint(rc, c, sha); live++; }
        }
        else { delete o[key]; changed = true; }
      });
      if(changed) qSave(o);
      if(live > 0) qArmReload();
      qRenumber();
    }
    function runNow(){
      var def = RT[state.cap]; if(!def) return;
      var sel = selectedPlats(def);
      if(!sel.length){ setStatus('Select at least one platform.', 'warn'); return; }
      if(!getTok()){ showTokenPanel(); return; }
      var go = $("cidash-run-go"); if(go){ go.disabled = true; }
      setStatus('Queuing\u2026', null);
      var t0 = Date.now();   // dispatch start — used to match the new run(s) for cancel
      var jobs = sel.map(function(k){ var p=def.platforms[k];
        return dispatchOne(p.wf, filledInputs(p)).then(function(res){ res.plat=k; return res; }); });
      Promise.all(jobs).then(function(results){
        if(go){ go.disabled = false; }
        if(results.some(function(r){return r.status===401;})){
          clearTok(); setStatus('That token was rejected (401). Paste a valid one.', 'err'); showTokenPanel(); return;
        }
        // Reflect any successful dispatch on the dashboard right away, before the
        // server-side status/Pages rebuild catches up (covers partial success too).
        var okPlats = results.filter(function(r){return r.ok;}).map(function(r){return r.plat;});
        if(okPlats.length){ markQueued(state.cap, state.sha, okPlats, state.parent, t0); captureRuns(state.cap, state.sha); }
        if(results.every(function(r){return r.ok;})){
          var n=results.length;
          setStatus('\u2713 Queued '+n+' run'+(n>1?'s':'')+'. <a href="https://github.com/'+REPO+'/actions" target="_blank" rel="noopener" style="color:var(--link)">View runs \u2197</a>', 'ok');
        } else {
          var multi = Object.keys(RT[state.cap].platforms).length > 1;
          var parts = results.map(function(r){ return (multi?cap(r.plat)+': ':'') + (r.ok?'queued':('HTTP '+r.status)); });
          var has403 = results.some(function(r){return r.status===403;});
          var has404 = results.some(function(r){return r.status===404;});
          var hint;
          if (has403) hint = ' \u2014 <strong>403</strong>: the token is missing the <strong>Actions: Read and write</strong> permission. On the token page open <strong>Permissions \u2192 Repository permissions \u2192 Actions \u2192 Read and write</strong> (selecting the repository is not enough), then <strong>Update</strong> and run again.';
          else if (has404) hint = ' \u2014 <strong>404</strong>: the token cannot see <code>'+esc(REPO)+'</code>. Grant it <strong>Repository access</strong> to this repo AND the <strong>Actions: Read and write</strong> permission.';
          else hint = ' \u2014 check the token has <strong>Actions: Read and write</strong> on this repository.';
          setStatus(parts.join(' \u00b7 ') + hint + ' <a href="#" id="cidash-fixtok" style="color:var(--link);white-space:nowrap">Update token \u2192</a>', 'err');
          var ft=$("cidash-fixtok"); if(ft) ft.addEventListener('click', function(e){ e.preventDefault(); showTokenPanel(); });
        }
      });
    }
    function showTokenPanel(){ var p=$("cidash-tok-panel"); if(p){ p.style.display='block'; var i=$("cidash-tok-input"); if(i) i.focus(); } }
    function hideTokenPanel(){ var p=$("cidash-tok-panel"); if(p){ p.style.display='none'; } }
    function saveTokAndRun(){ var i=$("cidash-tok-input"); var v=(i&&i.value||'').trim(); if(!v){ if(i) i.focus(); return; } setTok(v); hideTokenPanel(); runNow(); }
    function render(){
      var def = RT[state.cap]; if(!def) return;
      var keys = Object.keys(def.platforms);
      var multi = keys.length > 1;
      var sel = selectedPlats(def);
      var haveTok = !!getTok();
      var h = '';
      h += '<p style="margin:0 0 12px;color:var(--fg-muted);font-size:.85em">Run <strong>'+esc(def.label)+'</strong> for commit <code style="font-family:monospace">'+esc(state.short)+'</code>. Click <strong>Run now</strong> and it is queued on GitHub Actions \u2014 no terminal.</p>';
      if (multi){
        h += '<div style="display:flex;gap:18px;margin:0 0 14px;font-size:.9em">';
        keys.forEach(function(k){
          h += '<label style="display:inline-flex;align-items:center;gap:6px;cursor:pointer"><input type="checkbox" class="cidash-plat" id="cidash-plat-'+k+'" '+(sel.indexOf(k)>=0?'checked':'')+' style="accent-color:var(--link)">'+esc(cap(k))+'</label>';
        });
        h += '</div>';
      }
      // One-time token setup panel (hidden until needed).
      h += '<div id="cidash-tok-panel" style="display:none;border:1px solid var(--border);border-radius:8px;padding:12px;background:var(--surface);margin:0 0 12px">';
      h += '<div style="font-size:.82em;color:var(--fg);font-weight:600;margin-bottom:6px">One-time setup \u2014 a token to queue runs</div>';
      h += '<ol style="font-size:.8em;color:var(--fg-muted);margin:0 0 8px;padding-left:18px;line-height:1.6">';
      h += '<li><a href="'+tokenSetupUrl()+'" target="_blank" rel="noopener" style="color:var(--link)">Create a fine-grained token \u2197</a> \u2014 opens with the name, owner, and <strong>Actions: Read and write</strong> already set.</li>';
      h += '<li><strong>Repository access</strong> \u2192 Only select repositories \u2192 add <code>'+esc(REPO)+'</code>. <span style="opacity:.8">(the one step a link can\u2019t pre-fill)</span></li>';
      h += '<li><strong>Permissions \u2192 Repository permissions \u2192 Actions \u2192 Read and write</strong>. Required \u2014 selecting the repo alone gives a 403.</li>';
      h += '<li>Generate, then paste it below.</li>';
      h += '</ol>';
      h += '<div style="font-size:.76em;color:var(--fg-muted);margin-bottom:8px">Stored only in this browser (localStorage); sent only to api.github.com \u2014 never to the repo, the page, or CI.</div>';
      h += '<div style="display:flex;gap:8px;flex-wrap:wrap"><input id="cidash-tok-input" type="password" autocomplete="off" placeholder="github_pat_\u2026 or ghp_\u2026" style="flex:1 1 240px;min-width:180px;padding:7px 10px;background:var(--bg);color:var(--fg);border:1px solid var(--border);border-radius:6px;font-family:ui-monospace,Menlo,monospace;font-size:.8em">';
      h += '<button class="cidash-btn cidash-go" id="cidash-tok-save">Save &amp; run</button>';
      h += '<button class="cidash-btn cidash-ghost" id="cidash-tok-cancel">Cancel</button></div></div>';
      // Status line (dispatch result / hints).
      h += '<div id="cidash-run-status" style="font-size:.82em;min-height:1.2em;margin:0 0 12px"></div>';
      // Primary actions.
      h += '<div style="display:flex;gap:10px;flex-wrap:wrap;align-items:center">';
      h += '<button class="cidash-btn cidash-go" id="cidash-run-go">\u25B6 Run now</button>';
      sel.forEach(function(k){
        h += '<a class="cidash-btn cidash-ghost" style="text-decoration:none" href="'+actionsUrl(def.platforms[k].wf)+'" target="_blank" rel="noopener">Run '+esc(multi?cap(k):def.label)+' on GitHub \u2197</a>';
      });
      if (haveTok){ h += '<button class="cidash-btn cidash-ghost" id="cidash-tok-forget" title="Remove the token saved in this browser">Forget token</button>'; }
      h += '</div>';
      // CLI fallback, collapsed.
      var cmds = sel.map(function(k){ return ghCmd(def.platforms[k].wf, def.platforms[k].inputs); });
      h += '<details style="margin-top:14px"><summary style="cursor:pointer;color:var(--link);font-size:.82em">Prefer the command line?</summary>';
      h += '<div style="position:relative;margin:10px 0 0"><pre id="cidash-run-cmd" style="background:var(--surface);border:1px solid var(--border);border-radius:8px;padding:12px;margin:0;font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:.78em;white-space:pre-wrap;word-break:break-word">'+esc(cmds.join("\n"))+'</pre>';
      h += '<button onclick="cidashRunCopy()" style="position:absolute;top:8px;right:8px;background:var(--bg);border:1px solid var(--border);color:var(--fg);border-radius:6px;padding:4px 10px;font-size:.74em;cursor:pointer">Copy</button></div>';
      h += '<p style="color:var(--fg-muted);font-size:.76em;margin:8px 0 0">Run with the <a href="https://cli.github.com/" target="_blank" rel="noopener" style="color:var(--link)">GitHub CLI</a>, or use <strong>Run on GitHub</strong> above and paste the SHA into the form.</p></details>';
      $("cidash-run-body").innerHTML = h;
      Array.prototype.forEach.call(document.querySelectorAll('.cidash-plat'), function(b){ b.addEventListener('change', render); });
      var go=$("cidash-run-go"); if(go) go.addEventListener('click', runNow);
      var sv=$("cidash-tok-save"); if(sv) sv.addEventListener('click', saveTokAndRun);
      var cn=$("cidash-tok-cancel"); if(cn) cn.addEventListener('click', hideTokenPanel);
      var fg=$("cidash-tok-forget"); if(fg) fg.addEventListener('click', function(){ clearTok(); render(); setStatus('Token removed from this browser.', null); });
      var ti=$("cidash-tok-input"); if(ti) ti.addEventListener('keydown', function(e){ if(e.key==='Enter'){ e.preventDefault(); saveTokAndRun(); } });
    }
    window.cidashRunCopy = function(){
      var pre = $("cidash-run-cmd"); if(!pre || !navigator.clipboard) return;
      navigator.clipboard.writeText(pre.textContent);
    };
    window.cidashRunClose = function(){ var m=$("cidash-run-modal"); if(m){ m.style.display='none'; } if(!qModalOpen()){ document.body.style.overflow=''; } };
    function openRun(c, sha, parent, short){
      if(!RT[c]) return;
      state = {cap:c, sha:sha||'', parent:parent||'', short:short||''};
      $("cidash-run-title").textContent = "Run " + RT[c].label;
      render();
      $("cidash-run-modal").style.display='block';
      document.body.style.overflow='hidden';
    }
    // ── Manage a queued run: view it on GitHub, or cancel it ──────
    // Clicking a "Queued" badge opens this dialog (rather than jumping straight to
    // the Actions tab) so the user can either follow the run or stop it. Cancel
    // POSTs the runs/{id}/cancel REST API with the same browser token used to queue.
    var qState = { cap:null, sha:'' };
    function qModalOpen(){ var m=$("cidash-q-modal"); return !!(m && m.style.display==='block'); }
    function runModalOpen(){ var m=$("cidash-run-modal"); return !!(m && m.style.display==='block'); }
    window.cidashQClose = function(){ var m=$("cidash-q-modal"); if(m){ m.style.display='none'; } qState={cap:null,sha:''}; if(!runModalOpen()){ document.body.style.overflow=''; } };
    function qSetStatus(html, kind){
      var s = $("cidash-q-status"); if(!s) return;
      var col = kind==='ok' ? '#3fb950' : (kind==='err' ? '#f85149' : (kind==='warn' ? '#d29922' : 'var(--fg-muted)'));
      s.style.color = col; s.innerHTML = html || '';
    }
    function qViewLinks(c, entry){
      // Prefer a direct link to each captured run; else the workflow's runs page
      // (or the repo's Actions tab) so "view" always works even before capture.
      var def = RT[c]; var runs = (entry && entry.runs) || []; var multi = Object.keys(def.platforms).length > 1;
      var out = [];
      if(runs.length){
        runs.forEach(function(r){
          var nm = multi ? cap(r.plat) : def.label;
          out.push('<a class="cidash-btn cidash-ghost" style="text-decoration:none" href="'+esc(r.url||('https://github.com/'+REPO+'/actions'))+'" target="_blank" rel="noopener">View '+esc(nm)+' run \u2197</a>');
        });
      } else {
        var seen = {};
        (entry.plats||[]).forEach(function(plat){
          var p = def.platforms[plat]; if(!p || seen[p.wf]) return; seen[p.wf]=1;
          out.push('<a class="cidash-btn cidash-ghost" style="text-decoration:none" href="'+actionsUrl(p.wf)+'" target="_blank" rel="noopener">View '+esc(def.label)+' runs \u2197</a>');
        });
        if(!out.length){ out.push('<a class="cidash-btn cidash-ghost" style="text-decoration:none" href="https://github.com/'+REPO+'/actions" target="_blank" rel="noopener">View on GitHub Actions \u2197</a>'); }
      }
      return out.join('');
    }
    function qReportUrl(entry){
      // A failed overlay that replaced a deployed RESULT cell stashed that cell's
      // original chip HTML (entry.orig). If that chip linked to a deployed report
      // (anything other than a bare GitHub Actions run page), hand it back so the
      // failed dialog can route to the report - where a unit-test / analyzer failure
      // is actually explained - instead of only the Actions log. A hard run failure
      // that produced no report (no orig, or an Actions-only link) yields '' -> the
      // dialog keeps just the "View run" (Actions) button.
      var o = entry && entry.orig; if(!o) return '';
      var m = /href="([^"]+)"/i.exec(o); if(!m) return '';
      var u = m[1];
      if(/github\.com\/[^"']*\/actions\//i.test(u)) return '';
      return u;
    }
    function renderQ(){
      var c = qState.cap, sha = qState.sha; if(!c) return;
      var o = qLoad(); var entry = o[c+'|'+sha]; var def = RT[c];
      if(!entry || !def){ cidashQClose(); return; }
      var failed = !!entry.failed;
      var plats = (entry.plats||[]).map(cap).join(', ');
      var short = entry.short || sha.slice(0,7);
      var live = qLiveEntries(o); var pos = 0;
      live.forEach(function(en, idx){ if(en.key===c+'|'+sha) pos = idx+1; });
      var h = '';
      h += '<p style="margin:0 0 12px;color:var(--fg-muted);font-size:.85em"><strong>'+esc(def.label)+'</strong> for commit <code style="font-family:monospace">'+esc(short)+'</code>'+(plats?' \u00b7 '+esc(plats):'')+(failed?' \u2014 <span style="color:#f85149">this run failed</span>.':'.')+'</p>';
      if(!failed && live.length>1 && pos){ h += '<p style="margin:0 0 12px;font-size:.85em">Place in queue: <strong>#'+pos+'</strong> <span style="color:var(--fg-muted)">of '+live.length+' queued from this browser (oldest first).</span></p>'; }
      h += '<div id="cidash-q-status" style="font-size:.82em;min-height:1.2em;margin:0 0 12px"></div>';
      h += '<div style="display:flex;gap:10px;flex-wrap:wrap;align-items:center">';
      var repUrl = failed ? qReportUrl(entry) : '';
      h += qViewLinks(c, entry);
      if(failed){
        if(repUrl){ h += '<a class="cidash-btn" style="text-decoration:none" href="'+repUrl+'" target="_blank" rel="noopener">View report \u2197</a>'; }
        h += '<button class="cidash-btn" id="cidash-q-rerun">Re-run</button>';
        h += '<button class="cidash-btn cidash-ghost" id="cidash-q-dismiss">Dismiss</button>';
      }
      else { h += '<button class="cidash-btn cidash-danger" id="cidash-q-cancel">\u2715 Cancel run</button>'; }
      h += '</div>';
      if(failed){ h += '<p style="color:var(--fg-muted);font-size:.76em;margin:12px 0 0">'+(repUrl?'<strong>View report</strong> opens this activity\u2019s report to see what failed. ':'')+'<strong>View</strong> opens the failed run on GitHub to see what went wrong. <strong>Re-run</strong> starts it again for this commit. <strong>Dismiss</strong> clears this badge.</p>'; }
      else { h += '<p style="color:var(--fg-muted);font-size:.76em;margin:12px 0 0"><strong>Cancel run</strong> stops it on GitHub Actions (uses the same token you queued it with). <strong>View</strong> opens it on GitHub, where you can watch it or stop it manually.</p>'; }
      $("cidash-q-body").innerHTML = h;
      var cb=$("cidash-q-cancel"); if(cb) cb.addEventListener('click', function(){ cancelQueued(c, sha); });
      var rb=$("cidash-q-rerun"); if(rb) rb.addEventListener('click', function(){ var rp=(entry.parent||''), rs=(entry.short||sha.slice(0,7)); qForget(c, sha); cidashQClose(); openRun(c, sha, rp, rs); });
      var db=$("cidash-q-dismiss"); if(db) db.addEventListener('click', function(){ qForget(c, sha); cidashQClose(); });
    }
    function openQ(c, sha){
      if(!RT[c]) return;
      qState = { cap:c, sha:sha };
      var entry = qLoad()[c+'|'+sha];
      $("cidash-q-title").textContent = (entry && entry.failed ? "Failed: " : "Queued: ") + RT[c].label;
      renderQ();
      $("cidash-q-modal").style.display='block';
      document.body.style.overflow='hidden';
      // Look up the run id(s) now (if not captured yet) so Cancel + the direct run
      // links resolve; re-render when they arrive.
      if(entry && !((entry.runs||[]).length)){
        captureRuns(c, sha, 1, function(){ if(qState.cap===c && qState.sha===sha) renderQ(); });
      }
    }
    function cancelQueued(c, sha){
      var entry = qLoad()[c+'|'+sha]; if(!entry){ cidashQClose(); return; }
      if(!getTok()){ qSetStatus('No token in this browser \u2014 use \u201cView on GitHub Actions\u201d to cancel it there.', 'warn'); return; }
      var btn = $("cidash-q-cancel"); if(btn){ btn.disabled = true; }
      var runs = (entry.runs||[]).filter(function(r){ return r && r.id; });
      if(runs.length){ doCancel(c, sha, runs); return; }
      // No run id captured yet — try one more lookup before giving up.
      qSetStatus('Finding the run\u2026', null);
      captureRuns(c, sha, 1, function(){
        var r2 = ((qLoad()[c+'|'+sha]||{}).runs||[]).filter(function(r){ return r && r.id; });
        if(!r2.length){ if(btn){ btn.disabled=false; } qSetStatus('Could not find the run automatically \u2014 use \u201cView on GitHub Actions\u201d to cancel it there.', 'err'); return; }
        doCancel(c, sha, r2);
      });
    }
    function doCancel(c, sha, runs){
      var tok = getTok();
      qSetStatus('Cancelling\u2026', null);
      var jobs = runs.map(function(r){
        return fetch('https://api.github.com/repos/'+REPO+'/actions/runs/'+r.id+'/cancel', {
          method:'POST', headers:{ 'Authorization':'Bearer '+tok, 'Accept':'application/vnd.github+json', 'X-GitHub-Api-Version':'2022-11-28' }
        }).then(function(resp){ return { id:r.id, ok:(resp.status===202||resp.status===409), status:resp.status }; })
          .catch(function(){ return { id:r.id, ok:false, status:0 }; });
      });
      Promise.all(jobs).then(function(res){
        var btn = $("cidash-q-cancel");
        if(res.some(function(r){return r.status===401;})){ clearTok(); if(btn){ btn.disabled=false; } qSetStatus('That token was rejected (401). Re-queue with a valid token, or cancel it on GitHub.', 'err'); return; }
        if(res.every(function(r){return r.ok;})){
          qForget(c, sha);
          qSetStatus('\u2713 Cancelled \u2014 the run is stopping on GitHub.', 'ok');
          setTimeout(cidashQClose, 1100);
        } else {
          if(btn){ btn.disabled=false; }
          var has403 = res.some(function(r){return r.status===403;});
          var hint = has403 ? ' The token needs <strong>Actions: Read and write</strong> on this repo.' : '';
          qSetStatus('Could not cancel ('+res.map(function(r){return 'HTTP '+r.status;}).join(', ')+').'+hint+' Use \u201cView on GitHub Actions\u201d to stop it there.', 'err');
        }
      });
    }
    document.addEventListener('click', function(e){
      var a = e.target.closest ? e.target.closest('a.cidash-run') : null;
      if(!a) return;
      e.preventDefault();
      openRun(a.getAttribute('data-cap'), a.getAttribute('data-sha'), a.getAttribute('data-parent'), a.getAttribute('data-short'));
    });
    document.addEventListener('click', function(e){
      var b = e.target.closest ? e.target.closest('.cidash-queued') : null;
      if(!b) return;
      e.preventDefault();
      var td = b.closest ? b.closest('td') : null; if(!td) return;
      openQ(td.getAttribute('data-qcap'), td.getAttribute('data-qsha'));
    });
    document.addEventListener('keydown', function(e){
      if((e.key==='Enter' || e.key===' ') && e.target && e.target.classList && e.target.classList.contains('cidash-queued')){
        e.preventDefault();
        var td = e.target.closest ? e.target.closest('td') : null;
        if(td) openQ(td.getAttribute('data-qcap'), td.getAttribute('data-qsha'));
      }
    });
    document.addEventListener('keydown', function(e){ if(e.key==='Escape'){ if(qModalOpen()) cidashQClose(); else cidashRunClose(); } });
    // ── "Run all history" backfill card ──────────────────────────────
    // On a fresh dashboard (gated server-side: no results, nothing running) the
    // card offers to queue CI for the WHOLE history in one click, oldest commit
    // first. Snapshots are not per-commit (every snapshot cell dispatches the same
    // gallery workflow), so instead of one run per row we fire a SINGLE run in the
    // workflow's "backfill" mode — it walks history oldest→newest and seeds from
    // the already-deployed gallery, so nothing is rendered twice. Mass Compile /
    // VI Analyzer / VIDiff are per-revision, so those are dispatched once each,
    // oldest first and gently throttled. The card self-hides once anything is
    // queued and can be dismissed (remembered per repo).
    var BF_DISMISS_KEY = "lvci_backfill_dismissed";
    function bfCard(){ return document.getElementById('lvci-backfill'); }
    function bfDismissed(){ try{ return localStorage.getItem(BF_DISMISS_KEY) === REPO; }catch(e){ return false; } }
    function bfHide(){ var c=bfCard(); if(c) c.style.display='none'; }
    function bfDismiss(){ try{ localStorage.setItem(BF_DISMISS_KEY, REPO); }catch(e){} bfHide(); }
    function bfTokPanel(show){ var p=document.getElementById('lvci-bf-tok'); if(p) p.style.display = show ? 'block' : 'none'; }
    function bfStatus(html, kind){
      var s=document.getElementById('lvci-bf-status'); if(!s) return;
      var col = kind==='ok' ? '#3fb950' : (kind==='err' ? '#f85149' : (kind==='warn' ? '#d29922' : 'var(--fg-muted)'));
      s.style.color=col; s.innerHTML=html||'';
    }
    function bfCells(){
      // Every empty run-glyph paired with its row order. The table is rendered
      // newest-first, so a larger row index = an older commit; dispatching in
      // descending row order is therefore oldest-first.
      var out=[]; var rows=document.querySelectorAll('tbody tr[data-project]');
      Array.prototype.forEach.call(rows, function(tr, idx){
        Array.prototype.forEach.call(tr.querySelectorAll('a.cidash-run'), function(a){
          out.push({ cap:a.getAttribute('data-cap'), sha:a.getAttribute('data-sha'),
                     parent:a.getAttribute('data-parent')||'', order:idx });
        });
      });
      return out;
    }
    function bfShow(){
      var c=bfCard(); if(!c) return;
      var cells=bfCells(); if(!cells.length){ c.style.display='none'; return; }
      // Hide on a repo we've dismissed, or once this dashboard has queued one of
      // its own cells ("disappear after anything is run"); otherwise show with a live count.
      var queued = qLoad();
      var queuedHere = cells.some(function(x){ return !!queued[x.cap+'|'+x.sha]; });
      if(bfDismissed() || queuedHere){ c.style.display='none'; return; }
      var shas={}; cells.forEach(function(x){ shas[x.sha]=1; });
      var n=document.getElementById('lvci-bf-count'); if(n) n.textContent=String(Object.keys(shas).length);
      c.style.display='';
    }
    function bfFill(t, sha, parent){ return String(t).replace(/\{sha\}/g, sha).replace(/\{parent\}/g, parent||''); }
    function bfInputs(p, sha, parent){ var o={}; Object.keys(p.inputs).forEach(function(k){ o[k]=bfFill(p.inputs[k], sha, parent); }); return o; }
    // Shared dispatch engine for BOTH the fresh-install backfill card and the
    // Populate-history dialog: queue a set of empty run-cells, snapshots as ONE
    // backfill run (the workflow walks the whole history oldest->newest and dedupes)
    // and the per-revision activities oldest-first, gently throttled so a long
    // history doesn't trip GitHub's secondary rate limits. Resolves with an
    // {ok, err, total} tally; the caller owns its own status line + buttons.
    // Turn collected dispatch failures into an accurate message. A 404 means the
    // targeted workflow is not RUNNABLE on this repo -- the file is not installed
    // (tooling out of date), or it exists but GitHub never registered it as a
    // workflow (fork installs that pushed the files while Actions was disabled) --
    // NOT a token problem, so callers do not re-prompt for a token in that case.
    // Both causes share one remedy: a tooling update rewrites every workflow file
    // (version stamp), so the update push registers them. .tok = whether the
    // failure is token/permission related.
    function dispatchFailMsg(fails){
      fails = fails || [];
      var has = function(code){ return fails.some(function(f){ return f.status===code; }); };
      if(has(401)) return { html:'That saved token was rejected (401) \u2014 it is invalid or expired. <a href="'+tokenSetupUrl()+'" target="_blank" rel="noopener" style="color:var(--link)">Create or update a token \u2197</a>', tok:true };
      if(has(404)){
        var wfs=[]; fails.forEach(function(f){ if(f.status===404 && f.wf && wfs.indexOf(f.wf)<0) wfs.push(f.wf); });
        var list=wfs.map(function(w){ return '<code>'+esc(w)+'</code>'; }).join(', ');
        return { html:'<strong>Not a token problem</strong> \u2014 your saved token worked, but '+(wfs.length>1?'these workflows are':'this workflow is')+' not runnable on <code>'+esc(REPO)+'</code> (HTTP 404): '+list+'. Either the file is not installed (this repository\u2019s LabVIEW CI tooling is out of date), or it exists but GitHub never registered it as a workflow \u2014 typical for fork installs that pushed the pipeline while Actions was disabled. Both have the same fix: update from <strong>What\u2019s new \u2192 Update</strong> \u2014 the update rewrites every workflow file so GitHub registers '+(wfs.length>1?'them':'it')+' \u2014 then queue again.', tok:false };
      }
      if(has(403)) return { html:'The saved token is missing the <strong>Actions: Read and write</strong> permission on <code>'+esc(REPO)+'</code> (HTTP 403). On the token page set <strong>Permissions \u2192 Repository permissions \u2192 Actions \u2192 Read and write</strong>, then <strong>Update</strong>. <a href="'+tokenSetupUrl()+'" target="_blank" rel="noopener" style="color:var(--link)">Update token \u2197</a>', tok:true };
      if(has(422)) return { html:'GitHub rejected the dispatch (HTTP 422) \u2014 usually a bad branch ref (<code>'+esc(BRANCH)+'</code>) or inputs.', tok:false };
      var st=(fails[0]||{}).status; return { html:'Could not queue runs (HTTP '+esc(String(st||'?'))+'). <a href="https://github.com/'+REPO+'/actions" target="_blank" rel="noopener" style="color:var(--link)">View Actions \u2197</a>', tok:false };
    }
    var QUEUE_CONFIRM_MAX = 10;   // confirm before firing more than this many runs at once
    // Count how many workflow runs dispatchCells(cells, opts) will queue, WITHOUT
    // dispatching -- so a large batch can be confirmed first. Mirrors the `total`
    // computed inside dispatchCells (snapshots -> one backfill; each warm-container
    // batch (cap,platform) group -> one dispatch; remaining per-revision platforms
    // counted individually).
    function dispatchTotal(cells, opts){
      opts = opts || {}; var reuse = !!opts.reuse;
      var sawSnap=false; var perRev=[];
      (cells||[]).forEach(function(x){
        if(x.cap==='snapshots'){ sawSnap=true; }
        else if(!(x.cap==='vidiff' && !x.parent)){ perRev.push(x); }
      });
      var batchSeen={}; var batchGroups=0; var perRevPlats=0;
      perRev.forEach(function(x){
        cellPlatforms(x).forEach(function(plat){
          var p=(RT[x.cap] && RT[x.cap].platforms) ? RT[x.cap].platforms[plat] : null;
          if(reuse && p && p.batch){ var key=x.cap+'|'+plat; if(!batchSeen[key]){ batchSeen[key]=1; batchGroups++; } }
          else { perRevPlats++; }
        });
      });
      return perRevPlats + (sawSnap?1:0) + batchGroups;
    }
    // Ask before queuing a large batch (guards against accidentally firing a whole
    // history from the Populate-history dialog). Returns false only when the user
    // cancels a >QUEUE_CONFIRM_MAX batch; a native confirm() keeps it robust + modal.
    function confirmLargeQueue(n){
      if(n <= QUEUE_CONFIRM_MAX) return true;
      return window.confirm('This will queue '+n+' workflow runs on '+REPO+'.\n\nThat can use a lot of GitHub Actions minutes. Queue all '+n+' now?');
    }
    function dispatchCells(cells, statusFn, opts){
      opts = opts || {}; var reuse = !!opts.reuse;
      var perRev=[]; var snapShas=[]; var sawSnap=false; var snapForce=false;
      cells.forEach(function(x){
        if(x.cap==='snapshots'){
          sawSnap=true; snapShas.push(x.sha);
          if(x.mode==='rerun' || x.done) snapForce=true;
        }
        else if(!(x.cap==='vidiff' && !x.parent)){ perRev.push(x); }   // a root commit has no base to diff
      });
      perRev.sort(function(a,b){ return b.order - a.order; });   // oldest first
      // Warm-container batch routing: when "keep one container warm" is on, every
      // (cap, platform) that exposes a 'batch' backfill workflow is collapsed into
      // ONE dispatch carrying the whole oldest→newest SHA list, so a single warm
      // container renders every selected revision (docker exec per commit) instead
      // of a fresh container per revision. A cell's platforms WITHOUT a batch
      // target (e.g. Linux) keep dispatching per revision. x._plats holds the
      // per-revision platforms that survive after batch ones are siphoned off.
      var batchGroups={}; var batchOrder=[];
      perRev.forEach(function(x){
        var keep=[];
        cellPlatforms(x).forEach(function(plat){
          var p=(RT[x.cap] && RT[x.cap].platforms) ? RT[x.cap].platforms[plat] : null;
          if(reuse && p && p.batch){
            var key=x.cap+'|'+plat; var g=batchGroups[key];
            if(!g){ g={cap:x.cap, plat:plat, wf:p.batch.wf, shasInput:(p.batch.shas_input||'shas'), shas:[], force:false}; batchGroups[key]=g; batchOrder.push(key); }
            if(g.shas.indexOf(x.sha)<0) g.shas.push(x.sha);
            if(x.mode==='rerun' || x.done) g.force=true;
          } else { keep.push(plat); }
        });
        x._plats=keep;
      });
      var perRevActive=perRev.filter(function(x){ return x._plats.length>0; });
      var total=perRevActive.reduce(function(n,x){ return n + x._plats.length; }, 0) + (sawSnap?1:0) + batchOrder.length;
      var t0=Date.now(); var ok=0, err=0, done=0; var fails=[];
      // Paint EVERY targeted cell as "Queued" up front, in one synchronous pass, so
      // the whole batch lights up the instant you click - independent of the throttled
      // dispatch chain below. A dispatch that truly fails then reverts just its own
      // cell, and a thrown error mid-chain can no longer leave the earlier cells un-queued.
      if(sawSnap){ snapShas.forEach(function(sha){ markQueued('snapshots', sha, ['all'], '', t0); }); }
      batchOrder.forEach(function(key){ var g=batchGroups[key]; g.shas.forEach(function(sha){ markQueued(g.cap, sha, [g.plat], '', t0); }); });
      perRevActive.forEach(function(x){ var d=RT[x.cap]; if(d) markQueued(x.cap, x.sha, x._plats, x.parent, t0); });
      // Stamp every batch-group member with its backfill workflow so run-id
      // capture (captureBatchRun, incl. the qSync retry path) knows where the
      // ONE shared run lives. Done AFTER the markQueued paints above, because
      // markQueued rebuilds the stored entry and would drop the stamp. (A cell
      // whose OTHER platform also dispatches per-revision gets re-marked when
      // that dispatch resolves and loses the stamp; it stays alive via its
      // per-revision run id, and the server-side attribution — backfill
      // run-names carry the SHA list — still covers the batch half.)
      if(batchOrder.length){
        var oStamp = qLoad();
        batchOrder.forEach(function(key){
          var g = batchGroups[key];
          g.shas.forEach(function(sha){ var eS = oStamp[g.cap+'|'+sha]; if(eS) eS.batchWf = g.wf; });
        });
        qSave(oStamp);
      }
      statusFn('Queuing '+total+' workflow'+(total>1?'s':'')+'\u2026', null);
      var chain=Promise.resolve();
      // 1) Snapshots — one backfill run for the whole history (oldest→newest, deduped).
      if(sawSnap){
        chain=chain.then(function(){
          var inputs={ mode:'backfill' }; if(snapForce) inputs.force='true';
          return dispatchOne('vi-snapshots.yml', inputs).then(function(r){
            done++;
            if(r.ok){ ok++; captureSnapshotRun(t0); }
            else { err++; fails.push({wf:'vi-snapshots.yml', status:r.status}); if(r.status===401) clearTok(); snapShas.forEach(function(sha){ qForget('snapshots', sha); }); }
            statusFn('Queuing\u2026 '+done+'/'+total, null);
          }).catch(function(){ err++; });
        });
      }
      // 2) Warm-container batch runs — one dispatch per (cap, platform) carrying the
      // whole SHA list; the backfill workflow walks them oldest→newest in one
      // container. force=true when any selected revision is a re-run/already done.
      batchOrder.forEach(function(key){
        var g=batchGroups[key];
        chain=chain.then(function(){
          var inputs={}; inputs[g.shasInput]=g.shas.join(' '); if(g.force) inputs.force='true';
          return dispatchOne(g.wf, inputs).then(function(r){
            done++;
            // On success, capture the ONE batch run's id into every member entry
            // (mirrors the snapshots path) — without it the whole group's badges
            // could never be reconciled and silently expired at QTTL.
            if(r.ok){ ok++; captureBatchRun(g.wf, t0); }
            else { err++; fails.push({wf:g.wf, status:r.status}); if(r.status===401) clearTok(); g.shas.forEach(function(sha){ qForget(g.cap, sha); }); }
            statusFn('Queuing\u2026 '+done+'/'+total, null);
          }).catch(function(){ err++; });
        }).then(function(){ return new Promise(function(res){ setTimeout(res, 650); }); });
      });
      // 3) Per-revision capabilities, oldest first, gently throttled.
      perRevActive.forEach(function(x){
        chain=chain.then(function(){
          var def=RT[x.cap]; if(!def){ return; }
          var plats=x._plats;
          var jobs=plats.map(function(k){
            var p=def.platforms[k]; var inputs=bfInputs(p, x.sha, x.parent);
            if(x.cap==='snapshots2' && (x.mode==='rerun' || x.done)) inputs.force='true';
            return dispatchOne(p.wf, inputs).then(function(r){ r.plat=k; return r; });
          });
          return Promise.all(jobs).then(function(results){
            done += results.length;
            if(results.some(function(r){return r.status===401;})) clearTok();
            results.forEach(function(r){ if(!r.ok) fails.push({wf:r.wf, status:r.status}); });
            var okPlats=results.filter(function(r){return r.ok;}).map(function(r){return r.plat;});
            if(okPlats.length){ ok++; markQueued(x.cap, x.sha, okPlats, x.parent, t0); captureRuns(x.cap, x.sha); }
            else { err++; qForget(x.cap, x.sha); }
            statusFn('Queuing\u2026 '+done+'/'+total, null);
          });
        }).catch(function(){ /* one cell's error never aborts the rest */ })
          .then(function(){ return new Promise(function(res){ setTimeout(res, 650); }); });
      });
      return chain.then(function(){ return { ok:ok, err:err, total:total, fails:fails }; });
    }
    function bfRunAll(){
      var cells=bfCells();
      if(!cells.length){ bfStatus('Nothing left to run \u2014 every revision is queued or already has results.', 'ok'); return; }
      if(!getTok()){ bfTokPanel(true); bfStatus('Add a token (Actions: Read and write) to queue runs.', 'warn'); var i=document.getElementById('lvci-bf-tok-input'); if(i) i.focus(); return; }
      bfTokPanel(false);
      if(!confirmLargeQueue(dispatchTotal(cells))){ bfStatus('Cancelled \u2014 nothing was queued.', 'warn'); return; }
      var runBtn=document.getElementById('lvci-bf-run'); var disBtn=document.getElementById('lvci-bf-dismiss');
      if(runBtn) runBtn.disabled=true; if(disBtn) disBtn.disabled=true;
      dispatchCells(cells, bfStatus).then(function(res){
        if(runBtn) runBtn.disabled=false; if(disBtn) disBtn.disabled=false;
        if(res.ok && !res.err){
          bfStatus('\u2713 Queued '+res.ok+' workflow'+(res.ok>1?'s':'')+', oldest first \u2014 results appear as they finish. <a href="https://github.com/'+REPO+'/actions" target="_blank" rel="noopener" style="color:var(--link)">View runs \u2197</a>', 'ok');
          bfDismiss();
        } else if(res.ok && res.err){
          var pm=dispatchFailMsg(res.fails);
          bfStatus('Queued '+res.ok+', but '+res.err+' could not be dispatched. '+pm.html, 'warn');
          if(pm.tok) bfTokPanel(true);
        } else {
          var fm=dispatchFailMsg(res.fails);
          bfStatus(fm.html, 'err');
          if(fm.tok) bfTokPanel(true);
        }
      });
    }
    function bfInit(){
      var c=bfCard(); if(!c) return;
      var run=document.getElementById('lvci-bf-run'); if(run) run.addEventListener('click', function(){ histOpen(); });
      var dis=document.getElementById('lvci-bf-dismiss'); if(dis) dis.addEventListener('click', function(){ bfDismiss(); });
      var save=document.getElementById('lvci-bf-tok-save'); if(save) save.addEventListener('click', function(){ var i=document.getElementById('lvci-bf-tok-input'); var v=(i&&i.value||'').trim(); if(!v){ if(i) i.focus(); return; } setTok(v); bfRunAll(); });
      var inp=document.getElementById('lvci-bf-tok-input'); if(inp) inp.addEventListener('keydown', function(e){ if(e.key==='Enter'){ e.preventDefault(); var v=(inp.value||'').trim(); if(v){ setTok(v); bfRunAll(); } } });
      var link=document.getElementById('lvci-bf-tok-link'); if(link) link.href=tokenSetupUrl();
      var cust=document.getElementById('lvci-bf-custom'); if(cust) cust.addEventListener('click', function(e){ e.preventDefault(); histOpen(); });
      bfShow();
    }
    // ── "Populate dashboard history" dialog ──────────────────────────────
    // Opened from the header's More menu (window.lvciRunHistory) and the fresh
    // install card's "Customize…" link. The user chooses where in history to
    // start, whether to run the lean diff-based pass (VI Snapshots + VIDiff only —
    // the modified-file visual history) or every activity, and exactly which
    // activities to queue. Reuses dispatchCells (oldest-first, snapshots as one
    // backfill) + the same token and optimistic-queued bridge as every other
    // dispatch on the dashboard.
    var HIST = __HIST__;                        // [{sha, short, msg}] newest-first (project revisions)
    var RAN = __CAPS_RAN__;                     // {cap:1} caps with a result/active run somewhere in history
    var CAP_META = {                            // label + sub for each known capability
      'snapshots':   ['VI Snapshots', 'Renders every changed VI (classic HTML snapshots)'],
      'snapshots2':  ['VI Browser 2.0 Snapshots', 'Renders position-aware snapshots for the VI Browser 2.0 view'],
      'vidiff':      ['VIDiff',       'Visual diff of the VIs each revision changed'],
      'masscompile': ['Mass Compile', 'Compiles the whole project, each revision'],
      'vi-analyzer': ['VI Analyzer',  'Runs the VI Analyzer suite, each revision'],
      'unit-tests':  ['Unit Tests',   'Runs the unit-test suite, each revision'],
      'antidoc':     ['Antidoc',      'Generates project documentation, each revision'],
      'builds':      ['Builds',       'Runs the project build specifications (EXE, PPL, ...), each revision'],
      'sbom-generation': ['SBOM Generation', 'Generates a CycloneDX dependency report, each revision']
    };
    var CAP_ORDER = ['snapshots','snapshots2','vidiff','masscompile','vi-analyzer','unit-tests','antidoc','builds','sbom-generation'];
    var DIFF_CAPS = { snapshots:1, snapshots2:1, vidiff:1 };  // the lean "diff-based" subset
    var PLATFORM_CAPS = { snapshots2:1, vidiff:1, masscompile:1, 'vi-analyzer':1, builds:1 };
    var platState = {};                         // cap id -> selected platform keys for history rows
    function capPlatforms(capId){ return (RT[capId] && RT[capId].platforms) ? Object.keys(RT[capId].platforms) : []; }
    function histHasPlatformPicker(capId){ return !!PLATFORM_CAPS[capId] && capPlatforms(capId).length > 1; }
    function histSelectedPlatforms(capId){
      var keys=capPlatforms(capId);
      if(!histHasPlatformPicker(capId)) return keys;
      var selected=(platState[capId] || keys.slice()).filter(function(k){ return keys.indexOf(k)>=0; });
      return selected.length ? selected : keys;
    }
    function cellPlatforms(x){ return x.platform ? [x.platform] : histSelectedPlatforms(x.cap); }
    function histPlatformHtml(capId){
      if(!histHasPlatformPicker(capId)) return '';
      var selected=histSelectedPlatforms(capId);
      var label=(CAP_META[capId]||[capId])[0];
      return '<span class="cidash-hist-plats" role="group" aria-label="'+esc(label)+' platforms">'
        + capPlatforms(capId).map(function(k){
            return '<label><input type="checkbox" class="cidash-hist-plat" data-cap="'+esc(capId)+'" data-platform="'+esc(k)+'" '+(selected.indexOf(k)>=0?'checked':'')+'>'+esc(cap(k))+'</label>';
          }).join('') + '</span>';
    }
    // The activities offered are exactly the workers THIS repo actually has: any
    // capability with at least one empty (never-run) cell to fill, plus any that
    // already has results (shown so the picture is complete - queuing only ever
    // fills empty cells). Driven by the live table, so a worker that isn't
    // installed never appears and a newly-added one (e.g. Unit Tests) always does.
    function histInstalledCaps(){
      var seen={}; bfCells().forEach(function(x){ seen[x.cap]=1; });
      if(HIST.length && RT.snapshots2) seen.snapshots2=1;
      // Require an installed run target (RT[c]): a capability whose workflow is not
      // present in this repo is never offered, even if it ran here historically.
      return CAP_ORDER.filter(function(c){ return (seen[c] || RAN[c]) && RT[c]; });
    }
    function histModal(){ return document.getElementById('cidash-hist-modal'); }
    function cidashHistClose(){ var m=histModal(); if(m) m.style.display='none'; document.body.style.overflow=''; try{ if(window.parent!==window) window.parent.postMessage('lvci:hist-close',location.origin); }catch(e){} }
    function histStatus(html, kind){
      var s=document.getElementById('cidash-hist-status'); if(!s) return;
      var col = kind==='ok' ? '#3fb950' : (kind==='err' ? '#f85149' : (kind==='warn' ? '#d29922' : 'var(--fg-muted)'));
      s.style.color=col; s.innerHTML=html||'';
    }
    function histTokPanel(show){ var p=document.getElementById('cidash-hist-tok'); if(p) p.style.display = show ? 'block' : 'none'; }
    var actMode = {};                           // cap -> 'off' | 'fill' | 'rerun' (per-activity choice)
    var reuseWarm = false;                       // opt-in: keep ONE container warm across the queued revisions
    // ── Warm-container reuse + time estimate ───────────────────────────────
    // A (cap, platform) is "batch-capable" when its run target carries a 'batch'
    // backfill workflow (Windows only for now). The reuse toggle is only offered
    // when at least one installed activity is batch-capable, and only batch-capable
    // platforms actually collapse into a single warm-container run.
    function capBatchPlatforms(capId){
      var def=RT[capId]; if(!def || !def.platforms) return [];
      return Object.keys(def.platforms).filter(function(k){ return !!(def.platforms[k] && def.platforms[k].batch); });
    }
    function anyBatchAvailable(){ return histInstalledCaps().some(function(c){ return capBatchPlatforms(c).length>0; }); }
    // Default per-platform fallbacks (seconds) used only until a repo has run
    // history to measure from. Windows containers are the slow case this feature
    // targets; Linux start in seconds.
    var TIMING_DEFAULT = { windows:{ startup:600, work:120 }, linux:{ startup:45, work:90 }, all:{ startup:120, work:90 } };
    function timingFor(cap, plat){
      // Returns {startup, work} seconds for one (cap, platform), derived from the
      // server-injected medians (work = in-container compile time; total = whole
      // run wall-clock; startup = total - work). Falls back to platform defaults.
      var d=TIMING_DEFAULT[plat] || TIMING_DEFAULT.windows;
      var t=(RT_TIMING && RT_TIMING[cap] && RT_TIMING[cap][plat]) ? RT_TIMING[cap][plat] : null;
      var work = (t && t.work>0) ? t.work : d.work;
      var startup;
      if(t && t.total>0){ startup = Math.max(20, t.total - work); }
      else { startup = d.startup; }
      return { startup:startup, work:work, measured: !!(t && t.total>0) };
    }
    function fmtDur(sec){
      sec=Math.max(0, Math.round(sec));
      var h=Math.floor(sec/3600), m=Math.floor((sec%3600)/60);
      if(h>0) return h+'h'+(m>0?(' '+m+'m'):'');
      if(m>0) return m+'m';
      return sec+'s';
    }
    function histEstimate(){
      // Fresh vs warm-batch total compute time for the cells about to be queued.
      // Fresh: every (cell, platform) pays startup+work. Warm: batch-capable
      // (cap, platform) groups pay startup ONCE + work per revision; everything
      // else is unchanged. Returns null when nothing measurable is selected.
      var cells=histCells(); if(!cells.length) return null;
      var fresh=0, warm=0, anyMeasured=false, anyBatch=false;
      var groups={};   // cap|plat -> count (batch-capable, warm only)
      cells.forEach(function(x){
        if(x.cap==='snapshots') return;   // snapshots are already one backfill run
        cellPlatforms(x).forEach(function(plat){
          var tm=timingFor(x.cap, plat); if(tm.measured) anyMeasured=true;
          fresh += tm.startup + tm.work;
          var p=(RT[x.cap]&&RT[x.cap].platforms)?RT[x.cap].platforms[plat]:null;
          if(reuseWarm && p && p.batch){
            anyBatch=true; var key=x.cap+'|'+plat;
            if(!groups[key]){ groups[key]={tm:tm, n:0}; }
            groups[key].n++;
          } else {
            warm += tm.startup + tm.work;
          }
        });
      });
      Object.keys(groups).forEach(function(key){ var g=groups[key]; warm += g.tm.startup + g.n*g.tm.work; });
      return { fresh:fresh, warm:warm, anyMeasured:anyMeasured, anyBatch:anyBatch };
    }
    function histAllCells(){
      // EVERY per-revision activity cell on the table, tagged empty (a never-run run
      // glyph) vs done (a cell that already carries a result). bfCells() sees only the
      // empty glyphs; gathering the result cells too is what lets this dialog RE-RUN
      // an activity, not just fill the blanks. A queued/running cell (cidash-active-cell)
      // is gathered too as "done" so an in-flight activity stays re-runnable (the new run
      // replaces it via concurrency) instead of showing "none in scope".
      var out=[]; var rows=document.querySelectorAll('tbody tr[data-project]');
      Array.prototype.forEach.call(rows, function(tr, idx){
        Array.prototype.forEach.call(tr.querySelectorAll('a.cidash-run'), function(a){
          out.push({ cap:a.getAttribute('data-cap'), sha:a.getAttribute('data-sha'),
                     parent:a.getAttribute('data-parent')||'', order:idx, done:false });
        });
        Array.prototype.forEach.call(tr.querySelectorAll('td.cidash-cap-cell'), function(td){
          var done = td.getAttribute('data-done') === 'false' ? false : true;
          out.push({ cap:td.getAttribute('data-cap'), sha:td.getAttribute('data-sha'),
                     parent:td.getAttribute('data-parent')||'', order:idx, done:done });
        });
        Array.prototype.forEach.call(tr.querySelectorAll('td.cidash-active-cell'), function(td){
          out.push({ cap:td.getAttribute('data-cap'), sha:td.getAttribute('data-sha'),
                     parent:td.getAttribute('data-parent')||'', order:idx, done:true });
        });
        // A client-side optimistic overlay (Queued / Running / Failed) can REPLACE an
        // empty run glyph, leaving the cell with ONLY data-qcap/data-qsha (the run's
        // <a> - and thus the cap/sha this dialog keys off - is gone). Gather those too
        // so a cell whose only state is an in-browser overlay (e.g. a unit-test run that
        // FAILED) stays re-runnable here instead of vanishing as "none in scope". A
        // cap-cell/active-cell that ALSO carries an overlay is already counted above, so
        // skip it to avoid double-counting; parent is recovered from the stored entry.
        Array.prototype.forEach.call(tr.querySelectorAll('td.cidash-queued-cell[data-qcap]'), function(td){
          if(td.classList.contains('cidash-cap-cell') || td.classList.contains('cidash-active-cell')) return;
          var qc=td.getAttribute('data-qcap'), qs=td.getAttribute('data-qsha');
          if(!qc || !qs) return;
          var qe=(qLoad()[qc+'|'+qs])||{};
          out.push({ cap:qc, sha:qs, parent:qe.parent||'', order:idx, done:true });
        });
      });
      if(RT.snapshots2){
        HIST.forEach(function(r, idx){
          var cov=r.snapshots2||{};
          capPlatforms('snapshots2').forEach(function(platform){
            var c=cov[platform]||{}; var total=Number(c.total||0), have=Number(c.have||0);
            if(total>0){ out.push({ cap:'snapshots2', sha:r.sha, parent:'', order:idx, done:(have>=total), platform:platform }); }
          });
        });
      }
      return out;
    }
    function histScopeMode(){ var r=document.querySelector('input[name="cidash-hist-scope"]:checked'); return r ? r.value : 'all'; }
    // The revision pickers mirror the dashboard table: dependency-only revisions
    // (only a .vipc/.vip changed) are hidden unless "Show dependency-only" is on,
    // and a free-text search narrows by short SHA or commit title. histVisible() is
    // the newest-first set every scope (all / range / specific) actually operates on.
    var histSearch = '';
    var histInclDep = false;
    function histMatchSearch(r){
      if(!histSearch) return true;
      var q=histSearch.toLowerCase();
      return (String(r.short||'').toLowerCase().indexOf(q)>=0)
          || (String(r.sha||'').toLowerCase().indexOf(q)>=0)
          || (String(r.msg||'').toLowerCase().indexOf(q)>=0);
    }
    function histVisible(){ return HIST.filter(function(r){ return (histInclDep || !r.dep_only) && histMatchSearch(r); }); }
    function histIdxIn(list, sha){ for(var i=0;i<list.length;i++){ if(list[i].sha===sha) return i; } return -1; }
    function histIncludedShas(){
      // Three scopes drive which revisions are queued (visible set is newest-first):
      //   all      -> every visible revision
      //   range    -> Start (older bound) .. Stop (newer bound), inclusive; a blank
      //               Start = oldest and a blank Stop = newest, and a reversed pair
      //               is swapped so the range is always valid
      //   specific -> exactly the ticked revisions (one or many)
      var mode=histScopeMode(); var inc={};
      if(mode==='specific'){
        Array.prototype.forEach.call(document.querySelectorAll('input.cidash-hist-spec:checked'), function(b){ inc[b.value]=1; });
        return inc;
      }
      var vis=histVisible();
      if(mode==='range'){
        var f=document.getElementById('cidash-hist-from'); var t=document.getElementById('cidash-hist-to');
        var iFrom = (f && f.value) ? histIdxIn(vis, f.value) : vis.length-1;   // '' = oldest
        var iTo   = (t && t.value) ? histIdxIn(vis, t.value) : 0;              // '' = newest
        if(iFrom<0) iFrom=vis.length-1; if(iTo<0) iTo=0;
        var lo=Math.min(iFrom,iTo), hi=Math.max(iFrom,iTo);
        for(var j=lo;j<=hi;j++){ if(vis[j]) inc[vis[j].sha]=1; }
        return inc;
      }
      vis.forEach(function(r){ inc[r.sha]=1; });
      return inc;
    }
    function histRenderRevList(){
      // (Re)build the "All" count, the range dropdowns and the specific checklist
      // from histVisible(), preserving current selections where the revision is
      // still visible. Called on open and whenever the search / dep toggle changes.
      var vis=histVisible();
      var ac=document.getElementById('cidash-hist-allcount');
      if(ac) ac.textContent='All '+vis.length+' revision'+(vis.length===1?'':'s');
      var optsHtml=vis.map(function(r){ return '<option value="'+esc(r.sha)+'">'+esc((r.short||'')+(r.msg?(' \u2014 '+r.msg):''))+'</option>'; }).join('');
      ['cidash-hist-from','cidash-hist-to'].forEach(function(id){
        var sel=document.getElementById(id); if(!sel) return;
        var prev=sel.value;
        var head=(id==='cidash-hist-from')?'<option value="">Oldest (beginning)</option>':'<option value="">Newest (latest)</option>';
        sel.innerHTML=head+optsHtml;
        sel.value=(prev && vis.some(function(r){return r.sha===prev;}))?prev:'';
      });
      var checked={};
      Array.prototype.forEach.call(document.querySelectorAll('input.cidash-hist-spec:checked'), function(b){ checked[b.value]=1; });
      var host=document.getElementById('cidash-hist-speclist'); if(!host) return;
      if(!vis.length){ host.innerHTML='<div class="cidash-hist-specempty">No revisions match.</div>'; return; }
      host.innerHTML=vis.map(function(r){
        return '<label class="cidash-hist-specitem"><input type="checkbox" class="cidash-hist-spec" value="'+esc(r.sha)+'"'+(checked[r.sha]?' checked':'')+'>'
          + '<span class="sh">'+esc(r.short||'')+'</span><span class="ms">'+esc(r.msg||'')+'</span>'
          + (r.dep_only?'<span class="cidash-hist-deptag" title="Only an external dependency (.vipc/.vip) changed in this revision">dep</span>':'')
          + '</label>';
      }).join('');
      Array.prototype.forEach.call(document.querySelectorAll('input.cidash-hist-spec'), function(b){ b.addEventListener('change', histRefresh); });
    }
    function histScopeApply(){
      // Reveal only the sub-control for the chosen scope.
      var mode=histScopeMode();
      var rr=document.getElementById('cidash-hist-rangerow'); if(rr) rr.style.display = (mode==='range') ? '' : 'none';
      var sw=document.getElementById('cidash-hist-specwrap'); if(sw) sw.style.display = (mode==='specific') ? '' : 'none';
    }
    function histCells(){
      // The exact cells to dispatch, from each activity's mode + the revision scope:
      //   off    -> nothing
      //   fill   -> only the empty (never-run) cells          (the old default)
      //   rerun  -> empty AND already-done cells: fill gaps and replace existing results
      var inc=histIncludedShas(); var out=[];
      histAllCells().forEach(function(x){
        if(!inc[x.sha]) return;
        var mode=actMode[x.cap]||'off'; if(mode==='off') return;
        if(x.cap==='vidiff' && !x.parent) return;        // a root commit has no base to diff
        if(x.platform && histSelectedPlatforms(x.cap).indexOf(x.platform)<0) return;
        if(mode==='fill' && x.done) return;              // fill leaves finished cells alone
        x.mode=mode;
        out.push(x);
      });
      return out;
    }
    function histCounts(){
      // Per-cap {fill, done} tally within the current revision scope \u2014 drives each
      // row's count hint and which segment buttons are live.
      var inc=histIncludedShas(); var by={};
      histAllCells().forEach(function(x){
        if(!inc[x.sha]) return;
        if(x.cap==='vidiff' && !x.parent) return;
        if(x.platform && histSelectedPlatforms(x.cap).indexOf(x.platform)<0) return;
        var c=by[x.cap]||(by[x.cap]={fill:0,done:0});
        if(x.done) c.done++; else c.fill++;
      });
      return by;
    }
    function histCountText(cap, c){
      if(c.fill>0 && c.done>0) return c.fill+' missing \u00b7 '+c.done+' done';
      if(c.fill>0) return c.fill+' to fill';
      if(c.done>0) return 'all '+c.done+' done';
      return 'none in scope';
    }
    function histClampMode(cap, mode, c){
      // The mode actually SHOWN for an activity in the current revision scope.
      // actMode keeps the user's raw INTENT; this reduces it to a mode that can
      // queue at least one run, so the highlighted segment is never also disabled
      // (the old cause of a lit-but-greyed "Re-run"). Widening the scope later can
      // re-enable the original intent because actMode itself is left untouched.
      //   fill      -> needs a missing cell, else there is nothing to fill (Skip)
      //   rerun     -> every selected revision with this activity; existing results
      //                are replaced and missing cells are filled
      if(mode==='off') return 'off';
      if(mode==='rerun')    return (c.done>0 || c.fill>0) ? 'rerun' : 'off';
      return c.fill>0 ? 'fill' : 'off';
    }
    function histRefresh(){
      // Recompute everything that depends on the current selection: each row's count
      // hint + segment availability + active state, then the summary and Queue button.
      var counts=histCounts();
      histInstalledCaps().forEach(function(cap){
        var c=counts[cap]||{fill:0,done:0};
        // Clamp the stored intent to a runnable mode for the highlight + skip
        // dimming; dispatch (histCells) naturally agrees with it.
        var mode=histClampMode(cap, actMode[cap]||'off', c);
        var chip=document.getElementById('cidash-hist-count-'+cap); if(chip) chip.textContent=histCountText(cap, c);
        var seg=document.getElementById('cidash-hist-seg-'+cap);
        if(seg){
          // A segment is disabled only when it would queue nothing, and always
          // carries a tooltip saying why (answering "why is Re-run greyed out?").
          var bFill=seg.querySelector('button[data-mode="fill"]');
          if(bFill){ bFill.disabled=(c.fill===0);
            bFill.title=(c.fill>0 ? (cap==='snapshots'?'Render the snapshots still missing in the selected revisions':'Queue only the selected revisions missing this result')
                                  : (cap==='snapshots'?'Nothing to render \u2014 every selected revision already has its snapshots':'Nothing to fill \u2014 every selected revision already has this result')); }
          var bRe=seg.querySelector('button[data-mode="rerun"]');
          if(bRe){ bRe.disabled=((c.done||0)+(c.fill||0)===0);
            bRe.title=((c.done||0)+(c.fill||0)>0 ? 'Run every selected revision for this activity; existing results are replaced and missing ones are filled'
                                               : 'Nothing to re-run \u2014 no selected revisions have this activity'); }
          Array.prototype.forEach.call(seg.querySelectorAll('button'), function(b){ b.classList.toggle('on', b.getAttribute('data-mode')===mode); });
        }
        var row=document.getElementById('cidash-hist-actrow-'+cap); if(row) row.classList.toggle('skip', mode==='off');
      });
      var cells=histCells();
      var shas={}; var snaps=0; var perRevRuns=0; var anyRe=false; var groupKeys={};
      cells.forEach(function(x){
        shas[x.sha]=1; if(x.done) anyRe=true;
        if(x.cap==='snapshots'){ snaps=1; return; }
        cellPlatforms(x).forEach(function(plat){
          var p=(RT[x.cap]&&RT[x.cap].platforms)?RT[x.cap].platforms[plat]:null;
          if(reuseWarm && p && p.batch){ groupKeys[x.cap+'|'+plat]=1; }   // one warm-container run per (cap,platform)
          else { perRevRuns++; }
        });
      });
      var runs=perRevRuns + Object.keys(groupKeys).length + snaps;
      var nrev=Object.keys(shas).length;
      var est=histEstimate();
      var sum=document.getElementById('cidash-hist-summary');
      if(sum){
        if(!HIST.length){ sum.innerHTML='No project revisions to populate yet \u2014 commit some VIs first.'; }
        else if(!runs){
          var anyOn=histInstalledCaps().some(function(c){ return (actMode[c]||'off')!=='off'; });
          var canRe=histInstalledCaps().some(function(c){ var cc=counts[c]||{}; return (actMode[c]||'off')==='fill' && (cc.done||0)>0; });
          if(canRe){ sum.innerHTML='Nothing to fill \u2014 the selected revisions already have these results. Switch an activity to <b>Re-run</b> to rebuild them.'; }
          else if(anyOn){ sum.innerHTML='Nothing to queue for the selected revisions and activities.'; }
          else { sum.innerHTML='Pick at least one activity below \u2014 every one is set to <b>Skip</b>.'; }
        } else {
          // Live, history-driven time estimate. When the warm-container option is on
          // and at least one activity is batch-capable, show Fresh vs Warm compute
          // and the saving; otherwise show the single estimate and nudge the option.
          var estHtml='';
          if(est){
            if(reuseWarm && est.anyBatch){
              estHtml = ' <span style="color:var(--fg-muted)">Est. compute: warm <b>'+fmtDur(est.warm)+'</b> vs fresh '+fmtDur(est.fresh)+' \u2014 saves \u2248 <b>'+fmtDur(Math.max(0,est.fresh-est.warm))+'</b> (runs serially in one container).</span>';
            } else {
              estHtml = ' <span style="color:var(--fg-muted)">Est. compute \u2248 '+fmtDur(est.fresh)
                + (anyBatchAvailable()?' \u2014 tick \u201ckeep one container warm\u201d below to cut the repeated startup':'')+'.</span>';
            }
            if(!est.anyMeasured) estHtml += ' <span style="color:var(--fg-muted)">(approximate \u2014 no run history yet)</span>';
          }
          sum.innerHTML='Will queue <b>'+runs+'</b> run'+(runs>1?'s':'')+' across <b>'+nrev+'</b> revision'+(nrev>1?'s':'')+', oldest first.'
            + (anyRe?' <span style="color:var(--fg-muted)">Re-runs replace the existing result.</span>':'')
            + (runs>=20 && !reuseWarm?' <span style="color:var(--fg-muted)">They queue and run in order, paced by your concurrency limit.</span>':'')
            + estHtml;
        }
      }
      var go=document.getElementById('cidash-hist-go'); if(go) go.disabled = !runs;
    }
    function histSetMode(cap, mode){
      actMode[cap]=mode; histRefresh();
    }
    function histPreset(kind){
      // The quick chips \u2014 each just sets every row's mode, then the user fine-tunes.
      // "new" and "diff" reproduce the old global "only workers that haven't run yet"
      // and "diff-based" toggles, now as one-click starting points over per-row control.
      histInstalledCaps().forEach(function(cap){
        if(kind==='fill') actMode[cap]='fill';
        else if(kind==='rerun') actMode[cap]='rerun';
        else if(kind==='clear') actMode[cap]='off';
        else if(kind==='diff') actMode[cap]=DIFF_CAPS[cap]?'fill':'off';
        else if(kind==='new') actMode[cap]=RAN[cap]?'off':'fill';
      });
      histRefresh();
    }
    function histRender(){
      var body=document.getElementById('cidash-hist-body'); if(!body) return;
      var h='';
      h += '<p class="cidash-hist-intro">Queue CI for revisions that already exist so the dashboard fills in. Runs <strong>oldest \u2192 newest</strong>. For each activity, choose to <b>Fill</b> only the missing results or <b>Re-run</b> every selected revision.</p>';
      var anyDep = HIST.some(function(r){ return r.dep_only; });
      h += '<div class="cidash-hist-sec"><label class="cidash-hist-lbl">Which revisions</label>';
      h += '<div class="cidash-hist-revfilter"><input type="text" id="cidash-hist-revsearch" class="cidash-hist-search" placeholder="Search by title or SHA\u2026" autocomplete="off" spellcheck="false" value="'+esc(histSearch)+'">';
      if(anyDep) h += '<label class="cidash-hist-inclchk" title="Revisions whose only change is an external dependency (.vipc/.vip). Hidden by default, matching the dashboard."><input type="checkbox" id="cidash-hist-incldep"'+(histInclDep?' checked':'')+'> Show dependency-only revisions</label>';
      h += '</div>';
      h += '<div class="cidash-hist-scope">';
      h += '<label class="cidash-hist-radio"><input type="radio" name="cidash-hist-scope" value="all" checked> <span id="cidash-hist-allcount">All revisions</span> <span class="sub">\u2014 the full history</span></label>';
      h += '<label class="cidash-hist-radio"><input type="radio" name="cidash-hist-scope" value="range"> A range of history</label>';
      h += '<div class="cidash-hist-rangerow" id="cidash-hist-rangerow" style="display:none">';
      h += '<label>Start at <select id="cidash-hist-from"></select></label>';
      h += '<label>stop at <select id="cidash-hist-to"></select></label>';
      h += '</div>';
      h += '<label class="cidash-hist-radio"><input type="radio" name="cidash-hist-scope" value="specific"> Specific revision(s)</label>';
      h += '<div id="cidash-hist-specwrap" style="display:none">';
      h += '<div class="cidash-hist-spectools"><a href="#" id="cidash-hist-spec-all">Select all</a><a href="#" id="cidash-hist-spec-none">Clear</a></div>';
      h += '<div class="cidash-hist-speclist" id="cidash-hist-speclist"></div>';
      h += '</div>';
      h += '</div></div>';
      var instCaps=histInstalledCaps();
      h += '<div class="cidash-hist-sec"><div class="cidash-hist-actshead"><label class="cidash-hist-lbl" style="margin:0">Activities</label>';
      if(instCaps.length){
        h += '<div class="cidash-hist-quick">'
          + '<button type="button" class="cidash-hist-chip" data-preset="fill" title="Queue every missing result; leave finished revisions alone">Fill gaps</button>'
          + '<button type="button" class="cidash-hist-chip" data-preset="rerun" title="Re-run every selected revision, replacing existing results">Re-run all</button>'
          + '<button type="button" class="cidash-hist-chip" data-preset="new" title="Only activities that have never run anywhere \u2014 skip ones that already have any results">New only</button>'
          + '<button type="button" class="cidash-hist-chip" data-preset="diff" title="Just the visual history \u2014 VI Snapshots + VIDiff">Diff only</button>'
          + '<button type="button" class="cidash-hist-chip" data-preset="clear" title="Set every activity to Skip">Clear</button>'
          + '</div>';
      }
      h += '</div><div class="cidash-hist-acts">';
      if(!instCaps.length){
        h += '<div class="cidash-hist-actsempty">No runnable activities found for this repository.</div>';
      } else instCaps.forEach(function(cap){
        var m=CAP_META[cap]||[cap,''];
        h += '<div class="cidash-hist-act'+(histHasPlatformPicker(cap)?' has-plats':'')+'" id="cidash-hist-actrow-'+cap+'">'
          + '<div class="cidash-hist-actinfo"><div class="cidash-hist-actname">'+esc(m[0])+'</div><div class="cidash-hist-actsub">'+esc(m[1])+'</div></div>'
          + '<div class="cidash-hist-actmeta"><span class="cidash-hist-actcount" id="cidash-hist-count-'+cap+'"></span>'+histPlatformHtml(cap)+'</div>'
          + '<span class="cidash-seg" id="cidash-hist-seg-'+cap+'" role="group" aria-label="'+esc(m[0])+' mode">'
          + '<button type="button" data-cap="'+cap+'" data-mode="off">Skip</button>'
          + '<button type="button" data-cap="'+cap+'" data-mode="fill">'+(cap==='snapshots'?'Render':'Fill')+'</button>'
          + '<button type="button" data-cap="'+cap+'" data-mode="rerun">Re-run</button>'
          + '</span></div>';
      });
      h += '</div></div>';
      // Opt-in warm-container reuse — only shown when at least one installed
      // activity is batch-capable (has a backfill workflow). OFF by default, with
      // a prominent warning that state carries over between revisions.
      if(anyBatchAvailable()){
        h += '<div class="cidash-hist-sec"><label style="display:flex;gap:10px;align-items:flex-start;border:1px solid #da3633;background:rgba(218,54,51,.06);border-radius:8px;padding:10px 12px;cursor:pointer">'
          + '<input type="checkbox" id="cidash-hist-reuse" style="margin-top:3px">'
          + '<span style="font-size:.82em;line-height:1.5">'
          + '<b style="color:#da3633">\u26A0 Keep one container warm across revisions</b> <span style="color:var(--fg-muted)">(off by default)</span><br>'
          + '<span style="color:var(--fg-muted)">Pays the slow Windows container startup <b>once</b> for the whole batch instead of per revision \u2014 a big saving on long histories. '
          + 'The trade-off: container state (temp files, the LabVIEW object cache, the registry) <b>carries over</b> between revisions, so a revision whose code modifies the system can influence a later revision\u2019s result. '
          + 'The container image itself is never changed. Only Windows activities that support batching are affected \u2014 leave this off unless you understand the risk.</span>'
          + '</span></label></div>';
      }
      h += '<div id="cidash-hist-tok" style="display:none;border:1px solid var(--border);border-radius:8px;padding:12px;background:var(--surface);margin:0 0 12px">';
      h += '<div style="font-size:.82em;color:var(--fg);font-weight:600;margin-bottom:6px">One-time setup \u2014 a token to queue runs</div>';
      h += '<ol style="font-size:.8em;color:var(--fg-muted);margin:0 0 8px;padding-left:18px;line-height:1.6">';
      h += '<li><a href="'+tokenSetupUrl()+'" target="_blank" rel="noopener" style="color:var(--link)">Create a fine-grained token \u2197</a> \u2014 opens with the name, owner, and <strong>Actions: Read and write</strong> already set.</li>';
      h += '<li><strong>Repository access</strong> \u2192 Only select repositories \u2192 add <code>'+esc(REPO)+'</code>.</li>';
      h += '<li><strong>Permissions \u2192 Repository permissions \u2192 Actions \u2192 Read and write</strong>.</li>';
      h += '<li>Generate, then paste it below.</li></ol>';
      h += '<div style="display:flex;gap:8px;flex-wrap:wrap"><input id="cidash-hist-tok-input" type="password" autocomplete="off" placeholder="github_pat_\u2026 or ghp_\u2026" style="flex:1 1 240px;min-width:180px;padding:7px 10px;background:var(--bg);color:var(--fg);border:1px solid var(--border);border-radius:6px;font-family:ui-monospace,Menlo,monospace;font-size:.8em">';
      h += '<button class="cidash-btn cidash-go" id="cidash-hist-tok-save">Save &amp; queue</button></div></div>';
      h += '<div id="cidash-hist-summary" class="cidash-hist-summary"></div>';
      h += '<div id="cidash-hist-status" style="font-size:.82em;min-height:1.2em;margin:0 0 12px"></div>';
      h += '<div style="display:flex;gap:10px;flex-wrap:wrap;align-items:center">';
      h += '<button class="cidash-btn cidash-go" id="cidash-hist-go">\u25B6 Queue runs</button>';
      h += '<button class="cidash-btn cidash-ghost" id="cidash-hist-cancel">Cancel</button></div>';
      body.innerHTML=h;
      // Mirror the dashboard's current dependency-only filter so the dialog shows
      // the same revisions the table does; populate the pickers from the visible set.
      var _dd=document.getElementById('show-deponly'); histInclDep=!!(_dd && _dd.checked);
      var _idp=document.getElementById('cidash-hist-incldep'); if(_idp) _idp.checked=histInclDep;
      histRenderRevList();
      var _rs=document.getElementById('cidash-hist-revsearch'); if(_rs) _rs.addEventListener('input', function(){ histSearch=_rs.value.trim(); histRenderRevList(); histRefresh(); });
      if(_idp) _idp.addEventListener('change', function(){ histInclDep=!!_idp.checked; histRenderRevList(); histRefresh(); });
      instCaps.forEach(function(cap){ actMode[cap]='fill'; });   // open with every activity on Fill (gaps only)
      reuseWarm=false;   // warm-container reuse always starts OFF
      var rchk=document.getElementById('cidash-hist-reuse'); if(rchk) rchk.addEventListener('change', function(){ reuseWarm=!!rchk.checked; histRefresh(); });
      Array.prototype.forEach.call(document.querySelectorAll('.cidash-hist-chip'), function(b){ b.addEventListener('click', function(){ histPreset(b.getAttribute('data-preset')); }); });
      Array.prototype.forEach.call(document.querySelectorAll('.cidash-seg button'), function(b){ b.addEventListener('click', function(){ if(b.disabled) return; histSetMode(b.getAttribute('data-cap'), b.getAttribute('data-mode')); }); });
      Array.prototype.forEach.call(document.querySelectorAll('.cidash-hist-plat'), function(b){ b.addEventListener('change', function(){
        var cap=b.getAttribute('data-cap'); var platform=b.getAttribute('data-platform'); var keys=capPlatforms(cap);
        var selected=(platState[cap] || keys.slice()).filter(function(k){ return k!==platform; });
        if(b.checked) selected.push(platform);
        platState[cap]=selected.filter(function(k,i,a){ return keys.indexOf(k)>=0 && a.indexOf(k)===i; });
        histRefresh();
      }); });
      Array.prototype.forEach.call(document.querySelectorAll('input[name="cidash-hist-scope"]'), function(r){ r.addEventListener('change', function(){ histScopeApply(); histRefresh(); }); });
      var hf=document.getElementById('cidash-hist-from'); if(hf) hf.addEventListener('change', histRefresh);
      var ht=document.getElementById('cidash-hist-to'); if(ht) ht.addEventListener('change', histRefresh);
      var spa=document.getElementById('cidash-hist-spec-all'); if(spa) spa.addEventListener('click', function(e){ e.preventDefault(); Array.prototype.forEach.call(document.querySelectorAll('input.cidash-hist-spec'), function(b){ b.checked=true; }); histRefresh(); });
      var spn=document.getElementById('cidash-hist-spec-none'); if(spn) spn.addEventListener('click', function(e){ e.preventDefault(); Array.prototype.forEach.call(document.querySelectorAll('input.cidash-hist-spec'), function(b){ b.checked=false; }); histRefresh(); });
      var go=document.getElementById('cidash-hist-go'); if(go) go.addEventListener('click', histRun);
      var cancel=document.getElementById('cidash-hist-cancel'); if(cancel) cancel.addEventListener('click', cidashHistClose);
      var save=document.getElementById('cidash-hist-tok-save'); if(save) save.addEventListener('click', function(){ var i=document.getElementById('cidash-hist-tok-input'); var v=(i&&i.value||'').trim(); if(!v){ if(i) i.focus(); return; } setTok(v); histTokPanel(false); histRun(); });
      var tin=document.getElementById('cidash-hist-tok-input'); if(tin) tin.addEventListener('keydown', function(e){ if(e.key==='Enter'){ e.preventDefault(); var v=(tin.value||'').trim(); if(v){ setTok(v); histTokPanel(false); histRun(); } } });
      histScopeApply(); histRefresh();
    }
    function histOpen(opts){
      var m=histModal(); if(!m) return;
      histRender();
      if(opts && (opts.cap || opts.sha)) histPreselect(opts);
      m.style.display='block'; document.body.style.overflow='hidden';
    }
    function histPreselect(opts){
      // Open the dialog pre-configured to re-run a single document (a report's
      // "Re-run" button routes here via the shared header): scope to exactly that
      // revision, set only that activity to Re-run, and - when the activity is
      // platform-split - limit to the document's platform. Falls back silently to
      // the normal full dialog when the revision/activity isn't on this dashboard.
      try{
        if(opts.sha && histIdxIn(HIST, opts.sha)>=0){
          var sp=document.querySelector('input[name="cidash-hist-scope"][value="specific"]'); if(sp){ sp.checked=true; histScopeApply(); }
          Array.prototype.forEach.call(document.querySelectorAll('input.cidash-hist-spec'), function(b){ b.checked=(b.value===opts.sha); });
        }
        if(opts.cap){
          histInstalledCaps().forEach(function(cap){ actMode[cap]=(cap===opts.cap)?'rerun':'off'; });
          if(opts.platform && histHasPlatformPicker(opts.cap)){
            platState[opts.cap]=[opts.platform];
            Array.prototype.forEach.call(document.querySelectorAll('.cidash-hist-plat[data-cap="'+opts.cap+'"]'), function(b){ b.checked=(b.getAttribute('data-platform')===opts.platform); });
          }
        }
      }catch(e){}
      histRefresh();
    }
    function histRun(){
      var cells=histCells();
      if(!cells.length){ histStatus('Nothing to queue \u2014 the selected revisions already have results for those activities.', 'warn'); return; }
      if(!getTok()){ histTokPanel(true); histStatus('Add a token (Actions: Read and write) to queue runs.', 'warn'); var i=document.getElementById('cidash-hist-tok-input'); if(i) i.focus(); return; }
      histTokPanel(false);
      if(!confirmLargeQueue(dispatchTotal(cells, {reuse: reuseWarm}))){ histStatus('Cancelled \u2014 nothing was queued.', 'warn'); return; }
      var go=document.getElementById('cidash-hist-go'); var cancel=document.getElementById('cidash-hist-cancel');
      if(go) go.disabled=true; if(cancel) cancel.disabled=true;
      dispatchCells(cells, histStatus, {reuse: reuseWarm}).then(function(res){
        if(cancel) cancel.disabled=false;
        if(res.ok && !res.err){
          histStatus('\u2713 Queued '+res.ok+' run'+(res.ok>1?'s':'')+', oldest first \u2014 watch them fill in on the dashboard\u2026', 'ok');
          bfDismiss();   // the fresh-install nudge has served its purpose
          // Close the dialog so the just-queued cells - already showing "Queued" on
          // the table behind it - are revealed; the user asked to land on the
          // dashboard after queuing. A brief pause lets the confirmation register.
          setTimeout(cidashHistClose, 900);
        } else if(res.ok && res.err){
          if(go) go.disabled=false;
          var pm=dispatchFailMsg(res.fails);
          histStatus('Queued '+res.ok+', but '+res.err+' could not be dispatched. '+pm.html, 'warn');
          if(pm.tok) histTokPanel(true);
        } else {
          if(go) go.disabled=false;
          var fm=dispatchFailMsg(res.fails);
          histStatus(fm.html, 'err');
          if(fm.tok) histTokPanel(true);
        }
      });
    }
    // Exposed for the shared header's "Populate history" menu item.
    window.lvciRunHistory = histOpen;
    // When the dialog is opened inside an overlay iframe from a report page's
    // "Re-run" (host requests ?lvci-embed=1), show ONLY the dialog: hide the
    // dashboard header/table and make the page transparent, so the iframe reads
    // as a plain modal over the dimmed host page, not the whole dashboard behind it.
    function lvciEmbedMode(){
      try{
        var d=document.documentElement; if(d.classList.contains('lvci-embed')) return; d.classList.add('lvci-embed');
        var st=document.createElement('style');
        st.textContent='html.lvci-embed,html.lvci-embed body{background:transparent!important}'
          +'html.lvci-embed body>*:not(#cidash-hist-modal){display:none!important}'
          +'html.lvci-embed #cidash-hist-modal{background:rgba(0,0,0,.55)!important}';
        (document.head||document.documentElement).appendChild(st);
      }catch(e){}
    }
    // Apply embed hiding synchronously - this script runs while the page is still
    // parsing (before <main>/the table below), so the chrome is never painted.
    try{ if(new URLSearchParams(location.search||'').get('lvci-embed')==='1') lvciEmbedMode(); }catch(e){}
    // Exposed because the modal's "× Close" button and backdrop use inline onclick
    // handlers, which resolve against window — not this IIFE's scope. Without this
    // the close button silently did nothing (the Cancel button and Esc, which call
    // the inner function directly, were unaffected).
    window.cidashHistClose = cidashHistClose;
    document.addEventListener('keydown', function(e){ if(e.key==='Escape'){ var m=histModal(); if(m && m.style.display==='block') cidashHistClose(); } });
    // Re-apply optimistic "Queued" badges once the table exists (this script runs
    // before the table is parsed), and after every auto-refresh thereafter; wire
    // the backfill card the same way.
    // Auto-open the "Populate history" dialog when another page routed here for it
    // (the shared header's Populate history menu item, or a report's "Re-run"
    // button, navigate to ?lvci-populate=1[&cap&sha&platform] when the dashboard's
    // inline dialog isn't on their page). The trigger params are stripped so a
    // manual reload doesn't reopen it.
    function lvciAutoPopulate(){
      try{
        var p=new URLSearchParams(location.search||'');
        if(p.get('lvci-populate')!=='1') return;
        var embed=(p.get('lvci-embed')==='1');
        var opts={ cap:(p.get('cap')||''), sha:(p.get('sha')||''), platform:(p.get('platform')||'') };
        try{ ['lvci-populate','lvci-embed','cap','sha','platform'].forEach(function(k){ p.delete(k); });
             var qs=p.toString(); history.replaceState(null,'',location.pathname+(qs?('?'+qs):'')+location.hash); }catch(e){}
        if(embed) lvciEmbedMode();
        histOpen((opts.cap||opts.sha)?opts:undefined);
      }catch(e){}
    }
    if(document.readyState === 'loading'){ document.addEventListener('DOMContentLoaded', function(){ applyQueued(); qSync(); bfInit(); lvciAutoPopulate(); }); }
    else { applyQueued(); qSync(); bfInit(); lvciAutoPopulate(); }
    // Returning to the dashboard after kicking off a run elsewhere (e.g. "Re-run
    // analysis" from a report, or a run started in another tab) should reflect it
    // right away. The page otherwise only updates on its auto-refresh timer (up to
    // 15 min when idle), so re-apply the optimistic "Queued" overlays and re-sync
    // against GitHub whenever the tab is shown again or regains focus (throttled).
    var _qReChk = 0;
    function qRecheck(){ var n = Date.now(); if(n - _qReChk < 4000) return; _qReChk = n; try{ applyQueued(); qSync(); }catch(e){} }
    document.addEventListener('visibilitychange', function(){ if(!document.hidden) qRecheck(); });
    window.addEventListener('focus', qRecheck);
  })();
  </scr""" + """ipt>""").replace('__RUN_TARGETS__', run_targets_json).replace('__RUN_TIMING__', run_timing_json).replace('__HIST__', hist_json).replace('__CAPS_RAN__', caps_ran_json).replace('__REPO__', repo).replace('__BRANCH__', get_default_branch())

# ── Debug Run dialog ─────────────────────────────────────────────────────────
# Tools > Debug Run. A three-step flow (choose target -> connect -> hand off):
#   1. Pick the worker (Linux for now), the revision, which activities to run on
#      the go signal, and a session length; dispatch debug-session.yml.
#   2. Poll for the published connect info and open the browser noVNC desktop.
#   3. Tell the user to log into LabVIEW then press ENTER in the on-screen prompt
#      to run the selected activities; offer "End session" (cancels the run).
# Self-contained modal + controller, exposed as window.lvciDebugRun. Reuses the
# same dispatch token (lvci_dispatch_token / lvci_install_token) as every other
# dispatch on the dashboard.
debug_dialog = (r"""
  <div id="cidash-debug-modal" onclick="if(event.target===this)cidashDebugClose()" style="display:none;position:fixed;inset:0;z-index:320;background:rgba(0,0,0,.55)">
    <div role="dialog" aria-modal="true" aria-labelledby="cidash-debug-title" style="position:absolute;left:50%;top:50%;transform:translate(-50%,-50%);width:min(600px,calc(100% - 32px));max-height:calc(100% - 48px);overflow:auto;background:var(--bg);border:1px solid var(--border);border-radius:10px;box-shadow:0 10px 48px rgba(0,0,0,.5)">
      <div style="display:flex;align-items:center;justify-content:space-between;padding:12px 16px;border-bottom:1px solid var(--border);background:var(--surface)">
        <strong id="cidash-debug-title" style="font-size:.95em">Debug Run</strong>
        <button onclick="cidashDebugClose()" style="background:transparent;border:1px solid var(--border);color:var(--fg);padding:5px 12px;border-radius:6px;cursor:pointer;font-size:.82em">&#10005; Close</button>
      </div>
      <div id="cidash-debug-body" style="padding:16px"></div>
    </div>
  </div>
  <script>
  (function(){
    var RT = __RUN_TARGETS__;
    var HIST = __HIST__;
    var REPO = "__REPO__";
    var BRANCH = "__BRANCH__";
    var TOK_KEY = "lvci_dispatch_token";
    var WF = "debug-session.yml";
    var CAP_LABEL = { masscompile:'Mass Compile', vidiff:'VIDiff', snapshots2:'VI Browser 2.0 Snapshots', builds:'Builds' };
    function $(id){ return document.getElementById(id); }
    function esc(s){ return String(s==null?'':s).replace(/[&<>"']/g,function(c){return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]; }); }
    function getTok(){ try{ return localStorage.getItem(TOK_KEY)||localStorage.getItem("lvci_install_token")||''; }catch(e){ return ''; } }
    function setTok(v){ try{ localStorage.setItem(TOK_KEY, v); }catch(e){} }
    function ghHeaders(){ return { 'Authorization':'Bearer '+getTok(), 'Accept':'application/vnd.github+json', 'X-GitHub-Api-Version':'2022-11-28' }; }
    function pagesBase(){ var u=(window.LVCI&&window.LVCI.pagesUrl)||''; if(u&&u.charAt(u.length-1)!=='/') u+='/'; return u; }
    function tokenSetupUrl(){
      var owner=(REPO.split('/')[0])||'';
      return 'https://github.com/settings/personal-access-tokens/new?name='+encodeURIComponent('LabVIEW CI dispatch')
        +'&description='+encodeURIComponent('Queue LabVIEW CI runs for '+REPO)
        +(owner?'&target_name='+encodeURIComponent(owner):'')+'&actions=write';
    }
    // Existing debug sessions (any in-progress debug-session run), shown below the
    // start form: reconnect (Open) or End from ANY device, with a live progress
    // indicator for ones still queued or booting.
    var liveTimer = null, liveIds = '';
    function clearLiveTimer(){ if(liveTimer){ clearInterval(liveTimer); liveTimer=null; } }
    function ensureLiveCss(){
      if(document.getElementById('dbg-live-css')) return;
      var st=document.createElement('style'); st.id='dbg-live-css';
      st.textContent='.dbg-bar{height:6px;background:var(--border);border-radius:4px;overflow:hidden;margin:6px 0}'
        +'.dbg-bar>span{display:block;height:100%;width:0;background:#1f6feb;border-radius:4px;transition:width .4s}'
        +'.dbg-bar.dbg-ok>span{background:#2ea043}'
        +'.dbg-bar.dbg-indet>span{width:35%;animation:dbgSlide 1.15s ease-in-out infinite}'
        +'@keyframes dbgSlide{0%{margin-left:-35%}100%{margin-left:100%}}';
      (document.head||document.documentElement).appendChild(st);
    }
    function endRun(runId){
      if(!runId) return;
      if(!window.confirm('End debug session run #'+runId+'? This cancels the run and tears down the container.')) return;
      fetch('https://api.github.com/repos/'+REPO+'/actions/runs/'+runId+'/cancel', { method:'POST', headers:ghHeaders() })
        .then(function(){ setTimeout(refreshLiveSessions, 1500); }).catch(function(){});
    }
    function livePlat(r){ var m=/\((windows|linux)\)/i.exec(r.display_title||r.name||''); return m?m[1].toLowerCase():''; }
    function liveRow(r){
      var sh=(r.head_sha||'').slice(0,7); var plat=livePlat(r);
      return '<div class="dbg-live-row" style="padding:8px 0;border-bottom:1px solid var(--border)">'
        + '<div style="display:flex;align-items:center;gap:8px;flex-wrap:wrap">'
        +   '<span style="font-size:.85em"><a href="'+esc(r.html_url)+'" target="_blank" rel="noopener">run #'+r.id+' &#8599;</a> &middot; <code>'+esc(sh)+'</code>'+(plat?(' &middot; '+esc(plat)):'')+'</span>'
        +   '<span id="dbg-live-open-'+r.id+'" style="margin-left:auto"></span>'
        +   '<button class="dbg-live-end" data-run="'+r.id+'" style="background:transparent;border:1px solid #f85149;color:#f85149;padding:3px 10px;border-radius:6px;cursor:pointer;font-size:.8em">End</button>'
        + '</div>'
        + '<div class="dbg-bar dbg-indet" id="dbg-live-bar-'+r.id+'"><span></span></div>'
        + '<div id="dbg-live-stat-'+r.id+'" style="font-size:.78em;color:var(--fg-muted)">Checking status&hellip;</div>'
        + '</div>';
    }
    function setLiveBar(id, mode, pct){
      var el=$('dbg-live-bar-'+id); if(!el) return;
      el.className='dbg-bar'+(mode==='indet'?' dbg-indet':(mode==='ok'?' dbg-ok':''));
      var sp=el.firstChild; if(!sp) return;
      if(mode==='indet'){ sp.style.width=''; } else { sp.style.width=(pct==null?0:Math.max(4,Math.min(100,pct)))+'%'; }
    }
    function setLiveStat(id, html){ var el=$('dbg-live-stat-'+id); if(el) el.innerHTML=html; }
    function setLiveOpen(id, url){ var el=$('dbg-live-open-'+id); if(el) el.innerHTML='<a href="'+esc(url)+'" target="_blank" rel="noopener" style="display:inline-block;background:#2ea043;border:1px solid #2ea043;color:#fff;padding:4px 12px;border-radius:6px;text-decoration:none;font-size:.82em">Open remote desktop &#8599;</a>'; }
    function fillLiveRow(r){
      // Live? the connect file is published once the tunnel is up.
      fetch(pagesBase()+'debug/'+r.id+'.json?_='+Date.now(), { cache:'no-cache' })
        .then(function(x){ return x.ok?x.json():null; })
        .then(function(j){
          if(j&&j.url){ setLiveOpen(r.id, j.url); setLiveBar(r.id,'ok',100); setLiveStat(r.id,'<span style="color:#3fb950">Live &mdash; remote desktop ready.</span>'); return; }
          if(r.status==='queued'||r.status==='pending'){ setLiveBar(r.id,'indet'); setLiveStat(r.id,'Queued &mdash; waiting for a runner or another debug session to finish.'); return; }
          fetch('https://api.github.com/repos/'+REPO+'/actions/runs/'+r.id+'/jobs?per_page=5', { headers:ghHeaders(), cache:'no-cache' })
            .then(function(x){ return x.ok?x.json():null; })
            .then(function(d){
              var jobs=((d&&d.jobs)||[]);
              var total=0, done=0, cur='';
              jobs.forEach(function(j){ (j.steps||[]).forEach(function(s){ total++; if(s.status==='completed') done++; else if(s.status==='in_progress'&&!cur) cur=s.name; }); });
              if(total){ setLiveBar(r.id,'', Math.round(done/total*100)); setLiveStat(r.id,'Booting &mdash; '+esc(cur||'working')+' ('+done+'/'+total+')'); }
              else { setLiveBar(r.id,'indet'); setLiveStat(r.id,'Booting the container + LabVIEW&hellip;'); }
            }).catch(function(){ setLiveBar(r.id,'indet'); setLiveStat(r.id,'Booting&hellip;'); });
        }).catch(function(){ setLiveBar(r.id,'indet'); setLiveStat(r.id,'Checking status&hellip;'); });
    }
    function refreshLiveSessions(){
      var box=$('dbg-live'); if(!box) return;
      if(!getTok()){ box.innerHTML=''; liveIds=''; return; }
      fetch('https://api.github.com/repos/'+REPO+'/actions/workflows/'+encodeURIComponent(WF)+'/runs?per_page=20', { headers:ghHeaders(), cache:'no-cache' })
        .then(function(r){ return r.ok?r.json():null; })
        .then(function(d){
          var box=$('dbg-live'); if(!box) return;
          var runs=((d&&d.workflow_runs)||[]).filter(function(r){ return r.status!=='completed'; });
          var hdr='<div style="font-weight:600;font-size:.88em;margin:0 0 4px">Existing debug sessions</div>';
          if(!runs.length){ box.innerHTML=hdr+'<div style="font-size:.8em;color:var(--fg-muted)">None running. Started sessions run on the runner (independent of this browser) and appear here.</div>'; liveIds=''; return; }
          var ids=runs.map(function(r){ return r.id; }).join(',');
          if(ids!==liveIds){
            ensureLiveCss();
            box.innerHTML=hdr
              + '<div style="font-size:.78em;color:var(--fg-muted);margin:0 0 6px">Reconnect (Open) or end any session from here &mdash; on any device.</div>'
              + runs.map(liveRow).join('');
            liveIds=ids;
            var ends=box.querySelectorAll('.dbg-live-end');
            for(var i=0;i<ends.length;i++){ ends[i].addEventListener('click', function(){ endRun(this.getAttribute('data-run')); }); }
          }
          runs.forEach(fillLiveRow);
        }).catch(function(){});
    }
    // Quickly surface a just-dispatched run in the list (before the 5s poll settles).
    function kickLive(){ refreshLiveSessions(); setTimeout(refreshLiveSessions,1500); setTimeout(refreshLiveSessions,4000); setTimeout(refreshLiveSessions,8000); }
    function modal(){ return $('cidash-debug-modal'); }
    function cidashDebugClose(){ clearLiveTimer(); var m=modal(); if(m) m.style.display='none'; document.body.style.overflow=''; }
    window.cidashDebugClose = cidashDebugClose;
    function debugStatus(html, kind){
      var s=$('dbg-status'); if(!s) return;
      var col = kind==='ok' ? '#3fb950' : (kind==='err' ? '#f85149' : (kind==='warn' ? '#d29922' : 'var(--fg-muted)'));
      s.style.color=col; s.innerHTML=html||'';
    }
    // ── Config form + existing-sessions list ───────────────────────────
    function renderStep1(){
      var revs=HIST.slice(0,200).map(function(r){ return '<option value="'+esc(r.sha)+'">'+esc(r.short||r.sha.slice(0,7))+' &mdash; '+esc((r.msg||'').slice(0,60))+'</option>'; }).join('');
      var body=''
        + '<p style="margin:0 0 10px;color:var(--fg-muted);font-size:.9em">Start an interactive LabVIEW desktop on a worker container for a specific revision, then remote in. The project opens automatically and an on-screen menu lets you run CI operations (Mass Compile, Builds, VIDiff&hellip;) and watch them execute live.</p>'
        + '<div style="margin:0 0 12px;padding:8px 10px;border:1px solid var(--border);border-left:3px solid #d29922;border-radius:6px;background:rgba(210,153,34,.08);font-size:.85em;color:var(--fg)">Each debug session occupies one of this project\'s Actions runners for its whole lifetime and will block other CI jobs if no runner slots are free. End sessions as soon as you are done.</div>'
        + '<div style="margin:0 0 12px"><label style="font-weight:600;font-size:.85em" for="dbg-rev">Revision</label><br>'
        +   '<select id="dbg-rev" style="width:100%;max-width:100%;padding:6px;border:1px solid var(--border);border-radius:6px;background:var(--bg);color:var(--fg)">'+revs+'</select></div>'
        + '<div style="margin:0 0 14px"><label style="font-weight:600;font-size:.85em" for="dbg-min">Auto-end after</label><br>'
        +   '<select id="dbg-min" style="padding:6px;border:1px solid var(--border);border-radius:6px;background:var(--bg);color:var(--fg)">'
        +     '<option value="30">30 minutes</option><option value="45" selected>45 minutes</option><option value="60">60 minutes</option><option value="90">90 minutes</option><option value="120">120 minutes</option></select></div>'
        + (getTok()?'':'<div style="margin:0 0 12px"><label style="font-weight:600;font-size:.85em" for="dbg-tok">Dispatch token</label><br><input id="dbg-tok" type="password" placeholder="fine-grained PAT with Actions: write" style="width:100%;padding:6px;border:1px solid var(--border);border-radius:6px;background:var(--bg);color:var(--fg)"><div style="font-size:.8em;color:var(--fg-muted);margin-top:3px"><a href="'+tokenSetupUrl()+'" target="_blank" rel="noopener">Create one &#8599;</a></div></div>')
        + '<div style="display:flex;gap:8px;align-items:center;margin-top:6px;flex-wrap:wrap"><button id="dbg-start" title="Boot a LabVIEW debug desktop on this revision; pick operations from the on-screen menu once inside" style="background:#1f6feb;border:1px solid #1f6feb;color:#fff;padding:7px 16px;border-radius:6px;cursor:pointer;font-size:.9em">Start debug session</button><span id="dbg-status" style="font-size:.85em;color:var(--fg-muted)"></span></div>'
        + '<div id="dbg-live" style="margin:16px 0 0;border-top:1px solid var(--border);padding-top:12px;max-height:280px;overflow-y:auto"></div>';
      $('cidash-debug-body').innerHTML=body;
      try{ if(typeof window.lvciRevPicker==='function') window.lvciRevPicker($('dbg-rev')); }catch(e){}
      $('dbg-start').addEventListener('click', function(){ startSession(); });
      refreshLiveSessions();
      clearLiveTimer(); liveTimer=setInterval(refreshLiveSessions, 5000);
    }
    // Dispatch a debug session: boot a LabVIEW desktop on the chosen revision
    // with the project open; operations are chosen from the on-screen menu.
    function startSession(){
      var t=$('dbg-tok'); if(t && t.value.trim()){ setTok(t.value.trim()); }
      if(!getTok()){ debugStatus('Enter a token with <strong>Actions: write</strong> to start.', 'err'); return; }
      var sha=$('dbg-rev') ? $('dbg-rev').value : '';
      if(!sha){ debugStatus('Pick a revision.', 'err'); return; }
      var mins=$('dbg-min') ? $('dbg-min').value : '45';
      var startBtn=$('dbg-start');
      if(startBtn){ startBtn.disabled=true; startBtn.textContent='Starting...'; }
      debugStatus('Dispatching the debug session...');
      function reset(){ if(startBtn){ startBtn.disabled=false; startBtn.textContent='Start debug session'; } }
      var inputs={ commit_sha:sha, platform:'linux', actions:'', minutes:String(mins), open_source:'true' };
      fetch('https://api.github.com/repos/'+REPO+'/actions/workflows/'+encodeURIComponent(WF)+'/dispatches', {
        method:'POST', headers:Object.assign({'Content-Type':'application/json'}, ghHeaders()),
        body:JSON.stringify({ ref:BRANCH, inputs:inputs })
      }).then(function(r){
        reset();
        if(r.status===204){ debugStatus('Session dispatched &mdash; it will appear below as it boots.', 'ok'); kickLive(); return; }
        if(r.status===401){ try{ localStorage.removeItem(TOK_KEY); }catch(e){} debugStatus('Token rejected (401). Paste a valid token above.', 'err'); renderStep1(); return; }
        if(r.status===403){ debugStatus('Dispatch forbidden (403). The token needs <strong>Actions: write</strong> for <code>'+esc(REPO)+'</code>.', 'err'); return; }
        if(r.status===404){ debugStatus('Not found (404). <code>'+esc(WF)+'</code> is not installed, or the token cannot see this repo.', 'err'); return; }
        debugStatus('Dispatch failed (HTTP '+r.status+').', 'err');
      }).catch(function(e){ reset(); debugStatus('Network error: '+esc(String(e&&e.message||e)), 'err'); });
    }
    // ── Open ───────────────────────────────────────────────────────────────
    function debugOpen(){
      var m=modal(); if(!m) return;
      m.style.display='block'; document.body.style.overflow='hidden';
      renderStep1();
    }
    window.lvciDebugRun = debugOpen;
    document.addEventListener('keydown', function(e){ if(e.key==='Escape'){ var m=modal(); if(m && m.style.display==='block') cidashDebugClose(); } });
    // Auto-open when another page routed here for Debug Run (?lvci-debug=1); strip
    // the param so a manual reload doesn't reopen it.
    function lvciAutoDebug(){
      try{
        var p=new URLSearchParams(location.search||'');
        if(p.get('lvci-debug')!=='1') return;
        p.delete('lvci-debug');
        try{ var qs=p.toString(); history.replaceState(null,'',location.pathname+(qs?('?'+qs):'')+location.hash); }catch(e){}
        debugOpen();
      }catch(e){}
    }
    if(document.readyState==='loading'){ document.addEventListener('DOMContentLoaded', lvciAutoDebug); } else { lvciAutoDebug(); }
  })();
  </scr""" + """ipt>""").replace('__RUN_TARGETS__', run_targets_json).replace('__HIST__', hist_json).replace('__REPO__', repo).replace('__BRANCH__', get_default_branch())

# ── "Run CI for your whole history" card (fresh installs only) ───────────────
# A brand-new dashboard has no results, so every project cell shows a one-click
# run glyph. Rather than make the user click each one, this card offers to queue
# them ALL at once, oldest revision first. It is emitted ONLY when the page is
# output-free (no terminal results, nothing running) yet has revisions to run, so
# it never appears once CI output exists; the client controller (in run_dialog)
# also lets the user dismiss it and self-hides it once anything has been queued.
# No literal braces in the markup, so this stays a plain string (the page template
# below is an f-string; this is spliced in as {backfill_card}).
backfill_card = ''
if not any_output['on'] and not running_flag['on'] and run_count['n'] > 0:
    # NOTE: deliberately no per-repo "runs N at a time" claim here. The
    # config's maxParallel is not enforced for these workflow_dispatch
    # backfills (each is an independent run with its own per-revision
    # concurrency group) — pacing comes from GitHub's account-wide runner
    # concurrency, which queues the surplus in creation order.
    bf_conc_html = (
        '<span class="lvci-bf-note">'
        'Queued runs wait for a free runner and start automatically as earlier '
        'ones finish &mdash; nothing is lost &mdash; so it&rsquo;s safe to queue '
        'the whole history at once.'
        '</span>'
        '<details class="lvci-bf-info">'
        '<summary>&#9432; How concurrency works</summary>'
        '<div class="lvci-bf-infobody">'
        'GitHub caps how many CI jobs run at the same time <b>per account</b> &mdash; shared '
        'across every repository you own (for example 20 jobs on Free, 40 on Pro), not per '
        'repo &mdash; and queues the rest in the order they were created. A history backfill '
        'is paced by that account-wide limit; routine pushes and tooling updates draw from '
        'the same pool. If a queued run ever ends without reporting a result (out of Actions '
        'minutes, a billing block, or cancelled while waiting), its cell shows a red '
        '<b>Failed</b> badge linking to the dead run instead of quietly resetting.'
        '</div></details>'
    )
    backfill_card = (
        '<div id="lvci-backfill" class="lvci-backfill" role="region" aria-label="Run CI for existing history" style="display:none">'
        '<div class="lvci-bf-main">'
        '<div class="lvci-bf-icon" aria-hidden="true">&#9889;</div>'
        '<div class="lvci-bf-text">'
        '<strong>Populate the dashboard with your history</strong>'
        '<span>This dashboard has no results yet. Populate it with CI for all <b id="lvci-bf-count"></b> revisions &mdash; processed <b>oldest&nbsp;&rarr;&nbsp;newest</b> so snapshots and diffs build on one another with no duplicated work. The button opens a <a href="#" id="lvci-bf-custom" style="color:var(--link)">chooser</a> where you pick what to run &mdash; nothing starts until you confirm.</span>'
        + bf_conc_html +
        '</div>'
        '<div class="lvci-bf-actions">'
        '<button type="button" id="lvci-bf-run" class="cidash-btn cidash-go">&#9654; Populate history&hellip;</button>'
        '<button type="button" id="lvci-bf-dismiss" class="cidash-btn cidash-ghost">Dismiss</button>'
        '</div></div>'
        '<div id="lvci-bf-tok" class="lvci-bf-tok" style="display:none">'
        'Paste a token with <strong>Actions: Read and write</strong> on this repository &mdash; the same token you set the dashboard up with works here too, or <a id="lvci-bf-tok-link" target="_blank" rel="noopener" style="color:var(--link)">create one &#8599;</a>.'
        '<div style="display:flex;gap:8px;flex-wrap:wrap;margin-top:7px">'
        '<input id="lvci-bf-tok-input" type="password" autocomplete="off" placeholder="github_pat_&hellip; or ghp_&hellip;" style="flex:1 1 240px;min-width:180px;padding:7px 10px;background:var(--bg);color:var(--fg);border:1px solid var(--border);border-radius:6px;font-family:ui-monospace,Menlo,monospace;font-size:.8em">'
        '<button type="button" id="lvci-bf-tok-save" class="cidash-btn cidash-go">Save &amp; run</button>'
        '</div></div>'
        '<div id="lvci-bf-status" class="lvci-bf-status" role="status" aria-live="polite"></div>'
        '</div>'
    )

html = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <!-- Auto-refresh is driven by a guarded JS timer (lvciAutoRefresh, below) instead
       of an HTML meta refresh: a meta refresh can't be cancelled once parsed, so it
       would reload the page mid-install and destroy an open dialog (the "dialog
       vanished" bug). The JS timer reloads on the same cadence but waits while a
       dialog is open. -->
  <title>CI Dashboard — {repo_name}</title>
  <style>
    :root{{
      --bg:#0d1117;--surface:#161b22;--border:#30363d;
      --fg:#e6edf3;--fg-muted:#8b949e;--row-border:#21262d;
      --hover:#1c2128;--link:#58a6ff;
    }}
    @media(prefers-color-scheme:light){{
      :root{{
        --bg:#ffffff;--surface:#f6f8fa;--border:#d0d7de;
        --fg:#1f2328;--fg-muted:#57606a;--row-border:#eaeef2;
        --hover:#f3f4f6;--link:#0969da;
      }}
    }}
    *{{box-sizing:border-box}}
    body{{margin:0;padding:0;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;background:var(--bg);color:var(--fg)}}
    .lvci-main{{padding:20px}}
    .lvci-tablewrap{{width:100%;overflow-x:auto;-webkit-overflow-scrolling:touch}}
    h1{{font-size:1.4em;margin:0 0 4px}}
    .sub{{color:var(--fg-muted);font-size:.85em;margin-bottom:20px}}
    @media(max-width:980px){{
      .lvci-main{{padding:14px}}
      h1{{font-size:1.2em}}
      .controls{{align-items:stretch}}
      .cidash-search{{flex:1 1 260px;min-width:0}}
      .cidash-search input{{width:100%;max-width:none}}
      .cidash-segfilter{{flex:1 1 260px;min-width:0}}
      .cidash-segfilter button{{flex:1 1 auto;padding-left:8px;padding-right:8px}}
    }}
    @media(max-width:520px){{
      .lvci-main{{padding:10px}}
      .controls{{gap:8px}}
      .cidash-search,.cidash-segfilter,.cidash-colmenu,.cidash-colbtn{{width:100%}}
      .cidash-segfilter button{{font-size:.92em}}
    }}
    table{{border-collapse:collapse;width:100%;background:var(--surface);border:1px solid var(--border);border-radius:8px;overflow:hidden}}
    th{{text-align:left;padding:10px 8px;border-bottom:1px solid var(--border);color:var(--fg-muted);font-size:.8em;white-space:nowrap}}
    td{{border-bottom:1px solid var(--row-border);vertical-align:middle}}
    tr:last-child td{{border-bottom:none}}
    tr:hover{{background:var(--hover)}}
    a{{color:var(--link);text-decoration:none}}a:hover{{text-decoration:underline}}
    .nav{{margin-bottom:16px;font-size:.9em}}
    .nav a{{margin-right:16px;color:var(--link)}}
    .controls{{margin:0 0 12px;display:flex;align-items:center;gap:14px;flex-wrap:wrap;color:var(--fg-muted);font-size:.85em}}
    .controls input{{margin:0;accent-color:var(--link)}}
    /* When the shared header is present, the controls live in its sticky context
       bar (moved there on lvci:ready), so drop the standalone margin + fill the bar. */
    .lvci-ctxbar .controls{{margin:0;flex:1 1 auto}}
    .cidash-check{{display:inline-flex;align-items:center;gap:6px}}
    /* Search box + status filter (compose with the CI-only toggle below). */
    .cidash-search{{display:inline-flex;align-items:center;gap:7px;background:var(--surface);border:1px solid var(--border);border-radius:6px;padding:5px 10px}}
    .cidash-search svg{{width:14px;height:14px;color:var(--fg-muted);flex:0 0 auto}}
    .cidash-search input{{border:0;background:none;color:var(--fg);font:inherit;outline:none;width:210px;max-width:42vw}}
    .cidash-search input::placeholder{{color:var(--fg-muted)}}
    .cidash-segfilter{{display:inline-flex;border:1px solid var(--border);border-radius:6px;overflow:hidden}}
    .cidash-segfilter button{{background:var(--surface);border:0;border-right:1px solid var(--border);color:var(--fg-muted);padding:5px 12px;cursor:pointer;font:inherit;line-height:1.2}}
    .cidash-segfilter button:last-child{{border-right:0}}
    .cidash-segfilter button:hover{{color:var(--fg);background:var(--hover)}}
    .cidash-segfilter button.on{{background:var(--link);color:#fff}}
    /* Column-visibility menu (standard "Columns" dropdown of checkboxes). */
    .cidash-colmenu{{position:relative;display:inline-block}}
    .cidash-colbtn{{background:var(--surface);border:1px solid var(--border);color:var(--fg);padding:5px 11px;border-radius:6px;cursor:pointer;font-size:1em;line-height:1.2;display:inline-flex;align-items:center;gap:6px}}
    .cidash-colbtn:hover{{background:var(--hover)}}
    .cidash-colpanel{{position:absolute;top:calc(100% + 6px);left:0;z-index:120;min-width:200px;background:var(--surface);border:1px solid var(--border);border-radius:8px;box-shadow:0 8px 28px rgba(0,0,0,.35);padding:6px}}
    .cidash-colpanel[hidden]{{display:none}}
    .cidash-colpanel .hd{{font-size:.72em;color:var(--fg-muted);padding:4px 8px 6px;text-transform:uppercase;letter-spacing:.05em}}
    .cidash-colopt{{display:flex;align-items:center;gap:8px;padding:6px 8px;border-radius:5px;cursor:pointer;color:var(--fg);white-space:nowrap;font-size:.95em}}
    .cidash-colopt:hover{{background:var(--hover)}}
    .cidash-colopt input{{margin:0;accent-color:var(--link)}}
    .run-badge{{display:inline-flex;align-items:center;background:#1f6feb;color:#fff;padding:2px 8px;border-radius:4px;font-size:.75em;font-weight:600}}
    .run-badge a:hover{{text-decoration:underline}}
    /* Optimistic "just queued from this browser" cue: a dashed ring distinguishes
       a client-side queued badge from a server-confirmed running one. */
    .run-badge.cidash-queued{{outline:1px dashed rgba(255,255,255,.6);outline-offset:1px}}
    /* A run confirmed FAILED on GitHub: a sticky red badge (no spinner, no dashed
       "optimistic" ring) so an error shows instead of the cell silently reverting. */
    .run-badge.cidash-failed{{background:#da3633;outline:none}}
    /* The red "bang" inside a failed badge: a bold white exclamation on the red
       field, so a failed cell reads as a clickable error at a glance. */
    .run-badge.cidash-failed .cidash-bang{{font-weight:800;font-size:1.15em;line-height:1;margin-right:5px}}
    .run-spin{{width:9px;height:9px;border:2px solid rgba(255,255,255,.45);border-top-color:#fff;border-radius:50%;display:inline-block;animation:cidash-spin .7s linear infinite}}
    /* The Queued badge places the spinner directly before its label (no flex gap),
       so give the spinner a small right margin to keep the icon off the "Q". */
    .run-badge.cidash-queued .run-spin{{margin-right:5px}}
    @keyframes cidash-spin{{to{{transform:rotate(360deg)}}}}
    /* Status chips: a tinted pill + state glyph for every result cell. */
    .cidash-chip{{display:inline-flex;align-items:center;gap:5px;padding:3px 9px;border-radius:999px;font-size:.74em;font-weight:600;line-height:1.45;border:1px solid transparent;white-space:nowrap}}
    .cidash-chip svg{{width:12px;height:12px;flex:0 0 auto}}
    .cidash-chip a{{color:inherit}}
    .cidash-chip a:hover{{text-decoration:underline}}
    .cidash-chip.cc-pass{{background:rgba(46,160,67,.16);color:#3fb950;border-color:rgba(46,160,67,.35)}}
    .cidash-chip.cc-fail{{background:rgba(248,81,73,.16);color:#f85149;border-color:rgba(248,81,73,.4)}}
    .cidash-chip.cc-warn{{background:rgba(210,153,34,.16);color:#d29922;border-color:rgba(210,153,34,.35)}}
    .cidash-chip.cc-info{{background:rgba(56,139,253,.16);color:#58a6ff;border-color:rgba(56,139,253,.3)}}
    /* Solid pill: a hard failure (e.g. a Mass Compile run LabVIEW could not
       complete) fills the pill with a bright, saturated red so it stands out
       from a merely low percentage (which stays a subtly-tinted red). */
    .cidash-chip.cc-fail.cc-solid{{background:#da3633;color:#fff;border-color:#da3633}}
    .cidash-chip.cc-fail.cc-solid svg{{color:#fff}}
    /* Split pill: a neutral container whose individual values colour themselves
       (used when one cell reports two values that can differ in state, e.g. the
       Windows / Linux mass-compile percentages). Each value has an instant
       (no-delay) mouse-over naming its platform via data-tip. */
    .cidash-chip.cc-split{{background:rgba(110,118,129,.14);color:var(--fg-muted);border-color:rgba(110,118,129,.32)}}
    .cidash-chip .cidash-vseg{{position:relative;font-weight:700}}
    .cidash-chip .cidash-vseg.cc-pass{{color:#3fb950}}
    .cidash-chip .cidash-vseg.cc-fail{{color:#f85149}}
    .cidash-chip .cidash-vseg.cc-warn{{color:#d29922}}
    .cidash-chip .cidash-vseg.cc-muted{{color:#8b949e}}
    /* A hard-failed segment in a split pill is filled bright red (a small solid
       badge) so it reads as a failure, not just a low number. */
    .cidash-chip .cidash-vseg.cc-fail.cc-solid{{background:#da3633;color:#fff;padding:1px 7px;border-radius:6px}}
    .cidash-chip .cidash-vseg-sep{{opacity:.5;margin:0 4px;font-weight:400}}
    .cidash-chip .cidash-vseg[data-tip]:hover::after{{content:attr(data-tip);position:absolute;left:50%;bottom:calc(100% + 7px);transform:translateX(-50%);background:#1c2128;color:#e6edf3;border:1px solid #30363d;padding:2px 7px;border-radius:6px;font-size:.95em;font-weight:600;white-space:nowrap;pointer-events:none;z-index:30;box-shadow:0 2px 8px rgba(0,0,0,.4)}}
    .cidash-chip .cidash-vseg[data-tip]:hover::before{{content:"";position:absolute;left:50%;bottom:calc(100% + 2px);transform:translateX(-50%);border:5px solid transparent;border-top-color:#30363d;pointer-events:none;z-index:30}}
    @media(prefers-color-scheme:light){{
      .cidash-chip.cc-pass{{background:#dafbe1;color:#1a7f37;border-color:#2da44e55}}
      .cidash-chip.cc-fail{{background:#ffebe9;color:#cf222e;border-color:#cf222e44}}
      .cidash-chip.cc-warn{{background:#fff8c5;color:#9a6700;border-color:#9a670055}}
      .cidash-chip.cc-info{{background:#ddf4ff;color:#0969da;border-color:#0969da44}}
      .cidash-chip.cc-split{{background:#eaeef2;color:#57606a;border-color:#d0d7de}}
      .cidash-chip.cc-fail.cc-solid{{background:#cf222e;color:#fff;border-color:#cf222e}}
      .cidash-chip .cidash-vseg.cc-pass{{color:#1a7f37}}
      .cidash-chip .cidash-vseg.cc-fail{{color:#cf222e}}
      .cidash-chip .cidash-vseg.cc-warn{{color:#9a6700}}
      .cidash-chip .cidash-vseg.cc-muted{{color:#57606a}}
      .cidash-chip .cidash-vseg.cc-fail.cc-solid{{background:#cf222e;color:#fff}}
      .cidash-chip .cidash-vseg[data-tip]:hover::after{{background:#fff;color:#1f2328;border-color:#d0d7de;box-shadow:0 2px 8px rgba(140,149,159,.35)}}
      .cidash-chip .cidash-vseg[data-tip]:hover::before{{border-top-color:#d0d7de}}
    }}
    /* VIDiff "diff stat": different / new / deleted VI counts for a revision.
       Colour-coded modified-amber / added-green / deleted-red with grey slashes;
       zero parts dim so the eye lands on what actually changed. */
    .cidash-diffstat{{display:inline-flex;align-items:baseline;gap:3px;padding:3px 9px;border-radius:999px;font-size:.78em;font-weight:700;font-variant-numeric:tabular-nums;text-decoration:none;border:1px solid var(--border);background:var(--surface)}}
    .cidash-diffstat:hover{{border-color:var(--fg-muted)}}
    .cidash-diffstat .ds-sep{{color:var(--fg-muted);font-weight:400;opacity:.6}}
    .cidash-diffstat .ds-mod{{color:#d29922}}
    .cidash-diffstat .ds-add{{color:#3fb950}}
    .cidash-diffstat .ds-del{{color:#f85149}}
    .cidash-diffstat .ds-zero{{color:var(--fg-muted);opacity:.45;font-weight:600}}
    @media(prefers-color-scheme:light){{
      .cidash-diffstat .ds-mod{{color:#9a6700}}
      .cidash-diffstat .ds-add{{color:#1a7f37}}
      .cidash-diffstat .ds-del{{color:#cf222e}}
    }}
    /* Rich "Revision" cell: avatar + message (primary) + sha / author / date (meta). */
    .cidash-rev{{padding:8px;max-width:440px}}
    .cidash-rev-wrap{{display:flex;gap:10px;align-items:flex-start;min-width:0}}
    .cidash-avatar{{flex:0 0 auto;width:26px;height:26px;border-radius:50%;display:inline-flex;align-items:center;justify-content:center;color:#fff;font-size:.78em;font-weight:600;text-transform:uppercase;line-height:1}}
    .cidash-rev-body{{display:flex;flex-direction:column;min-width:0;gap:2px}}
    .cidash-rev-line1{{display:flex;align-items:center;gap:6px;min-width:0}}
    .cidash-rev-msg{{color:var(--fg);font-size:.9em;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;min-width:0;text-decoration:none}}
    .cidash-rev-msg:hover{{text-decoration:underline}}
    .cidash-rev-meta{{display:flex;align-items:center;gap:6px;font-size:.78em;color:var(--fg-muted);min-width:0;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}}
    .cidash-rev-sha{{font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;color:var(--link);text-decoration:none}}
    .cidash-rev-sha:hover{{text-decoration:underline}}
    .cidash-rev-dot{{color:var(--border)}}
    @media(max-width:980px){{
      .lvci-tablewrap{{overflow:visible}}
      #cidash-table{{display:block;width:100%;border:0;background:transparent}}
      #cidash-table thead{{display:none}}
      #cidash-table tbody{{display:block;width:auto}}
      /* Each card lays its capability cells out as a responsive grid of compact
         tiles (label above value) so they fill the card width instead of leaving
         a wide empty gutter between a left-aligned label and right-aligned badge. */
      #cidash-table tr{{display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:12px 14px;border:1px solid var(--border);border-radius:10px;margin:0 0 12px;padding:12px 14px;background:var(--surface)}}
      #cidash-table td{{display:block;width:auto;border:0;padding:0}}
      #cidash-table td.cidash-rev{{grid-column:1/-1;max-width:none;padding:2px 0 10px;border-bottom:1px solid var(--border);margin-bottom:0}}
      #cidash-table td:not(.cidash-rev){{display:flex!important;flex-direction:column;align-items:flex-start;gap:5px;text-align:left!important;min-width:0}}
      #cidash-table td:not(.cidash-rev)::before{{content:attr(data-label);color:var(--fg-muted);font-size:.72em;font-weight:600;text-transform:uppercase;letter-spacing:.04em;min-width:0}}
      #cidash-table td:not(.cidash-rev)>*{{max-width:100%}}
      .cidash-chip,.run-badge{{white-space:normal;text-align:left;justify-content:flex-start}}
    }}
    @media(max-width:520px){{
      .lvci-main{{padding:10px}}
      .lvci-ctxbar .controls{{gap:8px}}
      .lvci-ctxbar .cidash-search{{padding:4px 9px}}
      .lvci-ctxbar .cidash-segfilter button{{padding:6px 8px}}
      .lvci-ctxbar .cidash-colbtn{{justify-content:flex-start;padding:7px 10px}}
      #cidash-table tr{{padding:10px 12px;border-radius:8px;gap:11px 12px}}
      .cidash-rev{{padding:8px 0}}
      .cidash-rev-msg{{white-space:normal;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical}}
      .cidash-rev-meta{{flex-wrap:wrap;white-space:normal}}
    }}
    {run_dialog_css}
  </style>
</head>
<body>
  <script>window.LVCI={{context:'dashboard',repo:'{repo}',pagesUrl:'{pages_url}',isSource:{'true' if lvci_is_source else 'false'}}};</script>
  <script src="lvci-header.js" defer></script>
  <div id="lvci-modal" onclick="if(event.target===this)lvciClose()" style="display:none;position:fixed;inset:0;z-index:300;background:rgba(0,0,0,.55)">
    <div style="position:absolute;inset:24px;background:var(--bg);border:1px solid var(--border);border-radius:10px;overflow:hidden;display:flex;flex-direction:column;box-shadow:0 10px 48px rgba(0,0,0,.5)">
      <div style="display:flex;align-items:center;justify-content:space-between;padding:10px 16px;border-bottom:1px solid var(--border);background:var(--surface)">
        <strong id="lvci-modal-title" style="font-size:.95em">Configure Pipeline</strong>
        <button onclick="lvciClose()" style="background:transparent;border:1px solid var(--border);color:var(--fg);padding:5px 12px;border-radius:6px;cursor:pointer;font-size:.82em">✕ Close</button>
      </div>
      <iframe id="lvci-frame" title="LabVIEW CI dialog" src="about:blank" style="border:0;width:100%;flex:1;min-height:0"></iframe>
    </div>
  </div>
  <script>
    function lvciOpen(src, title) {{
      document.getElementById('lvci-frame').src = src;
      document.getElementById('lvci-modal-title').textContent = title;
      document.getElementById('lvci-modal').style.display = 'block';
      document.body.style.overflow = 'hidden';
    }}
    function lvciClose() {{
      document.getElementById('lvci-modal').style.display = 'none';
      document.getElementById('lvci-frame').src = 'about:blank';
      document.body.style.overflow = '';
    }}
    document.addEventListener('keydown', function (e) {{ if (e.key === 'Escape') lvciClose(); }});
    // Is the Apply / Configure / VI Analyzer / Unit-tests / What's-New iframe dialog
    // open? The auto-refresh timer and the queued-run fast reload both check this so a
    // page reload never blows away an in-progress install or its iframe (the
    // "dialog disappeared on its own" bug).
    function lvciModalOpen() {{
      var m = document.getElementById('lvci-modal');
      if (m && m.style.display === 'block') return true;
      // The Populate-history and Debug Run dialogs are their own modals; reloading
      // while either is open destroys its in-flight work too (the Debug Run
      // "dialog vanished while the container booted" bug).
      var h = document.getElementById('cidash-hist-modal');
      if (h && h.style.display === 'block') return true;
      var d = document.getElementById('cidash-debug-modal');
      if (d && d.style.display === 'block') return true;
      return false;
    }}
    // Auto-refresh on the cadence the server picked (60 s while CI is live, else
    // 15 min) -- but NEVER while a dialog is open, because reloading the page destroys
    // the install/Configure iframe and its in-flight work. When a refresh comes due
    // with a dialog open, keep re-checking and reload only once it is closed.
    (function () {{
      var REFRESH_MS = {refresh_secs} * 1000;
      if (!(REFRESH_MS > 0)) return;
      function tick() {{
        if (lvciModalOpen()) {{ setTimeout(tick, 5000); return; }}
        location.reload();
      }}
      setTimeout(tick, REFRESH_MS);
    }})();
  </script>
  {run_dialog}
  {debug_dialog}
  <main class="lvci-main">
  <h1>CI Dashboard — {repo_name}</h1>
  <div class="sub">Last updated: {now} &nbsp;|&nbsp; {refresh_note}</div>
  <div class="nav">
    <a href="https://github.com/{repo}">GitHub</a>
    <a href="https://github.com/{repo}/actions">Actions</a>
  </div>
  {backfill_card}
  <div class="controls">
    <div class="cidash-search">
      <svg viewBox="0 0 16 16" fill="currentColor" aria-hidden="true"><path d="M10.68 11.74a6 6 0 1 1 1.06-1.06l3.04 3.04a.75.75 0 1 1-1.06 1.06ZM12 7a5 5 0 1 0-10 0 5 5 0 0 0 10 0Z"/></svg>
      <input id="cidash-search" type="search" autocomplete="off" placeholder="Filter revisions, authors, SHAs...">
    </div>
    <div class="cidash-segfilter" id="cidash-statusfilter" role="group" aria-label="Filter by status">
      <button type="button" class="on" data-status="all">All</button>
      <button type="button" data-status="fail">Failed</button>
      <button type="button" data-status="running">Running</button>
      <button type="button" data-status="pass">Passed</button>
    </div>
    <label class="cidash-check" for="show-nonproject">
      <input type="checkbox" id="show-nonproject">
      Include CI-only revisions
    </label>
    <label class="cidash-check" for="show-deponly" title="Revisions whose only change is an external dependency (.vipc/.vip). They get CI results only when dependency-change CI is enabled.">
      <input type="checkbox" id="show-deponly"{' checked' if lvci_dep_ci_on else ''}>
      Include dependency-only revisions
    </label>
    <div class="cidash-colmenu">
      <button type="button" id="cidash-colbtn" class="cidash-colbtn" aria-haspopup="true" aria-expanded="false">&#9783; Columns &#9662;</button>
      <div id="cidash-colpanel" class="cidash-colpanel" role="menu" hidden></div>
    </div>
  </div>
  <div class="lvci-tablewrap">
  <table id="cidash-table">
    <thead>
      <tr>
        <th>Revision</th>
        <th style="text-align:center">Mass Compile</th>
        <th style="text-align:center">VI Analyzer</th>
        <th style="text-align:center">VIDiff</th>
        <th style="text-align:center">Snapshots</th>
        <th style="text-align:center">Unit Tests</th>
        <th style="text-align:center">Antidoc</th>
        <th style="text-align:center">Builds</th>
        <th style="text-align:center">SBOM</th>
      </tr>
    </thead>
    <tbody>{rows}</tbody>
  </table>
  </div>
  <div id="empty-state" style="display:none;padding:18px;text-align:center;color:var(--fg-muted);font-size:.9em">
    No project revisions in the recent window. <a href="#" onclick="document.getElementById('show-nonproject').click();return false" style="color:var(--link)">Include CI-only revisions</a> to see CI&nbsp;/&nbsp;tooling commits.
  </div>
  <div id="cidash-noresults" style="display:none;padding:18px;text-align:center;color:var(--fg-muted);font-size:.9em">
    No revisions match the current filters. <a href="#" id="cidash-clearf" style="color:var(--link)">Clear filters</a>.
  </div>
  </main>
  <script>
    // Relocate the controls (search / status filter / Include CI-only / Columns)
    // into the shared header's sticky context bar, so they sit in the SAME place
    // as the revision bar on the report / VI Browser pages and stay visible while
    // you scroll the table. A DOM move keeps every control's existing listeners;
    // if the header isn't present the controls simply stay inline (graceful).
    (function () {{
      function placeControls() {{
        var bar = document.getElementById('lvci-ctxbar');
        var controls = document.querySelector('.controls');
        if (bar && controls && controls.parentNode !== bar) {{
          bar.appendChild(controls);
          controls.classList.add('cidash-in-ctxbar');
        }}
      }}
      if (window.lvciHeaderReady) placeControls();
      else document.addEventListener('lvci:ready', placeControls, {{ once: true }});
    }})();
  </script>
  <script>
    (() => {{
      const checkbox = document.getElementById('show-nonproject');
      const depCheckbox = document.getElementById('show-deponly');
      const rows = document.querySelectorAll('tbody tr[data-project]');
      const emptyState = document.getElementById('empty-state');
      const noresults = document.getElementById('cidash-noresults');
      const search = document.getElementById('cidash-search');
      const segBtns = Array.from(document.querySelectorAll('#cidash-statusfilter button'));
      let statusFilter = 'all', term = '';
      // Roll a row up to one status from its rendered cells: a failed chip or a
      // confirmed-failed run badge -> fail; any live run/queued spinner badge ->
      // running; any result chip -> pass; otherwise no results yet.
      const rowStatus = (row) => {{
        if (row.querySelector('.cc-fail, .run-badge.cidash-failed')) return 'fail';
        if (row.querySelector('.run-badge')) return 'running';
        if (row.querySelector('.cidash-chip')) return 'pass';
        return 'none';
      }};
      // A row shows when it passes the CI-only toggle AND the dependency-only
      // toggle AND the search text AND the status filter (composed, so all the
      // controls stack). Dependency-only revisions (only .vipc/.vip changed) are
      // hidden unless their toggle is on — its default follows whether
      // dependency-change CI is enabled.
      const applyFilter = () => {{
        let visible = 0;
        rows.forEach((row) => {{
          const isProject = row.getAttribute('data-project') === 'true';
          const isDepOnly = row.getAttribute('data-deponly') === 'true';
          let show = isProject || checkbox.checked;
          if (show && isDepOnly && depCheckbox && !depCheckbox.checked) show = false;
          if (show && term) show = row.textContent.toLowerCase().includes(term);
          if (show && statusFilter !== 'all') show = rowStatus(row) === statusFilter;
          row.style.display = show ? '' : 'none';
          if (show) visible++;
        }});
        const filtering = !!term || statusFilter !== 'all';
        if (emptyState) emptyState.style.display = (!visible && !filtering) ? '' : 'none';
        if (noresults)  noresults.style.display  = (!visible &&  filtering) ? '' : 'none';
      }};
      checkbox.addEventListener('change', applyFilter);
      if (depCheckbox) depCheckbox.addEventListener('change', applyFilter);
      if (search) search.addEventListener('input', () => {{ term = search.value.trim().toLowerCase(); applyFilter(); }});
      segBtns.forEach((b) => b.addEventListener('click', () => {{
        segBtns.forEach((x) => x.classList.remove('on'));
        b.classList.add('on');
        statusFilter = b.getAttribute('data-status');
        applyFilter();
      }}));
      const clr = document.getElementById('cidash-clearf');
      if (clr) clr.addEventListener('click', (e) => {{
        e.preventDefault();
        if (search) search.value = '';
        term = ''; statusFilter = 'all';
        segBtns.forEach((x) => x.classList.toggle('on', x.getAttribute('data-status') === 'all'));
        applyFilter();
      }});
      applyFilter();
    }})();

    // Mobile cards: stamp each body cell with its column name so the responsive
    // <=820px layout (table collapses to cards) can show the column label beside
    // each value via CSS content:attr(data-label).
    (() => {{
      const ths = Array.from(document.querySelectorAll('#cidash-table > thead th')).map((t) => t.textContent.trim());
      document.querySelectorAll('#cidash-table > tbody > tr').forEach((tr) => {{
        Array.from(tr.children).forEach((td, i) => {{ if (ths[i]) td.setAttribute('data-label', ths[i]); }});
      }});
    }})();

    // Column-visibility menu: a standard "Columns" dropdown of checkboxes that
    // toggles each activity column on/off, persisted per-repo in localStorage so
    // the choice survives reloads. The Revision cell (column 0) is the row
    // identifier and is always shown. Persistence is keyed by column KEY, so the
    // idx values can change without invalidating a saved preference.
    (() => {{
      const STORE = 'lvci_dash_cols_{repo}';
      const COLS = [
        {{key:'masscompile', label:'Mass Compile', idx:1}},
        {{key:'vi-analyzer', label:'VI Analyzer',  idx:2}},
        {{key:'vidiff',      label:'VIDiff',       idx:3}},
        {{key:'snapshots',   label:'Snapshots',    idx:4}},
        {{key:'unit-tests',  label:'Unit Tests',   idx:5}},
        {{key:'antidoc',     label:'Antidoc',      idx:6}}
      ];
      const btn = document.getElementById('cidash-colbtn');
      const panel = document.getElementById('cidash-colpanel');
      if (!btn || !panel) return;
      const hidden = {{}};
      try {{ (JSON.parse(localStorage.getItem(STORE)) || []).forEach((k) => {{ hidden[k] = true; }}); }} catch (e) {{}}
      const persist = () => {{
        try {{ localStorage.setItem(STORE, JSON.stringify(COLS.map((c) => c.key).filter((k) => hidden[k]))); }} catch (e) {{}}
      }};
      const applyCol = (col) => {{
        const vis = !hidden[col.key];
        document.querySelectorAll('#cidash-table > thead > tr, #cidash-table > tbody > tr').forEach((tr) => {{
          const cell = tr.children[col.idx];
          if (cell) cell.style.display = vis ? '' : 'none';
        }});
      }};
      const head = document.createElement('div');
      head.className = 'hd';
      head.textContent = 'Show columns';
      panel.appendChild(head);
      COLS.forEach((col) => {{
        const lab = document.createElement('label');
        lab.className = 'cidash-colopt';
        const cb = document.createElement('input');
        cb.type = 'checkbox';
        cb.checked = !hidden[col.key];
        cb.addEventListener('change', () => {{ hidden[col.key] = !cb.checked; applyCol(col); persist(); }});
        lab.appendChild(cb);
        lab.appendChild(document.createTextNode(' ' + col.label));
        panel.appendChild(lab);
        applyCol(col);
      }});
      const setOpen = (open) => {{ panel.hidden = !open; btn.setAttribute('aria-expanded', open ? 'true' : 'false'); }};
      btn.addEventListener('click', (e) => {{ e.stopPropagation(); setOpen(panel.hidden); }});
      document.addEventListener('click', (e) => {{ if (!panel.hidden && !panel.contains(e.target) && e.target !== btn) setOpen(false); }});
      document.addEventListener('keydown', (e) => {{ if (e.key === 'Escape') setOpen(false); }});
    }})();
  </script>
</body>
</html>"""

os.makedirs('ci-out/dashboard', exist_ok=True)
with open('ci-out/dashboard/index.html', 'w', encoding='utf-8') as f:
    # Dedent the heredoc indentation
    import textwrap
    f.write(textwrap.dedent(html))

# Ensure VI Browser route exists so dashboard commit links do not 404.
os.makedirs('ci-out/dashboard/vi-snapshots', exist_ok=True)
# Framed report viewer (chrome + back-nav) that the Mass Compile badge links to.
os.makedirs('ci-out/dashboard/report', exist_ok=True)
os.makedirs('ci-out/dashboard/dependencies', exist_ok=True)
# Tooling pages come from PAGES_SRC (a composite action passes its own bundled
# dir so thin consumers need no copy); default to the in-repo location.
_pages_src = os.environ.get('PAGES_SRC', '.github/pages')
def _stage(src, dst):
    try:
        with open(src, 'r', encoding='utf-8') as sf, open(dst, 'w', encoding='utf-8') as df:
            df.write(sf.read())
    except FileNotFoundError:
        pass
for _name, _dst in [
    ('lvci-header.js', 'ci-out/dashboard/lvci-header.js'),
    ('vi-browser.html', 'ci-out/dashboard/vi-snapshots/index.html'),
    ('vi-interactive.html', 'ci-out/dashboard/vi-snapshots/vi-interactive.html'),
    # vi-render.js powers the in-place block-diagram renderer that vi-browser.html
    # and vi-interactive.html load; stage it beside them so the in-place view works.
    ('vi-render.js', 'ci-out/dashboard/vi-snapshots/vi-render.js'),
    ('report-viewer.html', 'ci-out/dashboard/report/index.html'),
    ('dependencies.html', 'ci-out/dashboard/dependencies.html'),
    ('whats-new.html', 'ci-out/dashboard/whats-new.html'),
    ('configure.html', 'ci-out/dashboard/configure.html'),
    ('vi-analyzer.html', 'ci-out/dashboard/vi-analyzer.html'),
    ('integrate.html', 'ci-out/dashboard/integrate.html'),
    ('unit-tests.html', 'ci-out/dashboard/unit-tests.html'),
    ('builds.html', 'ci-out/dashboard/builds.html'),
    # Implementation-level "How LabVIEW CI works" reference (linked from the FAQ
    # and the site header); staged so documentation edits actually deploy.
    ('documentation.html', 'ci-out/dashboard/documentation.html'),
    # Clients registry page (the header only surfaces it on the root repo, where
    # the discovery workflow publishes clients.json beside it).
    ('clients.html', 'ci-out/dashboard/clients.html'),
]:
    _stage(os.path.join(_pages_src, _name), _dst)

def _parse_vipc_packages(vipc_path):
  names = []
  try:
    with zipfile.ZipFile(vipc_path) as zf:
      with zf.open('config.xml') as cfg_xml:
        root = ET.parse(cfg_xml).getroot()
    for pkg in root.iter('Package'):
      name_el = pkg.find('Name')
      if name_el is not None and (name_el.text or '').strip():
        names.append(name_el.text.strip())
  except Exception as exc:
    return [], str(exc)
  return sorted(set(names)), ''

def _parse_container_config(path='.github/labview-ci.yml'):
  cfg = {'use': '', 'actions': {}, 'vipc': [], 'dragon': [], 'hasVipcList': False, 'hasDragonList': False}
  if not os.path.isfile(path):
    return cfg
  section = ''
  current = None
  try:
    with open(path, encoding='utf-8') as fh:
      for raw in fh:
        line = raw.replace('\t', '    ').rstrip('\n')
        if re.match(r'^\s*container:\s*$', line):
          section = 'container'; current = None; continue
        if re.match(r'^\S', line):
          section = ''; current = None
        if section == 'container':
          m = re.match(r'^\s{4}use:\s*"?([^"#]+)"?', line)
          if m:
            cfg['use'] = m.group(1).strip()
          if re.match(r'^\s{4}vipc:\s*$', line):
            section = 'vipc'; current = None; cfg['hasVipcList'] = True; continue
          if re.match(r'^\s{4}dragon:\s*$', line):
            section = 'dragon'; current = None; cfg['hasDragonList'] = True; continue
          if re.match(r'^\s{4}actions:\s*$', line):
            section = 'actions'; current = None; continue
        elif section in ('vipc', 'dragon'):
          if re.match(r'^\s{4}vipc:\s*$', line):
            section = 'vipc'; current = None; cfg['hasVipcList'] = True; continue
          if re.match(r'^\s{4}dragon:\s*$', line):
            section = 'dragon'; current = None; cfg['hasDragonList'] = True; continue
          if re.match(r'^\s{4}actions:\s*$', line):
            section = 'actions'; current = None; continue
          m = re.match(r'^\s{6}-\s*path:\s*"?([^"]+?)"?\s*$', line)
          if m:
            current = {'path': m.group(1).strip(), 'monitor': True, 'monitorOn': None}
            cfg[section].append(current)
            continue
          m = re.match(r'^\s{8}monitorOn:\s*(\S+)', line)
          if m and current is not None:
            current['monitorOn'] = [s.strip().lower() for s in m.group(1).split('+') if s.strip()]
            continue
          m = re.match(r'^\s{8}monitor:\s*(\S+)', line)
          if m and current is not None:
            current['monitor'] = (m.group(1).strip().lower() == 'true')
            continue
          if re.match(r'^\s{0,4}\S', line):
            section = 'container'; current = None
        elif section == 'actions':
          m = re.match(r'^\s{6}([A-Za-z0-9_.-]+):\s*"?([^"#]+)"?', line)
          if m:
            cfg['actions'][m.group(1)] = m.group(2).strip()
          if re.match(r'^\s{0,4}\S', line):
            section = 'container'
  except Exception:
    pass
  return cfg

def _repo_vipcs():
  skip = {'.git', 'ci-out', 'build', '_lvci', '__pycache__'}
  out = []
  for root, dirs, files in os.walk('.'):
    dirs[:] = [d for d in dirs if d not in skip]
    for name in files:
      if name.lower().endswith('.vipc'):
        path = os.path.join(root, name).replace('\\', '/')
        if path.startswith('./'):
          path = path[2:]
        out.append(path)
  return sorted(out, key=str.lower)

def _repo_dragons():
  skip = {'.git', 'ci-out', 'build', '_lvci', '__pycache__'}
  out = []
  for root, dirs, files in os.walk('.'):
    dirs[:] = [d for d in dirs if d not in skip]
    for name in files:
      if name.lower().endswith('.dragon'):
        path = os.path.join(root, name).replace('\\', '/')
        if path.startswith('./'):
          path = path[2:]
        out.append(path)
  return sorted(out, key=str.lower)

def _worker_manifest(platform, tag):
  if not tag or tag in ('base', 'none'):
    return None
  resolved = tag
  if tag == 'latest':
    latest = http_json(f"{pages_url}/workers/{platform}/latest.json")
    resolved = latest.get('version') if isinstance(latest, dict) else ''
  if not resolved:
    return None
  return http_json(f"{pages_url}/workers/{platform}/{resolved}/manifest.json")

def _manifest_packages(man):
  packages = set()
  if isinstance(man, dict):
    for pkg in man.get('vipm_packages') or []:
      if isinstance(pkg, dict):
        name = str(pkg.get('name') or '').strip()
        version = str(pkg.get('version') or '').strip()
        label = str(pkg.get('label') or '').strip()
        # VIPM records versions with a leading "v" (e.g. "v0.5.0.1") and
        # labels like "name-v0.5.0.1", but VIPC-declared identifiers carry no
        # "v" (e.g. "name-0.5.0.1"). Emit BOTH forms so the pending
        # comparison (an exact match against VIPC package ids) matches either.
        bare = version[1:] if version[:1] in ('v', 'V') else version
        for value in (name, label,
                      f'{name}-{version}' if name and version else '',
                      f'{name}-{bare}' if name and bare else ''):
          if value:
            packages.add(value)
      elif isinstance(pkg, str) and pkg.strip():
        packages.add(pkg.strip())
    # Always union the manifest's own vipc[].packages, which are stored in the
    # SAME identifier form the VIPC files declare (no "v" prefix) and include
    # every installed version of a package. Never return early on vipm_packages
    # alone -- that list can differ in version format and omit second versions.
    for vipc in man.get('vipc') or []:
      for pkg in vipc.get('packages') or []:
        if pkg:
          packages.add(pkg)
    if not packages and man.get('platform') == 'windows' and man.get('copied_from_base'):
      packages.update(_core_tooling_packages())
  return sorted(packages, key=str.lower)

def _core_tooling_packages():
  if not os.path.isfile(TOOLING_CORE_VIPC):
    return []
  packages, _ = _parse_vipc_packages(TOOLING_CORE_VIPC)
  return packages

# The core tooling VIPC is always baked into every worker; capability VIPCs live
# under .github/labview/<cap>/<cap>.vipc and are baked only when that capability's
# action workflow is installed (see build-labview-image.yml "Stage repo VIPC files").
TOOLING_CORE_VIPC = '.github/labview/vipm/ci-tooling.vipc'
_CAPABILITY_WORKFLOW = {
  'antidoc': '.github/workflows/run-antidoc-windows-container.yml',
}
# Platforms each capability's worker actually bakes/runs on. A capability VIPC is
# only compared against (and only nags to rebuild) these platforms, so e.g. the
# Windows-only Antidoc worker never raises a spurious "Linux is missing" prompt.
_CAPABILITY_PLATFORMS = {
  'antidoc': ['windows'],
}

def _capability_for_vipc(path):
  m = re.match(r'^\.github/labview/([^/]+)/(?:[^/]+)\.vipc$', path or '')
  if not m:
    return ''
  cap = m.group(1)
  return cap if cap in _CAPABILITY_WORKFLOW else ''

def _capability_enabled(cap):
  wf = _CAPABILITY_WORKFLOW.get(cap)
  return bool(wf and os.path.isfile(wf))

def _capability_platforms(cap):
  """Worker platforms a capability's VIPC is baked into (defaults to Windows)."""
  return list(_CAPABILITY_PLATFORMS.get(cap) or ['windows'])

def _vipc_role(path, config_vipc, has_config_list):
  """Classify a discovered VIPC and its monitoring state.

  - core:       ci-tooling.vipc, always monitored and locked.
  - capability: .github/labview/<cap>/<cap>.vipc, monitored by its capability.
  - project:    monitored when config.container.vipc has monitor:true; when no
                list exists yet, every project VIPC defaults to monitored.
  """
  if path == TOOLING_CORE_VIPC:
    return 'core', '', True, True, True
  cap = _capability_for_vipc(path)
  if cap:
    return 'capability', cap, _capability_enabled(cap), True, False
  # Any other VIPC under .github/ is internal/experimental tooling, NOT a user
  # project dependency: the worker "Stage repo VIPC files" step excludes .github/
  # from baking, so such a file can never be present on the standard workers and
  # must not be flagged as a missing dependency. Classify it as a tooling file
  # (capability role, no capability) so it shows N/A rather than a false gap.
  if (path or '').replace('\\', '/').startswith('.github/'):
    return 'capability', '', False, True, False
  entry = (config_vipc or {}).get(path)
  monitored = bool(entry and entry.get('monitor') is True) if has_config_list else True
  return 'project', '', monitored, monitored, False

def _manifest_failed_packages(man):
  """Package names a worker manifest reports as failing to install (optional).

  Surfaced as a red error marker on the Dependencies page. Manifests that only
  list successfully-applied packages yield an empty set (no false errors)."""
  out = set()
  if isinstance(man, dict):
    for name in man.get('failed_packages') or []:
      if isinstance(name, str) and name.strip():
        out.add(name.strip())
    for vipc in man.get('vipc') or []:
      for name in vipc.get('failed') or []:
        if isinstance(name, str) and name.strip():
          out.add(name.strip())
  return sorted(out, key=str.lower)

def _manifest_nipkg_packages(man):
  packages = []
  if isinstance(man, dict):
    for pkg in man.get('nipkg_packages') or []:
      if isinstance(pkg, dict) and pkg.get('name'):
        packages.append({'name': str(pkg.get('name') or ''), 'version': str(pkg.get('version') or '')})
  return sorted(packages, key=lambda p: p['name'].lower())

def _known_nipm_dependencies():
  return [
    {
      'name': 'LabVIEW',
      'source': 'NI Package Manager',
      'labview': True,
      'platforms': ['windows', 'linux'],
      'details': 'LabVIEW runtime and development packages from the NI feeds.',
    },
    {
      'name': 'VI Analyzer support',
      'source': 'NI Package Manager',
      'packages': {'windows': ['ni-viawin-labview-support'], 'linux': ['ni-vialin-labview-support']},
      'platforms': ['windows', 'linux'],
      'details': 'NI VI Analyzer support package used by the analyzer runners.',
    },
    {
      'name': 'NI Unit Test Framework',
      'source': 'NI Package Manager',
      'packages': {'windows': ['ni-utf-labview-support']},
      'platforms': ['windows'],
      'details': 'NI UTF support package used by the Windows unit-test runner.',
    },
    {
      'name': 'JKI Dragon',
      'source': 'NI Package Manager',
      'packages': {'windows': ['jki-dragon']},
      'platforms': ['windows'],
      'details': 'JKI Dragon dependency-management tooling installed from the LabVIEW 2026 NI Package Manager feed.',
    },
    {
      'name': 'VI Package Manager',
      'source': 'NI Package Manager',
      'packages': {'windows': ['ni-vipm']},
      'platforms': ['windows'],
      'details': 'NI-published Windows VIPM package that enables VIPC application during worker builds. Linux installs VIPM from the native Debian package during its worker build.',
    },
  ]

_DRAGON_MOD = None

def _load_dragon_module():
  """Import the shared .dragon TOML parser (.github/labview/dragon_deps.py).

  Returns the module, or False when it (or a TOML backend) is unavailable so the
  dashboard still builds. A repo without the parser simply shows no Dragon
  items."""
  global _DRAGON_MOD
  if _DRAGON_MOD is not None:
    return _DRAGON_MOD
  path = '.github/labview/dragon_deps.py'
  if not os.path.isfile(path):
    _DRAGON_MOD = False
    return _DRAGON_MOD
  try:
    import importlib.util
    spec = importlib.util.spec_from_file_location('dragon_deps', path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    _DRAGON_MOD = mod
  except Exception as exc:
    print(f"WARN: dragon parser unavailable: {exc}", file=sys.stderr)
    _DRAGON_MOD = False
  return _DRAGON_MOD

def _dragon_inventory():
  """Parse every repository .dragon file into the normalized Dragon model.

  Returns {'available', 'files', 'install_set', 'conflicts'}. Degrades to an empty
  inventory (available False) when the parser or a TOML backend is missing."""
  mod = _load_dragon_module()
  if not mod or not mod.toml_available():
    return {'available': False, 'files': [], 'install_set': [], 'conflicts': []}
  try:
    inv = mod.build_inventory('.')
    inv['available'] = True
    return inv
  except Exception as exc:
    print(f"WARN: could not build dragon inventory: {exc}", file=sys.stderr)
    return {'available': False, 'files': [], 'install_set': [], 'conflicts': []}

def _dragon_status_index(man):
  """Map (manager, package_id_lower) -> the worker manifest's Dragon status item."""
  out = {}
  if isinstance(man, dict):
    dragon = man.get('dragon')
    if isinstance(dragon, dict):
      for item in dragon.get('items') or []:
        if isinstance(item, dict) and item.get('package_id'):
          key = (str(item.get('manager') or 'vipm'), str(item['package_id']).lower())
          out[key] = item
  return out

def _vipm_version_index(man):
  """Map vipm package name -> installed version set from a worker manifest."""
  out = {}
  if isinstance(man, dict):
    for pkg in man.get('vipm_packages') or []:
      if isinstance(pkg, dict):
        name = str(pkg.get('name') or '').strip().lower()
        version = str(pkg.get('version') or '').strip()
        if name:
          out.setdefault(name, set()).add(version)
  return out

def _nipkg_version_index(man):
  """Map nipkg package name -> installed version set from a worker manifest."""
  out = {}
  if isinstance(man, dict):
    for pkg in man.get('nipkg_packages') or []:
      if isinstance(pkg, dict):
        name = str(pkg.get('name') or '').strip().lower()
        version = str(pkg.get('version') or '').strip()
        if name:
          out.setdefault(name, set()).add(version)
  return out

def _infer_dragon_status(dep, have_manifest, baked_packages, vipm_index, nipkg_index):
  """Best-effort Dragon status from worker inventories when no dragon block exists.

  Some worker manifests do not publish a dedicated `dragon.items` section yet.
  In that case, infer status from installed VIPM/NIPM package inventories so an
  already-baked dependency is not stuck at `not_attempted` forever."""
  if not have_manifest:
    return None
  manager = str(dep.get('manager') or 'vipm').strip().lower()
  pkg = str(dep.get('package_id') or '').strip()
  declared = str(dep.get('declared_version') or '').strip()
  if not pkg:
    return None

  if manager == 'vipm':
    low = pkg.lower()
    versions = sorted(v for v in (vipm_index.get(low) or set()) if v)
    # Match by explicit package/version id first (pkg-1.2.3 or pkg-v1.2.3), then
    # by package name/version from `vipm list --installed` parsing.
    ids = set()
    if declared:
      ids.add(f'{pkg}-{declared}'.lower())
      if not declared.lower().startswith('v'):
        ids.add(f'{pkg}-v{declared}'.lower())
    if ids and any(i in baked_packages for i in ids):
      return {
        'status': 'installed',
        'installed_version': declared,
        'message': 'inferred from Windows worker VIPM package inventory',
      }
    if declared and versions and declared in versions:
      return {
        'status': 'installed',
        'installed_version': declared,
        'message': 'inferred from Windows worker VIPM package inventory',
      }
    if versions:
      return {
        'status': 'wrong_version',
        'installed_version': versions[0],
        'message': 'package is installed, but at a different version',
      }
    return {
      'status': 'missing',
      'installed_version': '',
      'message': 'package not found in Windows worker VIPM package inventory',
    }

  if manager == 'nipm':
    low = pkg.lower()
    versions = sorted(v for v in (nipkg_index.get(low) or set()) if v)
    if not versions:
      return {
        'status': 'missing',
        'installed_version': '',
        'message': 'package not found in Windows worker NIPM package inventory',
      }
    if not declared or declared in versions:
      return {
        'status': 'installed',
        'installed_version': declared or versions[0],
        'message': 'inferred from Windows worker NIPM package inventory',
      }
    return {
      'status': 'wrong_version',
      'installed_version': versions[0],
      'message': 'package is installed, but at a different version',
    }

  return None

def _annotate_dragon_status(dep, status_index, have_manifest, baked_packages, vipm_index, nipkg_index):
  """Overlay an install status onto a declared Dragon dependency in place.

  A cross-file conflict always wins; otherwise the reconciled worker-manifest
  status is used; a missing manifest means the Windows worker has not been built
  yet (pending), and an item absent from a present manifest falls back to
  best-effort inference from installed-package inventories."""
  if dep.get('conflict'):
    dep['status'] = 'conflict'
    dep.setdefault('installed_version', '')
    dep.setdefault('message', 'declared with different versions across files')
    return
  st = status_index.get((dep['manager'], dep['package_id'].lower()))
  if st:
    dep['status'] = str(st.get('status') or 'not_attempted')
    dep['installed_version'] = str(st.get('installed_version') or '')
    dep['message'] = str(st.get('message') or '')
  else:
    inferred = _infer_dragon_status(dep, have_manifest, baked_packages, vipm_index, nipkg_index)
    if inferred:
      dep['status'] = inferred['status']
      dep['installed_version'] = inferred.get('installed_version', '')
      dep['message'] = inferred.get('message', '')
    else:
      dep['status'] = 'not_attempted' if have_manifest else 'pending'
      dep.setdefault('installed_version', '')
      dep.setdefault('message', '')

def _build_dragon_section(config=None):
  """Build the Dragon dependency block for the Dependencies index (Windows).

  Declared dependencies come from repository .dragon files. Each file carries its
  monitor flag so the Dependencies page can suppress warnings and auto-update
  triggers for unmonitored files while still showing the file in the table.
  """
  config = config or {}
  configured = {v.get('path', ''): v for v in (config.get('dragon') or []) if v.get('path')}
  has_config_list = bool(config.get('hasDragonList'))
  inv = _dragon_inventory()
  files_by_path = {}
  for f in inv.get('files') or []:
    if f.get('source_file'):
      files_by_path[f.get('source_file')] = f
  for path in _repo_dragons():
    files_by_path.setdefault(path, {'source_file': path, 'dependencies': []})
  for path in configured:
    files_by_path.setdefault(path, {'source_file': path, 'dependencies': []})
  files = [files_by_path[k] for k in sorted(files_by_path, key=str.lower)]
  for f in files:
    entry = configured.get(f.get('source_file') or '')
    f['monitored'] = bool(entry and entry.get('monitor') is True) if has_config_list else True
  if not files:
    # No .dragon files in this revision: skip the worker-manifest fetch.
    return {
      'available': inv.get('available', False),
      'column': 'windows',
      'ready': False,
      'health': '',
      'manifest_version': '',
      'files': [],
      'install_set': [],
      'conflicts': inv.get('conflicts') or [],
    }
  win_man = _worker_manifest('windows', 'latest')
  have_manifest = isinstance(win_man, dict)
  status_index = _dragon_status_index(win_man)
  win_dragon = win_man.get('dragon') if have_manifest else None
  baked_packages = {str(p).lower() for p in _manifest_packages(win_man)} if have_manifest else set()
  vipm_index = _vipm_version_index(win_man) if have_manifest else {}
  nipkg_index = _nipkg_version_index(win_man) if have_manifest else {}
  for item in inv.get('install_set') or []:
    _annotate_dragon_status(item, status_index, have_manifest, baked_packages, vipm_index, nipkg_index)
  for f in files:
    for dep in f.get('dependencies') or []:
      _annotate_dragon_status(dep, status_index, have_manifest, baked_packages, vipm_index, nipkg_index)
  return {
    'available': inv.get('available', False) or bool(files),
    'column': 'windows',
    'ready': have_manifest,
    'health': (win_dragon or {}).get('health', '') if isinstance(win_dragon, dict) else '',
    'manifest_version': win_man.get('version', '') if have_manifest else '',
    'files': files,
    'install_set': inv.get('install_set') or [],
    'conflicts': inv.get('conflicts') or [],
  }

def _system_dependencies():
  has_toimages = os.path.isdir('.github/labview/toimages')
  if not has_toimages:
    return []
  has_windows = os.path.isfile('.github/workflows/vi-snapshots-json-windows.yml')
  has_linux = os.path.isfile('.github/workflows/vi-snapshots-json.yml')
  return [
    {
      'name': 'VI Browser 2.0 toimages runner',
      'source': 'Repository Go code',
      'path': '.github/labview/toimages/main.go',
      'details': 'Batch runner compiled from repo Go source; Linux uses the toimages GHCR image and Windows builds runner.exe in the workflow.',
      'platforms': {
        'windows': {'present': has_windows, 'label': 'workflow-built' if has_windows else ''},
        'linux': {'present': has_linux, 'label': 'toimages image' if has_linux else ''},
      },
    },
    {
      'name': 'lvctl render engine',
      'source': 'Repository Go code + embedded VIs',
      'path': '.github/labview/toimages/_ni/labview/lvctl',
      'details': 'Renderer used by VI Browser 2.0 to drive LabVIEW and emit position-aware frame JSON.',
      'platforms': {
        'windows': {'present': has_windows, 'label': 'workflow-built' if has_windows else ''},
        'linux': {'present': has_linux, 'label': 'toimages image' if has_linux else ''},
      },
    },
  ]

def _compute_deps_pending(data):
  """Decide whether the repo declares project dependencies that are NOT yet baked
  into its current worker container(s). This drives the persistent dashboard
  banner and the Dependencies dialog. Conservative: only flags a project VIPC that
  is configured to be baked and parsed cleanly, whose packages are missing from the
  current Windows worker manifest; plus declared Dragon deps not yet installed."""
  cols = {c.get('key'): c for c in data.get('columns', [])}

  # Version-tolerant matching: a VIPC pins a specific build (e.g.
  # "wovalab_lib_antidoc_cli-3.3.0.117") but VIPM installs one version per
  # package, so a baked "...-3.4.0.126" satisfies the request. Compare on the
  # versionless base identifier so a present-but-different build is not flagged
  # as a missing dependency.
  def _pkg_base(pkg):
    return re.sub(r'-\d[\d.]*$', '', str(pkg or '')).lower()

  def baked(key):
    c = cols.get(key) or {}
    pkgs = c.get('packages') or []
    return {str(p).lower() for p in pkgs}, {_pkg_base(p) for p in pkgs}

  def _missing(pkgs, baked_pair):
    exact, bases = baked_pair
    return [p for p in pkgs if str(p).lower() not in exact and _pkg_base(p) not in bases]

  win_baked = baked('windows')
  # The Linux worker now installs VIPM natively and bakes repository VIPC (same as
  # Windows), so a Linux dependency gap is flagged independently -- but only when a
  # real Linux worker manifest resolved for this revision (a non-base, ready tag).
  # Otherwise (Linux unused / base image) there is no worker to bake into and we do
  # not surface a spurious Linux update prompt.
  lin_col = cols.get('linux') or {}
  lin_active = bool(lin_col.get('ready')) and str(lin_col.get('tag') or 'base') not in ('base', 'none')
  lin_baked = baked('linux') if lin_active else set()
  pending_pkgs = set()
  pending_files = []
  lin_pending_files = []
  for v in data.get('vipc', []):
    # Project VIPCs always participate. Capability VIPCs (e.g. Antidoc) participate
    # only when that capability is installed -- _vipc_role sets `configured` to the
    # capability-enabled flag, so an uninstalled capability is skipped here. Their
    # monitorOn is limited to the capability's platform(s) (Antidoc = Windows), so a
    # Windows-only capability never nags about the Linux worker.
    if v.get('role') not in ('project', 'capability') or not v.get('configured') or v.get('error'):
      continue
    pkgs = v.get('packages') or []
    if not pkgs:
      continue
    mon = v.get('monitorOn')
    if mon is None:
      mon = ['windows', 'linux']
    if 'windows' in mon:
      win_missing = _missing(pkgs, win_baked)
      if win_missing:
        pending_files.append(v.get('path'))
        pending_pkgs.update(win_missing)
    if lin_active and 'linux' in mon:
      lin_missing_pkgs = _missing(pkgs, lin_baked)
      if lin_missing_pkgs:
        lin_pending_files.append(v.get('path'))
        pending_pkgs.update(lin_missing_pkgs)
  lin_missing = bool(lin_pending_files)

  dragon = data.get('dragon') or {}
  monitored_dragon_keys = set()
  for f in dragon.get('files') or []:
    if f.get('monitored') is False:
      continue
    for dep in f.get('dependencies') or []:
      if dep.get('package_id'):
        monitored_dragon_keys.add((str(dep.get('manager') or 'vipm'), str(dep.get('package_id')).lower()))
  dragon_pending = []
  for item in dragon.get('install_set') or []:
    key = (str(item.get('manager') or 'vipm'), str(item.get('package_id') or '').lower())
    if key not in monitored_dragon_keys:
      continue
    st = str(item.get('status') or '')
    if st in ('pending', 'not_attempted', 'missing', 'wrong_version', 'conflict'):
      dragon_pending.append({'name': item.get('name') or item.get('package_id') or '', 'status': st})

  containers = []
  if pending_files:
    containers.append('windows')
  if lin_missing:
    containers.append('linux')

  all_files = sorted(set(pending_files) | set(lin_pending_files), key=str.lower)
  return {
    'schema': 1,
    'pending': bool(pending_files or lin_missing or dragon_pending),
    'repo': data.get('repo', ''),
    'sha': data.get('sha', ''),
    'packages': sorted(pending_pkgs, key=str.lower),
    'vipcs': all_files,
    'dragon': dragon_pending,
    'containers': sorted(set(containers)),
    'generated': __import__('datetime').datetime.utcnow().strftime('%Y-%m-%dT%H:%M:%SZ'),
  }

MONITOR_PLATFORMS = ['windows', 'linux']

def _monitor_on(entry, has_config_list, is_core):
  """Worker platforms a dependency file is monitored for. Back-compat: monitor:true
  with no monitorOn list = every platform; and when no config list exists yet every
  project file defaults to all platforms."""
  if is_core:
    return list(MONITOR_PLATFORMS)
  if not has_config_list:
    return list(MONITOR_PLATFORMS)
  if not entry or entry.get('monitor') is not True:
    return []
  on = entry.get('monitorOn')
  if isinstance(on, list) and on:
    low = [str(x).lower() for x in on]
    return [p for p in MONITOR_PLATFORMS if p in low]
  return list(MONITOR_PLATFORMS)

def _build_dependencies_index():
  config = _parse_container_config()
  config_vipc = {v.get('path', ''): v for v in config.get('vipc') or [] if v.get('path')}
  has_config_list = bool(config.get('hasVipcList'))
  vipcs = []
  vipc_paths = sorted(set(_repo_vipcs()) | set(config_vipc), key=str.lower)
  for path in vipc_paths:
    packages, error = _parse_vipc_packages(path) if os.path.isfile(path) else ([], '')
    role, capability, configured, monitored, locked = _vipc_role(path, config_vipc, has_config_list)
    if role == 'capability':
      # A capability VIPC is baked only into its capability's worker platform(s),
      # so limit its monitored columns accordingly (Antidoc = Windows only).
      monitor_on = _capability_platforms(capability)
    else:
      monitor_on = _monitor_on(config_vipc.get(path), has_config_list, role == 'core')
    vipcs.append({'path': path, 'role': role, 'capability': capability,
                  'tooling': role == 'core', 'configured': configured, 'monitored': monitored,
                  'monitorOn': monitor_on,
                  'locked': locked, 'packages': packages, 'error': error})
  columns = [
    {'key': 'windows', 'label': 'Windows', 'platform': 'windows', 'defaultTag': 'latest'},
    {'key': 'linux', 'label': 'Linux', 'platform': 'linux', 'defaultTag': 'latest'},
  ]
  manifest_cache = {}
  for col in columns:
    action_tag = config.get('actions', {}).get(col.get('action')) if col.get('action') else ''
    tag = col.get('tag') or action_tag or config.get('use') or col.get('defaultTag') or 'base'
    col['tag'] = tag
    if tag not in ('base', 'none'):
      cache_key = f"{col['platform']}:{tag}"
      if cache_key not in manifest_cache:
        manifest_cache[cache_key] = _worker_manifest(col['platform'], tag)
      man = manifest_cache[cache_key]
      col['ready'] = isinstance(man, dict)
      col['packages'] = _manifest_packages(man)
      col['failed_packages'] = _manifest_failed_packages(man)
      col['nipkg_ready'] = isinstance(man, dict)
      col['nipkg_packages'] = _manifest_nipkg_packages(man)
      col['labview_version'] = man.get('labview_version', '') if isinstance(man, dict) else ''
      col['version'] = man.get('version', tag) if isinstance(man, dict) else tag
    else:
      col['ready'] = True
      col['packages'] = []
      col['failed_packages'] = []
      col['nipkg_ready'] = True
      col['nipkg_packages'] = []
      col['labview_version'] = ''
      col['version'] = tag
  data = {
    'schema': 1,
    'repo': repo,
    'sha': os.environ.get('GITHUB_SHA', ''),
    'revisions': [{
      'sha': c.get('sha', ''),
      'short': (c.get('sha') or '')[:7],
      'message': ((c.get('commit') or {}).get('message') or c.get('sha', '')).split('\n')[0],
      'author': ((c.get('commit') or {}).get('author') or {}).get('name', ''),
      'date': ((c.get('commit') or {}).get('author') or {}).get('date', ''),
      'project': classify_commit(c['sha'])['is_project'],
      'dep_only': classify_commit(c['sha'])['is_dep_only'],
    } for c in commits_data if c.get('sha')],
    'config': config,
    'vipc': vipcs,
    'nipm': _known_nipm_dependencies(),
    'dragon': _build_dragon_section(config),
    'system': _system_dependencies(),
    'columns': columns,
  }
  with open('ci-out/dashboard/dependencies/index.json', 'w', encoding='utf-8') as fh:
    json.dump(data, fh, indent=2, ensure_ascii=True)
  # Persistent "dependencies pending" signal, read by the shared header on every
  # page to show the banner until the worker container(s) are updated.
  try:
    pending = _compute_deps_pending(data)
  except Exception as exc:
    print(f"WARN: could not compute deps-pending: {exc}", file=sys.stderr)
    pending = {'schema': 1, 'pending': False}
  with open('ci-out/dashboard/deps-pending.json', 'w', encoding='utf-8') as fh:
    json.dump(pending, fh, ensure_ascii=True)

try:
  _build_dependencies_index()
except Exception as exc:
  print(f"WARN: could not build dependencies index: {exc}", file=sys.stderr)
# Deploy a catalog.json at the Pages root so the version badge + What's New can
# read the installed version. Prefer the consumer's own catalog; else synthesize
# one from the manifest values resolved above.
_client_cat = os.environ.get('CATALOG_PATH', '.github/labview-ci/catalog.json')
if os.path.isfile(_client_cat):
    _stage(_client_cat, 'ci-out/dashboard/catalog.json')
elif lvci_version:
    with open('ci-out/dashboard/catalog.json', 'w', encoding='utf-8') as f:
        json.dump({'version': lvci_version,
                   'source': {'repo': lvci_src_repo, 'ref': lvci_src_ref}}, f, indent=2)

# The VI Browser's snapshot-independent index: every project revision plus the
# VI files present in it. Lets the browser render the file hierarchy and offer a
# "Generate snapshots" action even when nothing has been rendered yet. Deployed
# with keep_files:true, alongside (never clobbering) the snapshot workflow's
# commits.json / <sha>/manifest.json.
files_payload = {
    'repo': repo,
    'generated': __import__('datetime').datetime.utcnow().strftime('%Y-%m-%dT%H:%M:%SZ'),
    'commits': file_commits,
}
with open('ci-out/dashboard/vi-snapshots/files.json', 'w', encoding='utf-8') as f:
    json.dump(files_payload, f, ensure_ascii=False)
print(f"Dashboard built with {len(commits_data)} commits; "
      f"files.json has {len(file_commits)} project revision(s).")
