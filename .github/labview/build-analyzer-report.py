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


# A test-error block in the "Testing Errors" section. VI Analyzer heads each
# category with either <b>Name</b> (per-test errors) or <h3>Name</h3> (e.g.
# "VI Not Loadable"), followed by a 3-column table (VI Name / VI Path / message).
_ERR_BLOCK_RE = re.compile(r"<(?:b|h3)>(?P<test>.*?)</(?:b|h3)>\s*<table[^>]*>(?P<rows>.*?)</table>", re.S | re.I)
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


def _finalize(vis: list, summary: dict, errors: dict, testing_errors: list,
              meta_extra: dict, args: argparse.Namespace) -> dict:
    """Assemble the final report data dict from already-merged per-VI findings."""
    vis = sorted(vis, key=lambda v: (-v["total"], v["vi_rel"].lower()))
    summary = dict(summary)
    summary["findings"] = sum(v["total"] for v in vis)
    summary["vis_with_findings"] = len(vis)
    meta = {
        "sha": args.sha,
        "short": (args.sha or "")[:7],
        "platform": args.platform,
        "repo": args.repo,
        "pages_url": (args.pages_url or "").rstrip("/"),
        "pages_base": (getattr(args, "pages_base", "") or "../..").rstrip("/"),
        "commit": {"message": args.commit_msg, "author": args.author, "date": args.date},
        "generated_utc": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
    }
    meta.update(meta_extra or {})
    return {
        "meta": meta,
        "summary": summary,
        "errors": errors,
        "categories": CATEGORY_META,
        "category_order": CATEGORY_ORDER,
        "vis": vis,
        "rules": aggregate_rules(vis),
        "testing_errors": testing_errors,
    }


def _all_testing_errors(report: str) -> list:
    # Every VI that could not be tested, grouped by error category. Each item is
    # tagged `tooling` when it is a CI-tooling VI (under .github/ etc.) rather than
    # project code, so the report can NAME the VIs + errors and flag which ones are
    # just CI helpers (and therefore harmless), instead of showing a bare count.
    out = []
    for blk in parse_testing_errors(report):
        items = [{**it, "tooling": is_tooling_vi(it["vi_rel"])} for it in blk["items"]]
        if items:
            out.append({"test": blk["test"], "items": items})
    return out


def build_data(report: str, args: argparse.Namespace) -> dict:
    # Exclude CI-tooling VIs (.github/, ci-out/, build/) so the report lists only
    # project code — matching the snapshot gallery, which never renders them.
    vis = [v for v in parse_failed(report) if not is_tooling_vi(v["vi_rel"])]
    # "Failed Tests" (summary) counts failed test *instances* (VI x test); a single
    # failed test can emit several findings, so the listed rows (findings) usually
    # exceed it. Surface both so the per-severity/per-VI tallies reconcile (in _finalize).
    meta_extra = dict(parse_meta(report))
    return _finalize(vis, parse_summary(report), parse_errors(report),
                     _all_testing_errors(report), meta_extra, args)


# ── Multi-configuration merge ─────────────────────────────────────────────────
# When the project assigns different .viancfg test configurations to different
# subsets of code (config.viAnalyzer in .github/labview-ci.yml), the runner emits
# one native report per "pass" — a default pass over the whole tree plus one pass
# per rule — and a passes/manifest.json describing each. We merge them so each VI
# shows the findings of the configuration that actually applies to it: a rule's
# scope REPLACES the default for the VIs under it (first matching rule wins), and
# "exclude" scopes drop their VIs entirely.
def _norm_path(p: str) -> str:
    return (p or "").replace("\\", "/").strip("/").lower()


def _path_matches(vi_rel: str, scope: str) -> bool:
    """A VI is in a scope when the scope is the VI itself or a folder above it."""
    vr, s = _norm_path(vi_rel), _norm_path(scope)
    return bool(s) and (vr == s or vr.startswith(s + "/"))


def _parse_pass_report(text: str, label: str) -> dict:
    vis = [v for v in parse_failed(text) if not is_tooling_vi(v["vi_rel"])]
    for v in vis:
        v["config"] = label
    return {"vis": vis, "summary": parse_summary(text), "errors": parse_errors(text),
            "testing_errors": _all_testing_errors(text), "meta": parse_meta(text)}


