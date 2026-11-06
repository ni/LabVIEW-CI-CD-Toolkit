#!/usr/bin/env python3
"""
build-analyzer-report.py — Turn the dense, native LabVIEW VI Analyzer HTML report
into a friendly, navigable report that joins each VI's findings with its rendered
snapshot (front panel + block diagram) from the VI Browser gallery.

The native report (emitted by `LabVIEWCLI -OperationName RunVIAnalyzer
-ReportSaveType HTML`) is a flat dump: a summary table followed by one tiny table
per VI. This script parses it and writes:

    <out-dir>/index.html   — friendly report (becomes the deployed page)
    <out-dir>/raw.html     — the original native report, kept verbatim
    <out-dir>/summary.json — machine-readable counts (reusable by the dashboard)

Snapshots are resolved at VIEW time, in the browser, by fetching the commit's
`vi-snapshots/<sha>/manifest.json` from the same Pages origin — so this script
needs no access to the (separately, asynchronously produced) snapshot artifacts,
and the report shows snapshots as soon as that workflow finishes.

Usage:
    python3 build-analyzer-report.py \
        --in   ci-out/vi-analyzer/index.html \
        --out  ci-out/vi-analyzer \
        --sha  <commit-sha> \
        [--platform windows|linux] \
        [--commit-msg "..."] [--author "..."] [--date 2026-...Z] \
        [--repo owner/name] [--pages-url https://owner.github.io/repo]
"""

from __future__ import annotations

import argparse
import html
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path


# ── Severity taxonomy ────────────────────────────────────────────────────────
# VI Analyzer itself does not categorise its tests, so we impose a pragmatic
# taxonomy to colour-code and prioritise findings. Anything not listed defaults
# to "style" (readability / convention). Order matters: it drives sort priority
# and the summary breakdown.
CATEGORY_ORDER = ["correctness", "performance", "style", "documentation"]

CATEGORY_META = {
    "correctness":   {"label": "Correctness", "color": "#da3633", "blurb": "Likely bugs, error-handling gaps, broken VIs"},
    "performance":   {"label": "Performance", "color": "#bb8009", "blurb": "Avoidable runtime / memory cost"},
    "style":         {"label": "Style",       "color": "#1f6feb", "blurb": "Readability & diagram-convention issues"},
    "documentation": {"label": "Docs",        "color": "#6e7681", "blurb": "Missing descriptions, comments, spelling"},
}

TEST_CATEGORY = {
    # correctness — likely defects or error-handling problems
    "Broken VI": "correctness",
    "Error Cluster Wired": "correctness",
    "Auto Error Handling Enabled": "correctness",
    "For Loop Error Handling": "correctness",
    "For Loop Reference Handling": "correctness",
    "Reentrant VI Issues": "correctness",
    "Built Application Compatibility": "correctness",
    "Indexer Datatype": "correctness",
    "Bundling Duplicate Names": "correctness",
    "Duplicate Control Labels": "correctness",
    "Hidden Objects in Structures": "correctness",
    "Hidden Tunnels": "correctness",
    "Wired Terminals in Subdiagrams": "correctness",
    "Empty List Items": "correctness",
    "Array Default Values": "correctness",
    "Overlapping Controls": "correctness",
    "Connector Inputs and Outputs": "correctness",
    "Wait in While Loop": "correctness",
    # performance — avoidable runtime / memory cost
    "Enabled Debugging": "performance",
    "Arrays and Strings in Loops": "performance",
    "In Place Element Structure Usage": "performance",
    "Parallelizable Loops": "performance",
    "Value Property Usage": "performance",
    "Coercion Dots": "performance",
    "Unused Code": "performance",
    "Code Simplification": "performance",
    "Inlinable VIs": "performance",
    # documentation — descriptions, comments, spelling
    "VI Documentation": "documentation",
    "Comment Usage": "documentation",
    "Spell Check": "documentation",
    # everything else falls through to "style" (see categorise())
}


def categorise(test_name: str) -> str:
    return TEST_CATEGORY.get(test_name, "style")


# ── Text / path helpers ──────────────────────────────────────────────────────
def clean(text: str) -> str:
    """Unescape HTML entities and collapse whitespace (incl. newlines) to single
    spaces. VI Analyzer messages carry trailing double spaces and the native
    report wraps cells across lines."""
    return re.sub(r"\s+", " ", html.unescape(text)).strip()


_WORKSPACE_PREFIXES = ("c:\\workspace\\", "/workspace/")


def to_vi_rel(path: str) -> str:
    """Map a VI Analyzer path (`C:\\workspace\\a\\b.vi` or `/workspace/a/b.vi`)
    to the gallery's `vi_rel` key (`a/b.vi`)."""
    p = clean(path)
    low = p.lower()
    for pref in _WORKSPACE_PREFIXES:
        if low.startswith(pref):
            p = p[len(pref):]
            break
    return p.replace("\\", "/")


# VIs under these top-level paths are CI tooling, not project code. The snapshot
# gallery (build-snapshots.ps1) already skips them, so the analyzer report skips
# them too — otherwise their findings would list VIs that never get a snapshot
# ("No snapshot found"), keeping the report and VI Browser in agreement.
_TOOLING_VI_RE = re.compile(r"^(\.github|ci-out|build)/", re.I)


