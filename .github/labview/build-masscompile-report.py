#!/usr/bin/env python3
"""
build-masscompile-report.py — Turn the raw LabVIEW Mass Compile log into a
friendly, navigable report that groups problems by VI (like the VI Analyzer
report), lets you open each VI's rendered snapshot, toggles between the Windows
and Linux container results, and links back to the CI dashboard.

The Mass Compile log (emitted by `LabVIEWCLI -OperationName MassCompile`) is a
flat, hard-wrapped, often UTF-16 text dump. LabVIEW flags VIs it could not load
with `### Bad VI:` / `Path="…"`, and reports each unresolved subVI with
`Search failed to find "…" … Caller: "…"`. This script parses those, joins them
to the project's VIs, and writes:

    <out-dir>/index.html      — friendly report (the deployed page)
    <out-dir>/problems.json   — machine-readable data (also fetched by the OTHER
                                platform's report so the toggle can switch sides)
    <out-dir>/summary.json    — counts {total, ok, bad, percent, status, …}
                                (schema the dashboard already reads)

Run on the RUNNER (not in the container), after the compile, so it can both read
the log the container produced and enumerate the bind-mounted workspace to map
caller names → repo-relative paths (for snapshots) and count project VIs.

Usage:
    python3 build-masscompile-report.py \
        --log        ci-out/masscompile/masscompile.log \
        --workspace  "$GITHUB_WORKSPACE" \
        --out        ci-out/masscompile \
        --platform   windows|linux \
        [--meta      ci-out/masscompile/compile-meta.json] \
        [--sha SHA] [--repo owner/name] [--pages-url https://owner.github.io/repo] \
        [--commit-msg "…"] [--author "…"] [--date 2026-…Z] [--labview-version 2026]
"""

from __future__ import annotations

import argparse
import html
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path


# ── Problem taxonomy ─────────────────────────────────────────────────────────
# Two kinds of mass-compile problem, colour-coded like the analyzer report:
#   broken     — LabVIEW flagged the VI as bad (could not load / compile)
#   dependency — a subVI the VI calls could not be found in this environment
CATEGORY_ORDER = ["broken", "dependency"]
CATEGORY_META = {
    "broken":     {"label": "Failed to compile", "color": "#da3633",
                   "blurb": "LabVIEW flagged the VI as bad — it could not load or compile here"},
    "dependency": {"label": "Missing dependency", "color": "#bb8009",
                   "blurb": "A subVI / dependency could not be found in this container"},
}


# ── Text / path helpers ──────────────────────────────────────────────────────
def clean(text: str) -> str:
    """Collapse whitespace (incl. the log's hard newlines) to single spaces."""
    return re.sub(r"\s+", " ", text or "").strip()


_WORKSPACE_PREFIXES = ("c:\\workspace\\", "c:/workspace/", "/workspace/")


def to_vi_rel(path: str) -> str:
    """Map a container path (`C:\\workspace\\a\\b.vi` or `/workspace/a/b.vi`) to
    the gallery's `vi_rel` key (`a/b.vi`)."""
    p = clean(path).strip('"')
    low = p.lower().replace("/", "\\") if p.lower().startswith("c:") else p.lower()
    for pref in _WORKSPACE_PREFIXES:
        pl = pref.lower()
        if low.startswith(pl):
            p = p[len(pref):]
            break
    return p.replace("\\", "/").strip("/")


_TOOLING_VI_RE = re.compile(r"^(\.github|ci-out|build)/", re.I)


def is_tooling_vi(vi_rel: str) -> bool:
    return bool(_TOOLING_VI_RE.match(vi_rel or ""))


def group_for(vi_rel: str) -> str:
    """Top-level folder, mirroring the VI Browser tree. Root VIs → 'Project'."""
    parts = (vi_rel or "").split("/")
    return parts[0] if len(parts) > 1 and parts[0] else "Project"


def base_name(name_or_path: str) -> str:
    return re.split(r"[\\/]", clean(name_or_path).strip('"'))[-1]