def merge_passes(passes: list, args: argparse.Namespace) -> dict:
    """passes: ordered list of {kind, config, label, paths, data|None}."""
    rule_passes = [p for p in passes if p["kind"] == "rule" and p.get("config") != "none"]
    exclude_scopes = []
    for p in passes:
        if p["kind"] == "exclude" or (p["kind"] == "rule" and p.get("config") == "none"):
            exclude_scopes += p.get("paths", [])
    default_pass = next((p for p in passes if p["kind"] == "default"), None)

    def excluded(vr):
        return any(_path_matches(vr, s) for s in exclude_scopes)

    def claimed_by_rule(vr):
        return any(_path_matches(vr, s) for p in rule_passes for s in p.get("paths", []))

    merged = {}
    # Rules first, in order: the first rule whose scope contains a VI owns it.
    for p in rule_passes:
        scope = p.get("paths", [])
        for v in (p["data"]["vis"] if p.get("data") else []):
            vr = v["vi_rel"]
            if excluded(vr) or vr in merged:
                continue
            if any(_path_matches(vr, s) for s in scope):
                merged[vr] = v
    # Default fills every VI not excluded and not already owned by a rule scope.
    if default_pass and default_pass.get("data"):
        for v in default_pass["data"]["vis"]:
            vr = v["vi_rel"]
            if excluded(vr) or claimed_by_rule(vr) or vr in merged:
                continue
            merged[vr] = v

    ran = [p for p in passes if p.get("data")]
    summary = {k: sum(p["data"]["summary"].get(k, 0) for p in ran)
               for k in ("vis_analyzed", "tests_run", "passed", "failed", "skipped")}
    errors = {k: sum(p["data"]["errors"].get(k, 0) for p in ran)
              for k in ("vi_not_loadable", "test_not_loadable", "test_not_runnable", "test_error_out")}
    # Testing errors: union across passes, de-duplicated by (test, vi_rel, message).
    te_seen, testing_errors = set(), []
    te_index = {}
    for p in ran:
        for blk in p["data"]["testing_errors"]:
            tgt = te_index.get(blk["test"])
            if tgt is None:
                tgt = {"test": blk["test"], "items": []}
                te_index[blk["test"]] = tgt
                testing_errors.append(tgt)
            for it in blk["items"]:
                key = (blk["test"], it.get("vi_rel", ""), it.get("message", ""))
                if key in te_seen:
                    continue
                te_seen.add(key)
                tgt["items"].append(it)

    meta_extra = {}
    base_meta = (default_pass or (ran[0] if ran else {})).get("data", {}) if (default_pass or ran) else {}
    if base_meta:
        meta_extra.update(base_meta.get("meta", {}))
    # Which configurations contributed (shown when more than one applies).
    has_rules = any(p["kind"] == "rule" for p in passes)
    configs = []
    for p in passes:
        if p["kind"] == "default":
            if p.get("config") == "none":
                continue  # no default suite -> nothing to list for it
            configs.append({"label": p.get("label", "Built-in full test suite"), "scope": "all other VIs" if has_rules else "all VIs"})
        elif p["kind"] in ("rule", "exclude"):
            is_excl = p["kind"] == "exclude" or p.get("config") == "none"
            configs.append({"label": "Excluded (not tested)" if is_excl else p.get("label", p.get("config", "")),
                            "scope": ", ".join(p.get("paths", [])) or "(no paths)",
                            "exclude": is_excl})
    meta_extra["configs"] = configs
    return _finalize(list(merged.values()), summary, errors, testing_errors, meta_extra, args)


def build_data_from_passes(passes_dir: str, args: argparse.Namespace) -> dict:
    pd = Path(passes_dir)
    manifest = json.loads((pd / "manifest.json").read_text(encoding="utf-8"))
    passes = []
    for p in manifest.get("passes", []):
        data = None
        rep = p.get("report")
        if rep and (pd / rep).exists():
            text = (pd / rep).read_text(encoding="utf-8", errors="replace")
            data = _parse_pass_report(text, p.get("label") or p.get("config") or "")
        passes.append({**p, "data": data})
    return merge_passes(passes, args)