def is_tooling_vi(vi_rel: str) -> bool:
    return bool(_TOOLING_VI_RE.match(vi_rel or ""))


def group_for(vi_rel: str) -> str:
    """Top-level folder, mirroring build-gallery.py so groups line up with the
    VI Browser tree. VIs at the repo root are grouped under 'Project'."""
    parts = vi_rel.split("/")
    return parts[0] if len(parts) > 1 and parts[0] else "Project"


# ── Native-report parsing ────────────────────────────────────────────────────
def _find_int(report: str, label: str) -> int:
    m = re.search(r"<td>\s*" + re.escape(label) + r"\s*</td>\s*<td>\s*(\d+)\s*</td>", report, re.I)
    return int(m.group(1)) if m else 0


def parse_meta(report: str) -> dict:
    date_m = re.search(r"Original Analysis Performed\s*(.*?)\s*<br", report, re.I | re.S)
    dur_m = re.search(r"Total Analysis Time:\s*([0-9:.]+)", report, re.I)
    return {
        "analysis_date": clean(date_m.group(1)) if date_m else "",
        "duration": dur_m.group(1).strip() if dur_m else "",
    }


def parse_summary(report: str) -> dict:
    return {
        "vis_analyzed": _find_int(report, "VIs Analyzed"),
        "tests_run": _find_int(report, "Total Tests Run"),
        "passed": _find_int(report, "Passed Tests"),
        "failed": _find_int(report, "Failed Tests"),
        "skipped": _find_int(report, "Skipped Tests"),
    }


def parse_errors(report: str) -> dict:
    return {
        "vi_not_loadable": _find_int(report, "VI not loadable"),
        "test_not_loadable": _find_int(report, "Test not loadable"),
        "test_not_runnable": _find_int(report, "Test not runnable"),
        "test_error_out": _find_int(report, "Test error out"),
    }


# A VI block in the "Failed Tests" section:
#   <b>Name.vi</b> (C:\workspace\rel\path.vi)<br><table ...> ...rows... </table>
# The path may itself contain ')' (e.g. "Write Question Timer (s).vi"), so the
# capture is anchored on the ")<br><table" that always terminates the header.
_VI_BLOCK_RE = re.compile(
    r"<b>(?P<name>.*?)</b>\s*\((?P<path>.*?)\)\s*<br>\s*<table[^>]*>(?P<rows>.*?)</table>",
    re.S | re.I,
)
_ROW2_RE = re.compile(r"<tr>\s*<td>(?P<a>.*?)</td>\s*<td>(?P<b>.*?)</td>\s*</tr>", re.S | re.I)


def parse_failed(report: str) -> list[dict]:
    fail_i = report.find('name="fail"')
    err_i = report.find('name="err"')
    if fail_i < 0:
        return []
    section = report[fail_i: err_i if 0 <= err_i else len(report)]

    vis: list[dict] = []
    for blk in _VI_BLOCK_RE.finditer(section):
        name = clean(blk.group("name"))
        path = clean(blk.group("path"))
        vi_rel = to_vi_rel(path)
        failures = []
        for row in _ROW2_RE.finditer(blk.group("rows")):
            test = clean(row.group("a"))
            message = clean(row.group("b"))
            if test.lower() == "test":  # the <th>Test</th><th>Failure Message</th> header
                continue
            if not test:
                continue
            failures.append({"test": test, "message": message, "severity": categorise(test)})
        if not failures:
            continue
        sev_counts = {c: 0 for c in CATEGORY_ORDER}
        for f in failures:
            sev_counts[f["severity"]] += 1
        vis.append({
            "name": name,
            "path": path,
            "vi_rel": vi_rel,
            "group": group_for(vi_rel),
            "failures": failures,
            "total": len(failures),
            "sev_counts": sev_counts,
        })
    return vis


# A test-error block in the "Testing Errors" section:
#   <b>TestName</b><table ...><tr><th>..three..</th></tr> ...3-col rows... </table>
_ERR_BLOCK_RE = re.compile(r"<b>(?P<test>.*?)</b>\s*<table[^>]*>(?P<rows>.*?)</table>", re.S | re.I)
_ROW3_RE = re.compile(
    r"<tr>\s*<td>(?P<a>.*?)</td>\s*<td>(?P<b>.*?)</td>\s*<td>(?P<c>.*?)</td>\s*</tr>", re.S | re.I
)


def parse_testing_errors(report: str) -> list[dict]:
    err_i = report.find('name="err"')
    if err_i < 0:
        return []
    section = report[err_i:]
    out: list[dict] = []
    for blk in _ERR_BLOCK_RE.finditer(section):
        test = clean(blk.group("test"))
        items = []
        for row in _ROW3_RE.finditer(blk.group("rows")):
            vi_name = clean(row.group("a"))
            vi_path = clean(row.group("b"))
            message = clean(row.group("c"))
            if vi_name.lower() == "vi name":  # header row
                continue
            items.append({
                "vi_name": vi_name,
                "vi_path": vi_path,
                "vi_rel": to_vi_rel(vi_path),
                "message": message,
            })
        if items:
            out.append({"test": test, "items": items})
    return out