# ── Log reading (handles UTF-16 BOM from Windows Tee-Object, plain UTF-8) ─────
def read_log(path: Path) -> str:
    raw = path.read_bytes()
    if raw[:2] == b"\xff\xfe":
        return raw.decode("utf-16-le", "replace")
    if raw[:2] == b"\xfe\xff":
        return raw.decode("utf-16-be", "replace")
    if raw[:3] == b"\xef\xbb\xbf":
        return raw.decode("utf-8-sig", "replace")
    # Heuristic: lots of NULs ⇒ UTF-16 without BOM.
    if raw[:4000].count(b"\x00") > 200:
        return raw.decode("utf-16-le", "replace")
    return raw.decode("utf-8", "replace")


# ── Workspace enumeration (denominator + caller-name → vi_rel resolution) ─────
def index_workspace(workspace: Path) -> tuple[int, dict[str, list[str]]]:
    """Return (project_vi_count, basename_lower → [vi_rel, …]). Excludes the CI
    tooling under .github/ci-out/build so it matches the snapshot gallery and the
    existing percentage denominator."""
    total = 0
    by_base: dict[str, list[str]] = {}
    if not workspace or not workspace.is_dir():
        return 0, by_base
    ws = workspace.resolve()
    for root, dirs, files in os.walk(ws):
        # Prune tooling dirs early.
        rel_root = os.path.relpath(root, ws).replace("\\", "/")
        if rel_root != "." and is_tooling_vi(rel_root + "/"):
            dirs[:] = []
            continue
        for f in files:
            if not f.lower().endswith(".vi"):
                continue
            vi_rel = (("" if rel_root == "." else rel_root + "/") + f).replace("\\", "/")
            if is_tooling_vi(vi_rel):
                continue
            total += 1
            by_base.setdefault(f.lower(), []).append(vi_rel)
    return total, by_base


# ── Log parsing ──────────────────────────────────────────────────────────────
# "### Bad VI:" (optionally "Bad VI/subVI") then a quoted qualified name, then a
# Path="…" that may sit on the next hard-wrapped line. The path value contains no
# double-quote, so [^"]+ safely spans the wrap.
_BAD_VI_RE = re.compile(
    r'###\s*Bad VI(?:/subVI)?\s*:\s*"(?P<qual>[^"]*)"\s*Path="(?P<path>[^"]+)"',
    re.S | re.I,
)
# A loose fallback: any Path="…vi" flagged in the log (matches the existing
# summary's bad-count logic) in case a header/path pairing is reworded.
_PATH_RE = re.compile(r'Path="(?P<path>[^"]+\.vi)"', re.I)

# Unresolved subVI: dependency, where it was expected, and the calling VI. The
# separator between the "previously from" location and "Caller:" varies (+++ / +=+),
# so we just scan ahead non-greedily to Caller.
_SEARCH_RE = re.compile(
    r'Search failed to find\s*"(?P<dep>[^"]+)"\s*previously from\s*"(?P<from>[^"]+)".*?Caller:\s*"(?P<caller>[^"]+)"',
    re.S | re.I,
)


