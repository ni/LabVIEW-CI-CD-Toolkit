/*
 * lvci-header.js — the shared, site-wide navigation header for the LabVIEW CI
 * Pages site (dashboard, VI Analyzer reports, VI Browser, Configure, Apply, …).
 *
 * WHY THIS EXISTS
 *   Every page used to carry its own ad-hoc top nav, baked into action-runner
 *   output (the dashboard generator, the per-commit report generator, …). That
 *   made navigation inconsistent and meant changing a link or adding an action
 *   required regenerating reports. This file is a SINGLE shared asset deployed
 *   once to the Pages root: every page just declares a tiny `window.LVCI` config
 *   and loads this script, so the header is consistent everywhere and evolves
 *   independently of the content beneath it (reports stay immutable; the header
 *   updates the moment this file is redeployed).
 *
 * HOW A PAGE OPTS IN  (before this script, or via data-* on the script tag)
 *   <script>window.LVCI = {
 *       context: 'vi-analyzer-report',     // which page this is (see CONTEXTS)
 *       repo:    'owner/name',             // GitHub repo (for links + dispatch)
 *       pagesUrl:'https://o.github.io/n',  // Pages base (absolute; optional —
 *                                          //   derived from this script's src)
 *       sha:     '<40-hex>',  short:'<7>', // commit under view (report pages)
 *       platform:'windows',                // 'windows' | 'linux' (report pages)
 *       rawUrl:  'raw.html'                // native report (report pages)
 *   };</script>
 *   <script src="<pagesUrl>/lvci-header.js" defer></script>
 *
 * The script derives the Pages base from `pagesUrl` or, failing that, from its
 * own <script src>, so cross-depth links (root vs /vi-analyzer/<sha>/) all work.
 * It injects its own styles + DOM at the top of <body>; no placeholder needed.
 * It suppresses itself inside an iframe (the dashboard opens Configure/Apply in
 * a modal that already has its own chrome).
 */