def aggregate_rules(vis: list[dict]) -> list[dict]:
    """Pivot every failure by test (rule) so the most common issues across the
    whole project surface first."""
    rules: dict[str, dict] = {}
    for vi in vis:
        for f in vi["failures"]:
            r = rules.setdefault(f["test"], {
                "test": f["test"], "severity": f["severity"], "count": 0, "vis": {},
            })
            r["count"] += 1
            vr = r["vis"].setdefault(vi["vi_rel"], {
                "vi_rel": vi["vi_rel"], "vi_name": vi["name"], "count": 0, "messages": [],
            })
            vr["count"] += 1
            vr["messages"].append(f["message"])
    rules_list = []
    for r in rules.values():
        r["vi_count"] = len(r["vis"])
        r["vis"] = sorted(r["vis"].values(), key=lambda x: (-x["count"], x["vi_rel"].lower()))
        rules_list.append(r)
    rules_list.sort(key=lambda r: (-r["count"], r["test"].lower()))
    return rules_list


def build_data(report: str, args: argparse.Namespace) -> dict:
    # Exclude CI-tooling VIs (.github/, ci-out/, build/) so the report lists only
    # project code — matching the snapshot gallery, which never renders them.
    vis = [v for v in parse_failed(report) if not is_tooling_vi(v["vi_rel"])]
    vis.sort(key=lambda v: (-v["total"], v["vi_rel"].lower()))
    summary = parse_summary(report)
    # "Failed Tests" (summary) counts failed test *instances* (VI x test); a single
    # failed test can emit several findings, so the listed rows (findings) usually
    # exceed it. Surface both so the per-severity/per-VI tallies reconcile.
    summary["findings"] = sum(v["total"] for v in vis)
    summary["vis_with_findings"] = len(vis)

    # Same project-only scope for the testing-errors banner; drop blocks left empty.
    testing_errors = []
    for blk in parse_testing_errors(report):
        items = [it for it in blk["items"] if not is_tooling_vi(it["vi_rel"])]
        if items:
            testing_errors.append({"test": blk["test"], "items": items})

    return {
        "meta": {
            "sha": args.sha,
            "short": (args.sha or "")[:7],
            "platform": args.platform,
            "repo": args.repo,
            "pages_url": (args.pages_url or "").rstrip("/"),
            "commit": {"message": args.commit_msg, "author": args.author, "date": args.date},
            "generated_utc": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
            **parse_meta(report),
        },
        "summary": summary,
        "errors": parse_errors(report),
        "categories": CATEGORY_META,
        "category_order": CATEGORY_ORDER,
        "vis": vis,
        "rules": aggregate_rules(vis),
        "testing_errors": testing_errors,
    }


# ── Renderer ─────────────────────────────────────────────────────────────────
def render(data: dict) -> str:
    blob = json.dumps(data, ensure_ascii=False)
    # Neutralise any sequence that could break out of the <script> blob.
    blob = blob.replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")
    return _TEMPLATE.replace("__VIA_DATA_JSON__", blob)