# ── Renderer ─────────────────────────────────────────────────────────────────
def render(data: dict) -> str:
    blob = json.dumps(data, ensure_ascii=False)
    # Neutralise any sequence that could break out of the <script> blob.
    blob = blob.replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")
    # The shared site header (lvci-header.js, deployed once at the Pages root) reads
    # this config to render consistent nav + the report's context actions (Re-run
    # analysis, Native report, This commit). Pages base + commit are known here, so
    # bake a small config rather than depend on client-side parsing order.
    m = data.get("meta", {}) or {}
    pages = (m.get("pages_url") or "").rstrip("/")
    base = (m.get("pages_base") or "../..").rstrip("/")  # relative prefix to the Pages root
    hdr_cfg = {
        "context": "vi-analyzer-report",
        "repo": m.get("repo", ""),
        "pagesUrl": pages or base,
        "sha": m.get("sha", ""),
        "short": m.get("short", ""),
        "platform": m.get("platform", "windows"),
        "rawUrl": "raw.html",
    }
    hdr_src = base + "/lvci-header.js"  # reports deploy at vi-analyzer/<sha>/ (../..); a re-run sits one deeper
    out = _TEMPLATE.replace("__VIA_DATA_JSON__", blob)
    out = out.replace("__VIA_HEADER_CFG__", json.dumps(hdr_cfg, ensure_ascii=False))
    out = out.replace("__LVCI_HEADER_SRC__", hdr_src)
    out = out.replace("__PAGES_BASE__", base)
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description="Build a friendly VI Analyzer report from the native HTML report.")
    ap.add_argument("--in", dest="in_path", default="", help="Path to the native VI Analyzer index.html (single-config run)")
    ap.add_argument("--passes-dir", dest="passes_dir", default="", help="Directory of per-configuration passes (passes/manifest.json + native reports) for a multi-config run")
    ap.add_argument("--out", dest="out_dir", required=True, help="Output directory")
    ap.add_argument("--sha", default="", help="Commit SHA being analyzed")
    ap.add_argument("--platform", default="windows", choices=["windows", "linux"])
    ap.add_argument("--commit-msg", dest="commit_msg", default="")
    ap.add_argument("--author", default="")
    ap.add_argument("--date", default="")
    ap.add_argument("--repo", default="")
    ap.add_argument("--pages-url", dest="pages_url", default="")
    ap.add_argument("--pages-base", dest="pages_base", default="../..",
                    help="Relative prefix from the report to the Pages root (../.. for vi-analyzer/<sha>/; ../../.. for a deeper re-run path)")
    args = ap.parse_args()

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    if args.passes_dir:
        # Multi-configuration run: merge each pass's native report. Preserve the
        # default (or first) pass's native report as raw.html for reference.
        pd = Path(args.passes_dir)
        manifest = json.loads((pd / "manifest.json").read_text(encoding="utf-8"))
        passes = manifest.get("passes", [])
        raw_src = next((p for p in passes if p.get("kind") == "default" and p.get("report")), None) \
            or next((p for p in passes if p.get("report")), None)
        if raw_src and (pd / raw_src["report"]).exists():
            (out_dir / "raw.html").write_text((pd / raw_src["report"]).read_text(encoding="utf-8", errors="replace"), encoding="utf-8")
        data = build_data_from_passes(args.passes_dir, args)
    else:
        if not args.in_path:
            ap.error("one of --in or --passes-dir is required")
        in_path = Path(args.in_path)
        report = in_path.read_text(encoding="utf-8", errors="replace")
        # Preserve the native report verbatim before we overwrite index.html.
        if in_path.resolve() == (out_dir / "index.html").resolve():
            (out_dir / "raw.html").write_text(report, encoding="utf-8")
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
<script>window.LVCI=__VIA_HEADER_CFG__;</script>
<script src="__LVCI_HEADER_SRC__" defer></script>
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
.errbd{font-size:.82em;color:var(--fg-muted)}
.errnote{font-size:.84em;margin-top:8px;line-height:1.5}
.errnote code{background:var(--surface2);border:1px solid var(--border);border-radius:4px;padding:0 4px}
.errdetail{margin-top:10px;max-height:360px;overflow:auto}
.errcat{font-weight:600;font-size:.84em;margin:10px 0 2px}
.errcat .errcount{color:var(--fg-muted);font-weight:400}
.errband .errvi{font-weight:600}
.errband .errpath{font-weight:400;color:var(--fg-muted);font-size:.9em;word-break:break-all;margin-top:2px}
.errband .errmsg{color:var(--fg-muted)}
.errtool{display:inline-block;background:var(--surface2);border:1px solid var(--border);border-radius:20px;padding:0 8px;font-size:.72em;color:var(--fg-muted);font-weight:600;vertical-align:middle}

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

/* inline snapshot preview (rendered VI shown inside a card when it is expanded) */
.snapinline{margin:2px 0 12px;border:1px solid var(--border);border-radius:8px;overflow:hidden;background:var(--surface2)}
.snapinline-bar{display:flex;align-items:center;gap:10px;padding:6px 10px;border-bottom:1px solid var(--border);font-size:.78em;color:var(--fg-muted)}
.snapinline-cap{flex:1 1 auto}
.snapinline-frame{width:100%;height:360px;border:none;background:#fff;display:block}
.snapinline-open{flex:0 0 auto;background:none;border:1px solid var(--border);color:var(--link);border-radius:6px;padding:2px 9px;font:inherit;font-size:.95em;cursor:pointer}
.snapinline-open:hover{border-color:var(--link)}
.snapinline-note{padding:12px;font-size:.82em;color:var(--fg-muted);line-height:1.5}

/* raw-report download link in the summary sub-line */
.rawdl{white-space:nowrap}

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

/* re-run a single VI with a chosen configuration */
.rerunbtn{flex:0 0 auto;background:none;border:1px solid var(--border);color:var(--link);border-radius:6px;
  padding:3px 9px;font:inherit;font-size:.76em;cursor:pointer}
.rerunbtn:hover{border-color:var(--link)}
.viacfgpill{background:var(--surface2);border:1px solid var(--border)}
.viacfgs{color:var(--fg-muted);font-size:.82em;margin:-8px 0 14px}
.viacfgs code{background:var(--surface2);border:1px solid var(--border);border-radius:4px;padding:0 5px}
#rrback{position:fixed;inset:0;background:rgba(0,0,0,.5);opacity:0;pointer-events:none;transition:opacity .15s;z-index:50}
#rrback.show{opacity:1;pointer-events:auto}
#rrwrap{position:fixed;top:50%;left:50%;transform:translate(-50%,-46%);width:min(92vw,540px);max-height:86vh;overflow:auto;
  background:var(--surface);border:1px solid var(--border);border-radius:12px;z-index:51;opacity:0;pointer-events:none;
  transition:opacity .15s, transform .15s}
