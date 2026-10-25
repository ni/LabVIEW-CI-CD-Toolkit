#!/usr/bin/env python3
"""
build-unittest-report.py — Turn the JUnit XML produced by one or more LabVIEW
unit-test frameworks (JKI Caraya, JKI VI Tester, Astemes LUnit, and NI Unit Test
Framework) into ONE friendly, navigable report, exactly like the Mass Compile /
VI Analyzer reports: it groups results by tool → suite → test, surfaces failures
first, and lets you open the test VI's rendered snapshot (and, when derivable,
the VI *under test*) in the VI Browser for this revision.

WHY A SHARED REPORT
    Each framework reports differently (Caraya = assertion VIs, VI Tester =
    xUnit TestCase classes, UTF = .lvtest), but all three can emit JUnit XML.
    JUnit is therefore the common interchange: the runner script writes one XML
    per enabled tool, and this script merges them into a single unified model so
    the dashboard shows one "Unit Tests" result per revision with a pass-rate
    badge, and the viewer frames it in the shared site chrome (lvci-header.js).

INPUTS
    --results <dir>   directory of JUnit XML files. The tool for each file is
                      inferred from its name: caraya*.xml → Caraya,
                      vi-tester*/vitester* → VI Tester, lunit*.xml → LUnit,
                      utf*/unit-test* → UTF.
                      (Override per file with --junit TOOL:PATH, repeatable.)
    --workspace <dir> repo checkout — used to resolve a test's VI name to its
                      repo-relative path (for snapshots) and to derive the VI
                      under test by naming convention.

OUTPUTS
    <out>/index.html  the friendly report (the deployed page)
    <out>/results.json the unified model (so the report can be rebuilt, and the
                       OTHER platform's tab can lazy-load it)

The model + renderer intentionally mirror build-masscompile-report.py so the two
reports feel identical and share the snapshot drawer + VI-Browser deep links.
"""
from __future__ import annotations

import argparse
import glob
import html
import json
import os
import re
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote


# ── tool identity ────────────────────────────────────────────────────────────
TOOLS = {
    "caraya":    {"id": "caraya",    "label": "Caraya"},
    "vi-tester": {"id": "vi-tester", "label": "VI Tester"},
    "lunit":     {"id": "lunit",     "label": "LUnit"},
    "utf":       {"id": "utf",       "label": "Unit Test Framework"},
}
STATUS_ORDER = ["failed", "error", "skipped", "passed"]
STATUS_META = {
    "failed":  {"label": "Failed",  "color": "#da3633", "blurb": "An assertion failed."},
    "error":   {"label": "Errored", "color": "#bb8009", "blurb": "The test raised an unexpected error."},
    "skipped": {"label": "Skipped", "color": "#6e7681", "blurb": "The test was skipped."},
    "passed":  {"label": "Passed",  "color": "#2ea043", "blurb": "The test passed."},
}


def tool_for_filename(name: str) -> str:
    n = name.lower()
    if "caraya" in n:
        return "caraya"
    if "vitester" in n or "vi-tester" in n or "vi_tester" in n:
        return "vi-tester"
    if n.startswith("lunit"):
        return "lunit"
    if n.startswith("utf") or "unit-test-framework" in n or "lvtest" in n:
        return "utf"
    return "caraya"  # safe default; most hand-rolled JUnit looks like Caraya's


# ── VI path resolution (mirror of the mass-compile report's approach) ─────────
def index_workspace(workspace: Path) -> dict[str, list[str]]:
    """Map each VI's lower-case base name → list of repo-relative paths. Lets us
    resolve a test's bare VI name to a real path for the snapshot gallery."""
    by_base: dict[str, list[str]] = {}
    if not workspace or not workspace.is_dir():
        return by_base
    for root, _dirs, files in os.walk(workspace):
        # Skip CI tooling + build output so we match the snapshot gallery.
        rel_root = os.path.relpath(root, workspace)
        parts = rel_root.replace("\\", "/").split("/")
        if parts and parts[0] in (".git", ".github", "ci-out", "build"):
            continue
        for f in files:
            if f.lower().endswith(".vi"):
                rel = os.path.relpath(os.path.join(root, f), workspace).replace("\\", "/")
                by_base.setdefault(f[:-3].lower(), []).append(rel)
    return by_base


def to_rel(path: str) -> str:
    return (path or "").replace("\\", "/").lstrip("./")


def base_name(name_or_path: str) -> str:
    b = re.split(r"[\\/]", (name_or_path or "").strip())[-1]
    if b.lower().endswith(".vi"):
        b = b[:-3]
    return b


def resolve_vi(name_or_path: str, by_base: dict[str, list[str]]) -> str:
    """Best-effort: a JUnit testcase's name/classname/file → a unique repo path.
    Returns '' when unknown or ambiguous (the card then shows name-only)."""
    if not name_or_path:
        return ""
    p = to_rel(name_or_path)
    # Already a path that looks real and ends in .vi
    if "/" in p and p.lower().endswith(".vi"):
        return p
    hits = by_base.get(base_name(name_or_path).lower(), [])
    return hits[0] if len(hits) == 1 else ""