def main() -> None:
    ap = argparse.ArgumentParser(description="Build a friendly VI Analyzer report from the native HTML report.")
    ap.add_argument("--in", dest="in_path", required=True, help="Path to the native VI Analyzer index.html")
    ap.add_argument("--out", dest="out_dir", required=True, help="Output directory")
    ap.add_argument("--sha", default="", help="Commit SHA being analyzed")
    ap.add_argument("--platform", default="windows", choices=["windows", "linux"])
    ap.add_argument("--commit-msg", dest="commit_msg", default="")
    ap.add_argument("--author", default="")
    ap.add_argument("--date", default="")
    ap.add_argument("--repo", default="")
    ap.add_argument("--pages-url", dest="pages_url", default="")
    args = ap.parse_args()

    in_path = Path(args.in_path)
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    report = in_path.read_text(encoding="utf-8", errors="replace")

    # Preserve the native report verbatim before we overwrite index.html.
    raw_path = out_dir / "raw.html"
    if in_path.resolve() == (out_dir / "index.html").resolve():
        raw_path.write_text(report, encoding="utf-8")

    data = build_data(report, args)

    (out_dir / "summary.json").write_text(
        json.dumps({"meta": data["meta"], "summary": data["summary"], "errors": data["errors"],
                    "rule_counts": [{"test": r["test"], "severity": r["severity"], "count": r["count"],
                                     "vi_count": r["vi_count"]} for r in data["rules"]]},
                   indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    (out_dir / "index.html").write_text(render(data), encoding="utf-8")

    s = data["summary"]
    print(f"Friendly VI Analyzer report built: {s['failed']} failures across "
          f"{len(data['vis'])} VIs ({s['passed']}/{s['tests_run']} tests passed). "
          f"-> {out_dir / 'index.html'}")
    if not data["vis"] and s["failed"]:
        print("WARNING: report parsed 0 VIs but summary reports failures — check the native format.",
              file=sys.stderr)


# The friendly report is a single self-contained page. Data is injected as a JSON
# blob at __VIA_DATA_JSON__ and rendered entirely client-side.
_TEMPLATE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>VI Analyzer Report — LabVIEW-CI-with-Containers</title>
<style>
:root{
  --bg:#0d1117;--surface:#161b22;--surface2:#1c2128;--border:#30363d;--row:#21262d;
  --fg:#e6edf3;--fg-muted:#8b949e;--link:#58a6ff;--hover:#1c2128;
  --ok:#2ea043;--bad:#da3633;--warn:#bb8009;
}
@media(prefers-color-scheme:light){:root{
  --bg:#fff;--surface:#f6f8fa;--surface2:#eef2f6;--border:#d0d7de;--row:#eaeef2;
  --fg:#1f2328;--fg-muted:#57606a;--link:#0969da;--hover:#f3f4f6;
}}
*{box-sizing:border-box}
body{margin:0;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;background:var(--bg);color:var(--fg);font-size:14px}
a{color:var(--link);text-decoration:none}a:hover{text-decoration:underline}
.wrap{max-width:1180px;margin:0 auto;padding:20px}
h1{font-size:1.35em;margin:0 0 2px}
.sub{color:var(--fg-muted);font-size:.84em;margin-bottom:16px}
.sub a{color:var(--link)}
.nav{margin-bottom:16px;font-size:.86em}.nav a{margin-right:16px}

/* summary band */
.cards{display:flex;flex-wrap:wrap;gap:12px;margin-bottom:14px}
.card{background:var(--surface);border:1px solid var(--border);border-radius:10px;padding:12px 16px;min-width:120px;flex:1 1 auto}
.card .n{font-size:1.7em;font-weight:700;line-height:1.1}
.card .l{color:var(--fg-muted);font-size:.78em;text-transform:uppercase;letter-spacing:.04em;margin-top:2px}
.card.bad .n{color:var(--bad)}.card.ok .n{color:var(--ok)}
.bar{height:9px;border-radius:5px;background:var(--bad);overflow:hidden;margin:12px 0 4px;border:1px solid var(--border)}
.bar>span{display:block;height:100%;background:var(--ok)}
.barlabel{color:var(--fg-muted);font-size:.78em;margin-bottom:18px}

/* testing-errors banner */
.errband{background:rgba(218,54,51,.10);border:1px solid var(--bad);border-radius:10px;padding:12px 16px;margin-bottom:16px}
.errband h2{margin:0 0 6px;font-size:.95em;color:var(--bad)}
.errband details{margin-top:6px}
.errband summary{cursor:pointer;font-size:.86em}
.errband table{border-collapse:collapse;width:100%;margin-top:8px;font-size:.8em}
.errband td,.errband th{border:1px solid var(--border);padding:5px 8px;text-align:left;vertical-align:top}
.errband th{color:var(--fg-muted);font-weight:600}

/* tabs + toolbar */
.tabs{display:flex;gap:4px;border-bottom:1px solid var(--border);margin-bottom:12px}
.tabs button{background:none;border:none;border-bottom:2px solid transparent;color:var(--fg-muted);
  font:inherit;font-weight:600;padding:8px 14px;cursor:pointer;margin-bottom:-1px}
.tabs button.active{color:var(--fg);border-bottom-color:var(--link)}
.toolbar{display:flex;flex-wrap:wrap;align-items:center;gap:10px;margin-bottom:14px}
#q{flex:1 1 220px;min-width:180px;background:var(--surface);color:var(--fg);border:1px solid var(--border);
  border-radius:7px;padding:7px 10px;font-size:.86em}
.chips{display:flex;gap:6px;flex-wrap:wrap}
.chip{display:inline-flex;align-items:center;gap:6px;border:1px solid var(--border);background:var(--surface);
  border-radius:20px;padding:4px 11px;font-size:.78em;cursor:pointer;user-select:none;color:var(--fg-muted)}
.chip .dot{width:9px;height:9px;border-radius:50%}
.chip.on{color:var(--fg);border-color:var(--link)}
.chip.off{opacity:.45;text-decoration:line-through}
.tools{display:flex;gap:12px;font-size:.78em}
.tools button{background:none;border:none;color:var(--link);cursor:pointer;font:inherit;padding:0}
.count{color:var(--fg-muted);font-size:.8em;margin-left:auto}

/* groups + VI cards */
.group{margin-bottom:10px}
.group>summary{cursor:pointer;list-style:none;padding:8px 10px;background:var(--surface);border:1px solid var(--border);
  border-radius:8px;display:flex;align-items:center;gap:10px;font-weight:600}
.group>summary::-webkit-details-marker{display:none}
.group>summary .tw{transition:transform .12s ease;color:var(--fg-muted)}
.group[open]>summary .tw{transform:rotate(90deg)}
.gname{flex:0 1 auto}
.gmeta{color:var(--fg-muted);font-weight:400;font-size:.82em}
.vi{border:1px solid var(--border);border-radius:8px;margin:8px 0 0;background:var(--surface)}
.vi>summary{cursor:pointer;list-style:none;padding:9px 12px;display:flex;align-items:center;gap:10px}
.vi>summary::-webkit-details-marker{display:none}
.vi>summary .tw{color:var(--fg-muted);transition:transform .12s ease;flex:0 0 auto}
.vi[open]>summary .tw{transform:rotate(90deg)}
.viname{font-weight:600;flex:0 0 auto}
.virel{color:var(--fg-muted);font-size:.78em;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;flex:1 1 auto;min-width:0}
.sevdots{display:inline-flex;gap:4px;flex:0 0 auto}
.sevdot{display:inline-flex;align-items:center;gap:3px;font-size:.74em;color:var(--fg-muted)}
.sevdot .dot{width:9px;height:9px;border-radius:50%}
.pill{flex:0 0 auto;background:var(--surface2);border:1px solid var(--border);border-radius:20px;
  padding:1px 9px;font-size:.74em;color:var(--fg-muted)}
.snapbtn{flex:0 0 auto;background:none;border:1px solid var(--border);color:var(--link);border-radius:6px;
  padding:3px 9px;font:inherit;font-size:.76em;cursor:pointer}
.snapbtn:hover{border-color:var(--link)}
.vibody{padding:2px 12px 12px 30px}
.rule-in-vi{margin:10px 0 0}
.rule-in-vi .rh{display:flex;align-items:center;gap:8px;font-size:.86em;font-weight:600}
.rule-in-vi .rh .dot{width:9px;height:9px;border-radius:50%;flex:0 0 auto}
.rule-in-vi .cnt{color:var(--fg-muted);font-weight:400;font-size:.86em}
.msgs{margin:4px 0 0;padding:0 0 0 18px}
.msgs li{color:var(--fg-muted);font-size:.84em;margin:2px 0;line-height:1.45}

/* by-rule view */
.rule{border:1px solid var(--border);border-radius:8px;margin:8px 0 0;background:var(--surface)}
.rule>summary{cursor:pointer;list-style:none;padding:9px 12px;display:flex;align-items:center;gap:10px}
.rule>summary::-webkit-details-marker{display:none}
.rule>summary .tw{color:var(--fg-muted);transition:transform .12s ease}
.rule[open]>summary .tw{transform:rotate(90deg)}
.rule .rtitle{font-weight:600}
.rule .rbody{padding:2px 12px 12px 30px}
.rv{display:flex;align-items:center;gap:10px;padding:5px 0;border-top:1px solid var(--row)}
.rv:first-child{border-top:none}
.rv .nm{font-weight:600;font-size:.86em}
.rv .rl{color:var(--fg-muted);font-size:.78em;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;flex:1 1 auto;min-width:0}

.empty{color:var(--fg-muted);padding:30px;text-align:center}
.hidden{display:none!important}

/* snapshot drawer */
#backdrop{position:fixed;inset:0;background:rgba(0,0,0,.5);opacity:0;pointer-events:none;transition:opacity .15s;z-index:40}
#backdrop.show{opacity:1;pointer-events:auto}
#snap{position:fixed;top:0;right:0;height:100vh;width:min(70vw,860px);background:var(--bg);border-left:1px solid var(--border);
  transform:translateX(100%);transition:transform .18s ease;z-index:41;display:flex;flex-direction:column}