#rrwrap.show{opacity:1;pointer-events:auto;transform:translate(-50%,-50%)}
#rrhead{display:flex;align-items:center;gap:12px;padding:12px 16px;border-bottom:1px solid var(--border)}
#rrhead .t{font-weight:600;flex:1 1 auto}
#rrhead button{background:none;border:none;color:var(--fg);font-size:1.3em;cursor:pointer;line-height:1}
#rrbody{padding:14px 16px}
.rrintro{margin:0 0 12px;color:var(--fg-muted);font-size:.86em;line-height:1.5}
.rrlabel{display:block;font-size:.8em;color:var(--fg-muted);margin-bottom:5px}
#rrconfig{width:100%;background:var(--bg);color:var(--fg);border:1px solid var(--border);border-radius:7px;padding:8px 10px;font:inherit;font-size:.88em}
.rractions{margin-top:14px}
.rrgo{background:var(--link);color:#fff;border:none;border-radius:7px;padding:8px 14px;font:inherit;font-weight:600;cursor:pointer}
.rrgo:disabled{opacity:.6;cursor:default}
.rrstatus{margin-top:12px;font-size:.84em;display:none;line-height:1.5}
.rrstatus.show{display:block}
.rrstatus.ok{color:var(--ok)}.rrstatus.err{color:var(--bad)}
.rrnone{font-size:.86em;color:var(--fg-muted);line-height:1.5}
.rrtok{margin-top:12px;display:flex;flex-direction:column;gap:8px;font-size:.84em}
.rrtok input{background:var(--bg);color:var(--fg);border:1px solid var(--border);border-radius:7px;padding:7px 10px;font:inherit}
/* platform toggle */
.platrow{display:flex;align-items:center;gap:12px;margin-bottom:16px;flex-wrap:wrap}
#plat-toggle{display:inline-flex;align-items:center;border:1px solid var(--border);border-radius:8px;overflow:hidden}
#plat-toggle button{border:none;background:var(--surface);color:var(--fg-muted);cursor:pointer;font:inherit;
  font-weight:600;font-size:.82em;padding:7px 16px;display:inline-flex;align-items:center;gap:7px}
#plat-toggle button:hover:not(.active):not(:disabled){background:var(--hover);color:var(--fg)}
#plat-toggle button.active{background:var(--link);color:#fff}
#plat-toggle button:disabled{opacity:.4;cursor:default}
#plat-toggle .sep{width:1px;align-self:stretch;background:var(--border)}
.platnote{color:var(--fg-muted);font-size:.8em}
</style>
</head>
<body>
<div class="wrap">
  <h1>VI Analyzer Report</h1>
  <div class="sub" id="sub"></div>

  <div class="platrow">
    <div id="plat-toggle"></div>
    <span class="platnote" id="platnote"></span>
  </div>

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

<div id="rrback"></div>
<aside id="rrwrap" role="dialog" aria-modal="true" aria-labelledby="rrtitle">
  <div id="rrhead">
    <span class="t" id="rrtitle">Re-run VI Analyzer</span>
    <button id="rrclose" title="Close">&times;</button>
  </div>
  <div id="rrbody">
    <p class="rrintro">Re-run VI Analyzer on <code id="rrvi"></code> with a different test configuration. Only this VI is analyzed and its result is published separately &mdash; the revision's full report is left unchanged.</p>
    <label class="rrlabel" for="rrconfig">Test configuration (a committed <code>.viancfg</code>)</label>
    <select id="rrconfig"></select>
    <div class="rrnone" id="rrnone" hidden></div>
    <div class="rractions"><button class="rrgo" id="rrgo">Re-run this VI</button></div>
    <div class="rrstatus" id="rrstatus"></div>
    <div class="rrtok" id="rrtok" hidden></div>
  </div>
</aside>