# Strip common test-naming affixes to guess the VI *under test*:
#   "Foo Tests", "Test Foo", "Foo_Test", "TestFoo", "Foo.Test", "Foo UnitTest"
_TEST_AFFIX = re.compile(
    r"(^|[\s_\-.])(unit\s*)?tests?($|[\s_\-.])|"
    r"^test[\s_\-.]+|[\s_\-.]+test$",
    re.IGNORECASE,
)


def derive_target(test_name: str, by_base: dict[str, list[str]]) -> tuple[str, str]:
    """From a test VI base name, guess the VI under test by stripping a 'test'
    affix and resolving the remainder. Returns ('', '') when not derivable."""
    bn = base_name(test_name)
    cand = _TEST_AFFIX.sub(" ", bn).strip(" _-.")
    cand = re.sub(r"\s{2,}", " ", cand)
    if not cand or cand.lower() == bn.lower():
        return "", ""
    rel = resolve_vi(cand, by_base)
    return (rel, base_name(rel) if rel else cand) if rel else ("", "")


# ── JUnit parsing ─────────────────────────────────────────────────────────────
def _text(el) -> str:
    return (el.text or "").strip() if el is not None else ""


def parse_junit(xml_path: Path, tool: str, by_base: dict[str, list[str]]) -> list[dict]:
    """Parse one JUnit XML file into a list of suite dicts for `tool`."""
    try:
        root = ET.parse(str(xml_path)).getroot()
    except Exception as e:  # malformed XML shouldn't sink the whole report
        return [{
            "tool": tool, "name": f"(could not parse {xml_path.name})",
            "tests": 0, "failures": 0, "errors": 0, "skipped": 0, "time": 0.0,
            "cases": [], "parse_error": str(e),
        }]

    suites_el = root.iter("testsuite") if root.tag != "testsuite" else [root]
    out: list[dict] = []
    for s in suites_el:
        sname = s.get("name") or "(unnamed suite)"
        cases = []
        for c in s.findall("testcase"):
            name = c.get("name") or "(unnamed test)"
            classname = c.get("classname") or ""
            file_attr = c.get("file") or ""
            time = _to_float(c.get("time"))

            fail = c.find("failure")
            err = c.find("error")
            skip = c.find("skipped")
            if fail is not None:
                status, node = "failed", fail
            elif err is not None:
                status, node = "error", err
            elif skip is not None:
                status, node = "skipped", skip
            else:
                status, node = "passed", None
            message = (node.get("message") if node is not None else "") or ""
            details = _text(node) if node is not None else ""
            sysout = _text(c.find("system-out"))
            if sysout and status in ("failed", "error") and sysout not in details:
                details = (details + "\n\n" + sysout).strip()

            # Resolve the TEST VI (always shown when we can find it) from the
            # most specific identifier available: file → classname → name.
            test_rel = (resolve_vi(file_attr, by_base) or resolve_vi(classname, by_base)
                        or resolve_vi(name, by_base))
            test_name = base_name(test_rel) if test_rel else base_name(name)
            # Best-effort VI under test (naming convention).
            target_rel, target_name = derive_target(
                base_name(test_rel) if test_rel else name, by_base)

            cases.append({
                "name": name,
                "classname": classname,
                "status": status,
                "time": time,
                "message": clean(message),
                "details": clean(details),
                "test_vi_rel": test_rel,
                "test_vi_name": test_name,
                "target_vi_rel": target_rel,
                "target_vi_name": target_name,
            })

        out.append({
            "tool": tool,
            "name": sname,
            "tests": _to_int(s.get("tests"), len(cases)),
            "failures": _to_int(s.get("failures"), sum(c["status"] == "failed" for c in cases)),
            "errors": _to_int(s.get("errors"), sum(c["status"] == "error" for c in cases)),
            "skipped": _to_int(s.get("skipped"), sum(c["status"] == "skipped" for c in cases)),
            "time": _to_float(s.get("time")),
            "cases": cases,
        })
    return out


def _to_float(v) -> float:
    try:
        return round(float(v), 3)
    except (TypeError, ValueError):
        return 0.0


def _to_int(v, fallback: int) -> int:
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return fallback


def clean(text: str) -> str:
    text = re.sub(r"[ \t]+\n", "\n", (text or "").replace("\r\n", "\n"))
    return text.strip()


# ── assemble unified model ────────────────────────────────────────────────────
def collect_inputs(args) -> list[tuple[str, Path]]:
    pairs: list[tuple[str, Path]] = []
    for spec in (args.junit or []):
        tool, _, path = spec.partition(":")
        if path and tool in TOOLS:
            pairs.append((tool, Path(path)))
    if args.results and Path(args.results).is_dir():
        for f in sorted(glob.glob(os.path.join(args.results, "*.xml"))):
            pairs.append((tool_for_filename(os.path.basename(f)), Path(f)))
    return pairs