#snap.show{transform:none}
#snaphead{padding:10px 14px;border-bottom:1px solid var(--border);background:var(--surface);display:flex;align-items:center;gap:12px}
#snaphead .t{font-weight:600;flex:1 1 auto;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
#snaphead a{font-size:.82em}
#snaphead button{background:none;border:none;color:var(--fg);font-size:1.3em;cursor:pointer;line-height:1}
#snapframe{flex:1;border:none;background:#fff;width:100%}
#snapnote{padding:18px;color:var(--fg-muted);font-size:.86em;display:none}
</style>
</head>
<body>
<div class="wrap">
  <h1>VI Analyzer Report</h1>
  <div class="sub" id="sub"></div>
  <div class="nav" id="nav"></div>

  <div class="cards" id="cards"></div>
  <div class="bar"><span id="barfill"></span></div>
  <div class="barlabel" id="barlabel"></div>

  <div class="errband hidden" id="errband"></div>

  <div class="tabs">
    <button data-tab="vi" class="active">By VI</button>
    <button data-tab="rule">By Rule</button>
  </div>

  <div class="toolbar">
    <input id="q" type="search" placeholder="Filter by VI, rule, or message…">
    <div class="chips" id="chips"></div>
    <div class="tools"><button id="expand">Expand all</button><button id="collapse">Collapse all</button></div>
    <span class="count" id="rescount"></span>
  </div>

  <div id="view-vi"></div>
  <div id="view-rule" class="hidden"></div>
</div>

<div id="backdrop"></div>
<aside id="snap">
  <div id="snaphead">
    <span class="t" id="snaptitle">VI</span>
    <a id="snapopen" href="#" target="_blank">Open in VI Browser ↗</a>
    <button id="snapclose" title="Close">×</button>
  </div>
  <iframe id="snapframe" title="VI snapshot"></iframe>
  <div id="snapnote"></div>
</aside>

<script id="via-data" type="application/json">__VIA_DATA_JSON__</script>
<script>
const DATA = JSON.parse(document.getElementById('via-data').textContent);
const META = DATA.meta, SUM = DATA.summary, ERR = DATA.errors;
const CATS = DATA.categories, ORDER = DATA.category_order;
const SNAP_BASE = '../../vi-snapshots/';
const esc = s => String(s==null?'':s).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));

// active severity filters (all on by default)
const active = new Set(ORDER);
let query = '';

