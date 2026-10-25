#!/usr/bin/env python3
"""Validate that the installer catalog matches the source-owned files."""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
CATALOG = ROOT / ".github" / "labview-ci" / "catalog.json"
GITLAB_PROVIDER_ENTRY = ".github/labview-ci/providers/gitlab/"
GITLAB_PROVIDER_ROOT = ROOT / GITLAB_PROVIDER_ENTRY
GITLAB_PROVIDER_TARGET = ".gitlab/labview-ci"

# Files that exist ONLY in the tooling source repository -- the release and
# publishing machinery. A consumer install never has them, so the configurator
# (integrate.html), which is a CLIENT installer, classifies them as "old tooling
# files" and deletes them. Running the configurator against this repo (or a fork
# of it) therefore strips the repo's ability to publish at all. That is exactly
# what happened in e9cd54b on 2026-07-30, which also downgraded catalog.json from
# 4.12.4 to 4.11.10 and left 4.11.11-4.12.4 unpublished for two weeks.
#
# This list is the repo-side backstop for that class of mistake: it cannot be
# bypassed by a stale browser tab or a hand-edited PR the way a UI check can.
SOURCE_ONLY_FILES = [
    ".github/labview-ci/source.json",
    ".github/labview-ci/sync-gitlab-mirror.sh",
    ".github/labview/promote-release.py",
    ".github/labview/validate-catalog-source-sync.py",
    ".github/labview/vipm/build-tooling-vipc.py",
    ".github/pages/configure.html",
    ".github/pages/integrate.html",
    ".github/pages/whats-new.html",
    ".github/workflows/build-labview-image.yml",
    ".github/workflows/build-labview-linux-image.yml",
    ".github/workflows/catalog-source-sync.yml",
    ".github/workflows/copy-labview-image.yml",
    ".github/workflows/copy-labview-linux-image.yml",
    ".github/workflows/discover-clients.yml",
    ".github/workflows/integrate-deploy.yml",
    ".github/workflows/labview-ci.reusable.yml",
    ".github/workflows/promote-release.yml",
    ".github/workflows/release.yml",
    ".github/workflows/sync-gitlab-distribution.yml",
]

REQUIRED_CUSTOM_IMAGE_WINDOWS = [
    ".github/workflows/build-labview-image.yml",
    ".github/workflows/copy-labview-image.yml",
    ".github/docker/labview-ci-base.Dockerfile",
    ".github/docker/labview-ci.Dockerfile",
    ".github/labview/vipm/",
]

OBSOLETE_WINDOWS_WORKER_FILES = {
    ".github/docker/labview-vipm-base.Dockerfile",
    ".github/docker/labview-vipc-layer.Dockerfile",
}


def err(message: str) -> None:
    print(f"ERROR: {message}", file=sys.stderr)


def path_exists(relpath: str) -> bool:
    return (ROOT / relpath).exists()


SEMVER_TAG = re.compile(r"^v(\d+)\.(\d+)\.(\d+)$")


def parse_version(text: str) -> tuple[int, int, int] | None:
    match = re.match(r"^(\d+)\.(\d+)\.(\d+)$", (text or "").strip())
    return (int(match[1]), int(match[2]), int(match[3])) if match else None


def published_tag_names() -> set[str]:
    """Every published version as a bare string ("4.12.4"), empty if tags are unavailable."""
    try:
        out = subprocess.run(
            ["git", "tag", "--list", "v*.*.*"],
            cwd=ROOT, capture_output=True, text=True, timeout=30, check=True,
        )
    except (OSError, subprocess.SubprocessError):
        return set()
    names = set()
    for line in out.stdout.splitlines():
        tag = line.strip()
        if SEMVER_TAG.match(tag):
            names.add(tag[1:])
    return names