def parse_log(log: str, by_base: dict[str, list[str]]):
    """Parse the log into a dict keyed by VI (vi_rel when resolvable, else name)
    whose value is {name, vi_rel, problems:[…]}. Also return the set of distinct
    bad-VI vi_rels (the authoritative bad count for the percentage)."""
    vis: dict[str, dict] = {}

    def touch(key: str, name: str, vi_rel: str) -> dict:
        v = vis.get(key)
        if not v:
            v = {"name": name, "vi_rel": vi_rel, "problems": []}
            vis[key] = v
        # Prefer a resolved path/name if we learn one later.
        if vi_rel and not v["vi_rel"]:
            v["vi_rel"] = vi_rel
        if name and (not v["name"] or len(name) > len(v["name"])):
            v["name"] = name
        return v

    bad_rels: set[str] = set()

    # 1) Bad VIs (header + Path).
    paired_paths: set[str] = set()
    for m in _BAD_VI_RE.finditer(log):
        path = clean(m.group("path"))
        vi_rel = to_vi_rel(path)
        if not vi_rel.lower().endswith(".vi") or is_tooling_vi(vi_rel):
            continue
        paired_paths.add(path.lower())
        bad_rels.add(vi_rel.lower())
        name = base_name(vi_rel) or base_name(m.group("qual"))
        v = touch(vi_rel.lower(), name, vi_rel)
        v["problems"].append({
            "type": "Bad VI",
            "severity": "broken",
            "message": "LabVIEW flagged this VI as bad — it failed to load or compile in this container.",
        })

    # 1b) Any other Path="…vi" the log flags (keeps the bad count in lock-step
    # with the previous summary even if a header is reworded), as broken VIs.
    for m in _PATH_RE.finditer(log):
        path = clean(m.group("path"))
        if path.lower() in paired_paths:
            continue
        vi_rel = to_vi_rel(path)
        if not vi_rel.lower().endswith(".vi") or is_tooling_vi(vi_rel):
            continue
        # Only treat as bad if the line context actually says "Bad VI"; otherwise
        # a Path="…" can appear in benign lines. Cheap guard: look back a little.
        start = max(0, m.start() - 80)
        if "bad vi" not in log[start:m.start()].lower():
            continue
        bad_rels.add(vi_rel.lower())
        v = touch(vi_rel.lower(), base_name(vi_rel), vi_rel)
        if not any(p["severity"] == "broken" for p in v["problems"]):
            v["problems"].append({
                "type": "Bad VI",
                "severity": "broken",
                "message": "LabVIEW flagged this VI as bad — it failed to load or compile in this container.",
            })

    # 2) Missing dependencies, attributed to the calling VI.
    for m in _SEARCH_RE.finditer(log):
        dep = clean(m.group("dep"))
        frm = clean(m.group("from"))
        caller = clean(m.group("caller"))
        if not dep:
            continue
        caller_base = base_name(caller).lower()
        # Resolve the caller name → a unique repo-relative path (for snapshots).
        matches = by_base.get(caller_base, [])
        if len(matches) == 1:
            vi_rel = matches[0]
            key = vi_rel.lower()
            name = base_name(vi_rel)
        else:
            vi_rel = ""  # ambiguous or unknown → name-only card (snapshot best-effort)
            key = "name:" + caller_base
            name = base_name(caller)
        if is_tooling_vi(vi_rel):
            continue
        v = touch(key, name, vi_rel)
        msg = f'Could not find subVI "{dep}"'
        if frm:
            msg += f' (last known at {frm})'
        msg += "."
        # De-dupe identical dependency messages on the same VI.
        if not any(p["severity"] == "dependency" and p["message"] == msg for p in v["problems"]):
            v["problems"].append({"type": "Missing dependency", "severity": "dependency", "message": msg})

    return vis, bad_rels


def finalize_vis(vis: dict[str, dict]) -> list[dict]:
    out = []
    for v in vis.values():
        if not v["problems"]:
            continue
        sev_counts = {c: 0 for c in CATEGORY_ORDER}
        for p in v["problems"]:
            sev_counts[p["severity"]] = sev_counts.get(p["severity"], 0) + 1
        vi_rel = v["vi_rel"] or ""
        out.append({
            "name": v["name"] or (base_name(vi_rel) if vi_rel else "(unknown VI)"),
            "vi_rel": vi_rel,
            "group": group_for(vi_rel) if vi_rel else "Unresolved",
            "problems": sorted(v["problems"], key=lambda p: (CATEGORY_ORDER.index(p["severity"]), p["message"])),
            "total": len(v["problems"]),
            "sev_counts": sev_counts,
            "resolved": bool(vi_rel),
        })
    # Broken first, then by problem count, then name.
    out.sort(key=lambda v: (0 if v["sev_counts"]["broken"] else 1, -v["total"], v["vi_rel"].lower() or v["name"].lower()))
    return out