// ── header ───────────────────────────────────────────────────────────────
(function header(){
  const m = META;
  const commitMsg = (m.commit && m.commit.message) ? esc(m.commit.message) : '';
  const shaLink = m.repo && m.sha ? `https://github.com/${m.repo}/commit/${m.sha}` : '';
  document.getElementById('sub').innerHTML =
    (m.short ? `Commit ${shaLink?`<a href="${shaLink}">${esc(m.short)}</a>`:esc(m.short)} ` : '') +
    (commitMsg ? `&middot; ${commitMsg} ` : '') +
    (m.platform ? `&middot; ${esc(m.platform)} ` : '') +
    (m.analysis_date ? `&middot; ${esc(m.analysis_date)} ` : '') +
    (m.duration ? `&middot; ${esc(m.duration)} ` : '') +
    `&middot; generated ${esc(m.generated_utc||'')}`;
  const pages = m.pages_url || '../..';
  document.getElementById('nav').innerHTML =
    `<a href="${pages}/">CI Dashboard</a>` +
    `<a href="${SNAP_BASE}index.html${m.sha?`?sha=${m.sha}`:''}">VI Browser</a>` +
    `<a href="raw.html">Native report ↗</a>` +
    (m.repo ? `<a href="https://github.com/${m.repo}">GitHub</a>` : '');

  const pct = SUM.tests_run ? Math.round(SUM.passed/SUM.tests_run*1000)/10 : 0;
  // "Failed tests" is VI Analyzer's official count of failed test instances; a
  // single failed test can list several findings, so "Findings" (what this report
  // enumerates, and what the severity chips count) is usually larger.
  const cards = [
    {n:SUM.vis_analyzed, l:'VIs Analyzed'},
    {n:SUM.tests_run,    l:'Tests Run'},
    {n:SUM.passed,       l:'Passed', c:'ok'},
    {n:SUM.failed,       l:'Failed tests', c:'bad', sub:'test instances'},
    {n:(SUM.findings!=null?SUM.findings:SUM.failed), l:'Findings', c:'bad', sub:'individual issues'},
    {n:(SUM.vis_with_findings!=null?SUM.vis_with_findings:DATA.vis.length), l:'VIs w/ findings'},
  ];
  if(SUM.skipped) cards.push({n:SUM.skipped, l:'Skipped'});
  document.getElementById('cards').innerHTML = cards.map(c=>
    `<div class="card ${c.c||''}"><div class="n">${(c.n||0).toLocaleString()}</div><div class="l">${c.l}</div>`+
    (c.sub?`<div class="l" style="text-transform:none;letter-spacing:0;opacity:.75;margin-top:1px">${c.sub}</div>`:'')+`</div>`).join('');
  document.getElementById('barfill').style.width = pct+'%';
  document.getElementById('barlabel').textContent = `${pct}% of ${ (SUM.tests_run||0).toLocaleString() } tests passed · ${(SUM.failed||0).toLocaleString()} failed tests reported ${((SUM.findings!=null?SUM.findings:SUM.failed)||0).toLocaleString()} findings`;
})();

// ── testing errors banner ────────────────────────────────────────────────
(function errband(){
  const te = DATA.testing_errors||[];
  const totalErr = (ERR.vi_not_loadable+ERR.test_not_loadable+ERR.test_not_runnable+ERR.test_error_out)||0;
  if(!te.length && !totalErr) return;
  const el = document.getElementById('errband'); el.classList.remove('hidden');
  let rows = '';
  for(const blk of te){
    for(const it of blk.items){
      rows += `<tr><td>${esc(blk.test)}</td><td>${esc(it.vi_name)}</td><td>${esc(it.message)}</td></tr>`;
    }
  }
  el.innerHTML =
    `<h2>⚠ ${totalErr} testing error${totalErr===1?'':'s'} — these tests could not run (infrastructure, not code quality)</h2>`+
    `<div style="font-size:.82em;color:var(--fg-muted)">VI not loadable: ${ERR.vi_not_loadable} &middot; Test not loadable: ${ERR.test_not_loadable} &middot; Test not runnable: ${ERR.test_not_runnable} &middot; Test error out: ${ERR.test_error_out}</div>`+
    (rows?`<details><summary>Show details</summary><table><tr><th>Test</th><th>VI</th><th>Error</th></tr>${rows}</table></details>`:'');
})();

// ── severity chips ───────────────────────────────────────────────────────
(function chips(){
  const wrap = document.getElementById('chips');
  // totals per severity across all findings
  const totals = {}; for(const c of ORDER) totals[c]=0;
  for(const v of DATA.vis) for(const c of ORDER) totals[c]+=(v.sev_counts[c]||0);
  wrap.innerHTML = ORDER.map(c=>{
    const meta = CATS[c];
    return `<span class="chip on" data-sev="${c}" title="${esc(meta.blurb)}"><span class="dot" style="background:${meta.color}"></span>${esc(meta.label)} <b>${totals[c]}</b></span>`;
  }).join('');
  wrap.querySelectorAll('.chip').forEach(ch=>ch.addEventListener('click',()=>{
    const s = ch.dataset.sev;
    if(active.has(s)){active.delete(s);ch.classList.remove('on');ch.classList.add('off');}
    else{active.add(s);ch.classList.add('on');ch.classList.remove('off');}
    apply();
  }));
})();