def highest_published_version() -> tuple[tuple[int, int, int], str] | None:
    """Highest v<MAJOR>.<MINOR>.<PATCH> tag in this clone, or None if unknown.

    Returns None when git is unavailable or no version tags are present (a
    shallow CI checkout, or a clone fetched without tags). Callers must treat
    None as "could not check" rather than "nothing published" -- see main().
    """
    try:
        out = subprocess.run(
            ["git", "tag", "--list", "v*.*.*"],
            cwd=ROOT, capture_output=True, text=True, timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None

    best: tuple[tuple[int, int, int], str] | None = None
    for line in out.stdout.splitlines():
        match = SEMVER_TAG.match(line.strip())
        if not match:
            continue
        parsed = (int(match[1]), int(match[2]), int(match[3]))
        if best is None or parsed > best[0]:
            best = (parsed, line.strip())
    return best


def validate_gitlab_provider(catalog: dict, capabilities: list[dict]) -> list[str]:
    """Validate the vendored GitLab adapter and its catalog coverage."""
    failures: list[str] = []
    base_files = (catalog.get("base") or {}).get("files") or []
    if GITLAB_PROVIDER_ENTRY not in base_files:
        failures.append(
            f"base.files must include {GITLAB_PROVIDER_ENTRY!r} so GitLab installs can update locally"
        )

    manifest_path = GITLAB_PROVIDER_ROOT / "files.json"
    if not manifest_path.is_file():
        return failures + [f"GitLab provider manifest is missing: {manifest_path.relative_to(ROOT)}"]
    try:
        provider = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return failures + [f"GitLab provider manifest is invalid: {exc}"]

    if provider.get("targetRoot") != GITLAB_PROVIDER_TARGET:
        failures.append(
            f"GitLab provider targetRoot must be {GITLAB_PROVIDER_TARGET!r}"
        )
    files = provider.get("files")
    if not isinstance(files, list):
        return failures + ["GitLab provider files.json must contain a files list"]
    listed_files: set[str] = set()
    for relpath in files:
        path = Path(relpath) if isinstance(relpath, str) else None
        if path is None or not relpath or path.is_absolute() or ".." in path.parts or "\\" in relpath:
            failures.append(f"GitLab provider contains an unsafe file path: {relpath!r}")
            continue
        if relpath in listed_files:
            failures.append(f"GitLab provider lists {relpath!r} more than once")
            continue
        listed_files.add(relpath)
        if not (GITLAB_PROVIDER_ROOT / path).is_file():
            failures.append(f"GitLab provider lists missing file: {GITLAB_PROVIDER_ENTRY}{relpath}")
    for required in ("files.json", "templates/common.yml", "templates/pages.yml"):
        if required not in listed_files:
            failures.append(f"GitLab provider must package {required!r}")

    known_capabilities = {cap.get("id") for cap in capabilities if cap.get("id")}
    active_capabilities = {
        cap["id"] for cap in capabilities
        if cap.get("id") and cap.get("status") != "planned"
    }
    built_in = provider.get("builtInCapabilities") or []
    if not isinstance(built_in, list) or not all(isinstance(capability, str) for capability in built_in):
        failures.append("GitLab provider builtInCapabilities must be a list of capability ids")
        built_in = []
    if len(set(built_in)) != len(built_in):
        failures.append("GitLab provider builtInCapabilities contains duplicates")
    for capability in built_in:
        if capability not in known_capabilities:
            failures.append(f"GitLab provider names unknown built-in capability: {capability!r}")

    mappings = provider.get("capabilityTemplates")
    if not isinstance(mappings, dict):
        return failures + ["GitLab provider capabilityTemplates must be an object"]
    overlap = set(built_in) & set(mappings)
    if overlap:
        failures.append(f"GitLab provider maps built-in capabilities redundantly: {sorted(overlap)!r}")
    uncovered = active_capabilities - set(built_in) - set(mappings)
    if uncovered:
        failures.append(
            f"GitLab provider lacks native coverage for active capabilities: {sorted(uncovered)!r}"
        )
    by_id = {cap.get("id"): cap for cap in capabilities if cap.get("id")}
    for capability, templates in mappings.items():
        cap = by_id.get(capability)
        if cap is None:
            failures.append(f"GitLab provider maps unknown capability: {capability!r}")
            continue
        if not isinstance(templates, dict):
            failures.append(f"GitLab provider mapping for {capability!r} is not an object")
            continue
        supported_os = set(cap.get("supportsOs") or [])
        mapped_os = set(templates)
        if mapped_os != supported_os:
            failures.append(
                f"GitLab provider mapping for {capability!r} must cover exactly "
                f"{sorted(supported_os)!r}, got {sorted(mapped_os)!r}"
            )
        for os_name, template in templates.items():
            if os_name not in supported_os:
                failures.append(
                    f"GitLab provider maps unsupported {os_name!r} platform for {capability!r}"
                )
            if not isinstance(template, str) or template not in listed_files:
                failures.append(
                    f"GitLab provider template for {capability!r} ({os_name}) is not packaged: {template!r}"
                )
    return failures


def validate_source_distributions(catalog: dict) -> list[str]:
    """Validate the canonical source and its GitHub/GitLab distribution endpoints."""
    failures: list[str] = []
    pointer_path = ROOT / ".github" / "labview-ci" / "source.json"
    try:
        pointer = json.loads(pointer_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return [f"source.json is invalid: {exc}"]

    source = catalog.get("source") or {}
    if pointer.get("repo") != source.get("repo"):
        failures.append("source.json repo must match catalog.source.repo")
    if pointer.get("ref") != source.get("ref"):
        failures.append("source.json ref must match catalog.source.ref")

    distributions = pointer.get("distributions")
    if not isinstance(distributions, dict):
        return failures + ["source.json distributions must be an object"]
    for host in ("github", "gitlab"):
        entry = distributions.get(host)
        if not isinstance(entry, dict):
            failures.append(f"source.json distributions.{host} must be an object")
            continue
        repo = entry.get("repo")
        ref = entry.get("ref")
        url = entry.get("url")
        if not isinstance(repo, str) or not re.fullmatch(r"[^/\s]+(?:/[^/\s]+)+", repo):
            failures.append(f"source.json distributions.{host}.repo must be a namespace/project path")
        if not isinstance(ref, str) or not ref.strip():
            failures.append(f"source.json distributions.{host}.ref must be non-empty")
        if not isinstance(url, str) or not re.fullmatch(r"https://[^\s/]+(?:/[^\s]*)?", url):
            failures.append(f"source.json distributions.{host}.url must be an HTTPS base URL")

    github = distributions.get("github") or {}
    if github.get("repo") != pointer.get("repo") or github.get("ref") != pointer.get("ref"):
        failures.append("source.json distributions.github must match the canonical repo/ref")
    return failures


def main() -> int:
    failures: list[str] = []

    # Run FIRST: everything below reads source-owned files directly, so a repo
    # that has had its publishing machinery deleted should fail with this clear
    # message rather than a FileNotFoundError traceback further down.
    missing_source_files = [p for p in SOURCE_ONLY_FILES if not path_exists(p)]
    if missing_source_files:
        err(
            "This repository is the LabVIEW CI tooling SOURCE, but files that only "
            "the source repo carries have been deleted:"
        )
        for relpath in missing_source_files:
            err(f"  - {relpath}")
        err(
            "This is the signature of running the LabVIEW CI configurator (the client "
            "installer) against the source repo or a fork of it: it removes source-only "
            "files as 'old tooling' and overwrites catalog.json with an older payload. "
            "Restore these files from the last release tag instead of merging this change."
        )
        return 1

    catalog = json.loads(CATALOG.read_text(encoding="utf-8"))

    releases = (catalog.get("history") or {}).get("releases") or []
    if not releases:
        failures.append("catalog history.releases is empty")
    elif catalog.get("version") != releases[0].get("version"):
        failures.append(
            "catalog version must equal history.releases[0].version "
            f"({catalog.get('version')!r} != {releases[0].get('version')!r})"
        )

    # The catalog version is the version of record: release.yml reads it to cut
    # the tag and move the v<major> alias every client pins to. It must never go
    # backwards. It regressed once (4.12.4 -> 4.11.10) and the failure was silent
    # -- the file stayed internally consistent, so the check above still passed,
    # while release.yml quietly stopped publishing because v4.11.10 already
    # existed. Comparing against the tags is what makes that visible.
    current = parse_version(catalog.get("version") or "")
    if current is None:
        failures.append(
            f"catalog version {catalog.get('version')!r} is not MAJOR.MINOR.PATCH"
        )
    else:
        published = highest_published_version()
        if published is None:
            message = (
                "could not read version tags, so the catalog version was NOT checked "
                "against the published releases (fetch tags: actions/checkout with "
                "fetch-depth: 0)"
            )
            # Lenient for local runs on a shallow/tagless clone; strict in CI,
            # where a silently skipped guard is how this got missed the first time.
            if os.environ.get("GITHUB_ACTIONS") == "true":
                failures.append(message)
            else:
                print(f"WARNING: {message}", file=sys.stderr)
        elif current < published[0]:
            failures.append(
                f"catalog version {catalog['version']} is LOWER than the highest "
                f"published release {published[1]}. The version of record must never "
                "go backwards: release.yml would find the tag already present, publish "
                "nothing, and leave the v<major> alias stranded on the newer release. "
                f"Set version to something above {published[1].lstrip('v')} (and add a "
                "matching history.releases[0] entry)."
            )

    # Channel pointers must name a version that was actually PUBLISHED, not merely
    # written into the catalog. promote-release.yml used to bump the catalog and
    # rely on release.yml to tag it, but GITHUB_TOKEN pushes do not trigger
    # workflows -- so 4.10.2, 4.10.3 and 4.11.11 were announced and never tagged,
    # and the `beta` tag sat on v4.11.8 for three weeks while betaVersion said
    # 4.11.10. Pointing a channel at an untagged version strands every client on
    # that channel, so it is worth failing over.
    tagged = published_tag_names()
    for tier in ("stableVersion", "betaVersion"):
        pointer = (catalog.get(tier) or "").strip()
        if not pointer:
            continue
        if parse_version(pointer) is None:
            failures.append(f"{tier} {pointer!r} is not MAJOR.MINOR.PATCH")
        elif tagged and pointer not in tagged:
            failures.append(
                f"{tier} points at {pointer}, which has no v{pointer} tag. A channel "
                "must name a published release; clients on that channel would resolve "
                "to a version that does not exist."
            )

    capabilities = catalog.get("capabilities") or []
    failures.extend(validate_source_distributions(catalog))
    failures.extend(validate_gitlab_provider(catalog, capabilities))
    custom_image = next((cap for cap in capabilities if cap.get("id") == "custom-image"), None)
    if custom_image is None:
        failures.append("catalog is missing the custom-image capability")
    else:
        windows_files = custom_image.get("files", {}).get("windows") or []
        if windows_files != REQUIRED_CUSTOM_IMAGE_WINDOWS:
            failures.append(
                "custom-image windows files must exactly match the source-owned "
                f"Windows worker file set: {windows_files!r}"
            )
        obsolete = sorted(set(windows_files) & OBSOLETE_WINDOWS_WORKER_FILES)
        if obsolete:
            failures.append(f"custom-image still vendors obsolete worker files: {obsolete!r}")

    # A `required` capability is installed in every repository, so it must be
    # something a client can actually receive: shipping one that is planned, or
    # flagged defaultOff, would install a contradiction on every consumer.
    required_caps = [cap for cap in capabilities if cap.get("required")]
    if not required_caps:
        failures.append(
            "no capability is marked required. VI Snapshots 2.0 is installed "
            "everywhere so the VI Browser's 2.0 tab always has a workflow to run; "
            "dropping the flag silently makes it optional again."
        )
    for capability in required_caps:
        capability_id = capability.get("id", "<unknown>")
        if capability.get("status") in ("planned", "experimental"):
            failures.append(
                f"capability {capability_id!r} is required but {capability.get('status')!r}"
            )
        if capability.get("defaultOff"):
            failures.append(f"capability {capability_id!r} is both required and defaultOff")

    # Retired files are deleted from consumers on install/update, so a path here
    # must be gone from the source and installed by nothing.
    installable = set((catalog.get("base") or {}).get("files") or [])
    for capability in capabilities:
        for relpaths in (capability.get("files") or {}).values():
            installable.update(relpaths or [])
    for relpath in (catalog.get("retiredFiles") or {}).get("files") or []:
        if path_exists(relpath):
            failures.append(
                f"retired file still exists in the source: {relpath}. Installs delete "
                "it from every consumer, so it cannot also be a file this repo ships."
            )
        if relpath in installable:
            failures.append(f"retired file is still installed by the catalog: {relpath}")

    for capability in capabilities:
        capability_id = capability.get("id", "<unknown>")
        files = capability.get("files") or {}
        for os_name, relpaths in files.items():
            for relpath in relpaths or []:
                if not path_exists(relpath):
                    failures.append(
                        f"capability {capability_id!r} lists missing {os_name} file: {relpath}"
                    )

    workflow = ROOT / ".github" / "workflows" / "build-labview-image.yml"
    workflow_text = workflow.read_text(encoding="utf-8")
    docker_final = ROOT / ".github" / "docker" / "labview-ci.Dockerfile"
    docker_final_text = docker_final.read_text(encoding="utf-8")

    for relpath in REQUIRED_CUSTOM_IMAGE_WINDOWS:
        if not path_exists(relpath):
            failures.append(f"required custom-image source file is missing: {relpath}")

    for obsolete in OBSOLETE_WINDOWS_WORKER_FILES:
        if path_exists(obsolete):
            failures.append(f"obsolete Windows worker Dockerfile still exists: {obsolete}")
        if obsolete in workflow_text:
            failures.append(f"build-labview-image.yml still references obsolete file: {obsolete}")

    if ".github/docker/labview-ci-base.Dockerfile" not in workflow_text:
        failures.append("build-labview-image.yml does not reference labview-ci-base.Dockerfile")
    if "LCWC_BASE_IMAGE" not in workflow_text:
        failures.append("build-labview-image.yml does not define/use LCWC_BASE_IMAGE")
    if "FROM ${LCWC_BASE_IMAGE}" not in docker_final_text:
        failures.append("labview-ci.Dockerfile must start from LCWC_BASE_IMAGE")

    if failures:
        for failure in failures:
            err(failure)
        return 1

    print("Catalog/source sync validation passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())