# ── Status (mirrors masscompile.ps1 so the percentage/colour stay consistent) ─
def classify(exit_code: int, total: int, bad: int):
    real_error = exit_code is not None and exit_code != 0 and exit_code != 3
    ok = max(0, total - bad)
    percent = round(ok / total * 100) if total > 0 else 0
    if real_error or total <= 0:
        return "failed", 0, 0
    if exit_code == 0 and bad == 0:
        return "passed", ok, percent
    if ok <= 0:
        return "failed", 0, 0
    return "partial", ok, percent


def build_data(args: argparse.Namespace) -> dict:
    log = read_log(Path(args.log)) if Path(args.log).exists() else "(no log captured)"
    total, by_base = index_workspace(Path(args.workspace)) if args.workspace else (0, {})

    parsed, bad_rels = parse_log(log, by_base)
    vis = finalize_vis(parsed)
    bad = len(bad_rels)

    meta_extra = {}
    if args.meta and Path(args.meta).exists():
        try:
            meta_extra = json.loads(Path(args.meta).read_text(encoding="utf-8-sig"))
        except Exception:
            meta_extra = {}
    exit_code = meta_extra.get("exit", args.exit)
    duration = meta_extra.get("duration", args.duration)

    status, ok, percent = classify(exit_code, total, bad)
    missing_deps = sum(1 for v in vis for p in v["problems"] if p["severity"] == "dependency")

    summary = {
        "total": total, "ok": ok, "bad": bad, "percent": percent,
        "status": status, "exit": exit_code, "duration": duration,
        "missing_deps": missing_deps, "problem_vis": len(vis),
    }

    # Relative URL to the OTHER platform's problems.json. Windows report lives at
    # masscompile/<sha>/ ; Linux at masscompile/<sha>/linux/. So from Windows the
    # Linux data is at "linux/problems.json"; from Linux the Windows data is at
    # "../problems.json".
    if args.platform == "windows":
        platforms = [{"id": "windows", "url": None}, {"id": "linux", "url": "linux/problems.json"}]
        snap_depth = "../../"   # masscompile/<sha>/ → repo root
    else:
        platforms = [{"id": "windows", "url": "../problems.json"}, {"id": "linux", "url": None}]
        snap_depth = "../../../"  # masscompile/<sha>/linux/ → repo root

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
            "commit": {"message": args.commit_msg, "author": args.author, "date": args.date},
            "generated_utc": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        },
        "summary": summary,
        "categories": CATEGORY_META,
        "category_order": CATEGORY_ORDER,
        "platforms": platforms,
        "vis": vis,
    }