// ── render: By VI ────────────────────────────────────────────────────────
function sevDots(counts){
  return `<span class="sevdots">`+ORDER.filter(c=>counts[c]).map(c=>
    `<span class="sevdot"><span class="dot" style="background:${CATS[c].color}"></span>${counts[c]}</span>`).join('')+`</span>`;
}
function groupFailuresByTest(failures){
  const m = new Map();
  for(const f of failures){
    if(!m.has(f.test)) m.set(f.test,{test:f.test,severity:f.severity,messages:[]});
    m.get(f.test).messages.push(f.message);
  }
  return [...m.values()].sort((a,b)=> b.messages.length-a.messages.length || a.test.localeCompare(b.test));
}
function viCard(v){
  const rules = groupFailuresByTest(v.failures).map(r=>`
    <div class="rule-in-vi" data-sev="${r.severity}" data-text="${esc((r.test+' '+r.messages.join(' ')).toLowerCase())}">
      <div class="rh"><span class="dot" style="background:${CATS[r.severity].color}"></span>${esc(r.test)} <span class="cnt">×${r.messages.length}</span></div>
      <ul class="msgs">${r.messages.map(msg=>`<li>${esc(msg)}</li>`).join('')}</ul>
    </div>`).join('');
  return `<details class="vi" data-vi="${esc(v.vi_rel)}" data-vipath="${esc((v.vi_rel+' '+v.name).toLowerCase())}">
    <summary>
      <span class="tw">▶</span>
      <span class="viname">${esc(v.name)}</span>
      <span class="virel">${esc(v.vi_rel)}</span>
      ${sevDots(v.sev_counts)}
      <span class="pill">${v.total} finding${v.total===1?'':'s'}</span>
      <button class="snapbtn" data-snap="${esc(v.vi_rel)}" data-name="${esc(v.name)}">Snapshot</button>
    </summary>
    <div class="vibody">${rules}</div>
  </details>`;
}
function renderVI(){
  const groups = new Map();
  for(const v of DATA.vis){ if(!groups.has(v.group)) groups.set(v.group,[]); groups.get(v.group).push(v); }
  const names = [...groups.keys()].sort((a,b)=>a.localeCompare(b));
  const host = document.getElementById('view-vi');
  host.innerHTML = names.map(g=>{
    const vis = groups.get(g);
    const total = vis.reduce((s,v)=>s+v.total,0);
    return `<details class="group" open data-group="${esc(g)}">
      <summary><span class="tw">▶</span><span class="gname">${esc(g)}</span><span class="gmeta">${vis.length} VI${vis.length===1?'':'s'} · ${total} findings</span></summary>
      <div class="gbody">${vis.map(viCard).join('')}</div>
    </details>`;
  }).join('') || `<div class="empty">No failing VIs 🎉</div>`;
}

// ── render: By Rule ──────────────────────────────────────────────────────
function ruleCard(r){
  const vis = r.vis.map(v=>`
    <div class="rv" data-text="${esc((v.vi_rel+' '+v.messages.join(' ')).toLowerCase())}">
      <span class="nm">${esc(v.vi_name)}</span>
      <span class="rl">${esc(v.vi_rel)}</span>
      <span class="pill">×${v.count}</span>
      <button class="snapbtn" data-snap="${esc(v.vi_rel)}" data-name="${esc(v.vi_name)}">Snapshot</button>
    </div>`).join('');
  return `<details class="rule" data-sev="${r.severity}" data-text="${esc(r.test.toLowerCase())}">
    <summary>
      <span class="tw">▶</span>
      <span class="dot" style="width:9px;height:9px;border-radius:50%;background:${CATS[r.severity].color}"></span>
      <span class="rtitle">${esc(r.test)}</span>
      <span class="pill">${r.count} across ${r.vi_count} VI${r.vi_count===1?'':'s'}</span>
    </summary>
    <div class="rbody">${vis}</div>
  </details>`;
}
function renderRule(){
  document.getElementById('view-rule').innerHTML =
    DATA.rules.map(ruleCard).join('') || `<div class="empty">No findings 🎉</div>`;
}

// ── filtering ────────────────────────────────────────────────────────────
function apply(){
  const q = query;
  // By VI: a VI shows when its name/path matches the query (then all its
  // severity-permitted rules show) or it has a rule whose text matches.
  document.querySelectorAll('#view-vi .group').forEach(g=>{
    let gVisible = 0;
    g.querySelectorAll('.vi').forEach(vi=>{
      const viHit = !q || vi.dataset.vipath.includes(q);
      let any = 0;
      vi.querySelectorAll('.rule-in-vi').forEach(r=>{
        const show = active.has(r.dataset.sev) && (viHit || r.dataset.text.includes(q));
        r.classList.toggle('hidden', !show);
        if(show) any++;
      });
      vi.classList.toggle('hidden', any===0);
      if(any>0) gVisible++;
    });
    g.classList.toggle('hidden', gVisible===0);
  });
  // By Rule: a rule shows when its name matches (then all VIs show) or it has a
  // VI row whose path/message matches; severity must be enabled either way.
  document.querySelectorAll('#view-rule .rule').forEach(r=>{
    const okSev = active.has(r.dataset.sev);
    const nameHit = !q || r.dataset.text.includes(q);
    let any=0;
    r.querySelectorAll('.rv').forEach(rv=>{
      const show = okSev && (nameHit || rv.dataset.text.includes(q));
      rv.classList.toggle('hidden', !show); if(show) any++;
    });
    r.classList.toggle('hidden', !(okSev && any>0));
  });
  // result count reflects the active tab
  const tab = document.querySelector('.tabs button.active').dataset.tab;
  if(tab==='vi'){
    const n = [...document.querySelectorAll('#view-vi .vi')].filter(v=>!v.classList.contains('hidden')).length;
    document.getElementById('rescount').textContent = `${n} VI${n===1?'':'s'} shown`;
  } else {
    const n = [...document.querySelectorAll('#view-rule .rule')].filter(r=>!r.classList.contains('hidden')).length;
    document.getElementById('rescount').textContent = `${n} rule${n===1?'':'s'} shown`;
  }
}