<script id="via-data" type="application/json">__VIA_DATA_JSON__</script>
<script>
const DATA = JSON.parse(document.getElementById('via-data').textContent);
const META = DATA.meta, SUM = DATA.summary, ERR = DATA.errors;
const CATS = DATA.categories, ORDER = DATA.category_order;
const SNAP_BASE = '__PAGES_BASE__/vi-snapshots/';
// Show the per-VI configuration pill only when more than one *test* configuration
// applies (excludes don't count); the header note lists all configs incl excludes.
const MULTICFG = ((META.configs||[]).filter(c=>!c.exclude).length) >= 1;
const esc = s => String(s==null?'':s).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));

// ── platform toggle (switch between the Windows and Linux VI Analyzer reports) ──
// The Windows report lives at vi-analyzer/<sha>/ and the Linux report at
// vi-analyzer/<sha>/linux/, so the two reports are one directory apart. We probe
// the other platform's summary.json; if it exists the button navigates there,
// otherwise it is disabled with a note. (The VI Analyzer report renders entirely
// from its own injected blob, so switching loads the sibling report rather than
// re-rendering in place.)
(function platToggle(){
  const PLAT_LABEL = {windows:'Windows', linux:'Linux'};
  const ORDER_PLAT = ['windows','linux'];
  const self = META.platform === 'linux' ? 'linux' : 'windows';
  const other = self === 'linux' ? 'windows' : 'linux';
  const otherIndex   = self === 'linux' ? '../index.html'   : 'linux/index.html';
  const otherSummary = self === 'linux' ? '../summary.json' : 'linux/summary.json';
  const host = document.getElementById('plat-toggle');
  const note = document.getElementById('platnote');
  if(!host) return;
  function draw(otherOk){
    host.innerHTML = ORDER_PLAT.map((p,i)=>{
      const dis = (p!==self && !otherOk) ? 'disabled' : '';
      return (i?'<span class="sep"></span>':'') +
        `<button data-plat="${p}" class="${p===self?'active':''}" ${dis}>${esc(PLAT_LABEL[p])}</button>`;
    }).join('');
    host.querySelectorAll('button').forEach(b=>b.addEventListener('click',()=>{
      if(b.dataset.plat===self || b.disabled) return;
      window.location.href = otherIndex;
    }));
  }
  draw(false); // optimistic: keep the other platform disabled until the probe returns
  fetch(otherSummary,{cache:'no-store'}).then(r=>{
    if(r.ok){ draw(true); note.textContent=''; }
    else { note.textContent = `VI Analyzer has not run on ${PLAT_LABEL[other]} for this commit.`; }
  }).catch(()=>{ note.textContent = `VI Analyzer has not run on ${PLAT_LABEL[other]} for this commit.`; });
})();

// active severity filters (all on by default)
const active = new Set(ORDER);
let query = '';

// ── header ───────────────────────────────────────────────────────────────
(function header(){
  const m = META;
  const commitMsg = (m.commit && m.commit.message) ? esc(m.commit.message) : '';
  // The commit hash opens this revision's VI snapshots in the VI Browser; the
  // GitHub commit stays available via the trailing "commit on GitHub" link (and,
  // on a standalone report, the header's "This commit" action — which the shared
  // header suppresses when the report is embedded in the report-viewer iframe).
  const snapLink = m.sha ? SNAP_BASE + 'index.html?sha=' + encodeURIComponent(m.sha) : '';
  const ghLink   = m.repo && m.sha ? `https://github.com/${m.repo}/commit/${m.sha}` : '';
  document.getElementById('sub').innerHTML =
    (m.short ? `Commit ${snapLink?`<a href="${snapLink}" target="_blank" rel="noopener" title="Browse this commit's VI snapshots in the VI Browser">${esc(m.short)}</a>`:esc(m.short)} ` : '') +
    (commitMsg ? `&middot; ${commitMsg} ` : '') +
    (m.platform ? `&middot; ${esc(m.platform)} ` : '') +
    (m.analysis_date ? `&middot; ${esc(m.analysis_date)} ` : '') +
    (m.duration ? `&middot; ${esc(m.duration)} ` : '') +
    `&middot; generated ${esc(m.generated_utc||'')}` +
    (ghLink ? ` &middot; <a href="${ghLink}" target="_blank" rel="noopener" title="View this commit on GitHub">commit on GitHub &#8599;</a>` : '') +
    ` &middot; <a class="rawdl" href="raw.html" download="vi-analyzer-${esc(m.short||'report')}-raw.html" title="Download the native LabVIEW VI Analyzer HTML report this summary was built from">&#11015; Download raw report</a>`;

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
  if(META.configs && META.configs.length >= 1){
    const cfgnote = document.createElement('div'); cfgnote.className='viacfgs';
    cfgnote.innerHTML = 'Test configurations applied: ' + META.configs.map(c=>`<code>${esc(c.label)}</code>${c.scope?` <span style="opacity:.8">(${esc(c.scope)})</span>`:''}`).join(' · ');
    const sub=document.getElementById('sub'); sub.parentNode.insertBefore(cfgnote, sub.nextSibling);
  }
})();