# ── Renderer ─────────────────────────────────────────────────────────────────
def render(data: dict) -> str:
    blob = json.dumps(data, ensure_ascii=False)
    blob = blob.replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")
    # The shared site header (lvci-header.js, deployed once at the Pages root) reads
    # this config to render consistent nav + this report's context actions
    # (Regenerate report, Raw log, This commit) and the revision picker. Built from
    # meta so a rebuild from problems.json reproduces the header with no extra args.
    m = data.get("meta", {}) or {}
    pages = (m.get("pages_url") or "").rstrip("/")
    # Linux reports live one level deeper (masscompile/<sha>/linux/), Windows at
    # masscompile/<sha>/ — so the shared asset and the pagesUrl fallback differ.
    is_linux = m.get("platform") == "linux"
    hdr_src = "../../../lvci-header.js" if is_linux else "../../lvci-header.js"
    hdr_cfg = {
        "context": "masscompile-report",
        "repo": m.get("repo", ""),
        "pagesUrl": pages or ("../../.." if is_linux else "../.."),
        "sha": m.get("sha", ""),
        "short": m.get("short", ""),
        "platform": m.get("platform", "windows"),
        "rawUrl": "masscompile.log",
    }
    out = _TEMPLATE.replace("__MC_DATA_JSON__", blob)
    out = out.replace("__MC_HEADER_CFG__", json.dumps(hdr_cfg, ensure_ascii=False))
    out = out.replace("__LVCI_HEADER_SRC__", hdr_src)
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description="Build a friendly Mass Compile report from the raw log.")
    ap.add_argument("--log", required=True, help="Path to masscompile.log")
    ap.add_argument("--out", required=True, help="Output directory")
    ap.add_argument("--workspace", default="", help="Workspace root (to count VIs and resolve caller names)")
    ap.add_argument("--platform", default="windows", choices=["windows", "linux"])
    ap.add_argument("--meta", default="", help="compile-meta.json with exit/duration")
    ap.add_argument("--exit", type=int, default=0, help="Compile exit code (if no --meta)")
    ap.add_argument("--duration", type=float, default=0.0, help="Compile duration seconds (if no --meta)")
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

    (out_dir / "problems.json").write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    (out_dir / "summary.json").write_text(json.dumps(data["summary"], ensure_ascii=False), encoding="utf-8")
    (out_dir / "index.html").write_text(render(data), encoding="utf-8")

    s = data["summary"]
    print(f"Friendly Mass Compile report ({args.platform}): {s['percent']}% compiled "
          f"({s['ok']}/{s['total']} VIs, {s['bad']} bad, {s['missing_deps']} missing deps) "
          f"-> {out_dir / 'index.html'}")


# The report is a single self-contained page. The current platform's data is
# injected at __MC_DATA_JSON__; the other platform's data is fetched on demand
# when the toggle is used.
_TEMPLATE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Mass Compile Report — LabVIEW-CI-with-Containers</title>
<script>window.LVCI=__MC_HEADER_CFG__;</script>
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

/* platform toggle */
.platrow{display:flex;align-items:center;gap:12px;margin-bottom:14px;flex-wrap:wrap}
#plat-toggle{display:inline-flex;align-items:center;border:1px solid var(--border);border-radius:8px;overflow:hidden}
#plat-toggle button{border:none;background:var(--surface);color:var(--fg-muted);cursor:pointer;font:inherit;
  font-weight:600;font-size:.82em;padding:7px 16px;display:inline-flex;align-items:center;gap:7px}
#plat-toggle button:hover:not(.active):not(:disabled){background:var(--hover);color:var(--fg)}
#plat-toggle button.active{background:var(--link);color:#fff}
#plat-toggle button:disabled{opacity:.4;cursor:default}
#plat-toggle .sep{width:1px;align-self:stretch;background:var(--border)}
.platnote{color:var(--fg-muted);font-size:.8em}