// ── snapshot drawer (resolves vi_rel -> by-blob HTML via manifest.json) ───
let snapMap = null, snapFromSha = null;
async function fetchJson(u){ const r = await fetch(u,{cache:'no-store'}); if(!r.ok) throw new Error(r.status+' '+u); return r.json(); }
async function ensureSnapshots(){
  if(snapMap) return;
  snapMap = {};
  let m = META.sha ? await fetchJson(SNAP_BASE+META.sha+'/manifest.json').catch(()=>null) : null;
  if(m) snapFromSha = META.sha;
  if(!m && META.sha){
    const revisions = await fetchJson(SNAP_BASE+'files.json').catch(()=>[]);
    const revision = Array.isArray(revisions) && revisions.find(entry=>entry.sha===META.sha);
    if(revision && Array.isArray(revision.vis)){
      m = revision.vis.map(entry=>({vi_rel:entry.vi_rel,html:'by-blob/'+entry.blob.slice(0,2)+'/'+entry.blob+'.html'}));
      snapFromSha = META.sha;
    }
  }
  if(!m){
    const commits = await fetchJson(SNAP_BASE+'commits.json').catch(()=>[]);
    if(commits && commits.length){
      m = await fetchJson(SNAP_BASE+commits[0].sha+'/manifest.json').catch(()=>null);
      if(m) snapFromSha = commits[0].sha;
    }
  }
  if(m) for(const v of m) snapMap[v.vi_rel]=v.html;
}
async function openSnap(viRel, name){
  const back=document.getElementById('backdrop'), snap=document.getElementById('snap');
  const frame=document.getElementById('snapframe'), note=document.getElementById('snapnote');
  document.getElementById('snaptitle').textContent = name||viRel;
  document.getElementById('snapopen').href = SNAP_BASE+'index.html'+(META.sha?`?sha=${META.sha}`:'');
  back.classList.add('show'); snap.classList.add('show');
  frame.style.display='none'; note.style.display='block'; note.textContent='Resolving snapshot…';
  try{ await ensureSnapshots(); }catch(e){}
  const html = snapMap[viRel];
  if(html){
    frame.src = SNAP_BASE+'vi-interactive.html?file='+encodeURIComponent(html);
    frame.style.display='block'; note.style.display='none';
    if(snapFromSha && META.sha && snapFromSha!==META.sha){
      note.style.display='block';
      note.innerHTML=`<em>Showing the latest available snapshot (commit ${esc(snapFromSha.slice(0,7))}); this commit's snapshots may still be rendering.</em>`;
    }
  }else{
    frame.style.display='none'; note.style.display='block';
    note.innerHTML = `No snapshot found for <code>${esc(viRel)}</code>.<br>It may still be rendering, or this VI isn't in the snapshot gallery yet. Try the <a href="${SNAP_BASE}index.html${META.sha?`?sha=${META.sha}`:''}" target="_blank">VI Browser</a>.`;
  }
}
function closeSnap(){
  document.getElementById('backdrop').classList.remove('show');
  document.getElementById('snap').classList.remove('show');
  document.getElementById('snapframe').src='about:blank';
}
document.getElementById('snapclose').addEventListener('click',closeSnap);
document.getElementById('backdrop').addEventListener('click',closeSnap);
document.addEventListener('keydown',e=>{ if(e.key==='Escape') closeSnap(); });
document.addEventListener('click',e=>{
  const b=e.target.closest('.snapbtn'); if(!b) return;
  e.preventDefault(); e.stopPropagation();
  openSnap(b.dataset.snap, b.dataset.name);
});

// ── tabs / tools ─────────────────────────────────────────────────────────
document.querySelectorAll('.tabs button').forEach(btn=>btn.addEventListener('click',()=>{
  document.querySelectorAll('.tabs button').forEach(b=>b.classList.remove('active'));
  btn.classList.add('active');
  const tab=btn.dataset.tab;
  document.getElementById('view-vi').classList.toggle('hidden',tab!=='vi');
  document.getElementById('view-rule').classList.toggle('hidden',tab!=='rule');
  apply();
}));
document.getElementById('q').addEventListener('input',e=>{ query=e.target.value.trim().toLowerCase(); apply(); });
document.getElementById('expand').addEventListener('click',()=>{
  const sel = document.querySelector('.tabs button.active').dataset.tab==='vi' ? '#view-vi details' : '#view-rule details';
  document.querySelectorAll(sel).forEach(d=>d.open=true);
});
document.getElementById('collapse').addEventListener('click',()=>{
  const tab=document.querySelector('.tabs button.active').dataset.tab;
  if(tab==='vi') document.querySelectorAll('#view-vi .vi, #view-vi .group').forEach(d=>d.open=false);
  else document.querySelectorAll('#view-rule .rule').forEach(d=>d.open=false);
});

renderVI(); renderRule(); apply();
</script>
</body>
</html>
"""


if __name__ == "__main__":
    main()