def classify(passed: int, failed: int, errored: int, total: int) -> tuple[str, int]:
    if total == 0:
        return "empty", 0
    percent = round(100 * passed / total)
    if failed == 0 and errored == 0:
        return "passed", percent
    return "failed", percent


def build_data(args) -> dict:
    workspace = Path(args.workspace) if args.workspace else None
    by_base = index_workspace(workspace) if workspace else {}

    suites: list[dict] = []
    tools_seen: list[str] = []
    for tool, path in collect_inputs(args):
        if not path.exists():
            continue
        if tool not in tools_seen:
            tools_seen.append(tool)
        suites.extend(parse_junit(path, tool, by_base))

    passed = failed = errored = skipped = 0
    duration = 0.0
    for s in suites:
        duration += s.get("time", 0.0)
        for c in s["cases"]:
            if c["status"] == "passed":
                passed += 1
            elif c["status"] == "failed":
                failed += 1
            elif c["status"] == "error":
                errored += 1
            elif c["status"] == "skipped":
                skipped += 1
    total = passed + failed + errored + skipped
    status, percent = classify(passed, failed, errored, total)

    meta_extra = {}
    if args.meta and Path(args.meta).exists():
        try:
            meta_extra = json.loads(Path(args.meta).read_text(encoding="utf-8-sig"))
        except Exception:
            meta_extra = {}

    # Cross-platform tab (Windows report at unit-tests/<sha>/, Linux one level
    # deeper at unit-tests/<sha>/linux/), mirroring the mass-compile report.
    if args.platform == "windows":
        platforms = [{"id": "windows", "url": None}, {"id": "linux", "url": "linux/results.json"}]
        snap_depth = "../../"
    else:
        platforms = [{"id": "windows", "url": "../results.json"}, {"id": "linux", "url": None}]
        snap_depth = "../../../"

    # Optional marker written by run-unit-tests.ps1 when a configured tool could
    # not run because the selected container lacks the required tooling. Drives
    # the shared "missing container tooling" banner (see tooling_banner_html).
    tooling = {}
    res_dir = Path(args.results) if args.results else None
    if res_dir and (res_dir / "_tooling.json").exists():
        try:
            tooling = json.loads((res_dir / "_tooling.json").read_text(encoding="utf-8-sig"))
        except Exception:
            tooling = {}
    if not isinstance(tooling, dict):
        tooling = {}
    # PowerShell ConvertTo-Json may emit a single finding as an object rather than
    # a 1-element array; normalize so the model always carries a list.
    _miss = tooling.get("missing")
    tooling["missing"] = [_miss] if isinstance(_miss, dict) else (_miss or [])

    pages_url = (args.pages_url or "").rstrip("/")
    return {
        "meta": {
            "sha": args.sha,
            "short": (args.sha or "")[:7],
            "platform": args.platform,
            "repo": args.repo,
            "pages_url": pages_url,
            "snap_base": (pages_url + "/vi-snapshots/") if pages_url else (snap_depth + "vi-snapshots/"),
            "dash_url": (pages_url + "/") if pages_url else snap_depth,
            "labview_version": args.labview_version or meta_extra.get("labview_version", ""),
            "tools": [TOOLS[t] for t in tools_seen],
            "commit": {"message": args.commit_msg, "author": args.author, "date": args.date},
            "generated_utc": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        },
        "summary": {
            "tests": total, "passed": passed, "failed": failed, "errored": errored,
            "skipped": skipped, "percent": percent, "status": status,
            "duration": round(meta_extra.get("duration", duration), 1),
        },
        "status_order": STATUS_ORDER,
        "status_meta": STATUS_META,
        "platforms": platforms,
        "suites": suites,
        "tooling": tooling,
    }


# ── renderer ──────────────────────────────────────────────────────────────────
def _esc(s) -> str:
    return html.escape(str(s if s is not None else ""), quote=True)


def tooling_banner_html(missing: list, configure_url: str) -> str:
    """Render the site-wide "the selected container is missing this dependency"
    banner from a list of {tool,name,kind,detail} entries.

    A COMMON METAPHOR meant for every action's report: the runner writes
    <results>/_tooling.json with a `missing` list when a configured tool could not
    run for lack of container tooling, and the report renders this banner (in the
    body, so it also shows inside the dashboard's iframe where the shared site
    header self-suppresses) linking to the Configure Workers dialog.
    """
    if isinstance(missing, dict):
        missing = [missing]
    miss = [x for x in (missing or []) if isinstance(x, dict) and (x.get("kind") in (None, "", "missing-tooling"))]
    if not miss:
        return ""
    names = ", ".join(_esc(x.get("name") or x.get("tool") or "") for x in miss if (x.get("name") or x.get("tool")))
    detail = next((x.get("detail") for x in miss if x.get("detail")), "")
    detail_html = f'<div class="lvci-needtool-d">{_esc(detail)}</div>' if detail else ""
    cta = (f'<a class="lvci-needtool-cta" href="{_esc(configure_url)}" target="_top" rel="noopener">'
           'Set up the container</a>') if configure_url else ""
    return (
        '<div class="lvci-needtool" role="alert">'
        '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" '
        'stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">'
        '<path d="M10.29 3.86 1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z"/>'
        '<line x1="12" y1="9" x2="12" y2="13"/><line x1="12" y1="17" x2="12.01" y2="17"/></svg>'
        '<div class="lvci-needtool-t"><strong>This activity could not run.</strong>'
        'The selected container did not contain the dependencies needed to run this activity'
        f'{(" (" + names + ")") if names else ""}. '
        'Set up the container to add the required tooling, then re-run.'
        f'{detail_html}</div>'
        f'{cta}</div>'
    )