/* summary band */
.cards{display:flex;flex-wrap:wrap;gap:12px;margin-bottom:14px}
.card{background:var(--surface);border:1px solid var(--border);border-radius:10px;padding:12px 16px;min-width:120px;flex:1 1 auto}
.card .n{font-size:1.7em;font-weight:700;line-height:1.1}
.card .l{color:var(--fg-muted);font-size:.78em;text-transform:uppercase;letter-spacing:.04em;margin-top:2px}
.card.bad .n{color:var(--bad)}.card.ok .n{color:var(--ok)}.card.warn .n{color:var(--warn)}
.bar{height:9px;border-radius:5px;background:var(--bad);overflow:hidden;margin:12px 0 4px;border:1px solid var(--border)}
.bar>span{display:block;height:100%;background:var(--ok)}
.barlabel{color:var(--fg-muted);font-size:.78em;margin-bottom:18px}
.statusbadge{display:inline-block;padding:2px 9px;border-radius:5px;font-weight:700;font-size:.76em;color:#fff;vertical-align:middle;margin-left:8px}

/* toolbar */
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
.prob{margin:8px 0 0}
.prob .ph{display:flex;align-items:center;gap:8px;font-size:.86em;font-weight:600}
.prob .ph .dot{width:9px;height:9px;border-radius:50%;flex:0 0 auto}
.prob .cnt{color:var(--fg-muted);font-weight:400;font-size:.86em}
.msgs{margin:4px 0 0;padding:0 0 0 18px}
.msgs li{color:var(--fg-muted);font-size:.84em;margin:2px 0;line-height:1.45}

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
  <h1>Mass Compile Report <span class="statusbadge" id="statusbadge"></span></h1>
  <div class="sub" id="sub"></div>
  <div class="nav" id="nav"></div>

  <div class="platrow">
    <div id="plat-toggle"></div>
    <span class="platnote" id="platnote"></span>
  </div>

  <div class="cards" id="cards"></div>
  <div class="bar"><span id="barfill"></span></div>
  <div class="barlabel" id="barlabel"></div>

  <div class="toolbar">
    <input id="q" type="search" placeholder="Filter by VI, problem, or message…">
    <div class="chips" id="chips"></div>
    <div class="tools"><button id="expand">Expand all</button><button id="collapse">Collapse all</button></div>
    <span class="count" id="rescount"></span>
  </div>

  <div id="view-vi"></div>
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

<script id="mc-data" type="application/json">__MC_DATA_JSON__</script>
<script>
const SELF = JSON.parse(document.getElementById('mc-data').textContent);
const PLATFORMS = SELF.platforms || [{id:SELF.meta.platform,url:null}];
const SELF_PLATFORM = SELF.meta.platform;
const CACHE = { [SELF_PLATFORM]: SELF };
let CUR = SELF_PLATFORM;
let D = SELF;                       // currently rendered platform data
const META = SELF.meta;
const SNAP_BASE = META.snap_base;   // absolute (or depth-relative) vi-snapshots/
const esc = s => String(s==null?'':s).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const PLAT_LABEL = {windows:'Windows', linux:'Linux'};
const STATUS_COLOR = {passed:'#2ea043', partial:'#bb8009', failed:'#da3633'};

let active = new Set(D.category_order);
let query = '';

async function fetchJson(u){ const r = await fetch(u,{cache:'no-store'}); if(!r.ok) throw new Error(r.status+' '+u); return r.json(); }

// ── static header / nav (platform-independent) ───────────────────────────
(function header(){
  const m = META;
  const commitMsg = (m.commit && m.commit.message) ? esc(m.commit.message) : '';
  const shaLink = m.repo && m.sha ? `https://github.com/${m.repo}/commit/${m.sha}` : '';
  document.getElementById('sub').innerHTML =
    (m.short ? `Commit ${shaLink?`<a href="${shaLink}" target="_top">${esc(m.short)}</a>`:esc(m.short)} ` : '') +
    (commitMsg ? `&middot; ${commitMsg} ` : '') +
    (m.labview_version ? `&middot; LabVIEW ${esc(m.labview_version)} ` : '') +
    `&middot; generated ${esc(m.generated_utc||'')}`;
  // Top nav + context actions (Regenerate, Raw log, This commit) + the revision
  // picker are owned by the shared site header (lvci-header.js); this report only
  // emits its window.LVCI config in <head>. (The header hides itself when this
  // report is embedded in the report-viewer iframe, which has its own chrome.)
})();

// ── platform toggle ──────────────────────────────────────────────────────
function renderToggle(){
  const host = document.getElementById('plat-toggle');
  host.innerHTML = PLATFORMS.map((p,i)=>{
    const known = (p.id===SELF_PLATFORM) || (CACHE[p.id]!==undefined) || (p.url!=null);
    const dis = (p.id!==SELF_PLATFORM && p.url==null) ? 'disabled' : '';
    return (i? '<span class="sep"></span>':'') +
      `<button data-plat="${p.id}" class="${p.id===CUR?'active':''}" ${dis}>${esc(PLAT_LABEL[p.id]||p.id)}</button>`;
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
  if(!data){
    renderToggle();
    document.getElementById('platnote').textContent =
      `Mass Compile has not run on ${PLAT_LABEL[pid]||pid} for this commit.`;
    showEmptyPlatform(pid);
    return;
  }
  D = data; active = new Set(D.category_order);
  renderToggle(); renderAll();
}
function showEmptyPlatform(pid){
  document.getElementById('cards').innerHTML = '';
  document.getElementById('barfill').style.width = '0%';
  document.getElementById('barlabel').textContent = '';
  document.getElementById('statusbadge').style.display = 'none';
  document.getElementById('chips').innerHTML = '';
  document.getElementById('view-vi').innerHTML =
    `<div class="empty">No ${esc(PLAT_LABEL[pid]||pid)} mass-compile report for this commit yet.</div>`;
  document.getElementById('rescount').textContent = '';
}

// ── render everything from D (current platform) ──────────────────────────
function renderAll(){
  const m = D.meta, s = D.summary, CATS = D.categories, ORDER = D.category_order;
  document.getElementById('platnote').textContent =
    m.duration ? `Compiled in ${Math.round(m.duration)}s on ${PLAT_LABEL[CUR]||CUR}.` : '';

  const sb = document.getElementById('statusbadge');
  sb.style.display='inline-block';
  sb.textContent = s.status==='passed'?'all compiled':(s.status==='failed'?'compile failed':`${s.percent}% compiled`);
  sb.style.background = STATUS_COLOR[s.status] || '#6e7681';

  const cards = [
    {n:s.total, l:'Project VIs'},
    {n:s.ok, l:'Compiled OK', c:'ok'},
    {n:s.bad, l:'Bad VIs', c: s.bad?'bad':''},
    {n:s.missing_deps, l:'Missing deps', c: s.missing_deps?'warn':''},
    {n:s.problem_vis, l:'VIs w/ problems', c: s.problem_vis?'warn':''},
  ];
  document.getElementById('cards').innerHTML = cards.map(c=>
    `<div class="card ${c.c||''}"><div class="n">${(c.n||0).toLocaleString()}</div><div class="l">${esc(c.l)}</div></div>`).join('');
  document.getElementById('barfill').style.width = (s.percent||0)+'%';
  document.getElementById('barlabel').textContent =
    `${s.percent||0}% of ${ (s.total||0).toLocaleString() } project VIs compiled · ${(s.bad||0).toLocaleString()} bad · ${(s.missing_deps||0).toLocaleString()} missing dependencies`;

  // severity chips
  const totals = {}; for(const c of ORDER) totals[c]=0;
  for(const v of D.vis) for(const c of ORDER) totals[c]+=(v.sev_counts[c]||0);
  document.getElementById('chips').innerHTML = ORDER.map(c=>{
    const meta = CATS[c];
    const on = active.has(c);
    return `<span class="chip ${on?'on':'off'}" data-sev="${c}" title="${esc(meta.blurb)}"><span class="dot" style="background:${meta.color}"></span>${esc(meta.label)} <b>${totals[c]}</b></span>`;
  }).join('');
  document.querySelectorAll('#chips .chip').forEach(ch=>ch.addEventListener('click',()=>{
    const sv = ch.dataset.sev;
    if(active.has(sv)){active.delete(sv);} else {active.add(sv);}
    apply();
    ch.classList.toggle('on'); ch.classList.toggle('off');
  }));

  renderVI();
  apply();
}

function sevDots(counts, CATS, ORDER){
  return `<span class="sevdots">`+ORDER.filter(c=>counts[c]).map(c=>
    `<span class="sevdot"><span class="dot" style="background:${CATS[c].color}"></span>${counts[c]}</span>`).join('')+`</span>`;
}
function groupProblemsByType(problems){
  const m = new Map();
  for(const p of problems){
    const k = p.type+'|'+p.severity;
    if(!m.has(k)) m.set(k,{type:p.type,severity:p.severity,messages:[]});
    m.get(k).messages.push(p.message);
  }
  return [...m.values()].sort((a,b)=> b.messages.length-a.messages.length || a.type.localeCompare(b.type));
}
function viCard(v){
  const CATS = D.categories;
  const probs = groupProblemsByType(v.problems).map(r=>`
    <div class="prob" data-sev="${r.severity}" data-text="${esc((r.type+' '+r.messages.join(' ')).toLowerCase())}">
      <div class="ph"><span class="dot" style="background:${CATS[r.severity].color}"></span>${esc(r.type)} <span class="cnt">×${r.messages.length}</span></div>
      <ul class="msgs">${r.messages.map(msg=>`<li>${esc(msg)}</li>`).join('')}</ul>
    </div>`).join('');
  const relTxt = v.vi_rel || '(path not resolved)';
  const snap = v.vi_rel
    ? `<button class="snapbtn" data-snap="${esc(v.vi_rel)}" data-name="${esc(v.name)}">Snapshot</button>`
    : '';
  return `<details class="vi" data-vipath="${esc(((v.vi_rel||'')+' '+v.name).toLowerCase())}">
    <summary>
      <span class="tw">▶</span>
      <span class="viname">${esc(v.name)}</span>
      <span class="virel">${esc(relTxt)}</span>
      ${sevDots(v.sev_counts, CATS, D.category_order)}
      <span class="pill">${v.total} problem${v.total===1?'':'s'}</span>
      ${snap}
    </summary>
    <div class="vibody">${probs}</div>
  </details>`;
}
function renderVI(){
  const groups = new Map();
  for(const v of D.vis){ if(!groups.has(v.group)) groups.set(v.group,[]); groups.get(v.group).push(v); }
  const names = [...groups.keys()].sort((a,b)=>a.localeCompare(b));
  const host = document.getElementById('view-vi');
  host.innerHTML = names.map(g=>{
    const vis = groups.get(g);
    const total = vis.reduce((s,v)=>s+v.total,0);
    return `<details class="group" open data-group="${esc(g)}">
      <summary><span class="tw">▶</span><span class="gname">${esc(g)}</span><span class="gmeta">${vis.length} VI${vis.length===1?'':'s'} · ${total} problem${total===1?'':'s'}</span></summary>
      <div class="gbody">${vis.map(viCard).join('')}</div>
    </details>`;
  }).join('') || `<div class="empty">No compile problems on ${esc(PLAT_LABEL[CUR]||CUR)} 🎉</div>`;
}

// ── filtering ────────────────────────────────────────────────────────────
function apply(){
  const q = query;
  document.querySelectorAll('#view-vi .group').forEach(g=>{
    let gVisible = 0;
    g.querySelectorAll('.vi').forEach(vi=>{
      const viHit = !q || vi.dataset.vipath.includes(q);
      let any = 0;
      vi.querySelectorAll('.prob').forEach(r=>{
        const show = active.has(r.dataset.sev) && (viHit || r.dataset.text.includes(q));
        r.classList.toggle('hidden', !show);
        if(show) any++;
      });
      vi.classList.toggle('hidden', any===0);
      if(any>0) gVisible++;
    });
    g.classList.toggle('hidden', gVisible===0);
  });
  const n = [...document.querySelectorAll('#view-vi .vi')].filter(v=>!v.classList.contains('hidden')).length;
  document.getElementById('rescount').textContent = `${n} VI${n===1?'':'s'} shown`;
}

// ── snapshot drawer (resolves vi_rel → by-blob HTML via manifest.json) ────
let snapMap = null, snapFromSha = null;
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

// ── tools ────────────────────────────────────────────────────────────────
document.getElementById('q').addEventListener('input',e=>{ query=e.target.value.trim().toLowerCase(); apply(); });
document.getElementById('expand').addEventListener('click',()=>document.querySelectorAll('#view-vi details').forEach(d=>d.open=true));
document.getElementById('collapse').addEventListener('click',()=>document.querySelectorAll('#view-vi .vi, #view-vi .group').forEach(d=>d.open=false));

renderToggle();
renderAll();
</script>
</body>
</html>
"""


if __name__ == "__main__":
    main()