(function () {
  'use strict';

  // Never render inside the dashboard's iframe modal — that overlay has its own
  // title bar + close button, and a second header would be redundant/confusing.
  try { if (window.top !== window.self) return; } catch (e) { return; }

  var cfg = window.LVCI || {};

  // ── Resolve this script element + the Pages base URL ──────────────────────
  var me = document.currentScript;
  if (!me) {
    var ss = document.getElementsByTagName('script');
    for (var i = ss.length - 1; i >= 0; i--) {
      if ((ss[i].src || '').indexOf('lvci-header.js') >= 0) { me = ss[i]; break; }
    }
  }
  // data-* on the script tag are a fallback for pages that prefer not to set a
  // global (e.g. data-context, data-repo, data-sha, data-platform, data-raw).
  if (me && me.dataset) {
    ['context', 'repo', 'pagesUrl', 'sha', 'short', 'platform', 'rawUrl'].forEach(function (k) {
      var dk = k === 'pagesUrl' ? 'pages' : (k === 'rawUrl' ? 'raw' : k);
      if (cfg[k] == null && me.dataset[dk] != null) cfg[k] = me.dataset[dk];
    });
  }

  function trimSlash(s) { return String(s || '').replace(/\/+$/, ''); }
  // Prefer the Pages base derived from THIS script's own (resolved, absolute)
  // src — it is always same-origin, so nav links work whether the site is served
  // from production Pages or a local preview. cfg.pagesUrl is only a fallback for
  // the rare case where the script element can't be found.
  var base = '';
  if (me && me.src) base = trimSlash(me.src.replace(/\/[^\/]*$/, '')); // dir of the script
  if (!base) base = trimSlash(cfg.pagesUrl);
  if (!base) base = '.';
  var repo = cfg.repo || '';
  // Static pages (Configure, VI Browser, …) don't know the repo at build time;
  // derive it from a GitHub Pages PROJECT URL (https://<owner>.github.io/<repo>/…).
  if (!repo) {
    try {
      var hm = location.hostname.match(/^([^.]+)\.github\.io$/i);
      var seg = location.pathname.split('/').filter(Boolean)[0];
      if (hm && seg && seg.indexOf('.') < 0) repo = hm[1] + '/' + seg;
    } catch (e) {}
  }
  // A page may be ABOUT a different repository than the one whose assets serve it
  // — the centralized "What's New" page is served from the source site but upgrades
  // a consumer repo. cfg.brandRepo names that repo so the brand, its home link, and
  // the Dashboard / VI Browser nav all reflect it, while `repo` (the serving origin)
  // still drives the version + dispatch logic. Display-only: with no brandRepo every
  // link is exactly as before.
  var brandRepo = cfg.brandRepo || '';
  var navBase = base;
  if (brandRepo) { var _nb = trimSlash(pagesUrlForRepo(brandRepo)); if (_nb) navBase = _nb; }
  var ctx = cfg.context || 'page';

  // Canonical home of this tooling. The dashboard page assets are served by the
  // action verbatim — the installer's substitutions never rewrite them — so this
  // fallback rides onto every consumer dashboard still pointing at the root, the
  // same way faq.html / integrate.html anchor their links. loadVersion() refines
  // it from the same-origin catalog (and any relocation pointer it follows).
  var SOURCE_FALLBACK_REPO = 'ni/LabVIEW-CI-CD-Toolkit';
  var srcRepo = SOURCE_FALLBACK_REPO;
  var srcRef = 'main';

  // ── Design tokens + styles (match the GitHub-style dark/light tokens the
  //    rest of the site uses, so the header blends into every page). ─────────
  var CSS = [
    ':root{--lvh-h:54px}',
    '.lvci-hdr,.lvci-hdr *{box-sizing:border-box}',
    // flex-shrink:0 keeps the bar full-height when <body> is itself a flex
    // column (see the mount logic for full-height flex/grid pages).
    '.lvci-hdr{position:sticky;top:0;z-index:200;flex-shrink:0;display:flex;align-items:center;gap:14px;',
      'height:var(--lvh-h);padding:0 16px;font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;',
      'background:rgba(22,27,34,.86);-webkit-backdrop-filter:saturate(160%) blur(10px);backdrop-filter:saturate(160%) blur(10px);',
      'border-bottom:1px solid #30363d;color:#e6edf3}',
    '@media(prefers-color-scheme:light){.lvci-hdr{background:rgba(255,255,255,.86);border-bottom-color:#d0d7de;color:#1f2328}}',
    // Brand — shows the repository this dashboard belongs to (always visible),
    // with a small product kicker above it. The repo name truncates with an
    // ellipsis so the brand always fits, down to a narrow phone. Falls back to
    // the product name when the repo can't be derived (e.g. a preview build).
    '.lvci-brand{display:inline-flex;align-items:center;gap:10px;color:inherit;text-decoration:none;white-space:nowrap;flex:0 1 auto;min-width:0}',
    '.lvci-brand:hover{text-decoration:none}',
    '.lvci-brand:hover .lvci-name{text-decoration:underline}',
    '.lvci-brand svg{display:block;width:24px;height:24px;flex:0 0 auto}',
    '.lvci-brand .lvci-repo{display:flex;flex-direction:column;justify-content:center;min-width:0;overflow:hidden}',
    '.lvci-brand .lvci-kicker{font-weight:600;font-size:9px;letter-spacing:.07em;text-transform:uppercase;color:#8b949e;line-height:1.3}',
    '.lvci-brand .lvci-name{font-weight:700;font-size:15px;line-height:1.2;color:inherit;min-width:0;overflow:hidden;white-space:nowrap;text-overflow:ellipsis}',
    '@media(prefers-color-scheme:light){.lvci-brand .lvci-kicker{color:#57606a}}',
    '.lvci-brand .lvci-sub{font-weight:500;font-size:11px;color:#8b949e;border:1px solid #30363d;border-radius:999px;padding:1px 7px;margin-left:2px}',
    '@media(prefers-color-scheme:light){.lvci-brand .lvci-sub{color:#57606a;border-color:#d0d7de}}',
    // Primary nav
    '.lvci-nav{display:flex;align-items:center;gap:2px;flex:1 1 auto;min-width:0}',
    '.lvci-nav a{display:inline-flex;align-items:center;gap:6px;color:#8b949e;text-decoration:none;font-size:13.5px;font-weight:500;',
      'padding:6px 10px;border-radius:7px;white-space:nowrap}',
    '.lvci-nav a:hover{color:#e6edf3;background:rgba(177,186,196,.12)}',
    '.lvci-nav a.on{color:#e6edf3;background:rgba(177,186,196,.16)}',
    '.lvci-nav a.on::after{content:"";position:absolute}',
    '@media(prefers-color-scheme:light){.lvci-nav a:hover,.lvci-nav a.on{color:#1f2328;background:rgba(80,90,100,.10)}}',
    '.lvci-nav a .lvci-soon{font-size:9.5px;font-weight:600;color:#8b949e;border:1px solid #30363d;border-radius:999px;padding:0 5px;text-transform:uppercase;letter-spacing:.04em}',
    // Count pill beside a nav item (e.g. Clients), filled in once the registry loads.
    '.lvci-nav a .lvci-ncount{font-size:10px;font-weight:700;color:#8b949e;background:rgba(177,186,196,.16);border-radius:999px;padding:1px 6px;min-width:16px;text-align:center;line-height:1.4}',
    // Grouped nav dropdowns (Settings / Help) — desktop top-nav menus.
    '.lvci-navgrp{position:relative;display:inline-flex;align-items:center}',
    '.lvci-navgrp-btn{position:relative;display:inline-flex;align-items:center;gap:5px;color:#8b949e;background:transparent;border:0;font:inherit;font-size:13.5px;font-weight:500;padding:6px 10px;border-radius:7px;white-space:nowrap;cursor:pointer}',
    '.lvci-navgrp-btn:hover,.lvci-navgrp-btn.open{color:#e6edf3;background:rgba(177,186,196,.12)}',
    // Active "you are here" state for the grouped trigger (e.g. Settings while on a config page).
    '.lvci-navgrp-btn.on{color:#e6edf3;background:rgba(177,186,196,.16)}',
    '.lvci-navgrp-chev{display:inline-flex}',
    '.lvci-navgrp-chev svg{width:10px;height:10px;transition:transform .15s}',
    '.lvci-navgrp-btn.open .lvci-navgrp-chev svg{transform:rotate(180deg)}',
    '.lvci-navgrp-menu{left:0;right:auto}',
    '@media(prefers-color-scheme:light){.lvci-navgrp-btn{color:#57606a}.lvci-navgrp-btn:hover,.lvci-navgrp-btn.open{color:#1f2328;background:rgba(80,90,100,.10)}.lvci-navgrp-btn.on{color:#1f2328;background:rgba(80,90,100,.14)}}',
    ':root[data-lvci-theme=light] .lvci-navgrp-btn{color:#57606a}',
    ':root[data-lvci-theme=light] .lvci-navgrp-btn:hover,:root[data-lvci-theme=light] .lvci-navgrp-btn.open{color:#1f2328;background:rgba(80,90,100,.10)}',
    ':root[data-lvci-theme=light] .lvci-navgrp-btn.on{color:#1f2328;background:rgba(80,90,100,.14)}',
    ':root[data-lvci-theme=dark] .lvci-navgrp-btn{color:#8b949e}',
    ':root[data-lvci-theme=dark] .lvci-navgrp-btn:hover,:root[data-lvci-theme=dark] .lvci-navgrp-btn.open{color:#e6edf3;background:rgba(177,186,196,.12)}',
    ':root[data-lvci-theme=dark] .lvci-navgrp-btn.on{color:#e6edf3;background:rgba(177,186,196,.16)}',
    // Actions cluster (right)
    '.lvci-actions{display:flex;align-items:center;gap:8px;flex:0 0 auto}',
    '.lvci-btn{display:inline-flex;align-items:center;gap:6px;font-size:12.5px;font-weight:600;line-height:1;cursor:pointer;',
      'border-radius:7px;padding:7px 12px;border:1px solid #30363d;background:transparent;color:inherit;text-decoration:none;white-space:nowrap}',
    '.lvci-btn:hover{background:rgba(177,186,196,.12);text-decoration:none}',
    '@media(prefers-color-scheme:light){.lvci-btn{border-color:#d0d7de}.lvci-btn:hover{background:rgba(80,90,100,.08)}}',
    '.lvci-btn.primary{background:#238636;border-color:#238636;color:#fff}',
    '.lvci-btn.primary:hover{background:#2ea043}',
    '.lvci-btn.accent{background:#1f6feb;border-color:#1f6feb;color:#fff}',
    '.lvci-btn.accent:hover{background:#388bfd}',
    '.lvci-btn[disabled]{opacity:.55;cursor:default}',
    '.lvci-btn .lvci-spin{width:11px;height:11px;border:2px solid rgba(255,255,255,.5);border-top-color:#fff;border-radius:50%;display:inline-block;animation:lvci-spin .7s linear infinite}',
    '@keyframes lvci-spin{to{transform:rotate(360deg)}}',
    // Live CI activity chip — shown in the actions cluster ONLY while one or more
    // workflow runs are in flight; idle leaves the bar clean. It is its own
    // surface (no longer fused onto the version), so activity and version never
    // compete for one pill.
    '.lvci-run-chip{display:none;align-items:center;gap:7px;font-size:12px;font-weight:600;color:#1f6feb;text-decoration:none;',
      'border:1px solid #1f6feb;border-radius:999px;padding:4px 11px;white-space:nowrap}',
    '.lvci-run-chip.show{display:inline-flex}',
    '.lvci-run-chip:hover{background:rgba(31,111,235,.12);text-decoration:none}',
    '.lvci-run-chip .lvci-run-spin{width:9px;height:9px;border:2px solid currentColor;border-right-color:transparent;border-radius:50%;animation:lvci-spin .7s linear infinite}',
    '@media(prefers-color-scheme:light){.lvci-run-chip{color:#0969da;border-color:#0969da}.lvci-run-chip:hover{background:rgba(9,105,218,.08)}}',
    // Activity pill — compact run / queue / fail counts; each segment shows only
    // when its count > 0, and the whole pill links to the repo's Actions list.
    '.lvci-actpill{display:none;align-items:stretch;text-decoration:none;border:1px solid #30363d;border-radius:999px;overflow:hidden;white-space:nowrap;line-height:1}',
    '.lvci-actpill.show{display:inline-flex}',
    '.lvci-actpill:hover{background:rgba(177,186,196,.08)}',
    '.lvci-ap-seg{display:none;align-items:center;gap:6px;padding:4px 11px;font-size:12px;font-weight:600}',
    '.lvci-ap-seg.on{display:inline-flex}',
    '.lvci-ap-seg.on~.lvci-ap-seg.on{border-left:1px solid #30363d}',
    '.lvci-ap-dot{width:8px;height:8px;border-radius:50%;flex:0 0 auto;background:currentColor}',
    '.lvci-ap-spin{width:9px;height:9px;border:2px solid currentColor;border-right-color:transparent;border-radius:50%;animation:lvci-spin .7s linear infinite;flex:0 0 auto}',
    '.lvci-ap-run{color:#1f6feb}',
    '.lvci-ap-queue{color:#8b949e}',
    '.lvci-ap-fail{color:#f85149}',
    '@media(prefers-color-scheme:light){.lvci-actpill{border-color:#d0d7de}.lvci-ap-seg.on~.lvci-ap-seg.on{border-left-color:#d0d7de}.lvci-ap-run{color:#0969da}.lvci-ap-queue{color:#57606a}.lvci-ap-fail{color:#cf222e}}',
    // Update-available cue: a single amber dot on the menu trigger (the More
    // button on desktop, the hamburger on mobile). The version + update action
    // live inside that menu, so this dot is the at-a-glance "you can update" hint.
    '.lvci-mdot{display:none;position:absolute;top:5px;right:5px;width:7px;height:7px;border-radius:50%;background:#d29922;',
      'box-shadow:0 0 0 0 rgba(210,153,34,.5);animation:lvci-pulse 1.8s infinite}',
    '.has-update .lvci-mdot{display:block}',
    '@keyframes lvci-pulse{0%{box-shadow:0 0 0 0 rgba(210,153,34,.5)}70%{box-shadow:0 0 0 5px rgba(210,153,34,0)}100%{box-shadow:0 0 0 0 rgba(210,153,34,0)}}',
    // Version / update entry inside the menu(s): shows the installed version and
    // opens What's New; turns amber + reads "Update available" when behind.
    '.lvci-ddver .lvci-ddver-label{flex:0 1 auto;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}',
    '.lvci-ddver .lvci-ddver-tag{margin-left:auto;padding-left:10px;font-size:11px;font-weight:600;color:#8b949e;white-space:nowrap;font-variant-numeric:tabular-nums}',
    '.lvci-ddver.behind .lvci-ddver-tag,.lvci-ddver.behind .lvci-ic{color:#d29922}',
    '@media(prefers-color-scheme:light){.lvci-ddver .lvci-ddver-tag{color:#57606a}.lvci-ddver.behind .lvci-ddver-tag,.lvci-ddver.behind .lvci-ic{color:#bf8700}}',
    // Revision picker (per-revision reports: switch which revision you're viewing)
    '.lvci-rev{display:inline-flex;align-items:center;gap:7px;flex:0 1 auto;min-width:0}',
    '.lvci-rev .lvci-revlbl{font-size:11px;font-weight:600;color:#8b949e;white-space:nowrap;text-transform:uppercase;letter-spacing:.04em}',
    '.lvci-rev select{font:inherit;font-size:12.5px;font-weight:500;max-width:260px;text-overflow:ellipsis;color:#e6edf3;',
      'background:rgba(177,186,196,.10);border:1px solid #30363d;border-radius:7px;padding:6px 8px;cursor:pointer;color-scheme:dark}',
    '.lvci-rev select:hover{border-color:#8b949e}',
    '@media(prefers-color-scheme:light){.lvci-rev .lvci-revlbl{color:#57606a}.lvci-rev select{color:#1f2328;background:#fff;border-color:#d0d7de;color-scheme:light}}',
    // Persistent context bar (sticky below the header): one consistent place for
    // the "which revision" selector + prev/next steppers on per-revision reports.
    '.lvci-ctxbar{position:sticky;top:var(--lvh-h);z-index:199;display:flex;align-items:center;gap:12px;padding:8px 16px;background:rgba(22,27,34,.96);border-bottom:1px solid #30363d;flex-wrap:wrap}',
    '.lvci-ctxbar:empty{display:none}',
    '.lvci-rev-ctx{flex:0 1 auto}',
    '.lvci-rev-step{display:inline-flex;align-items:center;justify-content:center;width:26px;height:26px;border:1px solid #30363d;border-radius:7px;background:transparent;color:#8b949e;cursor:pointer;font-size:15px;line-height:1;flex:0 0 auto;padding:0}',
    '.lvci-rev-step:hover:not(:disabled){color:#e6edf3;background:rgba(177,186,196,.12)}',
    '.lvci-rev-step:disabled{opacity:.4;cursor:default}',
    '@media(prefers-color-scheme:light){.lvci-ctxbar{background:rgba(246,248,250,.96);border-bottom-color:#d0d7de}.lvci-rev-step{border-color:#d0d7de;color:#57606a}.lvci-rev-step:hover:not(:disabled){color:#1f2328;background:rgba(80,90,100,.10)}}',
    // Reusable searchable revision picker (button + popover) used by every
    // "select a revision" control so they all filter to LabVIEW-code changes.
    '.lvci-revpick{display:inline-flex;align-items:center;gap:8px;font:inherit;font-size:12.5px;font-weight:500;max-width:340px;color:#e6edf3;background:rgba(177,186,196,.10);border:1px solid #30363d;border-radius:7px;padding:6px 9px;cursor:pointer;color-scheme:dark}',
    '.lvci-revpick:hover{border-color:#8b949e}',
    '.lvci-revpick-cur{overflow:hidden;text-overflow:ellipsis;white-space:nowrap;max-width:300px}',
    '.lvci-revpick-caret{color:#8b949e;font-size:10px;flex:0 0 auto}',
    '.lvci-revpop{position:fixed;z-index:1000;width:max-content;max-width:min(94vw,560px);background:#161b22;border:1px solid #30363d;border-radius:10px;box-shadow:0 12px 34px rgba(0,0,0,.42);padding:8px;color:#e6edf3;color-scheme:dark}',
    '.lvci-revpop-search{width:100%;box-sizing:border-box;font:inherit;font-size:13px;padding:8px 10px;background:rgba(177,186,196,.08);color:#e6edf3;border:1px solid #30363d;border-radius:7px;margin:0 0 6px}',
    '.lvci-revpop-search::placeholder{color:#8b949e}',
    '.lvci-revpop-tools{display:flex;align-items:center;gap:7px;font-size:11.5px;color:#8b949e;cursor:pointer;padding:2px 4px 7px;user-select:none}',
    '.lvci-revpop-tools input{accent-color:#58a6ff;width:14px;height:14px;margin:0}',
    '.lvci-revpop-list{max-height:min(58vh,340px);overflow:auto;display:flex;flex-direction:column;gap:1px}',
    '.lvci-revopt{display:flex;align-items:center;gap:9px;padding:7px 9px;border-radius:7px;cursor:pointer;font-size:12.5px}',
    '.lvci-revopt .sh{font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;color:#8b949e;flex:0 0 auto}',
    '.lvci-revopt .ms{overflow:hidden;text-overflow:ellipsis;white-space:nowrap;color:#e6edf3;flex:1 1 auto;min-width:40px}',
    '.lvci-revopt .tag{flex:0 0 auto;font-size:9.5px;font-weight:700;letter-spacing:.03em;text-transform:uppercase;color:#8b949e;border:1px solid #30363d;border-radius:5px;padding:1px 5px}',
    '.lvci-revopt:hover,.lvci-revopt.active{background:rgba(177,186,196,.12)}',
    '.lvci-revopt.sel{background:rgba(88,166,255,.16)}',
    '.lvci-revopt.sel .ms{color:#58a6ff;font-weight:600}',
    '.lvci-revpop-empty{padding:12px 10px;color:#8b949e;font-size:12.5px}',
    '@media(prefers-color-scheme:light){.lvci-revpick{color:#1f2328;background:#fff;border-color:#d0d7de;color-scheme:light}.lvci-revpick-caret{color:#57606a}.lvci-revpop{background:#fff;border-color:#d0d7de;color:#1f2328;color-scheme:light}.lvci-revpop-search{background:#f6f8fa;color:#1f2328;border-color:#d0d7de}.lvci-revpop-search::placeholder{color:#57606a}.lvci-revpop-tools{color:#57606a}.lvci-revopt .sh,.lvci-revopt .tag{color:#57606a}.lvci-revopt .tag{border-color:#d0d7de}.lvci-revopt .ms{color:#1f2328}.lvci-revopt:hover,.lvci-revopt.active{background:rgba(80,90,100,.10)}.lvci-revopt.sel{background:rgba(9,105,218,.12)}.lvci-revopt.sel .ms{color:#0969da}.lvci-revpop-empty{color:#57606a}}',
    // Settings sub-nav: the per-repo configuration sections (Configure Pipeline / VI
    // Analyzer / Unit Testing) as a tab strip in the context bar, so the settings
    // pages read as one navigable area instead of isolated pages.
    '.lvci-subnav{display:inline-flex;align-items:center;gap:2px;flex-wrap:wrap}',
    '.lvci-subnav a{display:inline-flex;align-items:center;color:#8b949e;text-decoration:none;font-size:13px;font-weight:500;padding:5px 11px;border-radius:7px;white-space:nowrap;cursor:pointer}',
    '.lvci-subnav a:hover{color:#e6edf3;background:rgba(177,186,196,.12)}',
    '.lvci-subnav a.on{color:#e6edf3;background:rgba(177,186,196,.16)}',
    '@media(prefers-color-scheme:light){.lvci-subnav a{color:#57606a}.lvci-subnav a:hover,.lvci-subnav a.on{color:#1f2328;background:rgba(80,90,100,.10)}}',
    ':root[data-lvci-theme=light] .lvci-subnav a{color:#57606a}',
    ':root[data-lvci-theme=light] .lvci-subnav a:hover,:root[data-lvci-theme=light] .lvci-subnav a.on{color:#1f2328;background:rgba(80,90,100,.10)}',
    ':root[data-lvci-theme=dark] .lvci-subnav a{color:#8b949e}',
    ':root[data-lvci-theme=dark] .lvci-subnav a:hover,:root[data-lvci-theme=dark] .lvci-subnav a.on{color:#e6edf3;background:rgba(177,186,196,.16)}',
    // Hamburger (mobile)
    '.lvci-burger{position:relative;display:none;align-items:center;justify-content:center;width:38px;height:34px;border:1px solid #30363d;border-radius:7px;background:transparent;color:inherit;cursor:pointer;flex:0 0 auto}',
    '@media(prefers-color-scheme:light){.lvci-burger{border-color:#d0d7de}}',
    '.lvci-burger svg{width:18px;height:18px;display:block}',
    // Inline glyph wrapper — sizes the SVG and lets it inherit color (currentColor)
    '.lvci-ic{display:inline-flex;align-items:center;justify-content:center;flex:0 0 auto}',
    '.lvci-ic svg{width:100%;height:100%;display:block}',
    '.lvci-btn .lvci-ic{width:15px;height:15px}',
    // ── Secondary menus: Settings / Tools dropdowns, plus the shared menu style
    '.lvci-dropdown{position:relative;display:inline-flex;align-items:center}',
    '.lvci-dropdown-menu{display:none;position:absolute;top:100%;right:0;z-index:200;background:rgba(22,27,34,.98);border:1px solid #30363d;border-radius:10px;margin-top:8px;min-width:214px;padding:6px;',
      'box-shadow:0 8px 28px rgba(1,4,9,.5);-webkit-backdrop-filter:saturate(160%) blur(8px);backdrop-filter:saturate(160%) blur(8px)}',
    '.lvci-dropdown-menu.open{display:block;animation:lvci-pop .12s ease-out}',
    '@keyframes lvci-pop{from{opacity:0;transform:translateY(-4px)}to{opacity:1;transform:none}}',
    '.lvci-dropdown-menu>a,.lvci-dropdown-menu>button{display:flex;width:100%;align-items:center;gap:10px;padding:9px 10px;background:transparent;border:0;border-radius:7px;color:#e6edf3;text-decoration:none;font-size:13px;font-weight:500;cursor:pointer;text-align:left}',
    '.lvci-dropdown-menu>a:hover,.lvci-dropdown-menu>button:hover{background:rgba(177,186,196,.12)}',
    '.lvci-dropdown-menu .lvci-ic{width:17px;height:17px;color:#8b949e}',
    '.lvci-dropdown-menu .lvci-sep{height:1px;background:#30363d;margin:5px 4px}',
    '@media(prefers-color-scheme:light){.lvci-dropdown-menu{background:rgba(255,255,255,.98);border-color:#d0d7de;box-shadow:0 8px 28px rgba(140,149,159,.32)}.lvci-dropdown-menu>a,.lvci-dropdown-menu>button{color:#1f2328}.lvci-dropdown-menu>a:hover,.lvci-dropdown-menu>button:hover{background:rgba(80,90,100,.08)}.lvci-dropdown-menu .lvci-ic{color:#57606a}.lvci-dropdown-menu .lvci-sep{background:#d0d7de}}',
    // ── Share popover: copy a deep link to (or print) exactly what's shown ────
    '.lvci-share{position:relative;display:inline-flex;align-items:center;justify-content:center;width:34px;height:34px;border:1px solid #30363d;background:transparent;color:#8b949e;border-radius:7px;cursor:pointer}',
    '.lvci-share svg{width:17px;height:17px;display:block}',
    '.lvci-share:hover{background:rgba(177,186,196,.12);color:#e6edf3}',
    '.lvci-share.open{background:rgba(177,186,196,.16);color:#e6edf3;border-color:#8b949e}',
    '@media(prefers-color-scheme:light){.lvci-share{border-color:#d0d7de;color:#57606a}.lvci-share:hover,.lvci-share.open{background:rgba(80,90,100,.08);color:#1f2328}}',
    '.lvci-share-pop{min-width:312px;max-width:360px;padding:12px}',
    '.lvci-share-h{font-size:11px;font-weight:600;letter-spacing:.04em;text-transform:uppercase;color:#8b949e;margin:2px 2px 8px}',
    '.lvci-share-url{display:block;width:100%;padding:8px 9px;border-radius:7px;border:1px solid #30363d;background:#0d1117;color:#e6edf3;font:12px/1.4 ui-monospace,Menlo,Consolas,monospace}',
    '.lvci-share-row{display:flex;flex-wrap:wrap;gap:8px;margin-top:10px}',
    '.lvci-share-row .lvci-btn{padding:7px 11px}',
    '.lvci-share-hint{font-size:11px;line-height:1.45;color:#8b949e;margin:9px 2px 0}',
    '@media(prefers-color-scheme:light){.lvci-share-h,.lvci-share-hint{color:#57606a}.lvci-share-url{background:#fff;border-color:#d0d7de;color:#1f2328}}',
    // Appearance (theme) segmented control — lives in the popover and mobile menu
    '.lvci-theme{display:flex;align-items:center;justify-content:space-between;gap:10px;padding:7px 8px 8px 10px}',
    '.lvci-theme-lbl{font-size:11px;font-weight:600;letter-spacing:.04em;text-transform:uppercase;color:#8b949e}',
    '.lvci-seg{display:inline-flex;border:1px solid #30363d;border-radius:8px;overflow:hidden;background:rgba(177,186,196,.06)}',
    '.lvci-seg-btn{display:inline-flex;align-items:center;justify-content:center;width:33px;height:27px;border:0;background:transparent;color:#8b949e;cursor:pointer;padding:0}',
    '.lvci-seg-btn+.lvci-seg-btn{border-left:1px solid #30363d}',
    '.lvci-seg-btn svg{width:15px;height:15px;display:block}',
    '.lvci-seg-btn:hover{color:#e6edf3;background:rgba(177,186,196,.12)}',
    '.lvci-seg-btn.on{background:#1f6feb;color:#fff}',
    '@media(prefers-color-scheme:light){.lvci-theme-lbl{color:#57606a}.lvci-seg{border-color:#d0d7de;background:rgba(80,90,100,.04)}.lvci-seg-btn{color:#57606a}.lvci-seg-btn+.lvci-seg-btn{border-left-color:#d0d7de}.lvci-seg-btn:hover{color:#1f2328;background:rgba(80,90,100,.08)}.lvci-seg-btn.on{background:#0969da;color:#fff}}',
    '@media(max-width:820px){.lvci-dropdown{display:none}}',
    // Status line (re-run feedback) sits just under the bar, full width
    '.lvci-status{display:none;align-items:center;gap:8px;font-size:12.5px;padding:7px 16px;border-bottom:1px solid #30363d;',
      'background:rgba(22,27,34,.96);color:#8b949e}',
    '.lvci-status.show{display:flex}',
    '.lvci-status a{color:#58a6ff;text-decoration:none}.lvci-status a:hover{text-decoration:underline}',
    '@media(prefers-color-scheme:light){.lvci-status{background:#f6f8fa;border-bottom-color:#d0d7de;color:#57606a}}',
    // Token panel (re-run needs a PAT once)
    '.lvci-tok{display:none;flex-direction:column;gap:8px;max-width:680px;margin:10px 16px;padding:12px 14px;font-size:13px;line-height:1.5;',
      'background:#161b22;border:1px solid #30363d;border-radius:10px;color:#e6edf3}',
    '.lvci-tok.show{display:flex}',
    '.lvci-tok code{background:#0d1117;padding:1px 5px;border-radius:4px}',
    '.lvci-tok input{padding:7px 9px;border-radius:7px;border:1px solid #30363d;background:#0d1117;color:#e6edf3;font-family:ui-monospace,Menlo,monospace}',
    '@media(prefers-color-scheme:light){.lvci-tok{background:#fff;border-color:#d0d7de;color:#1f2328}.lvci-tok code{background:#eef2f6}.lvci-tok input{background:#fff;border-color:#d0d7de;color:#1f2328}}',
    // Rebuild banner (dashboard) — shown while the workflow that regenerates this
    // page (or the Pages publish that follows) is in flight; links to that run.
    '.lvci-rebuild{display:none;align-items:flex-start;gap:10px;padding:10px 16px;border-bottom:1px solid #30363d;background:rgba(31,111,235,.12);color:#e6edf3;font-size:12.5px;line-height:1.5}',
    '.lvci-rebuild.show{display:flex}',
    '.lvci-rebuild .lvci-rb-spin{flex:0 0 auto;width:14px;height:14px;margin-top:3px;border:2px solid rgba(31,111,235,.35);border-top-color:#1f6feb;border-radius:50%;animation:lvci-spin .7s linear infinite}',
    '.lvci-rebuild .lvci-rb-txt{min-width:0}',
    '.lvci-rebuild .lvci-rb-sub{color:#8b949e}',
    '.lvci-rebuild a{color:#58a6ff;text-decoration:none;font-weight:600;white-space:nowrap}',
    '.lvci-rebuild a:hover{text-decoration:underline}',
    '@media(prefers-color-scheme:light){.lvci-rebuild{background:rgba(9,105,218,.09);border-bottom-color:#d0d7de;color:#1f2328}.lvci-rebuild .lvci-rb-sub{color:#57606a}.lvci-rebuild a{color:#0969da}}',
    '.lvci-depbar{display:none;align-items:flex-start;gap:10px;padding:10px 16px;border-bottom:1px solid rgba(210,153,34,.45);background:rgba(210,153,34,.14);color:#e6edf3;font-size:12.5px;line-height:1.5}',
    '.lvci-depbar.show{display:flex}',
    '.lvci-depbar .lvci-dep-spin{flex:0 0 auto;width:14px;height:14px;margin-top:3px;border:2px solid rgba(210,153,34,.38);border-top-color:#d29922;border-radius:50%;animation:lvci-spin .7s linear infinite}',
    '.lvci-depbar .lvci-dep-txt{min-width:0}',
    '.lvci-depbar .lvci-dep-sub{color:#8b949e}',
    '.lvci-depbar a{color:#f0b72f;text-decoration:none;font-weight:600;white-space:nowrap}',
    '.lvci-depbar a:hover{text-decoration:underline}',
    '@media(prefers-color-scheme:light){.lvci-depbar{background:#fff8c5;border-bottom-color:#eedc82;color:#1f2328}.lvci-depbar .lvci-dep-sub{color:#57606a}.lvci-depbar a{color:#9a6700}}',
    // Global attention bar: a dismissible red banner naming the workflow(s) whose
    // newest run failed, with a one-click link straight to the failing run.
    '.lvci-alertbar{display:none;align-items:center;gap:10px;padding:9px 16px;border-bottom:1px solid rgba(248,81,73,.4);background:rgba(248,81,73,.13);color:#e6edf3;font-size:12.5px;line-height:1.5}',
    '.lvci-alertbar.show{display:flex}',
    '.lvci-alertbar .lvci-alert-ico{flex:0 0 auto;display:inline-flex;color:#f85149}',
    '.lvci-alertbar .lvci-alert-ico svg{width:16px;height:16px}',
    '.lvci-alertbar .lvci-alert-msg{flex:1 1 auto;min-width:0}',
    '.lvci-alertbar .lvci-alert-msg code{font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;background:rgba(248,81,73,.16);padding:1px 5px;border-radius:4px}',
    '.lvci-alertbar .lvci-alert-cta{flex:0 0 auto;color:#f85149;font-weight:600;text-decoration:none;white-space:nowrap}',
    '.lvci-alertbar .lvci-alert-cta:hover{text-decoration:underline}',
    '.lvci-alertbar .lvci-alert-x{flex:0 0 auto;display:inline-flex;align-items:center;justify-content:center;width:24px;height:24px;border:0;background:transparent;color:#8b949e;cursor:pointer;border-radius:6px;padding:0}',
    '.lvci-alertbar .lvci-alert-x svg{width:15px;height:15px}',
    '.lvci-alertbar .lvci-alert-x:hover{background:rgba(248,81,73,.18);color:#e6edf3}',
    '@media(prefers-color-scheme:light){.lvci-alertbar{background:#ffebe9;border-bottom-color:#ffc1bc;color:#1f2328}.lvci-alertbar .lvci-alert-x:hover{color:#1f2328}}',
    // Update-available bar: an amber, dismissible nudge on consumer repos when the
    // source tooling is newer than the installed build, linking to What's New.
    '.lvci-updbar{display:none;align-items:center;gap:10px;padding:9px 16px;border-bottom:1px solid rgba(210,153,34,.45);background:rgba(210,153,34,.14);color:#e6edf3;font-size:12.5px;line-height:1.5}',
    '.lvci-updbar.show{display:flex}',
    '.lvci-updbar .lvci-upd-ico{flex:0 0 auto;display:inline-flex;color:#d29922}',
    '.lvci-updbar .lvci-upd-ico svg{width:16px;height:16px}',
    '.lvci-updbar .lvci-upd-msg{flex:1 1 auto;min-width:0}',
    '.lvci-updbar .lvci-upd-msg code{font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;background:rgba(210,153,34,.16);padding:1px 5px;border-radius:4px}',
    '.lvci-updbar .lvci-upd-cta{flex:0 0 auto;color:#f0b72f;font-weight:600;text-decoration:none;white-space:nowrap}',
    '.lvci-updbar .lvci-upd-cta:hover{text-decoration:underline}',
    '.lvci-updbar .lvci-upd-x{flex:0 0 auto;display:inline-flex;align-items:center;justify-content:center;width:24px;height:24px;border:0;background:transparent;color:#8b949e;cursor:pointer;border-radius:6px;padding:0}',
    '.lvci-updbar .lvci-upd-x svg{width:15px;height:15px}',
    '.lvci-updbar .lvci-upd-x:hover{background:rgba(210,153,34,.18);color:#e6edf3}',
    '@media(prefers-color-scheme:light){.lvci-updbar{background:#fff8c5;border-bottom-color:#eedc82;color:#1f2328}.lvci-updbar .lvci-upd-ico{color:#9a6700}.lvci-updbar .lvci-upd-cta{color:#9a6700}.lvci-updbar .lvci-upd-x:hover{color:#1f2328}}',
    // ── Mobile menu ───────────────────────────────────────────────────────
    '.lvci-menu{display:none}',
    '@media(max-width:820px){',
      '.lvci-nav,.lvci-actions,.lvci-hdr>.lvci-rev{display:none}',
      '.lvci-burger{display:inline-flex}',
      '.lvci-brand{flex:1 1 auto}',
      '.lvci-menu.open{display:block;position:sticky;top:var(--lvh-h);z-index:199;',
        'background:rgba(22,27,34,.98);border-bottom:1px solid #30363d;padding:8px}',
      '.lvci-menu a,.lvci-menu button.lvci-m{display:flex;width:100%;align-items:center;gap:9px;text-align:left;',
        'color:#e6edf3;background:transparent;border:0;font-size:15px;font-weight:500;padding:11px 12px;border-radius:8px;text-decoration:none;cursor:pointer}',
      '.lvci-menu a:hover,.lvci-menu button.lvci-m:hover{background:rgba(177,186,196,.12)}',
      '.lvci-menu .lvci-ic{width:19px;height:19px;color:#8b949e}',
      '.lvci-menu .lvci-theme{padding:9px 12px}',
      '.lvci-menu .lvci-rev{display:flex;flex-direction:column;align-items:stretch;gap:4px;padding:8px 12px 4px}',
      '.lvci-menu .lvci-rev select{max-width:none;width:100%;font-size:15px;padding:10px}',
      '.lvci-menu .lvci-sep{height:1px;background:#30363d;margin:6px 4px}',
      '@media(prefers-color-scheme:light){.lvci-menu.open{background:#fff;border-bottom-color:#d0d7de}.lvci-menu a,.lvci-menu button.lvci-m{color:#1f2328}.lvci-menu .lvci-sep{background:#d0d7de}}',
    '}',
    // Give the page a little breathing room below the sticky bar on small screens
    '@media(max-width:820px){body{overflow-x:hidden}}',
    // Printing (Share -> Print, or the browser's own Print): drop the chrome so a
    // printout is just the report / snapshot content, not the surrounding header.
    '@media print{.lvci-hdr,.lvci-status,.lvci-tok,.lvci-rebuild,.lvci-depbar,.lvci-alertbar,.lvci-updbar,.lvci-ctxbar,.lvci-menu,.lvci-dropdown-menu{display:none !important}}',
    // ── Manual appearance override (Appearance control in the menu) ───────────
    // "System" keeps the prefers-color-scheme rules above. Forcing light/dark
    // sets data-lvci-theme on <html>; these rules re-assert the matching tokens
    // and header surfaces at higher specificity, so the choice wins over the OS
    // setting for both the shared header AND the CSS variables every CI page uses.
    ':root[data-lvci-theme=light]{--bg:#ffffff;--surface:#f6f8fa;--border:#d0d7de;--fg:#1f2328;--fg-muted:#57606a;--link:#0969da;--hover:#f3f4f6;--row-border:#eaeef2;--accent:#1f883d;--accent-fg:#fff;--chip:#eaeef2;--code:#f6f8fa;--warn:#9a6700}',
    ':root[data-lvci-theme=dark]{--bg:#0d1117;--surface:#161b22;--border:#30363d;--fg:#e6edf3;--fg-muted:#8b949e;--link:#58a6ff;--hover:#1c2128;--row-border:#21262d;--accent:#238636;--accent-fg:#fff;--chip:#21262d;--code:#010409;--warn:#9a6700}',
    // Forced LIGHT — header surfaces
    ':root[data-lvci-theme=light] .lvci-hdr{background:rgba(255,255,255,.86);border-bottom-color:#d0d7de;color:#1f2328}',
    ':root[data-lvci-theme=light] .lvci-brand .lvci-kicker,:root[data-lvci-theme=light] .lvci-brand .lvci-sub{color:#57606a}',
    ':root[data-lvci-theme=light] .lvci-brand .lvci-sub{border-color:#d0d7de}',
    ':root[data-lvci-theme=light] .lvci-nav a:hover,:root[data-lvci-theme=light] .lvci-nav a.on{color:#1f2328;background:rgba(80,90,100,.10)}',
    ':root[data-lvci-theme=light] .lvci-btn{border-color:#d0d7de}',
    ':root[data-lvci-theme=light] .lvci-btn:hover{background:rgba(80,90,100,.08)}',
    ':root[data-lvci-theme=light] .lvci-run-chip{color:#0969da;border-color:#0969da}',
    ':root[data-lvci-theme=light] .lvci-run-chip:hover{background:rgba(9,105,218,.08)}',
    ':root[data-lvci-theme=light] .lvci-actpill{border-color:#d0d7de}',
    ':root[data-lvci-theme=light] .lvci-ap-seg.on~.lvci-ap-seg.on{border-left-color:#d0d7de}',
    ':root[data-lvci-theme=light] .lvci-ap-run{color:#0969da}',
    ':root[data-lvci-theme=light] .lvci-ap-queue{color:#57606a}',
    ':root[data-lvci-theme=light] .lvci-ap-fail{color:#cf222e}',
    ':root[data-lvci-theme=light] .lvci-ddver .lvci-ddver-tag{color:#57606a}',
    ':root[data-lvci-theme=light] .lvci-ddver.behind .lvci-ddver-tag,:root[data-lvci-theme=light] .lvci-ddver.behind .lvci-ic{color:#bf8700}',
    ':root[data-lvci-theme=light] .lvci-burger{border-color:#d0d7de}',
    ':root[data-lvci-theme=light] .lvci-share{border-color:#d0d7de;color:#57606a}',
    ':root[data-lvci-theme=light] .lvci-share:hover,:root[data-lvci-theme=light] .lvci-share.open{background:rgba(80,90,100,.08);color:#1f2328}',
    ':root[data-lvci-theme=light] .lvci-share-h,:root[data-lvci-theme=light] .lvci-share-hint{color:#57606a}',
    ':root[data-lvci-theme=light] .lvci-share-url{background:#fff;border-color:#d0d7de;color:#1f2328}',
    ':root[data-lvci-theme=light] .lvci-dropdown-menu{background:rgba(255,255,255,.98);border-color:#d0d7de;box-shadow:0 8px 28px rgba(140,149,159,.32)}',
    ':root[data-lvci-theme=light] .lvci-dropdown-menu>a,:root[data-lvci-theme=light] .lvci-dropdown-menu>button{color:#1f2328}',
    ':root[data-lvci-theme=light] .lvci-dropdown-menu>a:hover,:root[data-lvci-theme=light] .lvci-dropdown-menu>button:hover{background:rgba(80,90,100,.08)}',
    ':root[data-lvci-theme=light] .lvci-dropdown-menu .lvci-ic{color:#57606a}',
    ':root[data-lvci-theme=light] .lvci-dropdown-menu .lvci-sep,:root[data-lvci-theme=light] .lvci-menu .lvci-sep{background:#d0d7de}',
    ':root[data-lvci-theme=light] .lvci-theme-lbl{color:#57606a}',
    ':root[data-lvci-theme=light] .lvci-seg{border-color:#d0d7de;background:rgba(80,90,100,.04)}',
    ':root[data-lvci-theme=light] .lvci-seg-btn{color:#57606a}',
    ':root[data-lvci-theme=light] .lvci-seg-btn+.lvci-seg-btn{border-left-color:#d0d7de}',
    ':root[data-lvci-theme=light] .lvci-seg-btn:hover{color:#1f2328;background:rgba(80,90,100,.08)}',
    ':root[data-lvci-theme=light] .lvci-seg-btn.on{background:#0969da;color:#fff}',
    ':root[data-lvci-theme=light] .lvci-menu .lvci-ic{color:#57606a}',
    ':root[data-lvci-theme=light] .lvci-rev .lvci-revlbl{color:#57606a}',
    ':root[data-lvci-theme=light] .lvci-rev select{color:#1f2328;background:#fff;border-color:#d0d7de;color-scheme:light}',
    ':root[data-lvci-theme=light] .lvci-ctxbar{background:rgba(246,248,250,.96);border-bottom-color:#d0d7de}',
    ':root[data-lvci-theme=light] .lvci-rev-step{border-color:#d0d7de;color:#57606a}',
    ':root[data-lvci-theme=light] .lvci-rev-step:hover:not(:disabled){color:#1f2328;background:rgba(80,90,100,.10)}',
    ':root[data-lvci-theme=light] .lvci-revpick{color:#1f2328;background:#fff;border-color:#d0d7de;color-scheme:light}',
    ':root[data-lvci-theme=light] .lvci-revpick-caret,:root[data-lvci-theme=light] .lvci-revopt .sh,:root[data-lvci-theme=light] .lvci-revpop-tools,:root[data-lvci-theme=light] .lvci-revpop-empty{color:#57606a}',
    ':root[data-lvci-theme=light] .lvci-revpop{background:#fff;border-color:#d0d7de;color:#1f2328;color-scheme:light}',
    ':root[data-lvci-theme=light] .lvci-revpop-search{background:#f6f8fa;color:#1f2328;border-color:#d0d7de}',
    ':root[data-lvci-theme=light] .lvci-revopt .ms{color:#1f2328}',
    ':root[data-lvci-theme=light] .lvci-revopt.sel{background:rgba(9,105,218,.12)}',
    ':root[data-lvci-theme=light] .lvci-revopt.sel .ms{color:#0969da}',
    ':root[data-lvci-theme=light] .lvci-status{background:#f6f8fa;border-bottom-color:#d0d7de;color:#57606a}',
    ':root[data-lvci-theme=light] .lvci-tok{background:#fff;border-color:#d0d7de;color:#1f2328}',
    ':root[data-lvci-theme=light] .lvci-tok code{background:#eef2f6}',
    ':root[data-lvci-theme=light] .lvci-tok input{background:#fff;border-color:#d0d7de;color:#1f2328}',
    ':root[data-lvci-theme=light] .lvci-menu.open{background:#fff;border-bottom-color:#d0d7de}',
    ':root[data-lvci-theme=light] .lvci-menu a,:root[data-lvci-theme=light] .lvci-menu button.lvci-m{color:#1f2328}',
    ':root[data-lvci-theme=light] .lvci-rebuild{background:rgba(9,105,218,.09);border-bottom-color:#d0d7de;color:#1f2328}',
    ':root[data-lvci-theme=light] .lvci-rebuild .lvci-rb-sub{color:#57606a}',
    ':root[data-lvci-theme=light] .lvci-rebuild a{color:#0969da}',
    ':root[data-lvci-theme=light] .lvci-depbar{background:#fff8c5;border-bottom-color:#eedc82;color:#1f2328}',
    ':root[data-lvci-theme=light] .lvci-depbar .lvci-dep-sub{color:#57606a}',
    ':root[data-lvci-theme=light] .lvci-depbar a{color:#9a6700}',
    ':root[data-lvci-theme=light] .lvci-alertbar{background:#ffebe9;border-bottom-color:#ffc1bc;color:#1f2328}',
    ':root[data-lvci-theme=light] .lvci-alertbar .lvci-alert-x:hover{color:#1f2328}',
    ':root[data-lvci-theme=light] .lvci-updbar{background:#fff8c5;border-bottom-color:#eedc82;color:#1f2328}',
    ':root[data-lvci-theme=light] .lvci-updbar .lvci-upd-ico{color:#9a6700}',
    ':root[data-lvci-theme=light] .lvci-updbar .lvci-upd-cta{color:#9a6700}',
    ':root[data-lvci-theme=light] .lvci-updbar .lvci-upd-x:hover{color:#1f2328}',
    // Forced DARK — counteract an OS light preference
    ':root[data-lvci-theme=dark] .lvci-alertbar{background:rgba(248,81,73,.13);border-bottom-color:rgba(248,81,73,.4);color:#e6edf3}',
    ':root[data-lvci-theme=dark] .lvci-updbar{background:rgba(210,153,34,.14);border-bottom-color:rgba(210,153,34,.45);color:#e6edf3}',
    ':root[data-lvci-theme=dark] .lvci-hdr{background:rgba(22,27,34,.86);border-bottom-color:#30363d;color:#e6edf3}',
    ':root[data-lvci-theme=dark] .lvci-brand .lvci-kicker,:root[data-lvci-theme=dark] .lvci-brand .lvci-sub{color:#8b949e}',
    ':root[data-lvci-theme=dark] .lvci-brand .lvci-sub{border-color:#30363d}',
    ':root[data-lvci-theme=dark] .lvci-nav a:hover,:root[data-lvci-theme=dark] .lvci-nav a.on{color:#e6edf3;background:rgba(177,186,196,.16)}',
    ':root[data-lvci-theme=dark] .lvci-btn{border-color:#30363d}',
    ':root[data-lvci-theme=dark] .lvci-btn:hover{background:rgba(177,186,196,.12)}',
    ':root[data-lvci-theme=dark] .lvci-run-chip{color:#1f6feb;border-color:#1f6feb}',
    ':root[data-lvci-theme=dark] .lvci-run-chip:hover{background:rgba(31,111,235,.12)}',
    ':root[data-lvci-theme=dark] .lvci-actpill{border-color:#30363d}',
    ':root[data-lvci-theme=dark] .lvci-ap-seg.on~.lvci-ap-seg.on{border-left-color:#30363d}',
    ':root[data-lvci-theme=dark] .lvci-ap-run{color:#1f6feb}',
    ':root[data-lvci-theme=dark] .lvci-ap-queue{color:#8b949e}',
    ':root[data-lvci-theme=dark] .lvci-ap-fail{color:#f85149}',
    ':root[data-lvci-theme=dark] .lvci-ddver .lvci-ddver-tag{color:#8b949e}',
    ':root[data-lvci-theme=dark] .lvci-ddver.behind .lvci-ddver-tag,:root[data-lvci-theme=dark] .lvci-ddver.behind .lvci-ic{color:#d29922}',
    ':root[data-lvci-theme=dark] .lvci-burger{border-color:#30363d}',
    ':root[data-lvci-theme=dark] .lvci-share{border-color:#30363d;color:#8b949e}',
    ':root[data-lvci-theme=dark] .lvci-share:hover,:root[data-lvci-theme=dark] .lvci-share.open{background:rgba(177,186,196,.12);color:#e6edf3}',
    ':root[data-lvci-theme=dark] .lvci-share-h,:root[data-lvci-theme=dark] .lvci-share-hint{color:#8b949e}',
    ':root[data-lvci-theme=dark] .lvci-share-url{background:#0d1117;border-color:#30363d;color:#e6edf3}',
    ':root[data-lvci-theme=dark] .lvci-dropdown-menu{background:rgba(22,27,34,.98);border-color:#30363d;box-shadow:0 8px 28px rgba(1,4,9,.5)}',
    ':root[data-lvci-theme=dark] .lvci-dropdown-menu>a,:root[data-lvci-theme=dark] .lvci-dropdown-menu>button{color:#e6edf3}',
    ':root[data-lvci-theme=dark] .lvci-dropdown-menu>a:hover,:root[data-lvci-theme=dark] .lvci-dropdown-menu>button:hover{background:rgba(177,186,196,.12)}',
    ':root[data-lvci-theme=dark] .lvci-dropdown-menu .lvci-ic{color:#8b949e}',
    ':root[data-lvci-theme=dark] .lvci-dropdown-menu .lvci-sep,:root[data-lvci-theme=dark] .lvci-menu .lvci-sep{background:#30363d}',
    ':root[data-lvci-theme=dark] .lvci-theme-lbl{color:#8b949e}',
    ':root[data-lvci-theme=dark] .lvci-seg{border-color:#30363d;background:rgba(177,186,196,.06)}',
    ':root[data-lvci-theme=dark] .lvci-seg-btn{color:#8b949e}',
    ':root[data-lvci-theme=dark] .lvci-seg-btn+.lvci-seg-btn{border-left-color:#30363d}',
    ':root[data-lvci-theme=dark] .lvci-seg-btn:hover{color:#e6edf3;background:rgba(177,186,196,.12)}',
    ':root[data-lvci-theme=dark] .lvci-seg-btn.on{background:#1f6feb;color:#fff}',
    ':root[data-lvci-theme=dark] .lvci-menu .lvci-ic{color:#8b949e}',
    ':root[data-lvci-theme=dark] .lvci-rev .lvci-revlbl{color:#8b949e}',
    ':root[data-lvci-theme=dark] .lvci-rev select{color:#e6edf3;background:rgba(177,186,196,.10);border-color:#30363d;color-scheme:dark}',
    ':root[data-lvci-theme=dark] .lvci-ctxbar{background:rgba(22,27,34,.96);border-bottom-color:#30363d}',
    ':root[data-lvci-theme=dark] .lvci-rev-step{border-color:#30363d;color:#8b949e}',
    ':root[data-lvci-theme=dark] .lvci-revpick{color:#e6edf3;background:rgba(177,186,196,.10);border-color:#30363d;color-scheme:dark}',
    ':root[data-lvci-theme=dark] .lvci-revpop{background:#161b22;border-color:#30363d;color:#e6edf3;color-scheme:dark}',
    ':root[data-lvci-theme=dark] .lvci-revpop-search{background:rgba(177,186,196,.08);color:#e6edf3;border-color:#30363d}',
    ':root[data-lvci-theme=dark] .lvci-revopt .ms{color:#e6edf3}',
    ':root[data-lvci-theme=dark] .lvci-revopt.sel{background:rgba(88,166,255,.16)}',
    ':root[data-lvci-theme=dark] .lvci-revopt.sel .ms{color:#58a6ff}',
    ':root[data-lvci-theme=dark] .lvci-status{background:rgba(22,27,34,.96);border-bottom-color:#30363d;color:#8b949e}',
    ':root[data-lvci-theme=dark] .lvci-tok{background:#161b22;border-color:#30363d;color:#e6edf3}',
    ':root[data-lvci-theme=dark] .lvci-tok code{background:#0d1117}',
    ':root[data-lvci-theme=dark] .lvci-tok input{background:#0d1117;border-color:#30363d;color:#e6edf3}',
    ':root[data-lvci-theme=dark] .lvci-menu.open{background:rgba(22,27,34,.98);border-bottom-color:#30363d}',
    ':root[data-lvci-theme=dark] .lvci-rebuild{background:rgba(31,111,235,.12);border-bottom-color:#30363d;color:#e6edf3}',
    ':root[data-lvci-theme=dark] .lvci-rebuild .lvci-rb-sub{color:#8b949e}',
    ':root[data-lvci-theme=dark] .lvci-rebuild a{color:#58a6ff}',
    ':root[data-lvci-theme=dark] .lvci-depbar{background:rgba(210,153,34,.14);border-bottom-color:rgba(210,153,34,.45);color:#e6edf3}',
    ':root[data-lvci-theme=dark] .lvci-depbar .lvci-dep-sub{color:#8b949e}',
    ':root[data-lvci-theme=dark] .lvci-depbar a{color:#f0b72f}',
    ':root[data-lvci-theme=dark] .lvci-menu a,:root[data-lvci-theme=dark] .lvci-menu button.lvci-m{color:#e6edf3}'
  ].join('\n');

  // ── Inline brand mark (a flow/analysis glyph) ─────────────────────────────
  var BRAND_SVG =
    '<svg viewBox="0 0 24 24" fill="none" aria-hidden="true">' +
      '<rect x="2.5" y="2.5" width="19" height="19" rx="5" stroke="#1f6feb" stroke-width="1.7"/>' +
      '<circle cx="8" cy="8" r="1.9" fill="#1f6feb"/>' +
      '<circle cx="16" cy="8" r="1.9" fill="#2ea043"/>' +
      '<circle cx="12" cy="16" r="1.9" fill="#d29922"/>' +
      '<path d="M8 8.6v3.2a1.6 1.6 0 0 0 1.6 1.6h4.8A1.6 1.6 0 0 0 16 11.8V8.6" stroke="#8b949e" stroke-width="1.5" stroke-linecap="round"/>' +
      '<path d="M12 13.4v1.1" stroke="#8b949e" stroke-width="1.5" stroke-linecap="round"/>' +
    '</svg>';
  // Crisp, stroke-based glyphs (Lucide-style). Sized via CSS; inherit color via
  // currentColor so they adapt to dark/light and hover states automatically.
  var ICON = {
    burger: '<svg viewBox="0 0 24 24" fill="none"><path d="M4 7h16M4 12h16M4 17h16" stroke="currentColor" stroke-width="2" stroke-linecap="round"/></svg>',
    more: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="5" r="1.3"/><circle cx="12" cy="12" r="1.3"/><circle cx="12" cy="19" r="1.3"/></svg>',
    integrate: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="9.5"/><line x1="12" y1="8" x2="12" y2="16"/><line x1="8" y1="12" x2="16" y2="12"/></svg>',
    configure: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><line x1="4" y1="21" x2="4" y2="14"/><line x1="4" y1="10" x2="4" y2="3"/><line x1="12" y1="21" x2="12" y2="12"/><line x1="12" y1="8" x2="12" y2="3"/><line x1="20" y1="21" x2="20" y2="16"/><line x1="20" y1="12" x2="20" y2="3"/><line x1="1.5" y1="14" x2="6.5" y2="14"/><line x1="9.5" y1="8" x2="14.5" y2="8"/><line x1="17.5" y1="16" x2="22.5" y2="16"/></svg>',
    vibrowser: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="4" width="18" height="14" rx="2"/><circle cx="8.5" cy="9" r="1.5"/><path d="M21 15l-4.5-4.5L7 19"/></svg>',
    vianalyzer: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="10.5" cy="10.5" r="6.5"/><path d="M15.5 15.5 21 21"/><path d="M7.8 10.6l2 2 3.2-3.6"/></svg>',
    builds: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 16V8a2 2 0 0 0-1-1.7l-7-4a2 2 0 0 0-2 0l-7 4A2 2 0 0 0 3 8v8a2 2 0 0 0 1 1.7l7 4a2 2 0 0 0 2 0l7-4A2 2 0 0 0 21 16z"/><path d="M3.3 7L12 12l8.7-5"/><path d="M12 22V12"/></svg>',
    sbom: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><path d="M14 2v6h6"/><path d="M8 13h8M8 17h8M8 9h2"/></svg>',
    update: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><path d="M7 10l5 5 5-5"/><line x1="12" y1="15" x2="12" y2="3"/></svg>',
    about: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="9.5"/><line x1="12" y1="16" x2="12" y2="11.5"/><line x1="12" y1="8" x2="12.01" y2="8"/></svg>',
    clients: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M17 20v-1.5a3.5 3.5 0 0 0-3.5-3.5h-6A3.5 3.5 0 0 0 4 18.5V20"/><circle cx="10.5" cy="8" r="3.5"/><path d="M21 20v-1.5a3.5 3.5 0 0 0-2.6-3.4"/><path d="M15.5 4.6a3.5 3.5 0 0 1 0 6.8"/></svg>',
    docs: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M4 4.5A1.5 1.5 0 0 1 5.5 3H18a1 1 0 0 1 1 1v15.5"/><path d="M6 17h13v3.5a.5.5 0 0 1-.5.5H6.5A1.5 1.5 0 0 1 5 19.5v-14"/><path d="M8 7.5h7M8 11h7"/></svg>',
    news: '<svg viewBox="0 0 24 24" fill="currentColor" stroke="none"><path d="M11.5 3l1.8 5.2 5.2 1.8-5.2 1.8L11.5 17l-1.8-5.2L4.5 10l5.2-1.8z"/><path d="M18 14l.8 2.2 2.2.8-2.2.8-.8 2.2-.8-2.2-2.2-.8 2.2-.8z"/></svg>',
    history: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M3 3v5h5"/><path d="M3.05 13A9 9 0 1 0 6 5.3L3 8"/><path d="M12 7v5l4 2"/></svg>',
    tests: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M9 3h6"/><path d="M10 3v6L5.4 17.5A1.5 1.5 0 0 0 6.7 20h10.6a1.5 1.5 0 0 0 1.3-2.5L14 9V3"/><line x1="7.5" y1="14" x2="16.5" y2="14"/></svg>',
    sun: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="4"/><path d="M12 1.8v2.4M12 19.8v2.4M4.2 4.2l1.7 1.7M18.1 18.1l1.7 1.7M1.8 12h2.4M19.8 12h2.4M4.2 19.8l1.7-1.7M18.1 5.9l1.7-1.7"/></svg>',
    moon: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 12.8A9 9 0 1 1 11.2 3a7 7 0 0 0 9.8 9.8z"/></svg>',
    system: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="2.5" y="3.5" width="19" height="13" rx="2"/><line x1="8.5" y1="20.5" x2="15.5" y2="20.5"/><line x1="12" y1="16.5" x2="12" y2="20.5"/></svg>',
    debug: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="2.5" y="3.5" width="19" height="13" rx="2"/><path d="M10 8.5l4 2.5-4 2.5z" fill="currentColor" stroke="none"/><line x1="8.5" y1="20.5" x2="15.5" y2="20.5"/><line x1="12" y1="16.5" x2="12" y2="20.5"/></svg>',
    share: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="18" cy="5" r="3"/><circle cx="6" cy="12" r="3"/><circle cx="18" cy="19" r="3"/><line x1="8.6" y1="13.5" x2="15.4" y2="17.5"/><line x1="15.4" y1="6.5" x2="8.6" y2="10.5"/></svg>',
    copy: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="9" y="9" width="11" height="11" rx="2"/><path d="M5 15V5a2 2 0 0 1 2-2h8"/></svg>',
    external: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M15 3h6v6"/><path d="M10 14 21 3"/><path d="M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6"/></svg>',
    printer: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M6 9V3h12v6"/><path d="M6 18H4a2 2 0 0 1-2-2v-3a2 2 0 0 1 2-2h16a2 2 0 0 1 2 2v3a2 2 0 0 1-2 2h-2"/><rect x="6" y="14" width="12" height="7" rx="1"/></svg>',
    check: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="M20 6 9 17l-5-5"/></svg>'
  };

  function esc(s) {
    return String(s == null ? '' : s).replace(/[&<>"']/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
    });
  }

  // Leading glyph for an action: a sized SVG wrapper (a.svg) or, as a fallback,
  // an escaped text/emoji glyph (a.icon). SVG strings are our own trusted markup.
  function iconHtml(a) {
    if (a.svg) return '<span class="lvci-ic">' + a.svg + '</span>';
    return a.icon ? esc(a.icon) + ' ' : '';
  }

  // ── Appearance: dark / light / system ─────────────────────────────────────
  // Persisted per browser. "system" (the default) follows prefers-color-scheme;
  // light/dark set data-lvci-theme on <html> so our overrides win over the OS
  // setting for the shared header AND every CI page's standard CSS tokens.
  var THEME_KEY = 'lvci-theme';
  function getTheme() {
    try { var t = localStorage.getItem(THEME_KEY); return (t === 'light' || t === 'dark') ? t : 'system'; }
    catch (e) { return 'system'; }
  }
  function applyTheme(t) {
    var r = document.documentElement;
    if (t === 'light' || t === 'dark') { r.setAttribute('data-lvci-theme', t); r.style.colorScheme = t; }
    else { r.removeAttribute('data-lvci-theme'); r.style.colorScheme = 'light dark'; }
  }
  function markThemeControls(t) {
    var btns = document.querySelectorAll('.lvci-seg-btn');
    for (var i = 0; i < btns.length; i++) btns[i].classList.toggle('on', btns[i].getAttribute('data-theme') === t);
  }
  function setTheme(t) {
    try { localStorage.setItem(THEME_KEY, t); } catch (e) {}
    applyTheme(t);
    markThemeControls(t);
  }
  // A compact segmented control (Light / Dark / System) for the menus.
  function themeControl() {
    var wrap = document.createElement('div'); wrap.className = 'lvci-theme';
    var lbl = document.createElement('span'); lbl.className = 'lvci-theme-lbl'; lbl.textContent = 'Appearance';
    var seg = document.createElement('div'); seg.className = 'lvci-seg';
    seg.setAttribute('role', 'group'); seg.setAttribute('aria-label', 'Appearance');
    var cur = getTheme();
    [['light', ICON.sun, 'Light'], ['dark', ICON.moon, 'Dark'], ['system', ICON.system, 'System']].forEach(function (o) {
      var b = document.createElement('button');
      b.type = 'button';
      b.className = 'lvci-seg-btn' + (o[0] === cur ? ' on' : '');
      b.setAttribute('data-theme', o[0]);
      b.title = o[2]; b.setAttribute('aria-label', o[2] + ' appearance');
      b.innerHTML = o[1];
      b.addEventListener('click', function (e) { e.stopPropagation(); setTheme(o[0]); });
      seg.appendChild(b);
    });
    wrap.appendChild(lbl); wrap.appendChild(seg);
    return wrap;
  }
  // Honour the saved choice as early as the script runs (minimises any flash).
  applyTheme(getTheme());

  // ── Primary navigation (the durable site sections). Data-driven so future
  //    capabilities — Builds, Documentation, Unit Tests — are a one-line add. ─
  var NAV = [
    { key: 'dependencies', label: 'Dependencies', href: navBase + '/dependencies.html' },
    // Developer documentation. `doc: true` resolves the href/target at render
    // time from docUrl() / docExternal() (the canonical source site's
    // documentation.html, opened in a new tab from a consumer dashboard) -- the
    // same source-relative rule the About link uses.
    { key: 'docs', label: 'Documentation', doc: true }
    // Future (uncomment / extend as capabilities land):
    // { key: 'builds', label: 'Builds', href: navBase + '/builds/', soon: true }
  ];
  // Which nav item is "current" for each context (drives the active pill).
  var NAV_ACTIVE = {
    'dashboard': 'dashboard',
    'vi-browser': 'dashboard',
    'dependencies': 'dependencies',
    'vi-analyzer-report': 'dashboard',
    'masscompile-report': 'dashboard',
    'unit-tests-report': 'dashboard',
    'antidoc-report': 'dashboard',
    'builds-report': 'dashboard',
    'sbom-report': 'dashboard',
    'unit-tests-config': 'settings',
    'worker-manifest': 'dashboard',
    'report-viewer': 'dashboard',
    'configure': 'settings',
    'vianalyzer': 'settings',
    'builds': 'settings',
    'integrate': '',
    'whats-new': 'tools',
    'faq': 'tools',
    'documentation': 'docs',
    'clients': 'tools'
  };

  // ── Per-revision DOCUMENT types ───────────────────────────────────────────
  // Every report that is produced for a specific commit is described here once.
  // This drives, for that context: the "Regenerate … for this revision" action
  // (dispatches `workflow[platform]` with inputs.commit_sha), the revision
  // <select> in the header (a page lives at `<prefix>/<sha>/index.html`, and a
  // report is "available" when `<prefix>/<sha>/summary.json` exists), the
  // dashboard's optimistic queued bridge key (`cap|sha`), and the status text.
  // Adding a FUTURE document type is a single entry here + emitting the same
  // window.LVCI config from its generator — no other change to this file.
  var DOCTYPES = {
    'vi-analyzer-report': {
      prefix: 'vi-analyzer', cap: 'vi-analyzer', label: 'VI Analyzer',
      regenLabel: 'Re-run analysis', rawLabel: 'Native report', rawName: 'raw.html',
      workflow: { windows: 'run-vi-analyzer-windows-container.yml',
                  linux:   'run-vi-analyzer-linux-container.yml' }
    },
    'masscompile-report': {
      prefix: 'masscompile', cap: 'masscompile', label: 'Mass Compile',
      regenLabel: 'Regenerate report', rawLabel: 'Raw log', rawName: 'masscompile.log',
      workflow: { windows: 'masscompile-windows-container.yml',
                  linux:   'masscompile-linux-container.yml' }
    },
    'unit-tests-report': {
      prefix: 'unit-tests', cap: 'unit-tests', label: 'Unit Tests',
      regenLabel: 'Re-run tests', rawLabel: 'Test results (JSON)', rawName: 'results.json',
      workflow: { windows: 'unit-tests-windows-container.yml',
                  linux:   'unit-tests-linux-container.yml' }
    },
    'antidoc-report': {
      prefix: 'antidoc', cap: 'antidoc', label: 'Antidoc',
      regenLabel: 'Regenerate docs', rawLabel: 'Run log', rawName: 'antidoc.log',
      workflow: { windows: 'run-antidoc-windows-container.yml' }
    },
    'builds-report': {
      prefix: 'builds', cap: 'builds', label: 'Builds',
      regenLabel: 'Rebuild', rawLabel: 'Build log', rawName: 'builds.log',
      workflow: { windows: 'build-binaries-windows-container.yml',
                  linux:   'build-binaries-linux-container.yml' }
    },
    'sbom-report': {
      prefix: 'sbom', cap: 'sbom-generation', label: 'SBOM',
      regenLabel: 'Regenerate SBOM', rawLabel: 'CycloneDX JSON', rawName: 'results/sbom.json',
      probeName: 'results.json',
      workflow: { windows: 'generate-sbom-windows-container.yml' }
    }
  };
  var DOC = DOCTYPES[ctx] || null;   // non-null only on a per-revision report

  // Order the per-revision activities appear in the context-bar Activity picker
  // (the report half of the unified Activity switcher; per-VI lenses join later).
  var LENS_ORDER = ['snapshots', 'masscompile-report', 'vi-analyzer-report', 'unit-tests-report', 'antidoc-report', 'builds-report', 'sbom-report'];

  var SHA_RE = /^[0-9a-f]{7,40}$/i;
  var revisionListCache = {};
  function jget(u) { return fetch(u, { cache: 'no-cache' }).then(function (r) { return r.ok ? r.json() : null; }).catch(function () { return null; }); }
  function currentRevisionSha() {
    if (cfg.sha && SHA_RE.test(cfg.sha)) return cfg.sha;
    try {
      var q = new URLSearchParams(location.search).get('sha');
      if (q && SHA_RE.test(q)) return q;
    } catch (e) {}
    try {
      var sel = document.getElementById('commit-select');
      var v = sel && sel.value;
      if (v && SHA_RE.test(v)) return v;
    } catch (e2) {}
    return '';
  }
  function loadRevisionList(root) {
    root = trimSlash(root || base);
    if (revisionListCache[root]) return revisionListCache[root];
    revisionListCache[root] = Promise.all([jget(root + '/vi-snapshots/files.json'), jget(root + '/vi-snapshots/commits.json')]).then(function (res) {
      var filesDoc = res[0], snap = res[1];
      var fileCommits = (filesDoc && Array.isArray(filesDoc.commits)) ? filesDoc.commits : [];
      var fileShas = {}; fileCommits.forEach(function (c) { if (c && c.sha) fileShas[c.sha] = 1; });
      var order = [], bySha = {};
      var put = function (c) { if (!c || !c.sha) return; var p = bySha[c.sha] || {}; for (var k in c) if (c[k] != null) p[k] = c[k]; bySha[c.sha] = p; if (order.indexOf(c.sha) < 0) order.push(c.sha); };
      (Array.isArray(snap) ? snap : []).forEach(function (c) { if (!fileShas[c.sha]) put(c); });
      fileCommits.forEach(function (c) { put({ sha: c.sha, short: c.short, message: c.message, author: c.author, date: c.date, dep_only: c.dep_only }); });
      var list = order.map(function (s) { return bySha[s]; }).filter(function (c) { return c && c.sha; });
      var cur = currentRevisionSha();
      if (cur && !bySha[cur]) list.unshift({ sha: cur, short: cur.slice(0, 7), message: '' });
      return list;
    }).catch(function () { return []; });
    return revisionListCache[root];
  }
  function resolveRevisionSha(root) {
    var cur = currentRevisionSha();
    if (cur) return Promise.resolve(cur);
    return loadRevisionList(root).then(function (list) { return (list[0] && list[0].sha) || ''; });
  }
  function activityDest(key, sha, root) {
    root = trimSlash(root || base);
    if (key === 'snapshots') return root + '/vi-snapshots/' + (sha ? '?sha=' + encodeURIComponent(sha) : '');
    var d = DOCTYPES[key]; if (!d || !sha) return root + '/';
        var rel = '../' + d.prefix + '/' + sha + '/index.html';
    var title = d.label + ' \u00b7 ' + sha.slice(0, 7);
    return root + '/report/index.html?type=' + encodeURIComponent(key)
         + '&sha=' + encodeURIComponent(sha)
         + (cfg.platform ? '&platform=' + encodeURIComponent(cfg.platform) : '')
          + '&src=' + encodeURIComponent(rel)
         + '&title=' + encodeURIComponent(title);
  }
  function wireActivityLink(el, key, root, close) {
    root = trimSlash(root || base);
    el.href = activityDest(key, currentRevisionSha(), root);
    resolveRevisionSha(root).then(function (sha) { if (sha) el.href = activityDest(key, sha, root); });
    el.addEventListener('click', function (e) {
      if (e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return;
      e.preventDefault();
      if (close) close();
      resolveRevisionSha(root).then(function (sha) { window.location.href = activityDest(key, sha, root); });
    });
  }

  // ── Context actions (surfaced on the right; collapse into the mobile menu).
  //    Each action: {label, kind|href, primary|accent, newTab}. `kind` triggers
  //    behavior owned by this header (configure/integrate modal-or-navigate,
  //    rerun dispatch); `href` is a plain link. ──────────────────────────────
  function buildActions() {
    var commitUrl = (repo && cfg.sha) ? ('https://github.com/' + repo + '/commit/' + cfg.sha) : '';
    // Per-revision reports (VI Analyzer, Mass Compile, …) share one action set,
    // driven by the DOCTYPES entry: Regenerate (dispatch this revision's run),
    // the native artifact (raw report / raw log), and the commit on GitHub.
    if (DOC) {
      // The native artifact (raw report / log) sits beside the report's own
      // index.html. When this header is the framed viewer chrome, derive that
      // path from the embedded source so it resolves regardless of platform or
      // depth; otherwise fall back to the report's own rawUrl (set by its
      // generator on the bare report page).
      var rawHref = cfg.rawUrl;
      if (cfg.embedded && cfg.framedSrc && DOC.rawName) rawHref = cfg.framedSrc.replace(/[^\/]*$/, DOC.rawName);
      return [
        { label: DOC.regenLabel, icon: '\u21bb', kind: 'rerun', accent: true },
        rawHref ? { label: DOC.rawLabel, icon: '\u2197', href: rawHref, newTab: true } : null,
        commitUrl ? { label: 'This commit', icon: '\u2197', href: commitUrl, newTab: true } : null
      ].filter(Boolean);
    }
    var A = {
      'dashboard': [
        { label: 'Apply to New Repo', svg: ICON.integrate, kind: 'integrate', primary: true }
      ],
      'worker-manifest': [],
      'vi-browser': [],
      'report-viewer': [],
      'configure': [],
      'integrate': [],
      'whats-new': [],
      'faq': []
    };
    return (A[ctx] || []).filter(Boolean);
  }
  function buildSecondaryActions() {
    // One canonical Settings/Tools menu, IDENTICAL across every primary-chrome
    // context so navigation is consistent everywhere - not just on the dashboard.
    // The Settings vs Tools split is applied later by the nav renderer
    // (SETTINGS_KINDS); Populate history + VI Browser renders fall under Tools.
    // "Populate history" works from any page: on the dashboard it opens the
    // dialog inline; on every other page runHistory() routes to the dashboard and
    // opens it there (see runHistory()).
    return [
      { label: 'Populate history', svg: ICON.history, kind: 'runhistory' },
      { label: 'Configure Pipeline', svg: ICON.configure, kind: 'configure' },
      { label: 'VI Analyzer', svg: ICON.vianalyzer, kind: 'vianalyzer' },
      { label: 'Unit Testing', svg: ICON.tests, kind: 'unittests' },
      { label: 'Builds', svg: ICON.builds, kind: 'builds' },
      { label: 'VI Browser renders', svg: ICON.vibrowser, kind: 'vibrowser' },
      { label: 'Debug Run', svg: ICON.debug, kind: 'debugrun' },
      { label: 'Clients', svg: ICON.clients, href: base + '/clients.html', source: true },
      { label: 'About', svg: ICON.about, href: aboutUrl(), about: true, newTab: aboutExternal() }
    ].filter(Boolean);
  }
  function buildDashboardNavItems() {
    return [
      { label: 'Overview', href: navBase + '/' },
      { label: 'VI Browser', svg: ICON.vibrowser, activity: 'snapshots' },
      { label: 'Mass Compile', svg: ICON.configure, activity: 'masscompile-report' },
      { label: 'VI Analyzer', svg: ICON.vianalyzer, activity: 'vi-analyzer-report' },
      { label: 'Unit Tests', svg: ICON.tests, activity: 'unit-tests-report' },
      { label: 'Antidoc', svg: ICON.docs, activity: 'antidoc-report' },
      { label: 'Builds', svg: ICON.builds, activity: 'builds-report' },
      { label: 'SBOM', svg: ICON.sbom, activity: 'sbom-report' }
    ];
  }

  // ── The canonical tooling site's Pages URL, derived from owner/repo the same
  //    way the rest of the dashboard does (clients.html, integrate.html): a
  //    user/org pages repo (<owner>.github.io) serves at the bare host; any other
  //    repo is a project page under /<repo>/. Empty if the source is unknown. ──
  // Derive any owner/name repo's GitHub Pages root: a user/org pages repo
  // (<owner>.github.io) serves at the bare host; any other repo is a project page
  // under /<name>/. Empty when the repo is unknown. (Hoisted — used by navBase.)
  function pagesUrlForRepo(r) {
    var p = String(r || '').split('/');
    var owner = p[0] || '', name = p[1] || '';
    if (!owner || !name) return '';
    var host = owner.toLowerCase() + '.github.io';
    return name.toLowerCase() === host ? ('https://' + host + '/')
                                       : ('https://' + host + '/' + name + '/');
  }
  function sourcePagesUrl() {
    var p = String(srcRepo || '').split('/');
    var owner = p[0] || '', name = p[1] || '';
    if (!owner || !name) return '';
    var host = owner.toLowerCase() + '.github.io';
    return name.toLowerCase() === host ? ('https://' + host + '/')
                                       : ('https://' + host + '/' + name + '/');
  }

  // ── About link: the About/FAQ page (faq.html) lives ONLY on the canonical
  //    source site — it is never staged onto consumer dashboards — so the menu
  //    entry must point at the root tooling's copy, derived from srcRepo (which
  //    loadVersion refines from the catalog + the source.json relocation pointer)
  //    rather than this site's own base. Falls back to the local base only if the
  //    source is somehow unknown. aboutExternal() is true on a consumer site (the
  //    target is a different Pages site) so the link opens in a new tab, keeping
  //    the user's own dashboard open — the same consumer→root rule Apply uses.
  function aboutUrl() {
    var su = sourcePagesUrl();
    return su ? su + 'faq.html' : base + '/faq.html';
  }
  function aboutExternal() {
    var su = trimSlash(sourcePagesUrl()).toLowerCase();
    return !!su && su !== base.toLowerCase();
  }

  // ── Documentation link: like the About/FAQ page, the long-form Documentation
  //    page (documentation.html) lives ONLY on the canonical source site, so the
  //    menu entry points at the root tooling's copy (derived from srcRepo) rather
  //    than this site's own base, and opens in a new tab from a consumer site.
  function docUrl() {
    var su = sourcePagesUrl();
    return su ? su + 'documentation.html' : base + '/documentation.html';
  }
  function docExternal() {
    var su = trimSlash(sourcePagesUrl()).toLowerCase();
    return !!su && su !== base.toLowerCase();
  }

  // ── Configure / Apply: open the dashboard's modal when present, else navigate
  //    to the standalone page (kept identical content). ──────────────────────
  function openPage(kind) {
    // Apply to New Repo always installs from the ROOT tooling repo. On a consumer
    // dashboard, send the user to the root site's own installer (new tab) so they
    // always get the latest Apply-to-New-Repo page + UX — never this repo's
    // vendored, possibly older, snapshot. On the source repo the local page IS
    // the latest, so fall through to the inline modal / navigation below.
    if (kind === 'integrate' && !cfg.isSource) {
      var su = sourcePagesUrl();
      if (su) { window.open(su + 'integrate.html', '_blank', 'noopener'); return; }
    }
    var map = {
      configure: { src: 'configure.html' + (repo ? ('?repo=' + encodeURIComponent(repo)) : ''), title: 'Configure Pipeline' },
      vibrowser: { src: 'configure.html' + (repo ? ('?repo=' + encodeURIComponent(repo)) : '') + '#vi-browser', title: 'VI Browser renders' },
      vianalyzer: { src: 'vi-analyzer.html' + (repo ? ('?repo=' + encodeURIComponent(repo)) : ''), title: 'VI Analyzer' },
      unittests: { src: 'unit-tests.html' + (repo ? ('?repo=' + encodeURIComponent(repo)) : ''), title: 'Unit Testing' },
      builds: { src: 'builds.html' + (repo ? ('?repo=' + encodeURIComponent(repo)) : ''), title: 'Builds' },
      integrate: { src: 'integrate.html', title: 'Apply to New Repo' }
    };
    var t = map[kind]; if (!t) return;
    if (typeof window.lvciOpen === 'function') { window.lvciOpen(t.src, t.title); return; }
    window.location.href = base + '/' + t.src;
  }

  // ── Populate history: open the dashboard's "Populate dashboard history" dialog
  //    (queue CI for existing revisions). The dialog itself lives in the dashboard
  //    generator, which exposes window.lvciRunHistory; this menu item is only
  //    offered on the dashboard context, where that hook is present. The guard
  //    keeps it inert anywhere the hook is absent (e.g. an older dashboard). ────
  function runHistory(opts) {
    // On the dashboard the dialog lives inline (window.lvciRunHistory); pass the
    // optional {cap, sha, platform} straight through so it can open pre-selected
    // to re-run a single document. On every OTHER page that hook is absent, so
    // open the dashboard's dialog in an iframe OVERLAY - the user stays on the
    // current page (the dashboard reads ?lvci-populate=1[&cap&sha&platform] and,
    // with &lvci-embed=1, hides its chrome so only the dialog shows; it posts
    // 'lvci:hist-close' when the dialog closes so this overlay self-dismisses).
    if (typeof window.lvciRunHistory === 'function') { window.lvciRunHistory(opts || undefined); return; }
    var u = base + '/?lvci-populate=1';
    if (opts && opts.cap) u += '&cap=' + encodeURIComponent(opts.cap);
    if (opts && opts.sha) u += '&sha=' + encodeURIComponent(opts.sha);
    if (opts && opts.platform) u += '&platform=' + encodeURIComponent(opts.platform);
    _openHistOverlay(u);
  }

  // Open the dashboard's populate-history dialog as a floating overlay iframe so
  // the user stays on the current report page. The iframe loads the dashboard in
  // embedded "dialog only" mode (&lvci-embed=1): the dashboard hides its chrome
  // and table and goes fully transparent, so only the dialog panel shows. THIS
  // overlay div provides the dim backdrop (a semi-transparent element inside a
  // transparent iframe does not reliably composite over the host page, which left
  // the report looking blanked), so the report stays visible-but-dimmed behind the
  // dialog. The dashboard auto-opens the pre-configured dialog and signals closure
  // via postMessage, at which point this overlay removes itself.
  function _openHistOverlay(url) {
    var existing = document.getElementById('lvci-hist-overlay');
    if (existing) { existing.remove(); }
    var frameUrl = url + (url.indexOf('?') >= 0 ? '&' : '?') + 'lvci-embed=1';
    var ov = document.createElement('div');
    ov.id = 'lvci-hist-overlay';
    ov.style.cssText = 'position:fixed;inset:0;z-index:9990;background:rgba(0,0,0,.55)';
    var frame = document.createElement('iframe');
    frame.src = frameUrl;
    frame.title = 'Populate dashboard history';
    frame.setAttribute('allowtransparency', 'true');
    frame.style.cssText = 'position:absolute;inset:0;width:100%;height:100%;border:0;background:transparent';
    function closeOv() {
      ov.remove();
      document.removeEventListener('keydown', onOvKey, true);
      window.removeEventListener('message', onOvMsg);
    }
    function onOvKey(e) { if (e.key === 'Escape') { e.stopPropagation(); closeOv(); } }
    function onOvMsg(e) {
      if (!e.isTrusted) return;
      try { if (new URL(frameUrl, location.href).origin !== e.origin) return; } catch (ex) { return; }
      if (e.data === 'lvci:hist-close' || e.data === 'lvci:hist-done') closeOv();
    }
    document.addEventListener('keydown', onOvKey, true);
    window.addEventListener('message', onOvMsg);
    ov.appendChild(frame);
    document.body.appendChild(ov);
  }

  // Settings sub-navigation: the per-repo configuration sections as a tab strip in
  // the context bar, so they read as one "Settings" area (with the current section
  // marked) instead of separate islands reachable only via the Settings menu. Plain
  // links (base + page + ?repo) so middle-/ctrl-click and the active state work.
  function makeSettingsNav() {
    var SECTIONS = [
      { key: 'configure',  label: 'Configure Pipeline', file: 'configure.html' },
      { key: 'vianalyzer', label: 'VI Analyzer',       file: 'vi-analyzer.html' },
      { key: 'unittests',  label: 'Unit Testing',      file: 'unit-tests.html' },
      { key: 'builds',     label: 'Builds',            file: 'builds.html' }
    ];
    var CUR = { 'configure': 'configure', 'vianalyzer': 'vianalyzer', 'unit-tests-config': 'unittests', 'builds': 'builds' };
    var cur = CUR[ctx] || '';
    var q = repo ? ('?repo=' + encodeURIComponent(repo)) : '';
    var wrap = document.createElement('div'); wrap.className = 'lvci-rev lvci-settings-ctx';
    var lbl = document.createElement('span'); lbl.className = 'lvci-revlbl'; lbl.textContent = 'Settings';
    var subnav = document.createElement('div'); subnav.className = 'lvci-subnav';
    SECTIONS.forEach(function (s) {
      var a = document.createElement('a');
      a.href = base + '/' + s.file + q;
      a.textContent = s.label;
      if (s.key === cur) { a.classList.add('on'); a.setAttribute('aria-current', 'page'); }
      subnav.appendChild(a);
    });
    wrap.appendChild(lbl); wrap.appendChild(subnav);
    return wrap;
  }

  // ── Generic page sub-navigation: a context-bar tab strip a page declares via
  //    window.LVCI.subnav so its in-page sub-views (e.g. the Dependencies page's
  //    VIPM / NI Packages / System Components) navigate from the SAME shared
  //    sub-header the Settings sections use, instead of a separate in-body tab
  //    bar. This is what makes sub-item navigation consistent across documents.
  //    The page keeps its panels in the body and switches them in response to
  //    the 'lvci:subnav' event (detail.key) this fires; window.lvciSetSubnavActive
  //    (key) lets the page reflect a programmatic change (deep link) back into
  //    the strip. Shape: subnav = { label, active, tabs: [{ key, label }] }.
  function makePageSubnav() {
    var sn = cfg.subnav;
    if (!sn || !sn.tabs || !sn.tabs.length) return null;
    var wrap = document.createElement('div'); wrap.className = 'lvci-rev lvci-settings-ctx';
    if (sn.label) { var lbl = document.createElement('span'); lbl.className = 'lvci-revlbl'; lbl.textContent = sn.label; wrap.appendChild(lbl); }
    var subnav = document.createElement('div'); subnav.className = 'lvci-subnav'; subnav.id = 'lvci-page-subnav';
    var active = sn.active || (sn.tabs[0] && sn.tabs[0].key);
    sn.tabs.forEach(function (t) {
      var a = document.createElement('a');
      a.href = '#' + t.key;
      a.setAttribute('role', 'tab');
      a.setAttribute('data-subnav-key', t.key);
      a.textContent = t.label;
      if (t.key === active) { a.classList.add('on'); a.setAttribute('aria-current', 'page'); }
      a.addEventListener('click', function (e) {
        e.preventDefault();
        lvciSetSubnavActive(t.key);
        try { window.dispatchEvent(new CustomEvent('lvci:subnav', { detail: { key: t.key } })); } catch (err) {}
      });
      subnav.appendChild(a);
    });
    wrap.appendChild(subnav);
    return wrap;
  }
  // Reflect the active sub-view back into the header strip (the page calls this
  // when it switches sub-views itself, e.g. restoring a deep link). Inert when
  // the strip isn't present (a page without the header, or no subnav declared).
  function lvciSetSubnavActive(key) {
    var strip = document.getElementById('lvci-page-subnav'); if (!strip) return;
    Array.prototype.forEach.call(strip.querySelectorAll('a[data-subnav-key]'), function (a) {
      var on = a.getAttribute('data-subnav-key') === key;
      a.classList.toggle('on', on);
      if (on) a.setAttribute('aria-current', 'page'); else a.removeAttribute('aria-current');
    });
  }
  window.lvciSetSubnavActive = lvciSetSubnavActive;

  // ── Regenerate this revision's report: dispatch a fresh run for THIS commit,
  //    reusing the dashboard's token + optimistic queued bridge (so the
  //    dashboard cell shows a spinner immediately). Owned here so it's one
  //    implementation for every per-revision document type (see DOCTYPES). ────
  var TOK_KEY = 'lvci_dispatch_token';
  var QKEY = 'lvci_queued_runs';
  function tok() { try { return localStorage.getItem(TOK_KEY) || ''; } catch (e) { return ''; } }
  function rerunWorkflow() {
    var wf = (DOC && DOC.workflow) || {};
    return cfg.platform === 'linux' ? (wf.linux || wf.windows) : (wf.windows || wf.linux);
  }
  function setStatus(html, kind) {
    var el = document.getElementById('lvci-status'); if (!el) return;
    el.innerHTML = html || '';
    el.className = 'lvci-status' + (html ? ' show' : '');
    el.style.color = kind === 'ok' ? '#3fb950' : (kind === 'err' ? '#f85149' : '');
  }
  function markQueued() {
    if (!DOC) return;
    try {
      var o = JSON.parse(localStorage.getItem(QKEY) || '{}') || {};
      o[DOC.cap + '|' + cfg.sha] = { ts: Date.now(), plats: [cfg.platform === 'linux' ? 'linux' : 'windows'],
                                     parent: '', short: (cfg.sha || '').slice(0, 7), runs: [] };
      localStorage.setItem(QKEY, JSON.stringify(o));
    } catch (e) {}
  }
  function showTokenPanel() {
    var p = document.getElementById('lvci-tok'); if (!p) return;
    var owner = (repo.split('/')[0]) || '';
    var url = 'https://github.com/settings/personal-access-tokens/new'
      + '?name=' + encodeURIComponent('LabVIEW CI dispatch')
      + '&description=' + encodeURIComponent('Dispatch CI runs for ' + repo)
      + '&target_name=' + encodeURIComponent(owner) + '&actions=write';
    p.innerHTML =
      '<div>Regenerating needs a fine-grained token with <strong>Actions: Read and write</strong> on '
      + '<code>' + esc(repo) + '</code>. <a href="' + url + '" target="_blank" rel="noopener" style="color:#58a6ff">Create one \u2197</a> '
      + '(stored only in this browser; shared with the dashboard\u2019s Run now).</div>'
      + '<input id="lvci-tok-in" type="password" placeholder="github_pat_\u2026" autocomplete="off" spellcheck="false">'
      + '<div><button class="lvci-btn primary" id="lvci-tok-save">Save &amp; regenerate</button></div>';
    p.className = 'lvci-tok show';
    var inp = document.getElementById('lvci-tok-in');
    if (inp) inp.focus();
    var save = document.getElementById('lvci-tok-save');
    if (save) save.addEventListener('click', function () {
      var v = (inp && inp.value || '').trim(); if (!v) { if (inp) inp.focus(); return; }
      try { localStorage.setItem(TOK_KEY, v); } catch (e) {}
      p.className = 'lvci-tok'; p.innerHTML = '';
      doDispatch();
    });
    if (inp) inp.addEventListener('keydown', function (e) { if (e.key === 'Enter' && save) save.click(); });
  }
  function doDispatch() {
    var btn = document.getElementById('lvci-rerun');
    var wf = rerunWorkflow();
    var label = (DOC && DOC.regenLabel) || 'Regenerate report';
    var docLabel = (DOC && DOC.label) || 'report';
    if (btn) { btn.disabled = true; btn.innerHTML = '<span class="lvci-spin"></span>Queuing\u2026'; }
    setStatus('Queuing a fresh ' + esc(docLabel) + ' run\u2026', null);
    fetch('https://api.github.com/repos/' + repo + '/actions/workflows/' + encodeURIComponent(wf) + '/dispatches', {
      method: 'POST',
      headers: { 'Authorization': 'Bearer ' + tok(), 'Accept': 'application/vnd.github+json',
                 'X-GitHub-Api-Version': '2022-11-28', 'Content-Type': 'application/json' },
      body: JSON.stringify({ ref: 'main', inputs: { commit_sha: cfg.sha } })
    }).then(function (r) {
      if (btn) { btn.disabled = false; btn.innerHTML = '\u21bb ' + esc(label); }
      if (r.status === 204) {
        markQueued();
        setStatus('\u2713 Queued a fresh run \u2014 the <a href="' + base + '/">dashboard</a> cell shows it working now; this report updates when the run finishes. '
          + '<a href="https://github.com/' + repo + '/actions/workflows/' + wf + '" target="_blank" rel="noopener">View runs \u2197</a>', 'ok');
        return;
      }
      if (r.status === 401) { try { localStorage.removeItem(TOK_KEY); } catch (e) {} setStatus('That token was rejected (401). Paste a valid one.', 'err'); showTokenPanel(); return; }
      if (r.status === 403) { setStatus('<strong>403</strong>: the token is missing <strong>Actions: Read and write</strong> on this repository.', 'err'); showTokenPanel(); return; }
      if (r.status === 404) { setStatus('<strong>404</strong>: the token cannot see <code>' + esc(repo) + '</code>. Grant it access + Actions: Read and write.', 'err'); showTokenPanel(); return; }
      setStatus('Dispatch failed (HTTP ' + r.status + ').', 'err');
    }).catch(function (e) {
      if (btn) { btn.disabled = false; btn.innerHTML = '\u21bb ' + esc(label); }
      setStatus('Network error: ' + esc(String(e && e.message || e)), 'err');
    });
  }
  function rerun() {
    // Re-running a per-revision report now goes through the dashboard's
    // "Populate history" dialog, pre-selected to re-run THIS document (this
    // revision + this activity), so every re-run shares one consistent flow and
    // the user can confirm scope/platform before queuing. (The inline dispatch
    // path - doDispatch + the header token panel - remains for any surface that
    // still calls it directly; the report Re-run button now routes here.)
    // Resolve the revision the user is actually viewing (cfg.sha, else the ?sha
    // param or the header's revision picker) and always pass the cap, so the
    // dialog scopes to exactly that document + revision instead of the full history.
    var sha = (typeof currentRevisionSha === 'function' ? currentRevisionSha() : '') || cfg.sha || '';
    if (DOC) { runHistory({ cap: DOC.cap, sha: sha, platform: cfg.platform }); return; }
    runHistory();
  }

  // ── Debug Run: open the dashboard's "Debug Run" dialog (boot a worker with a
  //    remote LabVIEW desktop over VNC). The dialog lives in the dashboard
  //    generator (window.lvciDebugRun); on every other page, route to the
  //    dashboard with ?lvci-debug=1 so it opens there. ───────────────────────
  function debugRun() {
    if (typeof window.lvciDebugRun === 'function') { window.lvciDebugRun(); return; }
    window.location.href = base + '/?lvci-debug=1';
  }

  // ── Action button factory ─────────────────────────────────────────────────
  function actionEl(a, mobile) {
    var el;
    if (a.href) {
      el = document.createElement('a');
      el.href = a.href;
      if (a.newTab) { el.target = '_blank'; el.rel = 'noopener'; }
    } else {
      el = document.createElement('button');
      el.type = 'button';
    }
    el.className = mobile ? 'lvci-m' : ('lvci-btn' + (a.primary ? ' primary' : (a.accent ? ' accent' : '')));
    if (a.kind === 'rerun' && !mobile) el.id = 'lvci-rerun';
    el.innerHTML = iconHtml(a) + esc(a.label);
    if (!a.href) {
      el.addEventListener('click', function () {
        if (a.kind === 'configure' || a.kind === 'integrate' || a.kind === 'unittests' || a.kind === 'vibrowser' || a.kind === 'vianalyzer' || a.kind === 'builds') openPage(a.kind);
        else if (a.kind === 'rerun') rerun();
        else if (a.kind === 'runhistory') runHistory();
        else if (a.kind === 'debugrun') debugRun();
      });
    }
    return el;
  }

  // Grouped top-nav dropdown (Settings / Tools): a nav-styled trigger + a standard
  // dropdown menu of the given secondary actions. Desktop only — the nav is hidden
  // on mobile, where these same items stay in the hamburger menu.
  //
  // Only one top-bar popover is open at a time: every popover (the Settings /
  // Tools nav dropdowns and Share) registers its close() in popoverCloses
  // and closes the others when it opens, so two menus can never overlap. Each
  // popover stops propagation on its own trigger click (so the document-level
  // outside-click handler doesn't immediately re-close it), which is exactly why
  // that handler can't dismiss a sibling on its own -- hence this explicit list.
  var popoverCloses = [];
  function closeOtherPopovers(self) {
    for (var i = 0; i < popoverCloses.length; i++) {
      if (popoverCloses[i] !== self) popoverCloses[i]();
    }
  }
  function makeNavDropdown(label, items, active, opts) {
    opts = opts || {};
    var dd = document.createElement('div');
    dd.className = 'lvci-navgrp';
    var btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'lvci-navgrp-btn';
    if (opts.id) btn.id = opts.id;
    if (active) { btn.classList.add('on'); btn.setAttribute('aria-current', 'page'); }
    btn.setAttribute('aria-haspopup', 'true');
    btn.setAttribute('aria-expanded', 'false');
    btn.innerHTML = esc(label) + '<span class="lvci-navgrp-chev" aria-hidden="true"><svg viewBox="0 0 16 16" fill="currentColor"><path d="M4 6l4 4 4-4z"/></svg></span>' + (opts.updateDot ? '<span class="lvci-mdot" aria-hidden="true"></span>' : '');
    dd.appendChild(btn);
    var menu = document.createElement('div');
    menu.className = 'lvci-dropdown-menu lvci-navgrp-menu';
    var close = function () { menu.classList.remove('open'); btn.classList.remove('open'); btn.setAttribute('aria-expanded', 'false'); };
    popoverCloses.push(close);
    items.forEach(function (a) {
      var el;
      if (a.activity) {
        el = document.createElement('a');
        wireActivityLink(el, a.activity, navBase, close);
      } else if (a.href) {
        el = document.createElement('a'); el.href = a.href;
        if (a.newTab) { el.target = '_blank'; el.rel = 'noopener'; }
        el.addEventListener('click', close);
      } else {
        el = document.createElement('button'); el.type = 'button';
        el.addEventListener('click', function () {
          if (a.kind === 'configure' || a.kind === 'vianalyzer' || a.kind === 'unittests' || a.kind === 'integrate' || a.kind === 'vibrowser' || a.kind === 'builds') openPage(a.kind);
          else if (a.kind === 'runhistory') runHistory();
          else if (a.kind === 'debugrun') debugRun();
          close();
        });
      }
      el.innerHTML = iconHtml(a) + esc(a.label);
      if (a.source) { el.style.display = 'none'; clientsEls.push(el); }
      if (a.about) aboutEls.push(el);
      menu.appendChild(el);
    });
    if (opts.tools) {
      if (menu.children.length) { var vsep = document.createElement('div'); vsep.className = 'lvci-sep'; menu.appendChild(vsep); }
      var ver = makeVerItem(); ver.addEventListener('click', close); menu.appendChild(ver); verEls.push(ver);
      var tsep = document.createElement('div'); tsep.className = 'lvci-sep'; menu.appendChild(tsep);
      menu.appendChild(themeControl());
    }
    btn.addEventListener('click', function (e) {
      e.stopPropagation();
      var open = !menu.classList.contains('open');
      if (open) closeOtherPopovers(close);
      menu.classList.toggle('open', open);
      btn.classList.toggle('open', open);
      btn.setAttribute('aria-expanded', open ? 'true' : 'false');
    });
    menu.addEventListener('click', function (e) { e.stopPropagation(); });
    document.addEventListener('click', close);
    document.addEventListener('keydown', function (e) { if (e.key === 'Escape') close(); });
    dd.appendChild(menu);
    return dd;
  }

  // Revision picker (per-revision reports only).
  // Reuses the VI Browser's "pick a commit" metaphor: a <select> of revisions
  // that, when changed, opens the SAME document type for that revision
  // (`<prefix>/<sha>/index.html`). The revision list comes from the SAME source
  // the VI Browser uses — vi-snapshots/{files.json,commits.json} — and is
  // filtered to revisions that actually have a report of this type (probed via
  // `<prefix>/<sha>/summary.json`), so it never lands on a 404. The revision
  // being viewed is always present + selected, even while the probe runs.
  // Where "view another revision" lands. Normally the bare per-revision report;
  // but when this header is the framed VIEWER chrome (report-viewer.html embeds
  // the report in an iframe), re-frame the viewer onto the new revision so the
  // chrome — and its Regenerate button — stays put instead of dropping the user
  // onto a bare (and, for older revisions, possibly header-less) report page.
  function docDest(sha) {
    if (!DOC || !sha) return base + '/';
    if (cfg.embedded && cfg.framedSrc) {
      var src = cfg.sha ? cfg.framedSrc.split(cfg.sha).join(sha) : cfg.framedSrc;
      var title = DOC.label + ' \u00b7 ' + sha.slice(0, 7);
      return base + '/report/index.html?type=' + encodeURIComponent(ctx)
           + '&sha=' + encodeURIComponent(sha)
           + (cfg.platform ? '&platform=' + encodeURIComponent(cfg.platform) : '')
           + '&src=' + encodeURIComponent(src)
           + '&title=' + encodeURIComponent(title);
    }
    return base + '/' + DOC.prefix + '/' + sha + '/index.html';
  }
  function makeRevPicker() {
    var wrap = document.createElement('div'); wrap.className = 'lvci-rev lvci-rev-ctx';
    var lbl = document.createElement('span'); lbl.className = 'lvci-revlbl'; lbl.textContent = 'Revision';
    var prev = document.createElement('button'); prev.type = 'button'; prev.className = 'lvci-rev-step'; prev.title = 'Newer revision'; prev.setAttribute('aria-label', 'Newer revision'); prev.innerHTML = '\u2039';
    var sel = document.createElement('select');
    sel.setAttribute('aria-label', 'View another revision\u2019s ' + (DOC ? DOC.label : '') + ' report');
    var cur = document.createElement('option');
    cur.value = cfg.sha || '';
    cur.textContent = (cfg.short || (cfg.sha || '').slice(0, 7)) || 'this revision';
    sel.appendChild(cur);
    sel.value = cfg.sha || '';
    var next = document.createElement('button'); next.type = 'button'; next.className = 'lvci-rev-step'; next.title = 'Older revision'; next.setAttribute('aria-label', 'Older revision'); next.innerHTML = '\u203a';
    var rp = revPicker(sel, {});
    function syncSteps() { prev.disabled = !rp.canPrev(); next.disabled = !rp.canNext(); }
    prev.addEventListener('click', function () { rp.stepPrev(); });
    next.addEventListener('click', function () { rp.stepNext(); });
    sel.addEventListener('change', function () {
      syncSteps();
      var v = sel.value;
      if (v && v !== cfg.sha) window.location.href = docDest(v);
    });
    sel._lvciSync = function () { rp.refresh(); syncSteps(); };
    wrap.appendChild(lbl); wrap.appendChild(prev); wrap.appendChild(rp.button); wrap.appendChild(next);
    syncSteps();
    return { wrap: wrap, sel: sel };
  }

  // Generic context-bar revision picker for a NON-report page (e.g. the
  // Dependencies page) that owns its own revision list + in-place reload. Same
  // chrome as makeRevPicker (label + prev/next steppers + the shared searchable
  // revPicker) but it does NOT navigate on change -- the page populates the
  // <select> options and listens to its 'change' to reload in place. Exposed to
  // the page as window.lvciRevBar { wrap, sel, refresh }.
  function makeRevBar() {
    var wrap = document.createElement('div'); wrap.className = 'lvci-rev lvci-rev-ctx';
    var lbl = document.createElement('span'); lbl.className = 'lvci-revlbl'; lbl.textContent = 'Revision';
    var prev = document.createElement('button'); prev.type = 'button'; prev.className = 'lvci-rev-step'; prev.title = 'Newer revision'; prev.setAttribute('aria-label', 'Newer revision'); prev.innerHTML = '\u2039';
    var sel = document.createElement('select'); sel.setAttribute('aria-label', 'Select a revision');
    var next = document.createElement('button'); next.type = 'button'; next.className = 'lvci-rev-step'; next.title = 'Older revision'; next.setAttribute('aria-label', 'Older revision'); next.innerHTML = '\u203a';
    var rp = revPicker(sel, {});
    function syncSteps() { prev.disabled = !rp.canPrev(); next.disabled = !rp.canNext(); }
    prev.addEventListener('click', function () { rp.stepPrev(); });
    next.addEventListener('click', function () { rp.stepNext(); });
    sel.addEventListener('change', syncSteps);
    wrap.appendChild(lbl); wrap.appendChild(prev); wrap.appendChild(rp.button); wrap.appendChild(next);
    syncSteps();
    return { wrap: wrap, sel: sel, refresh: function () { rp.refresh(); syncSteps(); } };
  }

  // ── Reusable searchable revision picker ──────────────────────────────────
  // Enhances a native <select> of revisions in place: the <select> stays the
  // source of truth (its value + 'change' event still drive the page) while this
  // renders a compact button + a searchable popover that HIDES CI-only and
  // dependency-only revisions by default (matching the dashboard), revealable
  // with a toggle, and filterable by short SHA / build number / commit title.
  // Every revision selector across the pages uses this so the experience is
  // identical. An <option> counts as hidden-by-default when it carries
  // data-deponly (only a .vipc/.vip changed) or data-project="0" (a CI / tooling
  // / merge commit); unclassified options are always shown (search still applies).
  // Exposed as window.lvciRevPicker(selectEl[, opts]); idempotent per <select>.
  function lvciRevSweepPops() {
    // Drop popovers whose trigger button was detached (a page re-rendered its
    // <select>), so re-attaching never leaks orphaned panels into <body>.
    var pops = document.querySelectorAll('.lvci-revpop');
    for (var i = 0; i < pops.length; i++) {
      var b = pops[i]._ownerBtn;
      if (b && !b.isConnected) { try { pops[i].parentNode.removeChild(pops[i]); } catch (e) {} }
    }
  }
  function revPicker(sel, opts) {
    opts = opts || {};
    if (!sel) return null;
    if (sel._lvciRevPick) { sel._lvciRevPick.refresh(); return sel._lvciRevPick; }
    lvciRevSweepPops();
    var showAll = false, active = -1;
    var btn = document.createElement('button');
    btn.type = 'button'; btn.className = 'lvci-revpick'; btn.setAttribute('aria-haspopup', 'listbox');
    var curEl = document.createElement('span'); curEl.className = 'lvci-revpick-cur';
    var caret = document.createElement('span'); caret.className = 'lvci-revpick-caret'; caret.setAttribute('aria-hidden', 'true'); caret.textContent = '\u25be';
    btn.appendChild(curEl); btn.appendChild(caret);
    sel.style.display = 'none';
    if (sel.parentNode) sel.parentNode.insertBefore(btn, sel.nextSibling);
    var pop = document.createElement('div'); pop.className = 'lvci-revpop'; pop.style.display = 'none'; pop._ownerBtn = btn;
    var search = document.createElement('input');
    search.type = 'search'; search.className = 'lvci-revpop-search';
    search.placeholder = opts.placeholder || 'Search by title, build # or SHA\u2026';
    search.autocomplete = 'off'; search.spellcheck = false;
    var tools = document.createElement('label'); tools.className = 'lvci-revpop-tools';
    var toolsChk = document.createElement('input'); toolsChk.type = 'checkbox';
    tools.appendChild(toolsChk); tools.appendChild(document.createTextNode(' Show CI & dependency-only revisions'));
    var list = document.createElement('div'); list.className = 'lvci-revpop-list'; list.setAttribute('role', 'listbox');
    pop.appendChild(search); pop.appendChild(tools); pop.appendChild(list);
    document.body.appendChild(pop);

    function isHidden(o) { return !!o && (o.dataset.deponly === '1' || o.dataset.deponly === 'true' || o.dataset.project === '0' || o.dataset.project === 'false'); }
    function anyHidden() { for (var i = 0; i < sel.options.length; i++) if (isHidden(sel.options[i])) return true; return false; }
    function tagOf(o) { return (o.dataset.deponly === '1' || o.dataset.deponly === 'true') ? 'dep' : (o.dataset.project === '0' || o.dataset.project === 'false') ? 'ci' : ''; }
    function filtered(useSearch) {
      var q = useSearch ? search.value.trim().toLowerCase() : '', out = [];
      for (var i = 0; i < sel.options.length; i++) {
        var o = sel.options[i];
        if (q) {                                    // a query searches across ALL revisions,
          if (((o.textContent || '') + ' ' + o.value).toLowerCase().indexOf(q) >= 0) out.push(i);
          continue;                                 // so a SHA / build # / title always finds its revision
        }
        if (!showAll && isHidden(o) && i !== sel.selectedIndex) continue;   // browsing: hide CI/dep (keep current)
        out.push(i);
      }
      return out;
    }
    function setLabel() {
      var o = sel.options[sel.selectedIndex];
      curEl.textContent = o ? (o.textContent || o.value) : (opts.empty || 'Select a revision\u2026');
    }
    function render() {
      var vis = filtered(true);
      tools.style.display = anyHidden() ? '' : 'none';
      toolsChk.checked = showAll;
      list.innerHTML = '';
      if (!vis.length) { list.innerHTML = '<div class="lvci-revpop-empty">No revisions match.</div>'; active = -1; return; }
      vis.forEach(function (i) {
        var o = sel.options[i], txt = o.textContent || o.value, sh = txt, ms = '';
        var d = txt.indexOf(' \u2014 ');
        if (d >= 0) { sh = txt.slice(0, d); ms = txt.slice(d + 3); }
        var row = document.createElement('div');
        row.className = 'lvci-revopt' + (i === sel.selectedIndex ? ' sel' : '');
        row.setAttribute('role', 'option'); row.dataset.idx = i;
        var shEl = document.createElement('span'); shEl.className = 'sh'; shEl.textContent = sh;
        var msEl = document.createElement('span'); msEl.className = 'ms'; msEl.textContent = ms;
        row.appendChild(shEl); row.appendChild(msEl);
        var tg = tagOf(o); if (tg) { var tEl = document.createElement('span'); tEl.className = 'tag'; tEl.textContent = tg; tEl.title = tg === 'dep' ? 'Only an external dependency (.vipc/.vip) changed' : 'CI / tooling / merge commit (no LabVIEW code change)'; row.appendChild(tEl); }
        row.addEventListener('mousedown', function (e) { e.preventDefault(); choose(parseInt(row.dataset.idx, 10)); });
        list.appendChild(row);
      });
      active = -1;
    }
    function rows() { return list.querySelectorAll('.lvci-revopt'); }
    function setActive(k) {
      var rs = rows(); if (!rs.length) return;
      active = Math.max(0, Math.min(rs.length - 1, k));
      for (var j = 0; j < rs.length; j++) rs[j].classList.toggle('active', j === active);
      if (rs[active]) rs[active].scrollIntoView({ block: 'nearest' });
    }
    function choose(i) {
      if (sel.options[i] && sel.value !== sel.options[i].value) { sel.selectedIndex = i; sel.dispatchEvent(new Event('change', { bubbles: true })); }
      setLabel(); close();
    }
    function place() {
      var r = btn.getBoundingClientRect();
      pop.style.minWidth = Math.max(r.width, 280) + 'px';
      var w = pop.offsetWidth || 320;
      pop.style.left = Math.max(8, Math.min(r.left, window.innerWidth - w - 8)) + 'px';
      var below = window.innerHeight - r.bottom;
      if (below < 300 && r.top > below) { pop.style.top = 'auto'; pop.style.bottom = (window.innerHeight - r.top + 4) + 'px'; }
      else { pop.style.bottom = 'auto'; pop.style.top = (r.bottom + 4) + 'px'; }
    }
    function open() {
      if (pop.style.display !== 'none') return;
      search.value = ''; if (!anyHidden()) showAll = false; render(); pop.style.display = 'block'; place();
      btn.setAttribute('aria-expanded', 'true');
      document.addEventListener('mousedown', onDoc, true);
      window.addEventListener('resize', close); window.addEventListener('scroll', close, true);
      setTimeout(function () { search.focus(); }, 0);
    }
    function close() {
      if (pop.style.display === 'none') return;
      pop.style.display = 'none'; btn.setAttribute('aria-expanded', 'false');
      document.removeEventListener('mousedown', onDoc, true);
      window.removeEventListener('resize', close); window.removeEventListener('scroll', close, true);
    }
    function onDoc(e) { if (pop.contains(e.target) || btn.contains(e.target)) return; close(); }
    btn.addEventListener('click', function () { if (pop.style.display === 'none') open(); else close(); });
    search.addEventListener('input', render);
    toolsChk.addEventListener('change', function () { showAll = toolsChk.checked; render(); });
    search.addEventListener('keydown', function (e) {
      if (e.key === 'ArrowDown') { e.preventDefault(); setActive(active + 1); }
      else if (e.key === 'ArrowUp') { e.preventDefault(); setActive(active - 1); }
      else if (e.key === 'Enter') { e.preventDefault(); var rs = rows(); var el = rs[active >= 0 ? active : 0]; if (el) choose(parseInt(el.dataset.idx, 10)); }
      else if (e.key === 'Escape') { e.preventDefault(); close(); btn.focus(); }
    });
    sel.addEventListener('change', setLabel);
    function stepTo(dir) { var v = filtered(false); var pos = v.indexOf(sel.selectedIndex); var n = pos + dir; if (pos >= 0 && n >= 0 && n < v.length) choose(v[n]); }
    var ctl = {
      button: btn,
      refresh: function () { setLabel(); if (pop.style.display !== 'none') render(); },
      stepPrev: function () { stepTo(-1); },
      stepNext: function () { stepTo(1); },
      canPrev: function () { var v = filtered(false); var pos = v.indexOf(sel.selectedIndex); return pos > 0; },
      canNext: function () { var v = filtered(false); var pos = v.indexOf(sel.selectedIndex); return pos >= 0 && pos < v.length - 1; },
      close: close
    };
    sel._lvciRevPick = ctl; setLabel();
    return ctl;
  }
  window.lvciRevPicker = revPicker;

  // Activity picker (per-revision reports) — the report half of the unified
  // Activity switcher: while viewing one of a revision's reports, jump to another
  // report for the SAME revision in place (Mass Compile -> VI Analyzer, …) instead
  // of returning to the dashboard. Mirrors the revision picker (a labelled <select>
  // in the context bar) and reuses its summary.json availability probe, so a report
  // that was not produced for this revision is shown disabled rather than 404ing.
  function lensDest(key, sha) {
    return activityDest(key, sha || cfg.sha || '', base);
  }
  function reportExists(d, sha) {
    if (!d || !sha) return Promise.resolve(false);
    var root = base + '/' + d.prefix + '/' + sha + '/';
    return fetch(root + (d.probeName || 'summary.json'), { method: 'HEAD', cache: 'no-cache' })
      .then(function (r) { return r.ok ? true : fetch(root + 'index.html', { method: 'HEAD', cache: 'no-cache' }).then(function (rr) { return rr.ok; }); })
      .catch(function () { return fetch(root + 'index.html', { method: 'HEAD', cache: 'no-cache' }).then(function (r) { return r.ok; }).catch(function () { return false; }); });
  }
  function makeLensPicker(opts) {
    opts = opts || {};
    var current = opts.current || ctx;
    var getSha = opts.getSha || function () { return cfg.sha; };
    var deferProbe = !!opts.deferProbe;   // VI Browser: sha is dynamic -> check at click time, not upfront
    // In the VI Browser a ?lens= deep link means a report lens is active on load;
    // start the picker on it so the dropdown matches the framed report.
    if (deferProbe) { try { var _ql = new URLSearchParams(location.search).get('lens'); if (_ql && LENS_ORDER.indexOf(_ql) >= 0) current = _ql; } catch (e) {} }
    var wrap = document.createElement('div'); wrap.className = 'lvci-rev lvci-lens-ctx';
    var lbl = document.createElement('span'); lbl.className = 'lvci-revlbl'; lbl.textContent = 'Activity';
    var sel = document.createElement('select');
    sel.setAttribute('aria-label', 'Switch to another view of this revision');
    // Lenses grouped for scale (a flat list does not stay legible as activities grow):
    // Code & changes (snapshots/diff) | Quality (the checks) | Artifacts (docs, ...).
    var LENS_GROUPS = [
      { label: 'Code & changes', keys: ['snapshots'] },
      { label: 'Quality',        keys: ['masscompile-report', 'vi-analyzer-report', 'unit-tests-report'] },
      { label: 'Artifacts',      keys: ['antidoc-report', 'builds-report', 'sbom-report'] }
    ];
    function lensLabel(key) {
      if (key === 'snapshots') return 'Snapshots';
      var d = DOCTYPES[key]; return d ? d.label : null;
    }
    function addOpt(parent, key) {
      var label = lensLabel(key); if (!label) return;
      var o = document.createElement('option'); o.value = key; o.textContent = label;
      if (key === current) o.selected = true;
      parent.appendChild(o);
    }
    var placed = {};
    LENS_GROUPS.forEach(function (g) {
      var keys = g.keys.filter(function (k) { return LENS_ORDER.indexOf(k) >= 0 && lensLabel(k); });
      if (!keys.length) return;
      var og = document.createElement('optgroup'); og.label = g.label;
      keys.forEach(function (k) { addOpt(og, k); placed[k] = 1; });
      sel.appendChild(og);
    });
    // Any lens not assigned to a group (future-proofing) -> ungrouped, in order.
    LENS_ORDER.forEach(function (key) { if (!placed[key]) addOpt(sel, key); });
    sel.value = current;
    var note = document.createElement('span'); note.style.cssText = 'font-size:11px;color:#8b949e;white-space:nowrap';
    // Switch lens. A page that can host a lens in its OWN pane (the VI Browser
    // exposes window.lvciRenderLens) renders it in place; otherwise we navigate to
    // the framed report. `current` tracks the live lens so re-selecting / reverting
    // works after an in-place switch.
    function go(k, s) {
      if (deferProbe && typeof window.lvciRenderLens === 'function') {
        try { if (window.lvciRenderLens(k, s)) { current = k; return; } } catch (e) {}
      }
      window.location.href = lensDest(k, s);
    }
    sel.addEventListener('change', function () {
      var key = sel.value;
      if (!key) return;
      if (!deferProbe && key === current) return;   // report pages: re-picking the current lens is a no-op
      var sha = getSha();
      if (key === 'snapshots' || !deferProbe) { go(key, sha); return; }
      // VI Browser: the report may not exist for the live revision -> confirm first.
      var d = DOCTYPES[key];
      if (!d || !sha) { go(key, sha); return; }
      note.textContent = '';
      reportExists(d, sha)
        .then(function (ok) { if (ok) go(key, sha); else noReport(d); })
        .catch(function () { noReport(d); });
      function noReport(dd) {
        sel.value = current;
        note.textContent = 'No ' + dd.label + ' for this revision';
        setTimeout(function () { note.textContent = ''; }, 4000);
      }
    });
    wrap.appendChild(lbl); wrap.appendChild(sel); wrap.appendChild(note);
    // Upfront greying only when the revision is FIXED (report pages). The VI Browser
    // switches revision in place, so it checks availability at click time instead.
    if (!deferProbe && getSha()) {
      LENS_ORDER.forEach(function (key) {
        if (key === current || key === 'snapshots') return;
        var d = DOCTYPES[key]; if (!d) return;
        reportExists(d, getSha())
          .then(function (ok) { if (!ok) disableOpt(key, d); })
          .catch(function () { disableOpt(key, d); });
      });
    }
    function disableOpt(key, d) {
      for (var i = 0; i < sel.options.length; i++) {
        if (sel.options[i].value === key) { sel.options[i].disabled = true; sel.options[i].textContent = d.label + ' \u2014 none yet'; }
      }
    }
    return { wrap: wrap };
  }
  function optionLabel(c) {
    var msg = (c.message || '').split('\n')[0];
    return (c.short || (c.sha || '').slice(0, 7)) + (msg ? (' \u2014 ' + msg.slice(0, 42)) : '')
         + (c.sha === cfg.sha ? '  \u00b7 current' : '');
  }
  function loadRevisions(selects) {
    if (!DOC || !selects.length) return;
    loadRevisionList(base).then(function (list) {

      // Probe which revisions actually have a report of THIS type; the current
      // revision is known-present (it's the page we're on).
      var toCheck = list.filter(function (c) { return c.sha && c.sha !== cfg.sha; });
      var avail = {}; if (cfg.sha) avail[cfg.sha] = true;
      function fill() {
        var final = list.filter(function (c) { return c.sha === cfg.sha || avail[c.sha]; });
        if (!final.length) return;
        selects.forEach(function (sel) {
          sel.innerHTML = '';
          final.forEach(function (c) {
            var o = document.createElement('option'); o.value = c.sha; o.textContent = optionLabel(c);
            if (c.dep_only) o.dataset.deponly = '1';
            sel.appendChild(o);
          });
          sel.value = cfg.sha || final[0].sha;
          if (sel._lvciSync) sel._lvciSync();
        });
      }
      if (!toCheck.length) { fill(); return; }
      var idx = 0, done = 0, total = toCheck.length, CAP = 8;
      function next() {
        if (idx >= total) return;
        var c = toCheck[idx++];
        reportExists(DOC, c.sha)
          .then(function (ok) { avail[c.sha] = ok; })
          .then(function () { done++; if (done === total) fill(); else next(); });
      }
      for (var k = 0; k < Math.min(CAP, total); k++) next();
    }).catch(function () {});
  }

  // ── Version / update menu entry ───────────────────────────────────────────
  // A single home for the installed version + the update affordance (it replaces
  // the old always-on version pill). Used in the Tools dropdown and the mobile
  // menu; renderBadge() fills its label/tag/icon and toggles the amber "behind"
  // state. Always links to What's New (release notes / the update flow).
  //
  // The What's New / update flow is served from the SOURCE site so a client always
  // gets the latest update UI regardless of the (possibly old) tooling it has
  // installed; we pass this repo + installed version + source pointer as query
  // params (the page reads the client's catalog cross-origin for anything missing).
  // On the source's own dashboard (not a consumer) it stays the local page.
  function whatsNewUrl() {
    var parts = (srcRepo || '').split('/'), owner = parts[0], name = parts[1];
    if (!isConsumer || !owner || !name || srcRepo.toLowerCase() === (repo || '').toLowerCase())
      return base + '/whats-new.html';
    return 'https://' + owner + '.github.io/' + name + '/whats-new.html'
      + '?repo=' + encodeURIComponent(repo)
      + (verState.v ? '&from=' + encodeURIComponent(verState.v) : '')
      + '&src=' + encodeURIComponent(srcRepo)
      + '&ref=' + encodeURIComponent(srcRef || 'main');
  }
  function makeVerItem() {
    var a = document.createElement('a');
    a.className = 'lvci-ddver';
    a.href = whatsNewUrl();
    a.innerHTML = '<span class="lvci-ic">' + ICON.news + '</span>'
      + '<span class="lvci-ddver-label">What\u2019s new</span>'
      + '<span class="lvci-ddver-tag"></span>';
    return a;
  }

  // Share / print helpers. Deep linking is owned by each page: it keeps its
  // address bar pointed at the exact view shown (the dashboard's report links
  // already carry sha/src/type; the VI Browser mirrors the open VI + snapshot/
  // diff into the URL). The Share button just copies / opens / prints whatever
  // the current canonical URL is. A page may override that URL via
  // window.__lvciShareUrl() (the VI Browser builds it from its in-memory view
  // state) and customise printing via window.__lvciPrint() (e.g. print only the
  // embedded report iframe).
  function shareEnabled() { return !!DOC || ctx === 'vi-browser' || ctx === 'report-viewer'; }
  function shareWhat() { return DOC ? (DOC.label + ' report') : (ctx === 'vi-browser' ? 'view' : 'report'); }
  function shareHint() {
    if (ctx === 'vi-browser') return 'Opens the same VI and view (snapshot or diff) for whoever you send it to.';
    if (DOC) return 'Opens this revision\u2019s ' + DOC.label + ' report in the dashboard.';
    return 'Opens exactly what you\u2019re looking at now.';
  }
  function shareUrl() {
    try { if (typeof window.__lvciShareUrl === 'function') { var u = window.__lvciShareUrl(); if (u) return String(u); } } catch (e) {}
    return location.href;
  }
  function shareTitle() {
    var t = (document.title || '').trim();
    return t || ('LabVIEW CI' + (repo ? (' \u2014 ' + repo) : ''));
  }
  function doPrint() {
    try { if (typeof window.__lvciPrint === 'function') { window.__lvciPrint(); return; } } catch (e) {}
    try { window.print(); } catch (e) {}
  }
  // Copy via the async Clipboard API where available, else a hidden-textarea
  // fallback (http / older browsers). Resolves to true on success.
  function execCopy(txt) {
    try {
      var ta = document.createElement('textarea');
      ta.value = txt; ta.setAttribute('readonly', '');
      ta.style.position = 'fixed'; ta.style.top = '-1000px'; ta.style.opacity = '0';
      document.body.appendChild(ta); ta.select();
      var ok = document.execCommand('copy');
      document.body.removeChild(ta);
      return !!ok;
    } catch (e) { return false; }
  }
  function copyText(txt) {
    try {
      if (navigator.clipboard && navigator.clipboard.writeText) {
        return navigator.clipboard.writeText(txt).then(function () { return true; }, function () { return execCopy(txt); });
      }
    } catch (e) {}
    return Promise.resolve(execCopy(txt));
  }
  // Compact share for the mobile menu: native share sheet where the device offers
  // one, otherwise copy the link and confirm in the status line.
  function shareNow() {
    var url = shareUrl();
    try { if (navigator.share) { navigator.share({ title: shareTitle(), url: url }).catch(function () {}); return; } } catch (e) {}
    copyText(url).then(function (ok) { setStatus(ok ? 'Link copied to the clipboard.' : ('Copy this link: ' + esc(url)), ok ? 'ok' : null); });
  }
  // Desktop affordance: a share glyph in the actions cluster opening a popover
  // with the link (pre-selected), Copy / Open / Print, and a native Share button
  // where supported. Built only on shareable surfaces.
  function makeSharePopover() {
    var wrap = document.createElement('div'); wrap.className = 'lvci-dropdown';
    var btn = document.createElement('button');
    btn.type = 'button'; btn.className = 'lvci-share'; btn.id = 'lvci-share';
    btn.setAttribute('aria-haspopup', 'true'); btn.setAttribute('aria-expanded', 'false');
    btn.setAttribute('aria-label', 'Share this ' + shareWhat());
    btn.title = 'Share \u2014 copy a link to this ' + shareWhat();
    btn.innerHTML = ICON.share;
    wrap.appendChild(btn);

    var pop = document.createElement('div'); pop.className = 'lvci-dropdown-menu lvci-share-pop';
    var h = document.createElement('div'); h.className = 'lvci-share-h'; h.textContent = 'Share this ' + shareWhat();
    var input = document.createElement('input');
    input.className = 'lvci-share-url'; input.type = 'text'; input.readOnly = true;
    input.setAttribute('aria-label', 'Shareable link');
    var row = document.createElement('div'); row.className = 'lvci-share-row';
    var copyHtml = '<span class="lvci-ic">' + ICON.copy + '</span>Copy link';
    var copyBtn = document.createElement('button'); copyBtn.type = 'button'; copyBtn.className = 'lvci-btn accent';
    copyBtn.innerHTML = copyHtml;
    var openBtn = document.createElement('a'); openBtn.className = 'lvci-btn'; openBtn.target = '_blank'; openBtn.rel = 'noopener';
    openBtn.innerHTML = '<span class="lvci-ic">' + ICON.external + '</span>Open';
    var printBtn = document.createElement('button'); printBtn.type = 'button'; printBtn.className = 'lvci-btn';
    printBtn.innerHTML = '<span class="lvci-ic">' + ICON.printer + '</span>Print';
    row.appendChild(copyBtn); row.appendChild(openBtn); row.appendChild(printBtn);
    var nativeBtn = null;
    if (navigator.share) {
      nativeBtn = document.createElement('button'); nativeBtn.type = 'button'; nativeBtn.className = 'lvci-btn';
      nativeBtn.innerHTML = '<span class="lvci-ic">' + ICON.share + '</span>Share\u2026';
      row.appendChild(nativeBtn);
    }
    var hint = document.createElement('div'); hint.className = 'lvci-share-hint'; hint.textContent = shareHint();
    pop.appendChild(h); pop.appendChild(input); pop.appendChild(row); pop.appendChild(hint);
    wrap.appendChild(pop);

    function refresh() { var u = shareUrl(); input.value = u; openBtn.href = u; }
    var closeShare = function () { pop.classList.remove('open'); btn.classList.remove('open'); btn.setAttribute('aria-expanded', 'false'); };
    popoverCloses.push(closeShare);
    btn.addEventListener('click', function (e) {
      e.stopPropagation();
      var open = !pop.classList.contains('open');
      if (open) { closeOtherPopovers(closeShare); refresh(); }
      pop.classList.toggle('open', open);
      btn.classList.toggle('open', open);
      btn.setAttribute('aria-expanded', open ? 'true' : 'false');
      if (open) { try { input.focus(); input.select(); } catch (e2) {} }
    });
    pop.addEventListener('click', function (e) { e.stopPropagation(); });
    input.addEventListener('focus', function () { try { input.select(); } catch (e) {} });
    copyBtn.addEventListener('click', function () {
      copyText(shareUrl()).then(function (ok) {
        copyBtn.innerHTML = '<span class="lvci-ic">' + ICON.check + '</span>' + (ok ? 'Copied!' : 'Press \u2318/Ctrl+C');
        try { input.focus(); input.select(); } catch (e) {}
        setTimeout(function () { copyBtn.innerHTML = copyHtml; }, 1600);
      });
    });
    printBtn.addEventListener('click', function () { closeShare(); doPrint(); });
    if (nativeBtn) nativeBtn.addEventListener('click', function () {
      var u = shareUrl();
      try { navigator.share({ title: shareTitle(), url: u }).catch(function () {}); } catch (e) {}
    });
    document.addEventListener('click', closeShare);
    document.addEventListener('keydown', function (e) { if (e.key === 'Escape') closeShare(); });
    return wrap;
  }

  // ── Build the header DOM ──────────────────────────────────────────────────
  function build() {
    var style = document.createElement('style');
    style.setAttribute('data-lvci-header', '');
    style.textContent = CSS;
    document.head.appendChild(style);

    var hdr = document.createElement('header');
    hdr.className = 'lvci-hdr';

    // Brand — the repository this site is for, always visible, with a small
    // "LabVIEW CI" kicker so the product identity is kept. Falls back to the
    // product name when no repo can be derived. The whole mark links home.
    var brand = document.createElement('a');
    brand.className = 'lvci-brand';
    brand.href = navBase + '/';
    var displayRepo = brandRepo || repo;
    if (displayRepo) {
      var rname = displayRepo.split('/').pop();
      brand.title = displayRepo;                                      // full owner/name on hover
      brand.setAttribute('aria-label', 'LabVIEW CI \u2014 ' + displayRepo);
      brand.innerHTML = BRAND_SVG +
        '<span class="lvci-repo">' +
          '<span class="lvci-kicker">LabVIEW CI</span>' +
          '<span class="lvci-name">' + esc(rname) + '</span>' +
        '</span>';
    } else {
      brand.setAttribute('aria-label', 'LabVIEW CI');
      brand.innerHTML = BRAND_SVG + '<span class="lvci-repo"><span class="lvci-name">LabVIEW CI</span></span>';
    }
    hdr.appendChild(brand);

    // Primary nav
    var nav = document.createElement('nav');
    nav.className = 'lvci-nav';
    var activeKey = NAV_ACTIVE[ctx] || '';
    var dashboardItems = buildDashboardNavItems();
    nav.appendChild(makeNavDropdown('Dashboard', dashboardItems, activeKey === 'dashboard'));
    NAV.forEach(function (n) {
      var a = document.createElement('a');
      a.href = n.doc ? docUrl() : n.href;
      a.style.position = 'relative';
      if (n.doc ? docExternal() : n.newTab) { a.target = '_blank'; a.rel = 'noopener'; }
      if (n.key === activeKey) a.className = 'on';
      a.innerHTML = esc(n.label) + (n.soon ? ' <span class="lvci-soon">soon</span>' : '');
      nav.appendChild(a);
    });
    hdr.appendChild(nav);

    // Revision picker (per-revision report contexts only) — lives in the
    // persistent context bar below the header (built + mounted further down).
    var revBar = null;
    if (DOC) { revBar = makeRevPicker(); }
    else if (cfg.revPicker) { revBar = makeRevBar(); try { window.lvciRevBar = revBar; } catch (e) {} }

    // Actions
    var actions = document.createElement('div');
    actions.className = 'lvci-actions';
    // Live CI activity pill (transient) leads the cluster so the stable primary
    // action keeps its position when nothing is running.
    var runChip = document.createElement('a');
    runChip.className = 'lvci-actpill';
    runChip.id = 'lvci-actpill';
    if (repo) { runChip.href = 'https://github.com/' + repo + '/actions'; runChip.target = '_blank'; runChip.rel = 'noopener'; }
    runChip.innerHTML =
      '<span class="lvci-ap-seg lvci-ap-run"><span class="lvci-ap-spin" aria-hidden="true"></span><span class="lvci-ap-n">0</span></span>' +
      '<span class="lvci-ap-seg lvci-ap-queue"><span class="lvci-ap-dot" aria-hidden="true"></span><span class="lvci-ap-n">0</span></span>' +
      '<span class="lvci-ap-seg lvci-ap-fail"><span class="lvci-ap-dot" aria-hidden="true"></span><span class="lvci-ap-n">0</span></span>';
    actions.appendChild(runChip);
    buildActions().forEach(function (a) { actions.appendChild(actionEl(a, false)); });
    // Share — copy a deep link to (or print) exactly what's shown. Present on the
    // shareable surfaces (VI Browser snapshots/diffs + per-revision reports); the
    // link the page keeps in its address bar is what gets copied / printed / shared.
    if (shareEnabled()) actions.appendChild(makeSharePopover());
    // Desktop secondary navigation is intentionally two named menus everywhere:
    // Settings for configuration, Tools for page actions + Clients / About /
    // What's New / Appearance. This replaces the old Help + three-dot split.
    var secActions = buildSecondaryActions();
    var SETTINGS_KINDS = { configure: 1, vianalyzer: 1, unittests: 1, builds: 1 };
    var settingsItems = secActions.filter(function (a) { return SETTINGS_KINDS[a.kind]; });
    var toolsItems = secActions.filter(function (a) { return !SETTINGS_KINDS[a.kind]; });
    if (settingsItems.length) nav.appendChild(makeNavDropdown('Settings', settingsItems, activeKey === 'settings'));
    nav.appendChild(makeNavDropdown('Tools', toolsItems, activeKey === 'tools', { id: 'lvci-tools', updateDot: true, tools: true }));
    hdr.appendChild(actions);

    // Hamburger
    var burger = document.createElement('button');
    burger.className = 'lvci-burger';
    burger.id = 'lvci-burger';
    burger.setAttribute('aria-label', 'Menu');
    burger.innerHTML = ICON.burger + '<span class="lvci-mdot" aria-hidden="true"></span>';
    hdr.appendChild(burger);

    // Mobile menu
    var menu = document.createElement('div');
    menu.className = 'lvci-menu';
    dashboardItems.forEach(function (n) {
      var a = document.createElement('a');
      if (n.activity) wireActivityLink(a, n.activity, navBase, function () { menu.classList.remove('open'); });
      else a.href = n.href;
      a.innerHTML = iconHtml(n) + esc(n.label);
      menu.appendChild(a);
    });
    NAV.forEach(function (n) {
      var a = document.createElement('a');
      a.href = n.doc ? docUrl() : n.href;
      if (n.doc ? docExternal() : n.newTab) { a.target = '_blank'; a.rel = 'noopener'; }
      a.innerHTML = esc(n.label) + (n.soon ? ' <span class="lvci-soon">soon</span>' : '');
      menu.appendChild(a);
    });
    var acts = buildActions();
    if (acts.length) {
      var sep = document.createElement('div'); sep.className = 'lvci-sep'; menu.appendChild(sep);
      acts.forEach(function (a) { menu.appendChild(actionEl(a, true)); });
    }
    var secActs = buildSecondaryActions();
    if (secActs.length) {
      var sep = document.createElement('div'); sep.className = 'lvci-sep'; menu.appendChild(sep);
      secActs.forEach(function (a) {
        var el = actionEl(a, true);
        if (a.source) { el.style.display = 'none'; clientsEls.push(el); }
        if (a.about) aboutEls.push(el);
        menu.appendChild(el);
      });
    }
    // Share / Print (mobile) — the same shareable surfaces as the desktop popover;
    // Share prefers the device's native share sheet, falling back to copying.
    if (shareEnabled()) {
      var sepSh = document.createElement('div'); sepSh.className = 'lvci-sep'; menu.appendChild(sepSh);
      var shBtn = document.createElement('button'); shBtn.type = 'button'; shBtn.className = 'lvci-m';
      shBtn.innerHTML = '<span class="lvci-ic">' + ICON.share + '</span>' + esc('Share this ' + shareWhat());
      shBtn.addEventListener('click', function () { menu.classList.remove('open'); shareNow(); });
      menu.appendChild(shBtn);
      var prBtn = document.createElement('button'); prBtn.type = 'button'; prBtn.className = 'lvci-m';
      prBtn.innerHTML = '<span class="lvci-ic">' + ICON.printer + '</span>Print';
      prBtn.addEventListener('click', function () { menu.classList.remove('open'); doPrint(); });
      menu.appendChild(prBtn);
    }
    // Version / update entry (mobile) — same single home as the dropdown.
    var sepV = document.createElement('div'); sepV.className = 'lvci-sep'; menu.appendChild(sepV);
    var mVer = makeVerItem(); menu.appendChild(mVer); verEls.push(mVer);
    // Appearance (theme) control in the mobile menu
    var sepT = document.createElement('div'); sepT.className = 'lvci-sep'; menu.appendChild(sepT);
    menu.appendChild(themeControl());
    burger.addEventListener('click', function () { menu.classList.toggle('open'); });

    // Populate the revision picker(s) once mounted (async; filters to revisions
    // that actually have a report of this type).
    if (DOC) {
      loadRevisions(revBar ? [revBar.sel] : []);
    }

    // Status + token panel (used by re-run)
    var status = document.createElement('div'); status.id = 'lvci-status'; status.className = 'lvci-status';
    var tokp = document.createElement('div'); tokp.id = 'lvci-tok'; tokp.className = 'lvci-tok';

    // Rebuild banner (dashboard only) — hidden until the activity poll detects
    // the page-rebuild/publish run, then shown with a live link to it.
    var rebuild = null;
    if (REBUILD_ON) {
      rebuild = document.createElement('div');
      rebuild.id = 'lvci-rebuild';
      rebuild.className = 'lvci-rebuild';
      rebuild.setAttribute('role', 'status');
      rebuild.setAttribute('aria-live', 'polite');
      rebuild.innerHTML =
        '<span class="lvci-rb-spin" aria-hidden="true"></span>' +
        '<span class="lvci-rb-txt"><strong>Updating this dashboard\u2026 </strong>' +
        '<span class="lvci-rb-sub">A new version is being compiled by </span>' +
        '<a target="_blank" rel="noopener" href="https://github.com/' + repo + '/actions">the build workflow \u2197</a>' +
        '<span class="lvci-rb-sub">. This page refreshes automatically when it\u2019s done.</span></span>';
    }

    var depbar = document.createElement('div');
    depbar.id = 'lvci-depbar';
    depbar.className = 'lvci-depbar';
    depbar.setAttribute('role', 'status');
    depbar.setAttribute('aria-live', 'polite');
    depbar.innerHTML =
      '<span class="lvci-dep-spin" aria-hidden="true"></span>' +
      '<span class="lvci-dep-txt"><strong>Updating repo containers for new dependencies. </strong>' +
      '<span class="lvci-dep-sub">Pipeline tasks that need the worker image wait until the rebuilt container is saved to GHCR by </span>' +
      '<a target="_blank" rel="noopener" href="https://github.com/' + repo + '/actions">Build LabVIEW CI Image \u2197</a>' +
      '<span class="lvci-dep-sub">.</span></span>';

    // Persistent "dependencies pending" banner — shown on every page (read from
    // deps-pending.json published by the dashboard build) until the repo's worker
    // container(s) are updated with its declared VIPC/Dragon dependencies. Unlike
    // the transient rebuild bar above, this stays up until the update completes.
    var pendbar = document.createElement('div');
    pendbar.id = 'lvci-pendbar';
    pendbar.className = 'lvci-depbar';
    pendbar.setAttribute('role', 'alert');
    pendbar.innerHTML =
      '<span class="lvci-dep-txt"><strong>\u26A0\uFE0F Dependencies need to be installed into your containers. </strong>' +
      '<span class="lvci-dep-sub">Your project declares dependencies that are not yet baked into the worker container(s); container CI may error or show broken code until you update them. </span>' +
      '<a href="' + navBase + '/dependencies.html">Review &amp; update dependencies \u2197</a></span>';

    // Global attention bar (failure banner) — hidden until the activity poll
    // finds a workflow whose newest run failed; names it + links to the run.
    var alertBar = document.createElement('div');
    alertBar.id = 'lvci-alertbar';
    alertBar.className = 'lvci-alertbar';
    alertBar.setAttribute('role', 'alert');
    alertBar.setAttribute('aria-live', 'polite');
    alertBar.innerHTML =
      '<span class="lvci-alert-ico" aria-hidden="true"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M10.3 3.9 1.8 18a2 2 0 0 0 1.7 3h17a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0z"/><line x1="12" y1="9" x2="12" y2="13.5"/><line x1="12" y1="17" x2="12.01" y2="17"/></svg></span>' +
      '<span class="lvci-alert-msg"></span>' +
      '<a class="lvci-alert-cta" target="_blank" rel="noopener">View error \u2197</a>' +
      '<button type="button" class="lvci-alert-x" aria-label="Dismiss this alert"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><line x1="6" y1="6" x2="18" y2="18"/><line x1="18" y1="6" x2="6" y2="18"/></svg></button>';
    alertBar.querySelector('.lvci-alert-x').addEventListener('click', function () {
      var id = alertBar.getAttribute('data-top');
      if (id) alertDismiss(id);
      renderAlert();
    });

    // Update-available bar — hidden until loadVersion finds the source tooling is
    // newer than this (consumer) repo's installed build; nudges the owner to
    // upgrade and links to What's New. Dismissible per target version.
    var updBar = document.createElement('div');
    updBar.id = 'lvci-updbar';
    updBar.className = 'lvci-updbar';
    updBar.setAttribute('role', 'status');
    updBar.setAttribute('aria-live', 'polite');
    updBar.innerHTML =
      '<span class="lvci-upd-ico" aria-hidden="true">' + ICON.update + '</span>' +
      '<span class="lvci-upd-msg"></span>' +
      '<a class="lvci-upd-cta" href="#"></a>' +
      '<button type="button" class="lvci-upd-x" aria-label="Dismiss this update notice"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><line x1="6" y1="6" x2="18" y2="18"/><line x1="18" y1="6" x2="6" y2="18"/></svg></button>';
    updBar.querySelector('.lvci-upd-x').addEventListener('click', function () {
      var to = updBar.getAttribute('data-to');
      if (to) updBarDismiss(to);
      renderUpdateBar(true);
    });

    // Persistent context bar — the revision selector for per-revision reports,
    // in one consistent place under the header (only built when there's a revision).
    // On config pages it instead holds the Settings sub-nav (section tabs).
    var ctxbar = null;
    var isSettings = (NAV_ACTIVE[ctx] === 'settings');
    if (revBar || ctx === 'vi-browser' || ctx === 'dashboard' || isSettings || cfg.subnav) { ctxbar = document.createElement('div'); ctxbar.id = 'lvci-ctxbar'; ctxbar.className = 'lvci-ctxbar'; if (revBar) ctxbar.appendChild(revBar.wrap); if (isSettings) ctxbar.appendChild(makeSettingsNav()); var pageSub = makePageSubnav(); if (pageSub) ctxbar.appendChild(pageSub); }

    // ── Mount at the very top of <body> ──────────────────────────────────────
    // Some pages use <body> ITSELF as a full-height flex/grid layout container
    // (e.g. the VI Browser: `body{display:flex;height:100vh}` for a sidebar +
    // main pane). Inserting the header as a plain sibling would make it a flex/
    // grid ITEM laid out *beside* that content instead of above it. Detect that
    // and move the page's content into a wrapper that inherits the original
    // layout, so <body> becomes a vertical stack (header on top, content below).
    var cs = getComputedStyle(document.body);
    if (cs.display.indexOf('flex') >= 0 || cs.display.indexOf('grid') >= 0) {
      var wrap = document.createElement('div');
      wrap.className = 'lvci-content';
      // Re-home the page's own layout onto the wrapper (copy BEFORE mutating
      // <body>, since `cs` is a live computed-style reference).
      ['display', 'flexDirection', 'flexWrap', 'gap', 'rowGap', 'columnGap',
       'alignItems', 'alignContent', 'justifyContent', 'justifyItems',
       'gridTemplateColumns', 'gridTemplateRows', 'gridTemplateAreas',
       'gridAutoFlow', 'gridAutoRows', 'gridAutoColumns',
       'overflowX', 'overflowY'
      ].forEach(function (p) { wrap.style[p] = cs[p]; });
      wrap.style.flex = '1 1 auto';
      wrap.style.minHeight = '0';
      wrap.style.minWidth = '0';
      while (document.body.firstChild) wrap.appendChild(document.body.firstChild);
      document.body.appendChild(wrap);
      document.body.style.display = 'flex';
      document.body.style.flexDirection = 'column';
    }

    var first = document.body.firstChild;
    document.body.insertBefore(tokp, first);
    document.body.insertBefore(status, tokp);
    document.body.insertBefore(menu, status);
    document.body.insertBefore(hdr, menu);
    if (ctxbar) document.body.insertBefore(ctxbar, menu);     // persistent context bar (revision selector) under the header
    document.body.insertBefore(depbar, menu);                 // dependency/container rebuild bar
    document.body.insertBefore(pendbar, menu);                // persistent "dependencies pending" bar
    document.body.insertBefore(alertBar, menu);               // global attention bar, directly under the header
    document.body.insertBefore(updBar, menu);                 // update-available nudge (consumer repos)
    if (rebuild) document.body.insertBefore(rebuild, menu);   // directly under the bar

    renderBadge();   // initial paint (idle, or the persisted "Updating" flag)
    // Signal pages that the header (and its #lvci-ctxbar context bar) is mounted,
    // so a page can move its own revision selector / controls into the shared bar.
    try { window.lvciHeaderReady = true; document.dispatchEvent(new CustomEvent('lvci:ready')); } catch (e) {}

    // Persistent pending-dependencies banner (every page): the dashboard build
    // publishes deps-pending.json at the Pages root when the repo declares VIPC/
    // Dragon dependencies that are not yet baked into its worker container(s).
    try {
      fetch(base + '/deps-pending.json', { cache: 'no-cache' })
        .then(function (r) { return r.ok ? r.json() : null; })
        .then(function (d) {
          if (!d || !d.pending) return;
          var total = (d.packages || []).length + (d.dragon || []).length;
          if (total) {
            var sub = pendbar.querySelector('.lvci-dep-sub');
            if (sub) sub.textContent = 'Your project declares ' + total + ' dependency item' + (total === 1 ? '' : 's') +
              ' (in VIPC or .dragon files) not yet baked into the worker container(s); container CI may error or show broken code until you update them. ';
          }
          pendbar.classList.add('show');
        })
        .catch(function () {});
    } catch (e) {}

    // VI Browser owns #commit-select and moves it into this context bar on the
    // lvci:ready event above. Document switching now lives in the Dashboard menu,
    // which resolves that live selected revision before navigating.
  }

  // ── Badge state ───────────────────────────────────────────────────────────
  // Header status surfaces (all fed by the same poll + version check, so they
  // never fight over one element):
  //   - run chip   : a transient "N running" pill in the bar, only while CI runs
  //   - menu entry : the installed version + What's New / Update affordance
  //                  (amber "Update available" when behind; "Updating to vX..."
  //                  while a dispatched update is in flight)
  //   - menu dot   : one amber dot on the More button / hamburger whenever an
  //                  update is available or in progress
  var verState = { v: '', behind: false, to: '' };
  var runState = { active: 0, running: 0, queued: 0, names: [], list: [] };
  var failState = { list: [] };
  var verEls = [];
  var clientsEls = [];
  var aboutEls = [];
  // ── Tooling-upgrade (in-flight) state ────────────────────────────────
  // Distinct from a routine page rebuild: a REAL tooling update is being applied
  // — the apply-tooling-update workflow is running, OR an update PR was merged
  // and this repo's committed catalog is now ahead of the deployed build. While
  // set, the version menu entry reads "Updating to vX…" and links to the
  // in-flight action; the re-start "Update available" affordance is suppressed so
  // a second update can't be kicked off on top of the one already on its way.
  var isConsumer = false;                 // set by loadVersion (false on the source repo)
  var upState = { active: false, to: '', url: '' };
  var headV = '', headVAt = 0;            // this repo's committed (HEAD) catalog version
  var lastAct = [];                       // most recent active-run list (from the activity poll)
  // Page-rebuild banner: only the dashboard shows it (there "this page is being
  // regenerated" is literally true). buildWas remembers the prior poll so we can
  // auto-refresh exactly once when an in-flight rebuild finishes.
  var REBUILD_ON = (ctx === 'dashboard');
  var buildWas = false;
  var buildState = { active: false, url: '' };
  var depBuildState = { active: false, url: '' };

  // Re-point the About menu entries at the (possibly relocated) source site once
  // loadVersion has refined srcRepo — see aboutUrl(). Keeps href + new-tab target
  // in sync; a no-op until the menu has been built.
  function refreshAbout() {
    var href = aboutUrl(), ext = aboutExternal();
    aboutEls.forEach(function (el) {
      el.href = href;
      if (ext) { el.target = '_blank'; el.rel = 'noopener'; }
      else { el.removeAttribute('target'); el.removeAttribute('rel'); }
    });
  }

  // ── Version badge: read same-origin catalog.json for the installed version,
  //    and (on consumer repos) compare to the source repo to flag an update. ─
  function loadVersion() {
    fetch(base + '/catalog.json', { cache: 'no-cache' }).then(function (r) { return r.ok ? r.json() : null; })
      .then(function (cat) {
        if (!cat) return;
        var v = cat.version || '';
        verState.v = v;
        var upd = updGet();
        if (upd && cmpVer(v, upd.v) >= 0) updClear();                        // deployed caught up
        renderBadge();
        var src = (cat.source && cat.source.repo) || '';
        if (src) srcRepo = src;   // refine the Apply-to-New-Repo target from the live catalog
        refreshAbout();           // re-point About at the (now known) source site's faq.html
        isConsumer = !!(src && repo && src.toLowerCase() !== repo.toLowerCase());
        renderBadge();   // isConsumer/srcRepo known -> point What's New at the source site
        if (!isConsumer) { revealClients(); return; }   // root repo: surface Clients even before a scan has published clients.json
        // Now that the deployed version + consumer status are known, check right
        // away whether a tooling upgrade is mid-flight (don't wait for the poll).
        refreshHeadCatalog().then(resolveUpgrade);
        var ref = (cat.source && cat.source.ref) || 'main';
        srcRef = ref;
        // Follow the relocation pointer (.github/labview-ci/source.json): if the
        // tooling moved to a new official home, compare against THAT repo's latest
        // version so the "update available" dot reflects the real source. No-op when
        // the pointer is absent/unreachable or already names the recorded source.
        fetch('https://raw.githubusercontent.com/' + src + '/' + ref + '/.github/labview-ci/source.json', { cache: 'no-cache' })
          .then(function (r) { return r.ok ? r.json() : null; })
          .then(function (p) {
            if (p && p.repo && p.repo.toLowerCase() !== src.toLowerCase()) { src = p.repo; ref = p.ref || ref; srcRepo = src; srcRef = ref; refreshAbout(); }
            return fetch('https://raw.githubusercontent.com/' + src + '/' + ref + '/.github/labview-ci/catalog.json', { cache: 'no-cache' });
          })
          .then(function (r) { return r.ok ? r.json() : null; })
          .then(function (s) {
            if (!s || !s.version) return;
            // Release channel (cumulative): by default only a newer STABLE release
            // counts as an update, so a freshly published dev/beta build never nags.
            // The owner opts up per browser (lvci_update_channel = 'stable' | 'beta'
            // | 'dev'). Graceful fallback: a source with no stable/beta marked
            // behaves exactly as before (the latest published version is the target).
            var chan = 'beta';
            try { chan = localStorage.getItem('lvci_update_channel') || 'beta'; } catch (e) {}
            var betaV = s.betaVersion || '', stableV = s.stableVersion || '', tip = s.version;
            var newerV = function (a, b) { return !a ? b : (!b ? a : (cmpVer(a, b) >= 0 ? a : b)); };
            var target = (chan === 'dev') ? tip
              : (chan === 'beta' ? (newerV(betaV, stableV) || tip) : (stableV || tip));
            if (target && cmpVer(target, v) > 0) { verState.behind = true; verState.to = target; renderBadge(); }
          }).catch(function () {});
      }).catch(function () {});
  }
  // ── Clients registry (root/source repo only) ────────────────────────────
  // The Clients nav entry belongs to the ROOT repo that originates this tooling.
  // It is revealed when this repo is the source (no upstream — see loadVersion)
  // or once the published clients.json is read (then it also shows a count). It
  // stays hidden on consumer sites.
  function revealClients(count) {
    clientsEls.forEach(function (a) {
      a.style.display = '';
      if (count) { var c = a.querySelector('.lvci-ncount'); if (c) { c.textContent = count; c.hidden = false; } }
    });
  }
  function loadClients() {
    if (!clientsEls.length) return;
    fetch(base + '/clients.json', { cache: 'no-cache' }).then(function (r) { return r.ok ? r.json() : null; })
      .then(function (data) { if (data) revealClients((data.clients && data.clients.length) || data.count || 0); })
      .catch(function () {});
  }
  function cmpVer(a, b) {
    var pa = String(a).split('.').map(Number), pb = String(b).split('.').map(Number);
    for (var i = 0; i < 3; i++) { var d = (pa[i] || 0) - (pb[i] || 0); if (d) return d > 0 ? 1 : -1; }
    return 0;
  }

  // ── Page-rebuild banner (dashboard) ──────────────────────────────
  // While the workflow that regenerates THIS dashboard (dashboard-pages.yml) —
  // or the GitHub Pages publish that follows it — is in flight, the page on
  // screen is stale. Surface a banner naming + linking the run, and refresh once
  // it finishes so the freshly built version appears without a manual reload.
  function isDashGen(w) { return (w.path || '').toLowerCase().indexOf('dashboard-pages.yml') >= 0; }
  function isPagesPub(w) { return (w.name || '').toLowerCase() === 'pages build and deployment'; }
  function isWorkerBuild(w) { return (w.path || '').toLowerCase().indexOf('build-labview-image.yml') >= 0 || (w.name || '').toLowerCase() === 'build labview ci image'; }
  function pickRebuild(runs) {
    var gen = null, pub = null;
    for (var i = 0; i < runs.length; i++) {
      if (isDashGen(runs[i])) { if (!gen) gen = runs[i]; }
      else if (isPagesPub(runs[i])) { if (!pub) pub = runs[i]; }
    }
    return gen || pub;                          // prefer the generator over the bare publish
  }
  function pickWorkerBuild(runs) {
    for (var i = 0; i < runs.length; i++) if (isWorkerBuild(runs[i])) return runs[i];
    return null;
  }
  function renderDependencyBuild(run) {
    var bar = document.getElementById('lvci-depbar');
    if (!bar) return;
    if (!run) {
      bar.classList.remove('show');
      depBuildState = { active: false, url: '' };
      return;
    }
    var url = run.html_url || ('https://github.com/' + repo + '/actions/workflows/build-labview-image.yml');
    var a = bar.querySelector('a');
    if (a) { a.href = url; a.textContent = (run.name || 'Build LabVIEW CI Image') + ' \u2197'; }
    depBuildState = { active: true, url: url };
    bar.classList.add('show');
  }
  function renderRebuild(run) {
    var card = document.getElementById('lvci-rebuild');
    if (!card) return;
    if (!run) {
      card.classList.remove('show');
      if (buildState.active) { buildState = { active: false, url: '' }; renderBadge(); }
      return;
    }
    var url = run.html_url || ('https://github.com/' + repo + '/actions');
    var a = card.querySelector('a');
    if (a) {
      a.href = url;
      a.textContent = (run.name || 'the build workflow') + ' \u2197';
    }
    buildState = { active: true, url: url };
    card.classList.add('show');
    renderBadge();
  }
  var RELOAD_KEY = 'lvci_rebuild_reload';
  function anyModalOpen() {
    var ids = ['lvci-modal', 'cidash-run-modal', 'cidash-q-modal'];
    for (var i = 0; i < ids.length; i++) {
      var m = document.getElementById(ids[i]);
      if (m && m.style && m.style.display && m.style.display !== 'none') return true;
    }
    return false;
  }
  function autoRefresh() {
    // Don't reload out from under an open dialog; throttle so back-to-back
    // builds can't spin the page (at most one auto-reload per 12 s).
    if (anyModalOpen()) return;
    var last = 0;
    try { last = parseInt(sessionStorage.getItem(RELOAD_KEY) || '0', 10) || 0; } catch (e) {}
    if (Date.now() - last < 12000) return;
    try { sessionStorage.setItem(RELOAD_KEY, String(Date.now())); } catch (e) {}
    // brief settle for the Pages CDN edge to serve the new deploy
    setTimeout(function () { if (!anyModalOpen()) location.reload(); }, 4000);
  }

  // ── Tooling-upgrade detection (consumer repos) ─────────────────────────
  // Refresh this repo's committed (HEAD) catalog version (throttled). A value
  // ahead of the deployed build means an update PR was merged and is deploying
  // right now. On the source repo it also names the version currently being
  // compiled by dashboard-pages.yml. Private/thin repos without a vendored
  // catalog simply 404 here and fall back to the run check + optimistic flags.
  function refreshHeadCatalog() {
    if (!repo) return Promise.resolve();
    if (Date.now() - headVAt < 30000) return Promise.resolve();        // at most ~every 30s
    headVAt = Date.now();
    return fetch('https://raw.githubusercontent.com/' + repo + '/HEAD/.github/labview-ci/catalog.json', { cache: 'no-cache' })
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (c) { if (c && c.version) headV = String(c.version); })
      .catch(function () {});
  }
  // Decide whether a tooling upgrade is in flight (from the active runs + the
  // committed catalog), record where it links, and repaint. Safe to call often.
  function resolveUpgrade() {
    var act = lastAct || [];
    if (!isConsumer || !repo) {
      if (upState.active) { upState = { active: false, to: '', url: '' }; renderBadge(); }
      return;
    }
    // 1) The update workflow itself is running (just dispatched / opening the PR).
    var atu = null;
    for (var i = 0; i < act.length; i++) {
      if (((act[i].path || '') + '').toLowerCase().indexOf('apply-tooling-update.yml') >= 0) { atu = act[i]; break; }
    }
    if (atu) {
      upState = { active: true, to: verState.to || '', url: atu.html_url || ('https://github.com/' + repo + '/actions') };
      renderBadge(); return;
    }
    // 2) An update PR was merged: the committed catalog is ahead of the deployed
    //    build, so the dashboard is rebuilding/deploying it — link to that run.
    if (headV && verState.v && cmpVer(headV, verState.v) > 0) {
      var dep = pickRebuild(act);
      upState = { active: true, to: headV, url: (dep && dep.html_url) || ('https://github.com/' + repo + '/actions') };
      renderBadge(); return;
    }
    if (upState.active) { upState = { active: false, to: '', url: '' }; renderBadge(); }
  }

  // ── CI activity: poll the Actions API for in-flight runs. While any are
  //    queued/running, the badge above shows "N running" (with a spinner) in
  //    place of the version — so the dashboard visibly reflects work in
  //    progress (e.g. a deploy you're waiting on) instead of looking stale.
  //    Public repos work unauthenticated; private repos reuse the dashboard's
  //    dispatch token. Conditional (ETag) requests keep it within the
  //    unauthenticated rate budget — a 304 doesn't count and we poll only while
  //    the tab is visible. ──────────────────────────────────────────────────
  var ACT_MS = 60000, actEtag = '', actTimer = null;
  function ghHeaders() {
    var h = { 'Accept': 'application/vnd.github+json', 'X-GitHub-Api-Version': '2022-11-28' };
    var t = tok(); if (t) h['Authorization'] = 'Bearer ' + t;
    if (actEtag) h['If-None-Match'] = actEtag;
    return h;
  }
  var ACTIVE = { in_progress: 1, queued: 1, waiting: 1, pending: 1, requested: 1 };
  // Global attention bar: the most-recent still-failing workflows, with a
  // per-failure dismiss remembered in localStorage (a NEW failure re-appears).
  var ALERT_DKEY = 'lvci_alert_dismissed';
  // Owner-only maintenance chores, keyed by workflow file basename. A failed run
  // of one of these is actionable only by the maintainer (a mirror token, not
  // this repo's CI health), so it never feeds the public failure banner or the
  // pill's fail segment — the owner still sees it under GitHub's Actions tab.
  var MAINT_WORKFLOWS = { 'sync-gitlab-distribution.yml': 1 };
  function isMaintWorkflow(w) {
    return !!MAINT_WORKFLOWS[String(w.path || '').split('/').pop().toLowerCase()];
  }
  function alertDismissedIds() {
    try { var a = JSON.parse(localStorage.getItem(ALERT_DKEY) || '[]'); return Array.isArray(a) ? a : []; } catch (e) { return []; }
  }
  function alertDismiss(id) {
    try { var a = alertDismissedIds(); if (a.indexOf(String(id)) < 0) a.push(String(id)); while (a.length > 50) a.shift(); localStorage.setItem(ALERT_DKEY, JSON.stringify(a)); } catch (e) {}
  }
  // Update-available bar: remembers the LAST target version the owner dismissed,
  // so a freshly released newer version brings the nudge back.
  var UPDBAR_DKEY = 'lvci_updbar_dismissed';
  function updBarDismissedVer() { try { return String(localStorage.getItem(UPDBAR_DKEY) || ''); } catch (e) { return ''; } }
  function updBarDismiss(v) { try { localStorage.setItem(UPDBAR_DKEY, String(v || '')); } catch (e) {} }
  // Paint the update-available nudge. `behind` is the same signal the version menu
  // entry uses (a newer source build exists and no update is mid-flight); the bar
  // stays hidden while an update is running or once dismissed for this version.
  function renderUpdateBar(behind) {
    var bar = document.getElementById('lvci-updbar');
    if (!bar) return;
    var to = verState.to;
    var show = behind && isConsumer && !!to && updBarDismissedVer() !== String(to);
    if (!show) { bar.classList.remove('show'); bar.removeAttribute('data-to'); return; }
    var msg = bar.querySelector('.lvci-upd-msg');
    if (msg) msg.innerHTML = '<strong>A newer version of LabVIEW CI is available.</strong> '
      + 'You\u2019re on <code>v' + esc(verState.v) + '</code>; <code>v' + esc(to) + '</code> is ready to install.';
    var cta = bar.querySelector('.lvci-upd-cta');
    if (cta) { cta.href = whatsNewUrl(); cta.textContent = 'See what\u2019s new & update \u2192'; }
    bar.setAttribute('data-to', String(to));
    bar.classList.add('show');
  }
  function renderAlert() {
    var bar = document.getElementById('lvci-alertbar');
    if (!bar) return;
    var dismissed = alertDismissedIds();
    var show = (failState.list || []).filter(function (w) { return dismissed.indexOf(String(w.id)) < 0; });
    if (!show.length) { bar.classList.remove('show'); bar.removeAttribute('data-top'); return; }
    var top = show[0], n = show.length;
    var name = top.name || top.display_title || 'A workflow';
    var sha = (top.head_sha || '').slice(0, 7);
    var msg = bar.querySelector('.lvci-alert-msg');
    if (msg) {
      msg.innerHTML = '<strong>' + esc(n === 1 ? '1 activity needs your attention' : (n + ' activities need your attention'))
        + '</strong> \u2014 ' + esc(name) + ' failed' + (sha ? (' on <code>' + esc(sha) + '</code>') : '') + '.';
    }
    var cta = bar.querySelector('.lvci-alert-cta');
    if (cta) cta.href = top.html_url || (repo ? ('https://github.com/' + repo + '/actions') : '#');
    bar.setAttribute('data-top', String(top.id));
    bar.classList.add('show');
  }
  function loadActivity() {
    if (!repo) return;
    fetch('https://api.github.com/repos/' + repo + '/actions/runs?per_page=20', { cache: 'no-cache', headers: ghHeaders() })
      .then(function (r) {
        if (r.status === 304) return null;                                  // unchanged (free — no rate cost)
        var et = r.headers.get('ETag'); if (et) actEtag = et;
        return r.ok ? r.json() : { workflow_runs: [] };                     // 403/404/etc → treat as idle
      })
      .then(function (d) {
        if (!d) return;                                                     // 304: keep current badge
        var act = (d.workflow_runs || []).filter(function (w) { return ACTIVE[w.status]; });
        runState.active = act.length;
        runState.running = act.filter(function (w) { return w.status === 'in_progress'; }).length;
        runState.queued = act.length - runState.running;
        runState.list = act.slice(0, 8);
        runState.names = [];
        act.slice(0, 6).forEach(function (w) {
          var n = w.name || w.display_title || 'workflow run';
          if (runState.names.indexOf(n) < 0) runState.names.push(n);
        });
        // Failed activities (newest run per workflow that ended in failure) feed
        // BOTH the pill's fail segment and the global attention bar. The runs list
        // is newest-first, so the first run seen per workflow is its latest; a
        // workflow that has since gone green is therefore not flagged, and
        // maintenance workflows (MAINT_WORKFLOWS) are never flagged at all.
        var seenWf = {}, fails = [];
        (d.workflow_runs || []).forEach(function (w) {
          var key = w.path || w.name || ('wf' + w.workflow_id);
          if (seenWf[key]) return;
          seenWf[key] = 1;
          if (w.status === 'completed' && w.conclusion === 'failure' && !isMaintWorkflow(w)) fails.push(w);
        });
        failState.list = fails;
        renderBadge();
        renderAlert();
        if (REBUILD_ON) {
          var rb = pickRebuild(act);
          renderRebuild(rb);
          if (buildWas && !rb) autoRefresh();   // a rebuild we were showing just finished
          buildWas = !!rb;
        }
        renderDependencyBuild(pickWorkerBuild(act));
        // Tooling-upgrade indicator: remember the active runs, refresh the
        // committed catalog, then decide whether a real update is mid-flight (so
        // the menu links to it instead of offering to start another).
        lastAct = act;
        refreshHeadCatalog().then(function () { resolveUpgrade(); if (buildState.active) renderBadge(); });
      }).catch(function () { /* network blip: keep prior badge state */ });
  }
  function startActivity() {
    if (!repo) return;
    loadActivity();
    function arm() { if (!actTimer) actTimer = setInterval(function () { if (!document.hidden) loadActivity(); }, ACT_MS); }
    function disarm() { if (actTimer) { clearInterval(actTimer); actTimer = null; } }
    arm();
    document.addEventListener('visibilitychange', function () {
      if (document.hidden) disarm(); else { loadActivity(); arm(); }
    });
  }

  // In-flight update paint (preserves the dashboard's "Updating…" UX). The
  // What's New dialog dispatches the update then calls window.lvciMarkUpdating
  // (directly when standalone, or via window.parent from the dashboard modal).
  var UPD_KEY = 'lvci_updating', UPD_TTL = 30 * 60 * 1000;
  function updGet() {
    try { var o = JSON.parse(localStorage.getItem(UPD_KEY) || 'null'); if (o && (Date.now() - (o.ts || 0)) < UPD_TTL && o.v) return o; } catch (e) {}
    return null;
  }
  function updClear() { try { localStorage.removeItem(UPD_KEY); } catch (e) {} }
  // Paint all three surfaces from the current verState/runState (+ the optimistic
  // update flag). Safe to call repeatedly; each surface no-ops when not on the page.
  function renderBadge() {
    var upd = updGet();
    var localUpdating = !!(upd && (!verState.v || cmpVer(verState.v, upd.v) < 0));
    // A page rebuild only signals an UPGRADE when the version being compiled is
    // strictly newer than what's deployed. A same-version rebuild (the routine
    // status/push/schedule regeneration of THIS dashboard) is not an upgrade;
    // treating it as one is what pinned the menu at "Updating to vX -> vX".
    var buildTo = (buildState.active && headV && (!verState.v || cmpVer(headV, verState.v) > 0)) ? headV : '';
    // The version an in-flight update would land on, from the strongest signal:
    // a running update workflow, a merged-and-deploying build, this browser's
    // optimistic flag, or a rebuild that IS carrying a newer version.
    var upTo = upState.active ? (upState.to || (upd && upd.v) || verState.to || '')
                              : (buildTo || (upd ? upd.v : ''));
    // Only present the menu as "Updating" when the target is a REAL advance over
    // the deployed version. Without this guard a stuck or re-dispatched
    // same-version update run, a stale optimistic flag, or a routine same-version
    // rebuild would pin the menu at "Updating to vX -> vX" forever (the version
    // never moves, so the in-flight state never clears). When the deployed version
    // isn't known yet we can't compare, so allow it (the tag then shows just the
    // target, with no "from -> to" arrow).
    var realAdvance = !!upTo && (!verState.v || cmpVer(upTo, verState.v) > 0);
    var updating = (upState.active || localUpdating || !!buildTo) && realAdvance;
    var upUrl = upState.active ? upState.url
              : (buildState.active ? buildState.url : (repo ? ('https://github.com/' + repo + '/pulls') : ''));
    var behind = !updating && verState.behind;
    var hasUpdate = updating || behind;

    // 1) Live CI activity pill — run / queue / fail counts (each segment shown
    //    only when > 0; the pill hides entirely when there is no activity).
    var pill = document.getElementById('lvci-actpill');
    if (pill) {
      var nFail = (failState.list || []).length;
      var segs = [['lvci-ap-run', runState.running || 0], ['lvci-ap-queue', runState.queued || 0], ['lvci-ap-fail', nFail]];
      var anyOn = false;
      segs.forEach(function (s) {
        var seg = pill.querySelector('.' + s[0]); if (!seg) return;
        var on = s[1] > 0; if (on) anyOn = true;
        seg.classList.toggle('on', on);
        var ne = seg.querySelector('.lvci-ap-n'); if (ne) ne.textContent = s[1];
      });
      pill.classList.toggle('show', anyOn);
      var parts = [];
      if (runState.running) parts.push(runState.running + ' running');
      if (runState.queued) parts.push(runState.queued + ' queued');
      if (nFail) parts.push(nFail + ' failed');
      pill.title = parts.length ? ('CI activity: ' + parts.join(', ') + '\nOpen the repository\u2019s Actions list.') : '';
    }

    // 2) Update-available dot on the menu trigger(s).
    ['lvci-tools', 'lvci-burger'].forEach(function (id) {
      var el = document.getElementById(id);
      if (el) el.classList.toggle('has-update', hasUpdate);
    });

    // 3) Version / update entry inside the menu(s).
    verEls.forEach(function (a) {
      var ic = a.querySelector('.lvci-ic'),
          lbl = a.querySelector('.lvci-ddver-label'),
          tag = a.querySelector('.lvci-ddver-tag');
      a.classList.toggle('behind', hasUpdate);
      if (updating) {
        if (ic) ic.innerHTML = ICON.update;
        if (lbl) lbl.textContent = upTo ? ('Updating to v' + upTo + '\u2026') : 'Updating\u2026';
        if (tag) tag.textContent = (verState.v && upTo) ? ('v' + verState.v + ' \u2192 v' + upTo)
                                  : (upTo ? ('v' + upTo) : (verState.v ? ('v' + verState.v) : ''));
        // Link straight to the in-flight action; don't reopen What's New, which
        // would let you dispatch a second update on top of the running one.
        if (upUrl) { a.href = upUrl; a.target = '_blank'; a.rel = 'noopener'; }
        else { a.href = whatsNewUrl(); a.removeAttribute('target'); a.removeAttribute('rel'); }
        a.title = (upTo ? ('Updating to v' + upTo) : 'An update') + ' is in progress \u2014 click to watch the running action.';
      } else if (behind) {
        a.href = whatsNewUrl(); a.removeAttribute('target'); a.removeAttribute('rel');
        if (ic) ic.innerHTML = ICON.update;
        if (lbl) lbl.textContent = 'Update available';
        if (tag) tag.textContent = 'v' + verState.v + ' \u2192 v' + verState.to;
        a.title = 'Update available: v' + verState.v + ' \u2192 v' + verState.to;
      } else {
        a.href = whatsNewUrl(); a.removeAttribute('target'); a.removeAttribute('rel');
        if (ic) ic.innerHTML = ICON.news;
        if (lbl) lbl.textContent = 'What\u2019s new';
        if (tag) tag.textContent = verState.v ? ('v' + verState.v) : '';
        a.title = verState.v ? ('LabVIEW CI v' + verState.v) : 'LabVIEW CI';
      }
    });

    // 4) Prominent update-available banner under the header (consumer repos).
    renderUpdateBar(behind);
  }

  // The What's New dialog dispatches the update then calls window.lvciMarkUpdating
  // (directly when standalone, or via window.parent from the dashboard modal), so
  // the menu version entry flips to "Updating to vX..." immediately and persists.
  window.lvciMarkUpdating = function (v) {
    if (!v) return;
    try { localStorage.setItem(UPD_KEY, JSON.stringify({ v: v, ts: Date.now(), repo: repo })); } catch (e) {}
    renderBadge();
  };

  function init() { build(); if (cfg.isSource) revealClients(); loadVersion(); loadClients(); startActivity(); }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init);
  else init();
})();