def render(data: dict) -> str:
    blob = json.dumps(data, ensure_ascii=False)
    blob = blob.replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")
    m = data.get("meta", {}) or {}
    pages = (m.get("pages_url") or "").rstrip("/")
    is_linux = m.get("platform") == "linux"
    hdr_src = "../../../lvci-header.js" if is_linux else "../../lvci-header.js"
    hdr_cfg = {
        "context": "unit-tests-report",
        "repo": m.get("repo", ""),
        "pagesUrl": pages or ("../../.." if is_linux else "../.."),
        "sha": m.get("sha", ""),
        "short": m.get("short", ""),
        "platform": m.get("platform", "windows"),
    }
    dash = m.get("dash_url") or ""
    repo = m.get("repo") or ""
    cfg_url = (dash or "") + "configure.html" + ("?repo=" + quote(repo, safe="") if repo else "")
    banner = tooling_banner_html((data.get("tooling") or {}).get("missing") or [], cfg_url)

    out = _TEMPLATE.replace("__UT_DATA_JSON__", blob)
    out = out.replace("__UT_HEADER_CFG__", json.dumps(hdr_cfg, ensure_ascii=False))
    out = out.replace("__LVCI_HEADER_SRC__", hdr_src)
    out = out.replace("__UT_TOOLING_BANNER__", banner)
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description="Build a unified unit-test report from JUnit XML.")
    ap.add_argument("--results", default="", help="Directory of JUnit *.xml files (tool inferred from name)")
    ap.add_argument("--junit", action="append", default=[], help="Explicit TOOL:PATH (repeatable)")
    ap.add_argument("--out", required=True, help="Output directory")
    ap.add_argument("--workspace", default="", help="Repo checkout (resolve VI paths)")
    ap.add_argument("--platform", default="windows", choices=["windows", "linux"])
    ap.add_argument("--meta", default="", help="meta.json with duration/labview_version")
    ap.add_argument("--sha", default="")
    ap.add_argument("--repo", default="")
    ap.add_argument("--pages-url", dest="pages_url", default="")
    ap.add_argument("--commit-msg", dest="commit_msg", default="")
    ap.add_argument("--author", default="")
    ap.add_argument("--date", default="")
    ap.add_argument("--labview-version", dest="labview_version", default="")
    args = ap.parse_args()

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    data = build_data(args)
    (out_dir / "results.json").write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    (out_dir / "index.html").write_text(render(data), encoding="utf-8")
    s = data["summary"]
    print(f"unit-test report: {s['passed']}/{s['tests']} passed ({s['percent']}%) "
          f"- {s['failed']} failed - {s['errored']} errored - {s['skipped']} skipped -> {out_dir/'index.html'}")


