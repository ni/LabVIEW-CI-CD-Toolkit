#!/usr/bin/env python3
"""
install.py - Install LabVIEW CI capabilities into a target repository.

This is the catalog-driven installer that powers the "Integrate this CI pipeline"
button on the dashboard. It is invoked by install.sh / install.ps1 (which fetch
the tooling and locate Python), or directly:

    python3 .github/labview-ci/install.py --activities masscompile,vi-analyzer,dashboard \
                                          --os windows,linux --labview-version 2026

What it does
  1. Reads the capability catalog (.github/labview-ci/catalog.json) from the
     tooling SOURCE (the directory this script lives in, or --source).
  2. Resolves the file set for the selected activities x operating systems,
     plus their hard `requires`, plus the always-installed base files.
  3. Copies those files into the TARGET repo (cwd, or --target), creating dirs.
  4. Rewrites cosmetic branding (the source project name / owner / Pages host)
     to the target repo's identifiers in copied text files. Functional wiring
     (image name, Pages URL, LabVIEW version) is NOT rewritten - it already
     derives at runtime from the GitHub context and Actions variables.
  5. Writes a manifest (.github/labview-ci.yml) recording what was installed.
  6. Prints the remaining manual steps (enable Pages, set permissions/variables).

Nothing here runs LabVIEW, pushes commits, or mutates the remote: it only writes
files into the working tree, so the result is easy to review with `git diff`.

Dependencies: Python 3.8+ standard library only.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

# File extensions treated as text for branding substitution. Anything else
# (LabVIEW binaries, images, archives) is copied byte-for-byte.
TEXT_EXTS = {
    ".yml", ".yaml", ".ps1", ".sh", ".py", ".html", ".htm", ".md", ".json",
    ".xml", ".viancfg", ".txt", ".cfg", ".css", ".js", ".svg",
}

# The installer's own tooling directory is never rebranded: it must keep pointing
# at the tooling SOURCE repo (catalog.source) so re-runs / upgrades still work.
NO_SUBSTITUTION_PREFIX = ".github/labview-ci/"
DEFAULT_EXCLUDED_STATUSES = {"planned", "experimental"}
GITHUB_WORKFLOW_PREFIX = ".github/workflows/"
GITLAB_PROVIDER_SOURCE = ".github/labview-ci/providers/gitlab"
GITLAB_PROVIDER_TARGET = ".gitlab/labview-ci"
GITLAB_LEGACY_DASHBOARD_BUILDER = f"{GITLAB_PROVIDER_TARGET}/build-dashboard.py"

# Every vendored workflow file gets a first-line version stamp when installed or
# updated. The stamp is not decoration: GitHub only registers a workflow file
# (making it dispatchable via the API and listed under Actions) when a push
# touches it while Actions is enabled, or when an event first runs it. Fork
# installs typically push the whole pipeline while Actions is still disabled, so
# every workflow_dispatch-only workflow stays permanently unregistered -- the
# file exists on the default branch, yet dispatching it returns 404 forever
# (the dashboard's Reconfigure / "Save monitored files" buttons hit exactly
# this). An update can only heal that by touching the file, and git only
# records files whose CONTENT changed -- so the stamp embeds the tooling
# version, guaranteeing every "Update now" rewrites every workflow file and the
# update push (re)registers any that were stuck.
WORKFLOW_STAMP_RE = re.compile(r"^# LabVIEW CI tooling v[^\n]*\n")


def stamp_workflow(text: str, version: str) -> str:
    """Prepend (or refresh, when re-installing over an already-stamped copy) the
    single-line tooling-version stamp comment on a workflow file."""
    stamp = (f"# LabVIEW CI tooling v{version} - this version stamp changes on every tooling "
             "update so the update push touches this file and GitHub (re)registers the workflow.\n")
    return stamp + WORKFLOW_STAMP_RE.sub("", text, count=1)


def should_stamp(rel_path: str) -> bool:
    norm = rel_path.replace("\\", "/")
    return norm.startswith(GITHUB_WORKFLOW_PREFIX) and Path(norm).suffix.lower() in (".yml", ".yaml")


def log(msg: str = "") -> None:
    print(msg, flush=True)


def warn(msg: str) -> None:
    print(f"  ! {msg}", file=sys.stderr, flush=True)


def die(msg: str) -> None:
    print(f"ERROR: {msg}", file=sys.stderr, flush=True)
    sys.exit(1)


def parse_csv(value: str) -> list[str]:
    return [v.strip() for v in (value or "").split(",") if v.strip()]


def load_catalog(source_root: Path) -> dict:
    catalog_path = source_root / ".github" / "labview-ci" / "catalog.json"
    if not catalog_path.is_file():
        die(f"catalog not found at {catalog_path}. Use --source to point at the tooling checkout.")
    try:
        return json.loads(catalog_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        die(f"catalog.json is not valid JSON: {exc}")
    return {}  # unreachable


def detect_target_repo(target_root: Path, explicit: str | None) -> tuple[str | None, str | None]:
    """Return (owner, name) for the target repo, or (None, None) if unknown."""
    if explicit:
        if "/" in explicit:
            owner, name = explicit.split("/", 1)
            return owner, name
        warn(f"--repo '{explicit}' is not in owner/name form; ignoring.")
    # Try the git remote.
    try:
        url = subprocess.check_output(
            ["git", "-C", str(target_root), "remote", "get-url", "origin"],
            stderr=subprocess.DEVNULL,
        ).decode().strip()
    except (subprocess.CalledProcessError, FileNotFoundError):
        return None, None
    # Handle both git@github.com:owner/name.git and https://github.com/owner/name(.git)
    m = re.search(r"[:/]([^/:]+)/([^/]+?)(?:\.git)?$", url)
    if m:
        return m.group(1), m.group(2)
    return None, None


def read_manifest(target_root: Path):
    """Parse a previously-written .github/labview-ci.yml (our own simple format)."""
    p = target_root / ".github" / "labview-ci.yml"
    if not p.is_file():
        return None
    info = {
        "activities": [], "os": [], "labviewVersion": "", "installedVersion": "",
        "provider": "", "branch": "", "distributionHost": "", "distributionRepo": "",
        "distributionRef": "", "distributionUrl": "",
    }
    in_acts = False
    in_distribution = False
    for line in p.read_text(encoding="utf-8").splitlines():
        if re.match(r"^\s*activities:\s*$", line):
            in_acts = True
            continue
        if in_acts:
            m = re.match(r"^\s*-\s*(\S+)", line)
            if m:
                info["activities"].append(m.group(1))
                continue
            in_acts = False
        if re.match(r"^\s{2}distribution:\s*$", line):
            in_distribution = True
            continue
        if in_distribution:
            m = re.match(r"^\s{4}(host|repo|ref|url):\s*(\S.*?)\s*$", line)
            if m:
                key = {"host": "distributionHost", "repo": "distributionRepo",
                       "ref": "distributionRef", "url": "distributionUrl"}[m.group(1)]
                info[key] = m.group(2).strip().strip("'\"")
                continue
            if line and not line[0].isspace():
                in_distribution = False
        m = re.match(r'^\s*labviewVersion:\s*"?([^"\s]+)"?', line)
        if m:
            info["labviewVersion"] = m.group(1)
        m = re.match(r"^\s*branch:\s*(\S+)", line)
        if m:
            info["branch"] = m.group(1)
        m = re.match(r"^\s*installedVersion:\s*(\S+)", line)
        if m:
            info["installedVersion"] = m.group(1)
        m = re.match(r"^\s*provider:\s*(\S+)", line)
        if m:
            info["provider"] = m.group(1)
        m = re.match(r"^\s*os:\s*\[([^\]]*)\]", line)
        if m:
            info["os"] = [x.strip() for x in m.group(1).split(",") if x.strip()]
    return info


def build_substitutions(catalog: dict, owner: str | None, name: str | None,
                        provider: str = "github") -> list[tuple[str, str]]:
    if not owner or not name:
        return []
    pages_host = f"{owner.lower()}.gitlab.io" if provider == "gitlab" else f"{owner.lower()}.github.io"
    tokens = {
        "pagesHost": pages_host,
        "ownerRepo": f"{owner}/{name}",
        "repoName": name,
    }
    subs: list[tuple[str, str]] = []
    for rule in catalog.get("substitutions", {}).get("ordered", []):
        find = rule["find"]
        replace = rule["replaceWith"].format(**tokens)
        if find != replace:
            subs.append((find, replace))
    return subs


def required_activities(catalog: dict) -> list[str]:
    """Capabilities every installation gets, whatever the caller asked for.

    A capability flagged `required` in the catalog is not a choice: VI Snapshots
    2.0 is one, because the VI Browser's 2.0 tab is always visible and needs its
    render workflow to exist in the repository. Without this, a repo installed
    before the capability existed never gained it -- updates reuse the manifest's
    activity list, so the omission is self-perpetuating and the viewer's Run
    button 404s forever on a workflow that was never vendored.
    """
    return [
        c["id"] for c in catalog.get("capabilities", [])
        if c.get("required") and c.get("status", "stable") not in DEFAULT_EXCLUDED_STATUSES
    ]


def default_activities(catalog: dict) -> list[str]:
    defaults = [
        c["id"] for c in catalog.get("capabilities", [])
        if c.get("recommended") and c.get("status", "stable") not in DEFAULT_EXCLUDED_STATUSES
    ]
    return defaults + [a for a in required_activities(catalog) if a not in defaults]


def resolve_activities(catalog: dict, activities: list[str]) -> list[str]:
    """Expand hard capability dependencies, preserving the requested order.

    Required capabilities are appended to whatever was requested, so an older
    manifest (or an explicit --activities list that predates them) still heals on
    the next install or update.
    """
    by_id = {c["id"]: c for c in catalog.get("capabilities", [])}
    selected: list[str] = []
    stack = list(activities)
    stack.extend(a for a in required_activities(catalog) if a not in stack)
    while stack:
        cid = stack.pop(0)
        if cid in selected:
            continue
        cap = by_id.get(cid)
        if cap is None:
            warn(f"unknown activity '{cid}' - skipping.")
            continue
        if cap.get("status") == "planned":
            warn(f"activity '{cid}' is planned/not yet available - skipping.")
            continue
        selected.append(cid)
        for req in cap.get("requires", []):
            if req not in selected:
                stack.append(req)
    return selected


def resolve_file_list(catalog: dict, activities: list[str], os_list: list[str]) -> list[str]:
    by_id = {c["id"]: c for c in catalog.get("capabilities", [])}
    selected = resolve_activities(catalog, activities)

    files: list[str] = list(catalog.get("base", {}).get("files", []))

    for cid in selected:
        cap = by_id[cid]
        supported = set(cap.get("supportsOs", []))
        cap_os = supported & set(os_list)
        files.extend(cap.get("files", {}).get("any", []))
        for osname in sorted(cap_os):
            files.extend(cap.get("files", {}).get(osname, []))
        if supported and not cap_os:
            warn(f"'{cid}' supports {sorted(supported)} but you selected {os_list}; "
                 f"only its shared files were installed.")

    # De-duplicate, preserve order.
    seen: set[str] = set()
    ordered: list[str] = []
    for f in files:
        if f not in seen:
            seen.add(f)
            ordered.append(f)
    return ordered


def should_substitute(rel_path: str) -> bool:
    if rel_path.replace("\\", "/").startswith(NO_SUBSTITUTION_PREFIX):
        return False
    return Path(rel_path).suffix.lower() in TEXT_EXTS


def apply_substitutions(text: str, subs: list[tuple[str, str]]) -> str:
    for find, replace in subs:
        text = text.replace(find, replace)
    return text


def copy_one(src: Path, dst: Path, rel_path: str, subs: list[tuple[str, str]],
             force: bool, dry_run: bool, stats: dict,
             preserve: set = frozenset(), update: bool = False,
             stamp_version: str = "") -> None:
    norm = rel_path.replace("\\", "/")
    # On update, never clobber the consumer's own config files.
    if update and norm in preserve and dst.exists():
        stats["preserved"] += 1
        log(f"  preserve (cfg)  {rel_path}")
        return
    existed = dst.exists()
    if existed and not force:
        stats["skipped"] += 1
        log(f"  skip (exists)   {rel_path}")
        return
    if dry_run:
        stats["planned"] += 1
        log(f"  would {'update ' if (update and existed) else 'install'}  {rel_path}")
        return
    dst.parent.mkdir(parents=True, exist_ok=True)
    wants_subs = bool(subs) and should_substitute(rel_path)
    wants_stamp = bool(stamp_version) and should_stamp(rel_path)
    if wants_subs or wants_stamp:
        try:
            text = src.read_text(encoding="utf-8")
            if wants_subs:
                text = apply_substitutions(text, subs)
            if wants_stamp:
                text = stamp_workflow(text, stamp_version)
            dst.write_text(text, encoding="utf-8")
        except UnicodeDecodeError:
            shutil.copy2(src, dst)
    else:
        shutil.copy2(src, dst)
    if update and existed:
        stats["updated"] += 1
        log(f"  update          {rel_path}")
    else:
        stats["installed"] += 1
        log(f"  install         {rel_path}")


def copy_entry(entry: str, source_root: Path, target_root: Path,
               subs: list[tuple[str, str]], force: bool, dry_run: bool, stats: dict,
               preserve: set = frozenset(), update: bool = False,
               stamp_version: str = "") -> None:
    is_dir = entry.endswith("/")
    src = source_root / entry
    if is_dir:
        if not src.is_dir():
            warn(f"missing source directory {entry} - skipping.")
            return
        source_files = set()
        for child in sorted(src.rglob("*")):
            if child.is_file():
                rel = child.relative_to(source_root).as_posix()
                source_files.add(rel)
                copy_one(child, target_root / rel, rel, subs, force, dry_run, stats, preserve, update, stamp_version)
        # On update, mirror the source: prune tooling files that the source has
        # REMOVED so a vendored directory matches the source exactly. Without this,
        # a file the tooling deletes (e.g. an obsolete Go source) lingers on the
        # consumer and can break the build - duplicate Go declarations broke the
        # Windows VI Browser 2.0 render after viserver_windows.go was removed. Only
        # prune real orphans (never a file still in source) and never the consumer's
        # own preserved config files.
        if update:
            tgt_dir = target_root / entry
            if tgt_dir.is_dir():
                for child in sorted(tgt_dir.rglob("*"), reverse=True):
                    if not child.is_file():
                        continue
                    rel = child.relative_to(target_root).as_posix()
                    if rel in source_files or rel in preserve:
                        continue
                    if dry_run:
                        stats["planned"] += 1
                        log(f"  would prune     {rel}")
                        continue
                    try:
                        child.unlink()
                        stats["pruned"] += 1
                        log(f"  prune (removed) {rel}")
                    except OSError as exc:
                        warn(f"could not prune {rel}: {exc}")
                if not dry_run:
                    for child in sorted(tgt_dir.rglob("*"), reverse=True):
                        if child.is_dir():
                            try:
                                child.rmdir()  # only succeeds when empty
                            except OSError:
                                pass
    else:
        if not src.is_file():
            warn(f"missing source file {entry} - skipping.")
            return
        copy_one(src, target_root / entry, entry, subs, force, dry_run, stats, preserve, update, stamp_version)


def write_manifest(target_root: Path, catalog: dict, activities: list[str], os_list: list[str],
                   labview_version: str, image_name: str | None, branch: str,
                   dry_run: bool, provider: str = "github", distribution_host: str = "github",
                   distribution_repo: str = "", distribution_ref: str = "main",
                   distribution_url: str = "https://github.com") -> None:
    src = catalog.get("source", {})
    now = _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    lines = [
        "# LabVIEW CI install manifest - generated by .github/labview-ci/install.py",
        "# Records what was installed so the install can be reviewed, re-run, or upgraded.",
        f"schemaVersion: {catalog.get('schemaVersion', 1)}",
        f"installedVersion: {catalog.get('version', '0.0.0')}",
        f"installedAt: {now}",
        f"provider: {provider}",
        "source:",
        f"  repo: {src.get('repo', '')}",
        # Pin to the EXACT published version (an immutable tag), never the source's
        # own ref ("main"). The dashboard caller awk-reads this `ref` to check out
        # the tooling at runtime, so a consumer's whole pipeline only changes when
        # they run "Update now" (which rewrites this line) — not whenever the source
        # repo's main advances.
        f"  ref: v{catalog.get('version', '0.0.0')}",
        "  distribution:",
        f"    host: {distribution_host}",
        f"    repo: {distribution_repo}",
        f"    ref: {distribution_ref}",
        f"    url: {distribution_url}",
        "config:",
        f"  labviewVersion: \"{labview_version}\"",
        f"  branch: {branch}",
        f"  os: [{', '.join(os_list)}]",
    ]
    if image_name:
        lines.append(f"  imageName: {image_name}")
    lines.append("activities:")
    for a in activities:
        lines.append(f"  - {a}")
    content = "\n".join(lines) + "\n"
    dst = target_root / ".github" / "labview-ci.yml"
    if dry_run:
        log(f"  would write     .github/labview-ci.yml")
        return
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_text(content, encoding="utf-8")
    log(f"  write           .github/labview-ci.yml")


def print_next_steps(catalog: dict, owner: str | None, name: str | None, activities: list[str],
                     labview_version: str, image_name: str | None, print_vars: bool) -> None:
    repo = f"{owner}/{name}" if owner and name else "<owner>/<repo>"
    log("")
    log("Next steps")
    log("  1. Review the changes:        git status && git diff")
    log("  2. Commit and push:           git add .github && git commit -m \"Add LabVIEW CI\" && git push")
    log("  3. Enable GitHub Pages from the 'gh-pages' branch (Settings > Pages).")
    log("  4. Allow Actions to write:    Settings > Actions > General >")
    log("       'Workflow permissions' -> Read and write permissions.")
    if print_vars:
        log("  5. (Optional) Pin configuration as Actions variables:")
        log(f"       gh variable set LABVIEW_VERSION  -R {repo} -b {labview_version}")
        if image_name:
            log(f"       gh variable set LABVIEW_IMAGE_NAME -R {repo} -b {image_name}")
        log("     (All variables have safe fallbacks, so this is optional.)")
    if "custom-image" in activities:
        log("  6. Run 'Build LabVIEW CI Image' once so the analyzer image exists.")
    log("")
    log("Done. Open a pull request that changes a VI to see the pipeline run.")


def gitlab_pages_url(owner: str | None, name: str | None) -> str:
    if not owner or not name:
        return "https://<namespace>.gitlab.io/<project>"
    return f"https://{owner.lower()}.gitlab.io/{name}"


def gitlab_root_ci() -> str:
    return (
        "# LabVIEW CI - GitLab entrypoint. Installed by .github/labview-ci/install.py.\n"
        "# The shared provider-specific jobs live under .gitlab/labview-ci/.\n"
        "include:\n"
        "  - local: '.gitlab/labview-ci/pipeline.yml'\n"
    )


def gitlab_root_declares_required_stages(content: str) -> bool:
    """Whether a simple root-level `stages` declaration contains provider stages."""
    match = re.search(r"^stages:\s*(\[[^\n]*\])?\s*$", content, re.MULTILINE)
    if not match:
        return False
    inline = match.group(1)
    if inline:
        names = {part.strip().strip("'\"") for part in inline[1:-1].split(",")}
    else:
        tail = content[match.end():]
        body = []
        for line in tail.splitlines():
            if line and not line[0].isspace() and not line.startswith("#"):
                break
            body.append(line)
        names = set(re.findall(r"^\s*-\s*([^\s#]+)", "\n".join(body), re.MULTILINE))
    return {"prepare", "verify", "pages"}.issubset(names)


def validate_gitlab_root_ci(target_root: Path, force_root_ci: bool) -> bool:
    """Return whether the installer should write the root CI file.

    GitLab combines included YAML at the top level. An arbitrary existing root
    pipeline can therefore override the provider's `stages` list or carry its
    own include structure. Do not attempt a lossy text merge: write a root only
    for a new project, preserve a compatible include, or require explicit force.
    """
    root_ci_path = target_root / ".gitlab-ci.yml"
    if not root_ci_path.exists() or force_root_ci:
        return True
    try:
        existing = root_ci_path.read_text(encoding="utf-8")
    except OSError as exc:
        die(f"could not read existing .gitlab-ci.yml: {exc}")
    if ".gitlab/labview-ci/pipeline.yml" not in existing:
        die(".gitlab-ci.yml already exists and does not include .gitlab/labview-ci/pipeline.yml. "
            "Add that local include yourself and re-run, or use --force to replace the root pipeline.")
    if re.search(r"^stages:\s*", existing, re.MULTILINE) and not gitlab_root_declares_required_stages(existing):
        die(".gitlab-ci.yml includes LabVIEW CI but its root `stages` list does not contain "
            "prepare, verify, and pages. Add those stages yourself and re-run, or use --force "
            "to replace the root pipeline.")
    return False


def load_gitlab_provider(source_root: Path) -> tuple[Path, dict]:
    provider_root = source_root / GITLAB_PROVIDER_SOURCE
    manifest_path = provider_root / "files.json"
    if not manifest_path.is_file():
        die(f"GitLab provider manifest not found at {manifest_path}.")
    try:
        provider = json.loads(manifest_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        die(f"GitLab provider files.json is not valid JSON: {exc}")
    if not isinstance(provider.get("files"), list):
        die("GitLab provider files.json must contain a files list.")
    if provider.get("targetRoot") != GITLAB_PROVIDER_TARGET:
        die(f"GitLab provider targetRoot must be {GITLAB_PROVIDER_TARGET!r}.")
    for rel in provider["files"]:
        path = Path(rel) if isinstance(rel, str) else None
        if path is None or not rel or path.is_absolute() or ".." in path.parts:
            die(f"unsafe GitLab provider file entry: {rel!r}")
    return provider_root, provider


def gitlab_supported_activities(catalog: dict, provider: dict, activities: list[str],
                                os_list: list[str]) -> list[str]:
    """Return activities that have a native provider job for each applicable OS.

    A provider package must fail closed when a new catalog capability is added:
    recording it in the manifest without emitting a GitLab job would make an
    installation look successful while silently omitting the requested CI work.
    """
    by_id = {c["id"]: c for c in catalog.get("capabilities", [])}
    mappings = provider.get("capabilityTemplates", {})
    built_in = set(provider.get("builtInCapabilities", []))
    selected: list[str] = []
    for activity in activities:
        if activity in built_in:
            selected.append(activity)
            continue
        cap = by_id[activity]
        applicable = set(cap.get("supportsOs", [])) & set(os_list)
        templates = mappings.get(activity)
        if not isinstance(templates, dict):
            die(f"GitLab provider has no native template mapping for capability {activity!r}.")
        missing = sorted(applicable - set(templates))
        if missing:
            die(f"GitLab provider lacks native {', '.join(missing)} template(s) for {activity!r}.")
        if not applicable:
            warn(f"GitLab activity {activity!r} supports {cap.get('supportsOs', [])}, but you selected "
                 f"{os_list}; it will not be recorded in the install manifest.")
            continue
        selected.append(activity)
    return selected


def gitlab_pipeline_yml(activities: list[str], os_list: list[str], labview_version: str,
                        provider: dict) -> str:
    template_paths = ["templates/common.yml", "templates/pages.yml"]
    mappings = provider.get("capabilityTemplates", {})
    for activity in activities:
        for os_name in os_list:
            template = (mappings.get(activity, {}) or {}).get(os_name)
            if template and template not in template_paths:
                template_paths.append(template)
    includes = "".join(f"  - local: '.gitlab/labview-ci/{path}'\n" for path in template_paths)
    custom_image = "true" if "custom-image" in activities else "false"
    return (
        "# LabVIEW CI - GitLab pipeline. Generated by .github/labview-ci/install.py.\n"
        "# Capability jobs are native GitLab jobs installed under .gitlab/labview-ci/templates/.\n"
        "workflow:\n"
        "  rules:\n"
        "    - if: '$CI_PIPELINE_SOURCE == \"merge_request_event\"'\n"
        "    - if: '$CI_COMMIT_BRANCH == $CI_DEFAULT_BRANCH'\n"
        "    - if: '$CI_PIPELINE_SOURCE == \"web\"'\n"
        "    - if: '$CI_PIPELINE_SOURCE == \"schedule\"'\n\n"
        "stages:\n"
        "  - prepare\n"
        "  - verify\n"
        "  - pages\n\n"
        "variables:\n"
        "  LVCI_PROVIDER: gitlab\n"
        "  LVCI_TARGET_SHA: $CI_COMMIT_SHA\n"
        f"  LVCI_LABVIEW_VERSION: \"{labview_version}\"\n"
        f"  LVCI_USE_CUSTOM_IMAGE: \"{custom_image}\"\n"
        "  LVCI_WINDOWS_RUNNER_TAG: windows-docker\n"
        "  LVCI_LINUX_RUNNER_TAG: linux-docker\n"
        "  LVCI_SNAPSHOT_MODE: head\n"
        "  LVCI_SNAPSHOT_MAX_COMMITS: \"0\"\n"
        "  LVCI_SNAPSHOT_MAX_VIS: \"0\"\n"
        "  LVCI_SNAPSHOT_TIME_BUDGET_MINUTES: \"300\"\n"
        "  LVCI_SNAPSHOT_FORCE: \"false\"\n\n"
        "include:\n" + includes + "\n"
        "pages:\n"
        "  extends: .lvci:pages\n"
        "  rules:\n"
        "    - if: '$CI_COMMIT_BRANCH == $CI_DEFAULT_BRANCH'\n"
        "      when: always\n"
    )


def gitlab_legacy_dashboard_builder(path: Path) -> bool:
    """Return whether path is the dashboard-only scaffold emitted before native jobs."""
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return False
    return (
        text.startswith("#!/usr/bin/env python3\n")
        and "from __future__ import annotations" in text
        and 'PAGES_SRC = ROOT / ".github" / "pages"' in text
        and 'PUBLIC = ROOT / "public"' in text
    )


def prune_retired_files(catalog: dict, target_root: Path, keep: set[str],
                        dry_run: bool, stats: dict) -> None:
    """Delete tooling files this repository no longer ships.

    A retired workflow keeps firing in a client repo long after the source has
    dropped it -- build-toimages-image.yml outlived the render engine it built
    and failed on every push. The configurator's stale-file scan only recognises
    files by content signature, so anything that never mentioned the tooling by
    name survived every reinstall. This list is the explicit backstop.
    """
    for rel in catalog.get("retiredFiles", {}).get("files", []):
        if rel in keep:
            warn(f"retired file {rel} is still installed by the catalog - skipping.")
            continue
        target = target_root / rel
        if not target.is_file():
            continue
        if dry_run:
            stats["planned"] += 1
            log(f"  would prune     {rel} (retired)")
            continue
        try:
            target.unlink()
            stats["pruned"] += 1
            log(f"  prune (retired) {rel}")
        except OSError as exc:
            warn(f"could not prune {rel}: {exc}")


def prune_gitlab_provider_files(target_root: Path, provider: dict, dry_run: bool,
                                stats: dict) -> None:
    """Remove only obsolete files which a previous provider package owned."""
    provider_target = target_root / GITLAB_PROVIDER_TARGET
    old_manifest = provider_target / "files.json"
    current_files = set(provider["files"])
    previous_files: set[str] = set()
    if old_manifest.is_file():
        try:
            prior = json.loads(old_manifest.read_text(encoding="utf-8"))
            prior_files = prior.get("files") if prior.get("targetRoot") == GITLAB_PROVIDER_TARGET else None
            if not isinstance(prior_files, list):
                raise ValueError("missing files list")
            for rel in prior_files:
                path = Path(rel) if isinstance(rel, str) else None
                if path is None or not rel or path.is_absolute() or ".." in path.parts:
                    raise ValueError(f"unsafe path {rel!r}")
                previous_files.add(rel)
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
            warn(f"could not read prior GitLab provider manifest; skipping provider prune: {exc}")

    obsolete = sorted(previous_files - current_files)
    legacy_builder = target_root / GITLAB_LEGACY_DASHBOARD_BUILDER
    if gitlab_legacy_dashboard_builder(legacy_builder):
        obsolete.append("build-dashboard.py")

    for rel in sorted(set(obsolete)):
        target = provider_target / rel
        if not target.is_file():
            continue
        if dry_run:
            stats["planned"] += 1
            log(f"  would prune     {target.relative_to(target_root).as_posix()}")
            continue
        try:
            target.unlink()
            stats["pruned"] += 1
            log(f"  prune (removed) {target.relative_to(target_root).as_posix()}")
        except OSError as exc:
            warn(f"could not prune {target.relative_to(target_root).as_posix()}: {exc}")


def write_gitlab_scaffold(target_root: Path, source_root: Path, activities: list[str],
                          os_list: list[str], labview_version: str, dry_run: bool,
                          write_root_ci: bool, update: bool, stats: dict) -> None:
    provider_root, provider = load_gitlab_provider(source_root)
    generated = {
        f"{GITLAB_PROVIDER_TARGET}/pipeline.yml": gitlab_pipeline_yml(
            activities, os_list, labview_version, provider),
    }
    if write_root_ci:
        generated = {".gitlab-ci.yml": gitlab_root_ci(), **generated}
    else:
        log("  preserve         .gitlab-ci.yml (already includes LabVIEW CI)")
    for rel, content in generated.items():
        if dry_run:
            log(f"  would write     {rel}")
            continue
        dst = target_root / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_text(content, encoding="utf-8")
        log(f"  write           {rel}")
    if update:
        prune_gitlab_provider_files(target_root, provider, dry_run, stats)
    for rel in provider["files"]:
        path = Path(rel)
        src = provider_root / path
        if not src.is_file():
            die(f"GitLab provider file listed but missing: {src}")
        target_rel = f"{GITLAB_PROVIDER_TARGET}/{path.as_posix()}"
        if dry_run:
            log(f"  would write     {target_rel}")
            continue
        dst = target_root / target_rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
        log(f"  write           {target_rel}")


def print_gitlab_next_steps(owner: str | None, name: str | None) -> None:
    log("")
    log("Next steps")
    log("  1. Review the changes:        git status && git diff")
    log("  2. Commit and push to GitLab: git add .github .gitlab .gitlab-ci.yml && git commit -m \"Add LabVIEW CI for GitLab\" && git push")
    log("  3. In GitLab, let the pipeline run and publish the Pages artifact.")
    log("  4. Open Deploy > Pages for GitLab's authoritative Pages URL.")
    log("  5. Windows LabVIEW jobs require a registered Windows Docker GitLab Runner.")
    log("")
    log("GitLab provider status: native capability jobs, artifacts, Container Registry workers, and Pages dashboard installed.")


def thin_install(catalog: dict, target_root: Path, owner: str | None, name: str | None,
                 activities: list[str], os_list: list[str], labview_version: str,
                 branch: str, dry_run: bool) -> int:
    """Write thin caller workflows + Dependabot + config that reference the source
    repo's reusable workflow/actions at the major tag, instead of vendoring copies.
    A thin consumer holds only these small files; updates arrive via the moving tag.
    """
    src = catalog.get("source", {}) or {}
    src_repo = src.get("repo", "") or ""
    version = str(catalog.get("version", "0.0.0"))
    major = version.split(".")[0] if version else "1"
    alias = f"v{major}"
    # The caller pins the reusable workflow at the major alias (@v1) — a stable
    # orchestration "harness". The CAPABILITY version, however, is pinned to this
    # exact release in the config below (source.ref). The reusable workflow checks
    # out the tooling at that exact ref at runtime, so a consumer's capabilities
    # never change version automatically — only when they click "Update now",
    # which edits this config file (a token-free change; not a workflow file).
    cap_ref = f"v{version}"
    now = _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    acts = [a for a in activities
            if a in {c["id"] for c in catalog.get("capabilities", []) if c.get("status") != "planned"}]
    os_csv = ", ".join(os_list)

    def write(rel: str, content: str) -> None:
        dst = target_root / rel
        if dry_run:
            log(f"  would write     {rel}")
            return
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_text(content, encoding="utf-8")
        log(f"  write           {rel}")

    # 1) The CI caller — triggers + delegate to the reusable workflow @major.
    write(".github/workflows/labview-ci.yml",
        "# LabVIEW CI — thin caller. All logic lives in the shared reusable workflow;\n"
        "# this file owns only the triggers. Updates arrive automatically through the\n"
        f"# moving major tag (@{alias}); Dependabot can also bump it.\n"
        "name: LabVIEW CI\n\n"
        "on:\n"
        "  pull_request:\n"
        "    paths: ['**.vi', '**.ctl', '**.lvproj', '**.lvlib', '**.lvclass']\n"
        "  push:\n"
        f"    branches: [{branch}]\n"
        "    paths: ['**.vi', '**.ctl', '**.lvproj', '**.lvlib', '**.lvclass']\n"
        "  workflow_dispatch:\n\n"
        "jobs:\n"
        "  labview-ci:\n"
        "    permissions:\n"
        "      contents: write\n"
        "      statuses: write\n"
        "      packages: read\n"
        "      # The dependency gate reads worker-image build runs and, on a fresh fork\n"
        "      # whose worker image was never built, starts the first build itself.\n"
        "      actions: write\n"
        f"    uses: {src_repo}/.github/workflows/labview-ci.reusable.yml@{alias}\n"
        "    with:\n"
        f"      labview-version: \"{labview_version}\"\n"
        "    secrets: inherit\n")

    # 2) The dashboard caller — meta-triggered; delegates to the dashboard action
    #    at the version this repo opted into (config source.ref), checked out at
    #    runtime, so the dashboard never changes version automatically.
    if "dashboard" in acts:
        write(".github/workflows/dashboard.yml",
            "# CI Dashboard — thin caller. Rebuilds on every commit status, after the\n"
            "# LabVIEW CI workflow, and hourly. The build logic lives in the shared\n"
            "# dashboard action, pulled at the capability version this repo opted into\n"
            "# (.github/labview-ci.yml: source.ref) — so the dashboard updates only when\n"
            "# you opt in via \"Update now\", never automatically. Owns the triggers + deploy.\n"
            "name: CI Dashboard\n"
            "run-name: Publish CI dashboard - ${{ github.event_name == 'workflow_dispatch' && 'manual run' || github.sha }}\n\n"
            "on:\n"
            "  status:\n"
            "  workflow_run:\n"
            "    workflows: [\"LabVIEW CI\"]\n"
            "    types: [completed]\n"
            "  schedule:\n"
            "    - cron: '0 * * * *'\n"
            "  workflow_dispatch:\n\n"
            "concurrency:\n"
            "  group: dashboard-pages\n"
            "  cancel-in-progress: true\n\n"
            "permissions:\n"
            "  contents: write\n"
            "  statuses: read\n"
            "  actions: read\n\n"
            "jobs:\n"
            "  dashboard:\n"
            "    runs-on: ubuntu-latest\n"
            "    steps:\n"
            "      - uses: actions/checkout@v5\n"
            "      - name: Read opted-in tooling ref from config\n"
            "        id: cfg\n"
            "        shell: bash\n"
            "        run: |\n"
            "          REF=$(awk '/^[[:space:]]*ref:[[:space:]]/{print $2; exit}' .github/labview-ci.yml 2>/dev/null)\n"
            "          echo \"ref=${REF:-" + alias + "}\" >> \"$GITHUB_OUTPUT\"\n"
            "      - name: Check out tooling (opted-in version)\n"
            "        uses: actions/checkout@v5\n"
            "        with:\n"
            f"          repository: {src_repo}\n"
            "          ref: ${{ steps.cfg.outputs.ref }}\n"
            "          path: _lvci\n"
            "      - uses: ./_lvci/actions/dashboard\n"
            "        with:\n"
            "          github-token: ${{ secrets.GITHUB_TOKEN }}\n"
            "      - uses: peaceiris/actions-gh-pages@v4.1.0\n"
            "        id: deploy\n"
            "        continue-on-error: true\n"
            "        with:\n"
            "          github_token: ${{ secrets.GITHUB_TOKEN }}\n"
            "          publish_dir: ci-out/dashboard\n"
            "          destination_dir: .\n"
            "          keep_files: true\n"
            # Retry once if a concurrent gh-pages push (configurator site, VI
            # snapshots, report deploys) made this a non-fast-forward; peaceiris
            # re-clones gh-pages each run, so the retry starts from the new tip.
            "      - name: Wait out a gh-pages push race\n"
            "        if: steps.deploy.outcome == 'failure'\n"
            "        run: sleep 30\n"
            "      - name: Deploy dashboard to GitHub Pages (retry)\n"
            "        if: steps.deploy.outcome == 'failure'\n"
            "        uses: peaceiris/actions-gh-pages@v4.1.0\n"
            "        with:\n"
            "          github_token: ${{ secrets.GITHUB_TOKEN }}\n"
            "          publish_dir: ci-out/dashboard\n"
            "          destination_dir: .\n"
            "          keep_files: true\n")

    # 3) Dependabot — auto-PRs to bump the @major pin (token-free updates).
    write(".github/dependabot.yml",
        "# Auto-update the pinned LabVIEW CI tooling. Dependabot opens a reviewable PR\n"
        "# whenever the referenced reusable workflow / action tag gets a new release.\n"
        "version: 2\n"
        "updates:\n"
        "  - package-ecosystem: \"github-actions\"\n"
        "    directory: \"/\"\n"
        "    schedule:\n"
        "      interval: \"weekly\"\n"
        "    commit-message:\n"
        "      prefix: \"ci\"\n"
        "    labels:\n"
        "      - \"dependencies\"\n"
        "      - \"labview-ci\"\n")

    # 4) The consumer config the reusable workflow reads to gate activities.
    cfg = [
        "# .github/labview-ci.yml — LabVIEW CI consumer config (thin install).",
        "schemaVersion: 1",
        f"installedVersion: {version}",
        f"installedAt: {now}",
        "source:",
        f"  repo: {src_repo}",
        f"  ref: {cap_ref}",
        "  distribution:",
        "    host: github",
        f"    repo: {src_repo}",
        f"    ref: {src.get('ref', 'main')}",
        "    url: https://github.com",
        "config:",
        f"  labviewVersion: \"{labview_version}\"",
        f"  os: [{os_csv}]",
        "  concurrency:",
        "    # Per-repo cap on parallel CI jobs. GitHub's real limit is per ACCOUNT",
        "    # (Free 20, Pro 40, Team 60, Enterprise 500 jobs shared across ALL your",
        "    # repos), and submissions/upgrades draw from it too -- so keep it modest.",
        "    maxParallel: 5",
        "activities:",
    ] + [f"  - {a}" for a in acts] + [""]
    write(".github/labview-ci.yml", "\n".join(cfg))

    log("")
    if dry_run:
        log("Dry run (thin): re-run without --dry-run to write the files.")
        return 0
    repo = f"{owner}/{name}" if owner and name else "<owner>/<repo>"
    log("Thin install complete — your repo references the shared reusable workflow "
        f"at @{alias} and runs capabilities pinned to {cap_ref}.")
    log("")
    log("Next steps")
    log("  1. Review:  git status && git diff")
    log("  2. Commit:  git add .github && git commit -m \"Add LabVIEW CI (thin)\" && git push")
    log("  3. Enable GitHub Pages from the 'gh-pages' branch (Settings > Pages).")
    log("  4. Settings > Actions > General > Workflow permissions > Read and write.")
    if "custom-image" in acts:
        log("  5. (vi-analyzer) Build the shared image once, or set vars.LABVIEW_IMAGE_NAME.")
    log("")
    log(f"Updates: capabilities stay on {cap_ref} until you opt in. When a newer "
        "release ships, run the \"Update LabVIEW CI tooling\" workflow (the dashboard's "
        "\"Update now\" button) to bump source.ref — a reviewable, token-free PR.")
    return 0


def consumer_dashboard_workflow(catalog: dict, branch: str = "main") -> str:
    """Thin dashboard workflow for a vendored consumer.

    The dashboard generator lives in actions/dashboard, a LOCAL path that only
    exists in the tooling repo (the source's own dashboard-pages.yml runs it via
    ./actions/dashboard) and that cannot be copied into a consumer because the
    branding substitution would rewrite the generator's source-repo references.
    So a consumer runs the dashboard by checking the tooling out at its opted-in
    ref into _lvci/ at runtime and using ./_lvci/actions/dashboard - the same
    approach as --thin. This is written over the vendored copy after install.
    """
    src = catalog.get("source", {}) or {}
    src_repo = src.get("repo", "") or ""
    # Fallback ref for the generated workflow's awk (used only if labview-ci.yml
    # can't be read): pin to this exact version, not the source's "main".
    ref = f"v{catalog.get('version', '0.0.0')}"
    br = branch or "main"
    return (
        "# CI Dashboard \u2014 GitHub Pages. Thin caller installed by .github/labview-ci/install.py.\n"
        "# The dashboard build logic lives in the shared composite action, pulled at the tooling\n"
        "# version this repo opted into (.github/labview-ci.yml: source.ref) and checked out at\n"
        "# runtime \u2014 so this repo keeps no copy of the generator and the dashboard updates only\n"
        "# when you opt in. Owns the triggers + the Pages deploy.\n"
        "name: CI Dashboard \u2014 GitHub Pages\n"
        "run-name: Publish CI dashboard - ${{ github.event_name == 'workflow_dispatch' && 'manual run' || github.sha }}\n\n"
        "on:\n"
        "  # Build on the install merge + whenever the config changes, so the dashboard\n"
        "  # publishes itself the first time without waiting for the hourly schedule.\n"
        "  push:\n"
        "    branches: [" + br + "]\n"
        "    paths:\n"
        "      - '.github/labview-ci.yml'\n"
        "      - '.github/workflows/dashboard-pages.yml'\n"
        # A VIPC/Dragon change can add packages not yet baked into the worker
        # container(s); rebuild so the Dependencies page + update-container banner
        # reflect the new declaration.
        "      - '**/*.vipc'\n"
        "      - '**/*.dragon'\n"
        "  status:\n"
        "  workflow_run:\n"
        "    workflows:\n"
        '      - "Mass Compile \u2014 Windows Container"\n'
        '      - "Mass Compile Backfill \u2014 Windows Container"\n'
        '      - "Run VI Analyzer \u2014 Windows Container"\n'
        '      - "VIDiff Report \u2014 Windows Container"\n'
        '      - "VIDiff Report \u2014 Linux Container"\n'
        '      - "VIDiff Deploy \u2014 Pages + PR Comment"\n'
        '      - "VI Snapshots and VI Browser"\n'
        '      - "Build LabVIEW CI Image"\n'
        '      - "Build LabVIEW CI Image - Linux"\n'
        '      - "Generate SBOM \u2014 Windows Container"\n'
        "    types: [completed]\n"
        "  schedule:\n"
        "    - cron: '0 * * * *'\n"
        "  workflow_dispatch:\n\n"
        "concurrency:\n"
        "  group: dashboard-pages\n"
        "  cancel-in-progress: true\n\n"
        "permissions:\n"
        "  contents: write\n"
        "  statuses: read\n"
        "  actions: read\n\n"
        "jobs:\n"
        "  build-dashboard:\n"
        "    runs-on: ubuntu-latest\n"
        "    steps:\n"
        "      - name: Checkout repository\n"
        "        uses: actions/checkout@v5\n"
        "      - name: Read opted-in tooling ref from config\n"
        "        id: cfg\n"
        "        shell: bash\n"
        "        run: |\n"
        "          REF=$(awk '/^[[:space:]]*ref:[[:space:]]/{print $2; exit}' .github/labview-ci.yml 2>/dev/null)\n"
        '          echo "ref=${REF:-' + ref + '}" >> "$GITHUB_OUTPUT"\n'
        "      - name: Check out tooling (opted-in version)\n"
        "        uses: actions/checkout@v5\n"
        "        with:\n"
        "          repository: " + src_repo + "\n"
        "          ref: ${{ steps.cfg.outputs.ref }}\n"
        "          path: _lvci\n"
        "      - name: Build CI dashboard\n"
        "        uses: ./_lvci/actions/dashboard\n"
        "        with:\n"
        "          github-token: ${{ secrets.GITHUB_TOKEN }}\n"
        "      - name: Deploy dashboard to GitHub Pages\n"
        "        id: deploy\n"
        "        continue-on-error: true\n"
        "        uses: peaceiris/actions-gh-pages@v4.1.0\n"
        "        with:\n"
        "          github_token: ${{ secrets.GITHUB_TOKEN }}\n"
        "          publish_dir: ci-out/dashboard\n"
        "          destination_dir: .\n"
        "          keep_files: true\n"
        # Retry once if a concurrent gh-pages push (configurator site, VI
        # snapshots, report deploys) made this a non-fast-forward; peaceiris
        # re-clones gh-pages each run, so the retry starts from the new tip.
        "      - name: Wait out a gh-pages push race\n"
        "        if: steps.deploy.outcome == 'failure'\n"
        "        run: sleep 30\n"
        "      - name: Deploy dashboard to GitHub Pages (retry)\n"
        "        if: steps.deploy.outcome == 'failure'\n"
        "        uses: peaceiris/actions-gh-pages@v4.1.0\n"
        "        with:\n"
        "          github_token: ${{ secrets.GITHUB_TOKEN }}\n"
        "          publish_dir: ci-out/dashboard\n"
        "          destination_dir: .\n"
        "          keep_files: true\n"
    )


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Install LabVIEW CI capabilities into a repository.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--activities", default="",
                        help="Comma-separated capability ids (e.g. masscompile,vi-analyzer,dashboard).")
    parser.add_argument("--os", default="",
                        help="Comma-separated operating systems: windows,linux (default: catalog default).")
    parser.add_argument("--labview-version", default="",
                        help="LabVIEW year (default: catalog default, e.g. 2026).")
    parser.add_argument("--image-name", default="",
                        help="Override the GHCR image name (default: <repo>-labview).")
    parser.add_argument("--branch", default="",
                        help="Default branch the workflows trigger on (default: catalog default).")
    parser.add_argument("--repo", default="",
                        help="Target repo owner/name (default: inferred from the git remote).")
    parser.add_argument("--provider", choices=("github", "gitlab"), default=None,
                        help="Target CI provider to scaffold (default: github; --update uses the installed provider).")
    parser.add_argument("--source", default="",
                        help="Path to the tooling checkout to copy from (default: this script's repo root).")
    parser.add_argument("--source-distribution", choices=("github", "gitlab"), default="",
                        help="Distribution that supplied the tooling checkout (normally set by install.sh/install.ps1).")
    parser.add_argument("--source-distribution-repo", default="",
                        help="Repository on the tooling distribution (normally set by install.sh/install.ps1).")
    parser.add_argument("--source-distribution-ref", default="",
                        help="Branch or tag tracked for tooling updates (normally set by install.sh/install.ps1).")
    parser.add_argument("--source-distribution-url", default="",
                        help="Base URL for the tooling distribution (normally set by install.sh/install.ps1).")
    parser.add_argument("--target", default="",
                        help="Path to the target repo (default: current directory).")
    parser.add_argument("--list", action="store_true", help="List available capabilities and exit.")
    parser.add_argument("--dry-run", action="store_true", help="Show what would be installed without writing.")
    parser.add_argument("--force", action="store_true", help="Overwrite files that already exist.")
    parser.add_argument("--update", action="store_true",
                        help="Re-pull the latest tooling for an existing install (overwrites tooling, "
                             "preserves your config files). Reads the prior selection from the manifest.")
    parser.add_argument("--thin", action="store_true",
                        help="Thin install: write small caller workflows that reference the shared "
                             "reusable workflow + composite actions at the source repo's major tag "
                             "(e.g. @v1), plus Dependabot, instead of vendoring full copies. Updates "
                             "then arrive automatically via the moving tag — no token, no re-install.")
    parser.add_argument("--no-vars", action="store_true", help="Do not print the optional 'gh variable set' steps.")
    args = parser.parse_args()

    source_root = Path(args.source).resolve() if args.source else Path(__file__).resolve().parents[2]
    target_root = Path(args.target).resolve() if args.target else Path.cwd()
    catalog = load_catalog(source_root)

    if args.list:
        log(f"{catalog.get('name', 'LabVIEW CI')} v{catalog.get('version', '0.0.0')} - available capabilities:\n")
        for cap in catalog.get("capabilities", []):
            status = cap.get("status", "stable")
            tag = "" if status == "stable" else f" [{status}]"
            rec = " (recommended)" if cap.get("recommended") else ""
            log(f"  {cap['id']:<14}{tag}{rec}")
            log(f"      {cap['summary']}")
            log(f"      OS: {', '.join(cap.get('supportsOs', []))}")
            log("")
        return 0

    defaults = catalog.get("defaults", {})

    # In --update mode, recover the previous selection from the target's manifest so
    # a plain `install.py --update` re-pulls exactly what was installed before.
    manifest = read_manifest(target_root) if args.update else None
    if args.update and manifest is None:
        die("--update needs an existing install: .github/labview-ci.yml not found in the "
            "target. Run a normal install first.")
    prev = manifest or {}
    if args.provider:
        provider = args.provider
    elif args.update and prev.get("provider"):
        provider = prev["provider"].lower()
        if provider not in {"github", "gitlab"}:
            die(f"installed manifest has unsupported provider {prev['provider']!r}; "
                "pass --provider github or --provider gitlab explicitly.")
    else:
        provider = "github"

    activities = parse_csv(args.activities) or prev.get("activities") or default_activities(catalog)
    os_list = parse_csv(args.os) or prev.get("os") or list(defaults.get("os", ["windows"]))
    valid_os = {"windows", "linux"}
    bad_os = [o for o in os_list if o not in valid_os]
    if bad_os:
        die(f"invalid --os values {bad_os}; allowed: windows, linux.")
    labview_version = args.labview_version or prev.get("labviewVersion") or defaults.get("labviewVersion", "2026")
    branch = args.branch or prev.get("branch") or defaults.get("branch", "main")
    image_name = args.image_name or None
    source = catalog.get("source", {}) or {}
    distribution_host = args.source_distribution or prev.get("distributionHost") or "github"
    distribution_repo = args.source_distribution_repo or prev.get("distributionRepo") or source.get("repo", "")
    distribution_ref = args.source_distribution_ref or prev.get("distributionRef") or source.get("ref", "main")
    distribution_url = args.source_distribution_url or prev.get("distributionUrl")
    if distribution_host not in {"github", "gitlab"}:
        die(f"unsupported source distribution {distribution_host!r}")
    if not distribution_repo:
        die("source distribution repository is empty")
    if not distribution_ref:
        die("source distribution ref is empty")
    if not distribution_url:
        distribution_url = "https://gitlab.com" if distribution_host == "gitlab" else "https://github.com"

    # Update overwrites tooling files but preserves the consumer's own config.
    update = args.update
    force = args.force or update
    preserve = {p.replace("\\", "/") for p in catalog.get("userConfig", {}).get("files", [])}

    if provider == "gitlab" and args.thin:
        die("--thin is currently only supported for --provider github.")

    activities = resolve_activities(catalog, activities)
    if provider == "gitlab":
        _, gitlab_provider = load_gitlab_provider(source_root)
        activities = gitlab_supported_activities(catalog, gitlab_provider, activities, os_list)
        write_root_ci = validate_gitlab_root_ci(target_root, args.force)
    else:
        write_root_ci = False

    if source_root == target_root:
        warn("source and target are the same directory (installing into the tooling repo itself).")

    owner, name = detect_target_repo(target_root, args.repo)
    subs = build_substitutions(catalog, owner, name, provider)
    # Vendored workflows carry a static `branches: [main]` push-trigger filter that
    # YAML can't express as the default branch; rewrite it to the target repo's
    # actual default branch so push-to-default CI fires on non-"main" repos.
    if branch and branch != "main":
        subs.append(("branches: [main]", f"branches: [{branch}]"))
    if not subs:
        warn("target repo owner/name unknown - cosmetic branding left as-is "
             "(functional wiring still adapts at runtime). Pass --repo owner/name to rebrand.")

    log(f"{catalog.get('name', 'LabVIEW CI')} installer")
    log(f"  source:   {source_root}")
    log(f"  target:   {target_root}" + (f"  ({owner}/{name})" if owner and name else ""))
    log(f"  activities: {', '.join(activities)}")
    log(f"  os:         {', '.join(os_list)}")
    log(f"  labview:    {labview_version}")
    log(f"  provider:   {provider}")
    if update and prev.get("installedVersion"):
        log(f"  version:    {prev.get('installedVersion')} -> {catalog.get('version', '0.0.0')}")
    log(f"  mode:       {'dry-run ' if args.dry_run else ''}{'update' if update else ('thin install' if args.thin else 'install')}")
    log("")

    if args.thin:
        return thin_install(catalog, target_root, owner, name, activities, os_list,
                            labview_version, branch, args.dry_run)

    file_list = resolve_file_list(catalog, activities, os_list)
    if provider == "gitlab":
        file_list = [f for f in file_list if not f.replace("\\", "/").startswith(GITHUB_WORKFLOW_PREFIX)]
    stats = {"installed": 0, "updated": 0, "skipped": 0, "planned": 0, "preserved": 0, "pruned": 0}
    stamp_version = str(catalog.get("version", "0.0.0"))
    for entry in file_list:
        copy_entry(entry, source_root, target_root, subs, force, args.dry_run, stats,
                   preserve, update, stamp_version)
    prune_retired_files(catalog, target_root, set(file_list), args.dry_run, stats)

    # The dashboard generator (actions/dashboard) is a local path that only exists
    # in the tooling repo and can't be rebranded into a consumer, so the vendored
    # source dashboard-pages.yml (which runs ./actions/dashboard) can't work here.
    # Replace it with a thin caller that checks the tooling out at runtime.
    if provider == "github" and any(f.endswith("dashboard-pages.yml") for f in file_list) and not args.dry_run:
        dpath = target_root / ".github" / "workflows" / "dashboard-pages.yml"
        if dpath.exists():
            dpath.write_text(consumer_dashboard_workflow(catalog, branch), encoding="utf-8")
            log("  rewrite (thin)  .github/workflows/dashboard-pages.yml")

    if provider == "gitlab":
        write_gitlab_scaffold(target_root, source_root, activities, os_list,
                              labview_version, args.dry_run, write_root_ci, update, stats)

    write_manifest(target_root, catalog, activities,
                   os_list, labview_version, image_name, branch, args.dry_run, provider,
                   distribution_host, distribution_repo, distribution_ref, distribution_url)

    log("")
    if args.dry_run:
        verb = "updated" if update else "installed"
        extra = f", {stats['preserved']} config file(s) preserved" if update else ""
        if update and stats["pruned"]:
            extra += f", {stats['pruned']} removed file(s) would be pruned"
        log(f"Dry run: {stats['planned']} file(s) would be {verb}, {stats['skipped']} already present{extra}.")
        log("Re-run without --dry-run to apply" + ("." if update else " (add --force to overwrite existing files)."))
        return 0
    if update:
        pruned = f", {stats['pruned']} removed file(s) pruned" if stats["pruned"] else ""
        log(f"Update complete: {stats['updated']} file(s) refreshed, {stats['installed']} new, "
            f"{stats['preserved']} config file(s) preserved{pruned}.")
        log("")
        log("Next steps")
        log("  1. Review what changed:  git diff")
        add_paths = ".github .gitlab .gitlab-ci.yml" if provider == "gitlab" else ".github"
        log(f"  2. Commit the update:    git add {add_paths} && git commit -m \"Update LabVIEW CI\" && git push")
        return 0
    pruned = f"; {stats['pruned']} retired file(s) removed" if stats["pruned"] else ""
    log(f"Installed {stats['installed']} file(s); {stats['skipped']} skipped (already present){pruned}.")
    if stats["skipped"]:
        log("Use --force to overwrite skipped files.")
    if provider == "gitlab":
        print_gitlab_next_steps(owner, name)
    else:
        print_next_steps(catalog, owner, name, activities, labview_version, image_name, not args.no_vars)
    return 0


if __name__ == "__main__":
    sys.exit(main())