// ── testing errors banner ────────────────────────────────────────────────
(function errband(){
  const te = DATA.testing_errors||[];
  const totalErr = (ERR.vi_not_loadable+ERR.test_not_loadable+ERR.test_not_runnable+ERR.test_error_out)||0;
  if(!te.length && !totalErr) return;
  const el = document.getElementById('errband'); el.classList.remove('hidden');
  // Flatten the per-category VI-level detail so we can explain what failed.
  const items = []; for(const blk of te) for(const it of blk.items) items.push(it);
  const proj = items.filter(it=>!it.tooling), tool = items.filter(it=>it.tooling);
  let note;
  if(items.length){
    if(tool.length && !proj.length)
      note = `<div class="errnote">All ${tool.length} are <strong>CI helper VIs</strong> (under <code>.github/</code>), not your project code &mdash; they do not affect your analysis results. Each VI and its error is listed below.</div>`;
    else if(tool.length && proj.length)
      note = `<div class="errnote"><strong>${proj.length}</strong> in your project code, <strong>${tool.length}</strong> in CI helper VIs &mdash; each VI and its error is listed below.</div>`;
    else
      note = `<div class="errnote">The affected VIs and their errors are listed below.</div>`;
  } else {
    note = `<div class="errnote">The native report did not attach VI-level detail &mdash; open the <a href="raw.html" target="_blank" rel="noopener">native report</a> for specifics.</div>`;
  }
  const chip = it => it.tooling ? ` <span class="errtool" title="CI tooling VI, not your project code">CI tooling</span>` : '';
  const groups = te.map(blk=>{
    const rows = blk.items.map(it=>
      `<tr><td class="errvi">${esc(it.vi_name)}${chip(it)}<div class="errpath">${esc(it.vi_rel||it.vi_path)}</div></td><td class="errmsg">${esc(it.message)}</td></tr>`).join('');
    return `<div class="errcat">${esc(blk.test)} <span class="errcount">(${blk.items.length})</span></div>`+
      `<table><tr><th>VI</th><th>Error message</th></tr>${rows}</table>`;
  }).join('');
  el.innerHTML =
    `<h2>&#9888; ${totalErr} testing error${totalErr===1?'':'s'} &mdash; these tests could not run (infrastructure, not code quality)</h2>`+
    `<div class="errbd">VI not loadable: ${ERR.vi_not_loadable} &middot; Test not loadable: ${ERR.test_not_loadable} &middot; Test not runnable: ${ERR.test_not_runnable} &middot; Test error out: ${ERR.test_error_out}</div>`+
    note +
    (items.length ? `<div class="errdetail">${groups}</div>` : '');
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
      ${MULTICFG && v.config ? `<span class="pill viacfgpill" title="Analyzed with this configuration">${esc(v.config)}</span>` : ''}
      <button class="snapbtn" data-snap="${esc(v.vi_rel)}" data-name="${esc(v.name)}">Snapshot</button>
      <button class="rerunbtn" data-rerun="${esc(v.vi_rel)}" data-name="${esc(v.name)}" title="Re-run VI Analyzer on this VI with a chosen configuration">Re-run…</button>
    </summary>
    <div class="vibody"><div class="snapinline" data-snap="${esc(v.vi_rel)}" data-name="${esc(v.name)}"></div>${rules}</div>
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

// Inline snapshot preview inside each VI card: lazily load the rendered VI when
// the card is expanded so the graphical code sits right next to its findings.
// Nothing loads until a VI is opened; the full-size drawer stays available.
async function loadInlineSnap(host){
  if(host.dataset.loaded) return;
  host.dataset.loaded='1';
  const viRel=host.dataset.snap, name=host.dataset.name;
  host.innerHTML=`<div class="snapinline-note">Loading snapshot\u2026</div>`;
  try{ await ensureSnapshots(); }catch(e){}
  const htmlPath=snapMap && snapMap[viRel];
  if(htmlPath){
    const stale=(snapFromSha && META.sha && snapFromSha!==META.sha)
      ? `<div class="snapinline-note">Latest available snapshot (commit ${esc(snapFromSha.slice(0,7))}); this commit's may still be rendering.</div>` : '';
    host.innerHTML=
      `<div class="snapinline-bar"><span class="snapinline-cap">Front panel &amp; block diagram</span>`+
      `<button class="snapinline-open snapbtn" data-snap="${esc(viRel)}" data-name="${esc(name)}" title="Open a larger, interactive view">Open full view \u2197</button></div>`+
      `<iframe class="snapinline-frame" loading="lazy" src="${esc(SNAP_BASE+'vi-interactive.html?file='+encodeURIComponent(htmlPath))}" title="Snapshot of ${esc(name)}"></iframe>`+stale;
  }else{
    host.innerHTML=`<div class="snapinline-note">No snapshot yet for <code>${esc(viRel)}</code> \u2014 it may still be rendering. <a href="${SNAP_BASE}index.html${META.sha?`?sha=${META.sha}`:''}" target="_blank" rel="noopener">Open the VI Browser \u2197</a></div>`;
  }
}
// `toggle` does not bubble, so listen in the capture phase.
document.addEventListener('toggle',e=>{
  const vi=e.target;
  if(vi && vi.classList && vi.classList.contains('vi') && vi.open){
    const host=vi.querySelector('.snapinline');
    if(host) loadInlineSnap(host);
  }
}, true);

// ── re-run a single VI with a chosen .viancfg configuration ────────────────────
const RR_TOK = 'lvci_dispatch_token';
let rrConfigs = null, rrVi = '', rrPollTimer = null;
// A single-VI re-run dispatches the workflow that produced THIS report's platform,
// so a Linux report re-runs on Linux and a Windows report on Windows.
function rrWorkflow(){ return (META.platform === 'linux') ? 'run-vi-analyzer-linux-container.yml' : 'run-vi-analyzer-windows-container.yml'; }
function rrTok(){ try { return localStorage.getItem(RR_TOK)||''; } catch(e){ return ''; } }
// Slug must match the runner's deterministic re-run output path (PowerShell/bash).
function rrSlug(cfg, vi){ return (cfg+'__'+vi).toLowerCase().replace(/[^a-z0-9]+/g,'-').replace(/^-+|-+$/g,'').slice(0,80); }
function rrPagesBase(){ return (META.pages_url && /^https?:/i.test(META.pages_url)) ? META.pages_url.replace(/\/$/,'') : '../..'; }
async function rrDiscover(){
  if(rrConfigs) return rrConfigs;
  rrConfigs = [];
  if(!META.repo) return rrConfigs;
  const ref = META.sha || 'main';
  try {
    const r = await fetch(`https://api.github.com/repos/${META.repo}/git/trees/${encodeURIComponent(ref)}?recursive=1`,{cache:'no-store'});
    if(r.ok){ const d = await r.json(); rrConfigs = (d.tree||[]).filter(t=>t.type==='blob'&&/\.viancfg$/i.test(t.path)&&!/^(\.github|ci-out|build)\//i.test(t.path)).map(t=>t.path).sort(); }
  } catch(e){}
  return rrConfigs;
}
function rrStatus(html, kind){ const el=document.getElementById('rrstatus'); el.innerHTML=html||''; el.className='rrstatus'+(html?' show':'')+(kind?(' '+kind):''); }
async function openRerun(viRel){
  rrVi = viRel;
  document.getElementById('rrvi').textContent = viRel;
  document.getElementById('rrback').classList.add('show');
  document.getElementById('rrwrap').classList.add('show');
  rrStatus(''); document.getElementById('rrtok').hidden=true;
  const sel=document.getElementById('rrconfig'), none=document.getElementById('rrnone'), go=document.getElementById('rrgo');
  sel.innerHTML='<option>Loading configurations\u2026</option>'; sel.disabled=true; go.disabled=true; none.hidden=true; sel.style.display=''; go.style.display='';
  const cfgs = await rrDiscover();
  if(rrVi!==viRel) return;
  if(!cfgs.length){
    sel.style.display='none'; go.style.display='none'; none.hidden=false;
    none.innerHTML = 'No <code>.viancfg</code> configurations were found in this repository. Create one in LabVIEW (Tools \u203a VI Analyzer, choose tests, <strong>Save Configuration</strong>) and commit it \u2014 then you can re-run this VI against it.';
    return;
  }
  sel.innerHTML = cfgs.map(p=>`<option value="${esc(p)}">${esc(p)}</option>`).join('');
  sel.disabled=false; go.disabled=false;
}
function closeRerun(){ document.getElementById('rrback').classList.remove('show'); document.getElementById('rrwrap').classList.remove('show'); if(rrPollTimer){ clearTimeout(rrPollTimer); rrPollTimer=null; } }
function rrShowToken(){
  const p=document.getElementById('rrtok'); const owner=(META.repo.split('/')[0])||'';
  const url='https://github.com/settings/personal-access-tokens/new?name='+encodeURIComponent('LabVIEW CI dispatch')+'&description='+encodeURIComponent('Dispatch CI runs for '+META.repo)+'&target_name='+encodeURIComponent(owner)+'&actions=write';
  p.hidden=false;
  p.innerHTML = '<div>Re-running needs a fine-grained token with <strong>Actions: Read and write</strong> on <code>'+esc(META.repo)+'</code>. <a href="'+url+'" target="_blank" rel="noopener">Create one \u2197</a> (stored only in this browser; shared with the dashboard).</div><input id="rrtokin" type="password" placeholder="github_pat_\u2026" autocomplete="off" spellcheck="false"><button class="rrgo" id="rrtoksave">Save &amp; re-run</button>';
  const inp=document.getElementById('rrtokin'), save=document.getElementById('rrtoksave');
  if(inp) inp.focus();
  if(save) save.addEventListener('click',()=>{ const v=(inp&&inp.value||'').trim(); if(!v){ if(inp) inp.focus(); return; } try{ localStorage.setItem(RR_TOK,v); }catch(e){} p.hidden=true; rrDispatch(); });
  if(inp) inp.addEventListener('keydown',ev=>{ if(ev.key==='Enter'&&save) save.click(); });
}
function rrDispatch(){
  const sel=document.getElementById('rrconfig'); const cfg=sel.value; if(!cfg) return;
  if(!META.repo||!META.sha){ rrStatus('Re-running needs a repository and commit.','err'); return; }
  if(!rrTok()){ rrShowToken(); rrStatus('Paste a token to dispatch the run.'); return; }
  const wf=rrWorkflow(); const go=document.getElementById('rrgo');
  go.disabled=true; go.textContent='Queuing\u2026';
  rrStatus('Queuing a re-run of <code>'+esc(rrVi)+'</code> with <code>'+esc(cfg)+'</code>\u2026');
  fetch('https://api.github.com/repos/'+META.repo+'/actions/workflows/'+encodeURIComponent(wf)+'/dispatches',{
    method:'POST',
    headers:{'Authorization':'Bearer '+rrTok(),'Accept':'application/vnd.github+json','X-GitHub-Api-Version':'2022-11-28','Content-Type':'application/json'},
    body:JSON.stringify({ref:'main',inputs:{commit_sha:META.sha,files:rrVi,config:cfg}})
  }).then(r=>{
    go.disabled=false; go.textContent='Re-run this VI';
    if(r.status===204){
      const url=rrPagesBase()+'/vi-analyzer/'+encodeURIComponent(META.sha)+'/reruns/'+rrSlug(cfg,rrVi)+'/index.html';
      const runs='https://github.com/'+META.repo+'/actions/workflows/'+wf;
      rrStatus('\u2713 Queued. The result will appear as <a href="'+url+'" target="_blank" rel="noopener">this VI\u2019s re-run report \u2197</a> when the run finishes, and in the <a href="'+runs+'" target="_blank" rel="noopener">Actions run \u2197</a>.','ok');
      rrPoll(url);
      return;
    }
    if(r.status===401){ try{localStorage.removeItem(RR_TOK);}catch(e){} rrStatus('That token was rejected (401). Paste a valid one.','err'); rrShowToken(); return; }
    if(r.status===403){ rrStatus('<strong>403</strong>: the token is missing <strong>Actions: Read and write</strong> on this repository.','err'); rrShowToken(); return; }
    if(r.status===404){ rrStatus('<strong>404</strong>: the token cannot see <code>'+esc(META.repo)+'</code>. Grant it access + Actions: Read and write.','err'); return; }
    if(r.status===422){ rrStatus('This repository\u2019s VI Analyzer workflow doesn\u2019t accept a per-VI configuration yet \u2014 run a tooling update, then try again.','err'); return; }
    rrStatus('Dispatch failed (HTTP '+r.status+').','err');
  }).catch(e=>{ go.disabled=false; go.textContent='Re-run this VI'; rrStatus('Network error: '+esc(String(e&&e.message||e)),'err'); });
}
function rrPoll(url){
  let tries=0; if(rrPollTimer) clearTimeout(rrPollTimer);
  const tick=()=>{ tries++; fetch(url+'?_='+Date.now(),{cache:'no-store'}).then(r=>{ if(r.ok){ rrStatus('\u2713 The re-run result is ready: <a href="'+url+'" target="_blank" rel="noopener">open it \u2197</a>.','ok'); rrPollTimer=null; return; } if(tries<24){ rrPollTimer=setTimeout(tick,8000); } }).catch(()=>{ if(tries<24){ rrPollTimer=setTimeout(tick,8000); } }); };
  rrPollTimer=setTimeout(tick,8000);
}
document.getElementById('rrclose').addEventListener('click',closeRerun);
document.getElementById('rrback').addEventListener('click',closeRerun);
document.getElementById('rrgo').addEventListener('click',rrDispatch);
document.addEventListener('keydown',e=>{ if(e.key==='Escape') closeRerun(); });
document.addEventListener('click',e=>{ const b=e.target.closest('.rerunbtn'); if(!b) return; e.preventDefault(); e.stopPropagation(); openRerun(b.dataset.rerun); });

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

// The site header (lvci-header.js) owns "Re-run analysis" + the token flow now,
// reading window.LVCI for this commit; nothing report-specific is needed here.

renderVI(); renderRule(); apply();
</script>
</body>
</html>
"""


if __name__ == "__main__":
    main()