# ── HTML template (self-contained; chrome via lvci-header.js) ─────────────────
_TEMPLATE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Unit Tests — LabVIEW CI</title>
<script>window.LVCI = __UT_HEADER_CFG__;</script>
<script src="__LVCI_HEADER_SRC__" defer></script>
<style>
:root{--bg:#0d1117;--surface:#161b22;--surface2:#0d1117;--border:#30363d;--fg:#e6edf3;--muted:#8b949e;--link:#58a6ff;--code:#010409}
@media(prefers-color-scheme:light){:root{--bg:#fff;--surface:#f6f8fa;--surface2:#fff;--border:#d0d7de;--fg:#1f2328;--muted:#57606a;--link:#0969da;--code:#f6f8fa}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;line-height:1.5}
.wrap{max-width:1040px;margin:0 auto;padding:20px 18px 64px}
h1{font-size:1.45em;margin:0 0 2px}
.sub{color:var(--muted);font-size:.86em;margin:2px 0 16px}
.badge{display:inline-block;font-size:.72em;font-weight:700;color:#fff;border-radius:999px;padding:3px 10px;vertical-align:middle;margin-left:8px}
.toolchips{display:inline-flex;gap:6px;margin-left:8px;vertical-align:middle}
.toolchip{font-size:.66em;font-weight:600;color:var(--muted);border:1px solid var(--border);border-radius:999px;padding:2px 8px;text-transform:uppercase;letter-spacing:.03em}
.cards{display:flex;gap:10px;flex-wrap:wrap;margin:14px 0}
.card{flex:1 1 120px;min-width:110px;background:var(--surface);border:1px solid var(--border);border-radius:10px;padding:12px 14px}
.card .n{font-size:1.6em;font-weight:700;line-height:1}
.card .l{color:var(--muted);font-size:.78em;margin-top:3px}
.card.fail .n{color:#da3633}.card.err .n{color:#bb8009}.card.pass .n{color:#2ea043}.card.skip .n{color:#6e7681}
.bar{height:8px;background:var(--border);border-radius:999px;overflow:hidden;margin:6px 0 4px}
.barfill{height:100%;background:#2ea043;width:0%}
.barlabel{color:var(--muted);font-size:.78em;margin-bottom:14px}
.toolbar{display:flex;gap:10px;flex-wrap:wrap;align-items:center;margin:6px 0 14px}
.toolbar input[type=search]{flex:1 1 220px;min-width:180px;padding:8px 10px;background:var(--surface2);color:var(--fg);border:1px solid var(--border);border-radius:7px;font-size:.9em}
.chips{display:flex;gap:6px;flex-wrap:wrap}
.chip{display:inline-flex;align-items:center;gap:6px;font-size:.8em;border:1px solid var(--border);border-radius:999px;padding:4px 10px;cursor:pointer;user-select:none}
.chip.off{opacity:.45}.chip .dot{width:8px;height:8px;border-radius:50%}
.count{color:var(--muted);font-size:.8em;margin-left:auto}
.plat{display:inline-flex;border:1px solid var(--border);border-radius:7px;overflow:hidden;margin-left:8px;vertical-align:middle}
.plat button{background:transparent;color:var(--muted);border:0;padding:4px 11px;font-size:.8em;cursor:pointer}
.plat button.active{background:rgba(177,186,196,.16);color:var(--fg)}
.plat button[disabled]{opacity:.4;cursor:default}
details.suite{border:1px solid var(--border);border-radius:10px;margin-bottom:10px;background:var(--surface);overflow:hidden}
details.suite>summary{list-style:none;cursor:pointer;padding:11px 14px;display:flex;align-items:center;gap:10px}
details.suite>summary::-webkit-details-marker{display:none}
.tw{transition:transform .15s;color:var(--muted);font-size:.8em}
details[open]>summary .tw{transform:rotate(90deg)}
.sname{font-weight:600}.smeta{color:var(--muted);font-size:.8em;margin-left:auto;display:flex;gap:8px;align-items:center}
.spill{font-size:.72em;border:1px solid var(--border);border-radius:999px;padding:1px 8px}
.sbody{border-top:1px solid var(--border)}
.case{border-bottom:1px solid var(--border)}
.case:last-child{border-bottom:0}
.case>.ch{display:flex;align-items:center;gap:10px;padding:9px 14px;cursor:default}
.case.openable>.ch{cursor:pointer}
.statusdot{width:9px;height:9px;border-radius:50%;flex:0 0 auto}
.cname{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:.84em}
.ctime{color:var(--muted);font-size:.74em}
.cact{margin-left:auto;display:flex;gap:6px;align-items:center;flex-wrap:wrap}
.snapbtn{font-size:.72em;border:1px solid var(--border);border-radius:6px;background:var(--surface2);color:var(--fg);padding:3px 8px;cursor:pointer;white-space:nowrap}
.snapbtn.target{border-color:var(--link);color:var(--link)}
.cbody{padding:0 14px 12px 33px;display:none}
.case.open .cbody{display:block}
.msg{color:#f0a3a3;font-size:.85em;margin:2px 0 6px;white-space:pre-wrap;word-break:break-word}
.details{background:var(--code);border:1px solid var(--border);border-radius:8px;padding:9px 11px;font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:.78em;white-space:pre-wrap;word-break:break-word;max-height:280px;overflow:auto}
.empty{color:var(--muted);text-align:center;padding:40px 0}
.hidden{display:none!important}
/* shared "container is missing this tooling" banner */
.lvci-needtool{display:flex;align-items:flex-start;gap:12px;max-width:1040px;margin:16px auto 0;padding:13px 16px;background:rgba(187,128,9,.13);border:1px solid rgba(187,128,9,.45);border-left:4px solid #bb8009;border-radius:10px}
.lvci-needtool svg{flex:0 0 auto;width:20px;height:20px;color:#bb8009;margin-top:1px}
.lvci-needtool-t{flex:1 1 auto;font-size:.9em}
.lvci-needtool-t strong{display:block;margin-bottom:2px}
.lvci-needtool-d{color:var(--muted);font-size:.92em;margin-top:5px}
.lvci-needtool-cta{flex:0 0 auto;align-self:center;font-size:.85em;font-weight:600;color:#fff;background:#bb8009;border-radius:7px;padding:8px 13px;text-decoration:none;white-space:nowrap}
.lvci-needtool-cta:hover{background:#a17609}
@media(max-width:560px){.lvci-needtool{flex-wrap:wrap}.lvci-needtool-cta{align-self:stretch;text-align:center}}
/* snapshot drawer */
#backdrop{position:fixed;inset:0;background:rgba(1,4,9,.5);opacity:0;pointer-events:none;transition:opacity .15s;z-index:40}
#backdrop.show{opacity:1;pointer-events:auto}
#snap{position:fixed;top:0;right:0;height:100%;width:min(760px,92vw);background:var(--surface);border-left:1px solid var(--border);transform:translateX(100%);transition:transform .18s;z-index:50;display:flex;flex-direction:column}
#snap.show{transform:none}
#snaphead{display:flex;align-items:center;gap:12px;padding:11px 14px;border-bottom:1px solid var(--border)}
#snaphead .t{font-weight:600;font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:.84em;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
#snapopen{margin-left:auto;font-size:.8em;color:var(--link);text-decoration:none}
#snapclose{background:transparent;border:0;color:var(--muted);font-size:1.4em;cursor:pointer;line-height:1}
#snapframe{flex:1 1 auto;width:100%;border:0;background:var(--bg)}
#snapnote{padding:14px;color:var(--muted);font-size:.85em}
</style>
</head>
<body>
__UT_TOOLING_BANNER__
<div class="wrap">
  <h1>Unit Tests <span class="badge" id="statusbadge" style="display:none"></span>
    <span class="plat" id="plat-toggle"></span>
    <span class="toolchips" id="toolchips"></span></h1>
  <div class="sub" id="sub"></div>
  <div class="platnote" id="platnote" style="color:var(--muted);font-size:.82em;margin:-8px 0 10px"></div>

  <div class="cards" id="cards"></div>
  <div class="bar"><div class="barfill" id="barfill"></div></div>
  <div class="barlabel" id="barlabel"></div>

  <div class="toolbar">
    <input id="q" type="search" placeholder="Filter by test, suite, or message…">
    <div class="chips" id="chips"></div>
    <span class="count" id="rescount"></span>
  </div>

  <div id="view"></div>
</div>

<div id="backdrop"></div>
<aside id="snap">
  <div id="snaphead">
    <span class="t" id="snaptitle">VI</span>
    <a id="snapopen" href="#" target="_top">Open in VI Browser ↗</a>
    <button id="snapclose" title="Close">×</button>
  </div>
  <iframe id="snapframe" title="VI snapshot"></iframe>
  <div id="snapnote"></div>
</aside>

<script id="ut-data" type="application/json">__UT_DATA_JSON__</script>
<script>
const SELF = JSON.parse(document.getElementById('ut-data').textContent);
const PLATFORMS = SELF.platforms || [{id:SELF.meta.platform,url:null}];
const SELF_PLATFORM = SELF.meta.platform;
const CACHE = { [SELF_PLATFORM]: SELF };
let CUR = SELF_PLATFORM, D = SELF;
const META = SELF.meta;
const SNAP_BASE = META.snap_base;
const SMETA = SELF.status_meta, SORDER = SELF.status_order;
const TOOL_LABEL = {}; (SELF.meta.tools||[]).forEach(t=>TOOL_LABEL[t.id]=t.label);
const esc = s => String(s==null?'':s).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const PLAT_LABEL = {windows:'Windows', linux:'Linux'};
const STATUS_BADGE = {passed:'#2ea043', failed:'#da3633', empty:'#6e7681'};
let active = new Set(SORDER), query='';

async function fetchJson(u){ const r=await fetch(u,{cache:'no-store'}); if(!r.ok) throw new Error(r.status+' '+u); return r.json(); }

(function header(){
  const m = META;
  const commitMsg = (m.commit && m.commit.message) ? esc(m.commit.message) : '';
  const shaLink = m.repo && m.sha ? `https://github.com/${m.repo}/commit/${m.sha}` : '';
  document.getElementById('sub').innerHTML =
    (m.short ? `Commit ${shaLink?`<a href="${shaLink}" target="_top">${esc(m.short)}</a>`:esc(m.short)} ` : '') +
    (commitMsg ? `&middot; ${commitMsg} ` : '') +
    (m.labview_version ? `&middot; LabVIEW ${esc(m.labview_version)} ` : '') +
    `&middot; generated ${esc(m.generated_utc||'')}`;
  const tc = document.getElementById('toolchips');
  tc.innerHTML = (m.tools||[]).map(t=>`<span class="toolchip">${esc(t.label)}</span>`).join('');
})();

function renderToggle(){
  const host = document.getElementById('plat-toggle');
  host.innerHTML = PLATFORMS.map(p=>{
    const dis = (p.id!==SELF_PLATFORM && p.url==null) ? 'disabled' : '';
    return `<button data-plat="${p.id}" class="${p.id===CUR?'active':''}" ${dis}>${esc(PLAT_LABEL[p.id]||p.id)}</button>`;
  }).join('');
  host.querySelectorAll('button').forEach(b=>b.addEventListener('click',()=>switchPlatform(b.dataset.plat)));
}
async function switchPlatform(pid){
  if(pid===CUR) return;
  const p = PLATFORMS.find(x=>x.id===pid); if(!p) return;
  let data = CACHE[pid];
  if(data===undefined){
    document.getElementById('platnote').textContent = `Loading ${PLAT_LABEL[pid]||pid}…`;
    try{ data = await fetchJson(p.url); }catch(e){ data = null; }
    CACHE[pid] = data;
  }
  CUR = pid;
  if(!data){ renderToggle(); document.getElementById('platnote').textContent = `Unit tests have not run on ${PLAT_LABEL[pid]||pid} for this commit.`; showEmpty(pid); return; }
  D = data; active = new Set(SORDER); renderToggle(); renderAll();
}
function showEmpty(pid){
  document.getElementById('cards').innerHTML='';
  document.getElementById('barfill').style.width='0%';
  document.getElementById('barlabel').textContent='';
  document.getElementById('statusbadge').style.display='none';
  document.getElementById('chips').innerHTML='';
  document.getElementById('view').innerHTML=`<div class="empty">No ${esc(PLAT_LABEL[pid]||pid)} unit-test report for this commit yet.</div>`;
  document.getElementById('rescount').textContent='';
}

function renderAll(){
  const s = D.summary;
  document.getElementById('platnote').textContent = s.duration ? `Ran in ${s.duration}s on ${PLAT_LABEL[CUR]||CUR}.` : '';
  const sb = document.getElementById('statusbadge');
  sb.style.display='inline-block';
  sb.textContent = s.status==='passed'?'all passing':(s.status==='empty'?'no tests':`${s.percent}% passing`);
  sb.style.background = STATUS_BADGE[s.status] || '#6e7681';

  const cards=[
    {n:s.tests,l:'Tests'},
    {n:s.passed,l:'Passed',c:'pass'},
    {n:s.failed,l:'Failed',c:s.failed?'fail':''},
    {n:s.errored,l:'Errored',c:s.errored?'err':''},
    {n:s.skipped,l:'Skipped',c:s.skipped?'skip':''},
  ];
  document.getElementById('cards').innerHTML = cards.map(c=>`<div class="card ${c.c||''}"><div class="n">${(c.n||0).toLocaleString()}</div><div class="l">${esc(c.l)}</div></div>`).join('');
  document.getElementById('barfill').style.width=(s.percent||0)+'%';
  document.getElementById('barfill').style.background = (s.failed||s.errored)?'#bb8009':'#2ea043';
  document.getElementById('barlabel').textContent = `${s.percent||0}% of ${(s.tests||0).toLocaleString()} tests passed`;

  const totals={}; for(const k of SORDER) totals[k]=0;
  for(const su of D.suites) for(const c of su.cases) totals[c.status]=(totals[c.status]||0)+1;
  document.getElementById('chips').innerHTML = SORDER.map(k=>{
    const meta=SMETA[k], on=active.has(k);
    return `<span class="chip ${on?'on':'off'}" data-st="${k}" title="${esc(meta.blurb)}"><span class="dot" style="background:${meta.color}"></span>${esc(meta.label)} <b>${totals[k]||0}</b></span>`;
  }).join('');
  document.querySelectorAll('#chips .chip').forEach(ch=>ch.addEventListener('click',()=>{
    const k=ch.dataset.st; if(active.has(k))active.delete(k); else active.add(k);
    ch.classList.toggle('on'); ch.classList.toggle('off'); apply();
  }));
  renderSuites(); apply();
}

function caseRow(c){
  const openable = (c.status==='failed'||c.status==='error') && (c.message||c.details);
  const snapTest = c.test_vi_rel ? `<button class="snapbtn" data-snap="${esc(c.test_vi_rel)}" data-name="${esc(c.test_vi_name)}">Test VI</button>` : '';
  const snapTarget = c.target_vi_rel ? `<button class="snapbtn target" data-snap="${esc(c.target_vi_rel)}" data-name="${esc(c.target_vi_name)}" title="VI under test (derived)">VI under test</button>` : '';
  const hay = (c.name+' '+c.classname+' '+(c.message||'')+' '+(c.test_vi_name||'')).toLowerCase();
  return `<div class="case ${openable?'openable':''}" data-st="${c.status}" data-text="${esc(hay)}">
    <div class="ch">
      <span class="statusdot" style="background:${SMETA[c.status].color}"></span>
      <span class="cname">${esc(c.name)}</span>
      ${c.time?`<span class="ctime">${c.time}s</span>`:''}
      <span class="cact">${snapTarget}${snapTest}</span>
    </div>
    <div class="cbody">
      ${c.message?`<div class="msg">${esc(c.message)}</div>`:''}
      ${c.details?`<div class="details">${esc(c.details)}</div>`:''}
    </div>
  </div>`;
}
function renderSuites(){
  const host=document.getElementById('view');
  if(!D.suites.length){ host.innerHTML=`<div class="empty">No unit tests found for this commit.</div>`; return; }
  host.innerHTML = D.suites.map(su=>{
    const fails=su.cases.filter(c=>c.status==='failed'||c.status==='error').length;
    const open = fails>0 ? 'open':'';
    return `<details class="suite" ${open} data-suite="${esc((su.name+' '+su.tool).toLowerCase())}">
      <summary>
        <span class="tw">▶</span>
        <span class="sname">${esc(su.name)}</span>
        <span class="smeta">
          <span class="toolchip">${esc(TOOL_LABEL[su.tool]||su.tool)}</span>
          <span class="spill">${su.cases.length} test${su.cases.length===1?'':'s'}</span>
          ${fails?`<span class="spill" style="border-color:#da3633;color:#da3633">${fails} failing</span>`:''}
        </span>
      </summary>
      <div class="sbody">${su.cases.map(caseRow).join('')}</div>
    </details>`;
  }).join('');
  // expand/collapse a failing case to show its message/details
  host.querySelectorAll('.case.openable>.ch').forEach(ch=>ch.addEventListener('click',e=>{
    if(e.target.closest('.snapbtn')) return;
    ch.parentElement.classList.toggle('open');
  }));
  host.querySelectorAll('.snapbtn').forEach(b=>b.addEventListener('click',e=>{
    e.stopPropagation(); openSnap(b.dataset.snap, b.dataset.name);
  }));
}

function apply(){
  document.querySelectorAll('#view .suite').forEach(su=>{
    let vis=0;
    su.querySelectorAll('.case').forEach(c=>{
      const show = active.has(c.dataset.st) && (!query || c.dataset.text.includes(query) || su.dataset.suite.includes(query));
      c.classList.toggle('hidden', !show); if(show) vis++;
    });
    su.classList.toggle('hidden', vis===0);
  });
  const n=[...document.querySelectorAll('#view .case')].filter(c=>!c.classList.contains('hidden')).length;
  document.getElementById('rescount').textContent = `${n} test${n===1?'':'s'} shown`;
}
document.getElementById('q').addEventListener('input',e=>{ query=e.target.value.trim().toLowerCase(); apply(); });

// ── snapshot drawer (resolves vi_rel → by-blob HTML via manifest.json) ────
let snapMap=null, snapFromSha=null;
async function ensureSnapshots(){
  if(snapMap) return; snapMap={};
  let m = META.sha ? await fetchJson(SNAP_BASE+META.sha+'/manifest.json').catch(()=>null) : null;
  if(m) snapFromSha=META.sha;
  if(!m){
    const commits = await fetchJson(SNAP_BASE+'commits.json').catch(()=>[]);
    if(commits && commits.length){ m=await fetchJson(SNAP_BASE+commits[0].sha+'/manifest.json').catch(()=>null); if(m) snapFromSha=commits[0].sha; }
  }
  if(m) for(const v of m) snapMap[v.vi_rel]=v.html;
}
async function openSnap(viRel,name){
  const back=document.getElementById('backdrop'), snap=document.getElementById('snap');
  const frame=document.getElementById('snapframe'), note=document.getElementById('snapnote');
  document.getElementById('snaptitle').textContent = name||viRel;
  document.getElementById('snapopen').href = SNAP_BASE+'index.html'+(META.sha?`?sha=${META.sha}`:'');
  back.classList.add('show'); snap.classList.add('show');
  frame.style.display='none'; note.style.display='block'; note.textContent='Resolving snapshot…';
  try{ await ensureSnapshots(); }catch(e){}
  const html = snapMap[viRel];
  if(html){
    frame.src = SNAP_BASE+html; frame.style.display='block'; note.style.display='none';
    if(snapFromSha && META.sha && snapFromSha!==META.sha){ note.style.display='block'; note.innerHTML=`<em>Showing the latest available snapshot (commit ${esc(snapFromSha.slice(0,7))}); this commit's snapshots may still be rendering.</em>`; }
  }else{
    frame.style.display='none'; note.style.display='block';
    note.innerHTML = `No snapshot found for <code>${esc(viRel)}</code>.<br>It may still be rendering, or this VI isn't in the snapshot gallery yet. Try the <a href="${SNAP_BASE}index.html${META.sha?`?sha=${META.sha}`:''}" target="_blank">VI Browser</a>.`;
  }
}
function closeSnap(){ document.getElementById('backdrop').classList.remove('show'); document.getElementById('snap').classList.remove('show'); document.getElementById('snapframe').src='about:blank'; }
document.getElementById('snapclose').addEventListener('click',closeSnap);
document.getElementById('backdrop').addEventListener('click',closeSnap);
document.addEventListener('keydown',e=>{ if(e.key==='Escape') closeSnap(); });

renderToggle();
if(D.suites.length || D.summary.tests) renderAll(); else showEmpty(SELF_PLATFORM);
</script>
</body>
</html>
"""


if __name__ == "__main__":
    main()
