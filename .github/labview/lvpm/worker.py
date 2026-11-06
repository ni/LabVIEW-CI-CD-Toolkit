#!/usr/bin/env python3
"""Verify opt-in LVPM candidates and resolve Linux activity selections."""

import argparse
import base64
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
from urllib.parse import urlencode
from urllib.request import HTTPRedirectHandler, Request, build_opener

import yaml


TOOL_REVISION = "8e8bd7e390b7d4a77558af12cfa52f79079df409"
WORKFLOW = ".github/workflows/build-labview-lvpm-image.yml"
SELECTION = re.compile(r"(linux-lvpm-([0-9a-f]{64}))@(sha256:[0-9a-f]{64})")


def candidate_tag(fingerprint, base_digest, build_inputs_sha256, run_id, attempt):
    identity = {"requirements_fingerprint": fingerprint, "base_digest": base_digest,
                "build_inputs_sha256": build_inputs_sha256, "run_id": run_id, "attempt": attempt}
    encoded = json.dumps(identity, sort_keys=True, separators=(",", ":")).encode()
    return "linux-lvpm-" + hashlib.sha256(encoded).hexdigest()


def build_inputs_hash(repo):
    root = Path(repo)
    paths = [root / path for path in (WORKFLOW, ".github/docker/labview-ci-lvpm.Dockerfile",
             ".github/labview/lvpm/adapter.py", ".github/labview/lvpm/worker.py")]
    paths += [path for path in (root / ".github/labview/toimages").rglob("*") if path.is_file()]
    inputs = {path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
              for path in sorted(paths)}
    catalog = json.loads((root / ".github/labview-ci/catalog.json").read_text())["containers"]
    selected = next((entry for entry in catalog["supported"] if entry["id"] == catalog["default"]), None)
    if selected is None:
        raise ValueError("The catalog default container is not supported")
    base = {"default": catalog["default"], **{key: selected[key] for key in
            ("id", "status", "labviewYear", "linuxTag")}}
    inputs[".github/labview-ci/catalog.json#containers"] = hashlib.sha256(
        json.dumps(base, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return hashlib.sha256(json.dumps(inputs, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, new_url):
        raise ValueError("Refusing a registry credential redirect")


def verify_registry(image, tag, digest):
    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if not token:
        raise ValueError("Authenticated GHCR access requires GH_TOKEN or GITHUB_TOKEN")
    package = image.removeprefix("ghcr.io/")
    actor = os.environ.get("GITHUB_ACTOR") or package.split("/")[0]
    basic = base64.b64encode(f"{actor}:{token}".encode()).decode()
    opener = build_opener(NoRedirect())
    query = urlencode({"service": "ghcr.io", "scope": f"repository:{package}:pull"})
    request = Request(f"https://ghcr.io/token?{query}", headers={"Authorization": f"Basic {basic}"})
    with opener.open(request, timeout=30) as response:
        bearer = json.load(response)["token"]
    headers = {"Authorization": f"Bearer {bearer}", "Accept": ", ".join((
        "application/vnd.oci.image.index.v1+json", "application/vnd.oci.image.manifest.v1+json",
        "application/vnd.docker.distribution.manifest.list.v2+json",
        "application/vnd.docker.distribution.manifest.v2+json"))}
    for reference in (tag, digest):
        request = Request(f"https://ghcr.io/v2/{package}/manifests/{reference}", headers=headers, method="HEAD")
        with opener.open(request, timeout=30) as response:
            if response.headers.get("Docker-Content-Digest") != digest:
                raise ValueError("GHCR candidate tag or immutable digest has changed")


def verify_image_platform(image, inventory):
    if (not isinstance(image, dict) or not isinstance(inventory, dict)
            or image.get("Os") != "linux" or image.get("Architecture") != "amd64"
            or inventory.get("platform") != "linux" or inventory.get("architecture") != "amd64"):
        raise ValueError("Candidate image and installed inventory must both prove linux/amd64")


def verify_packages(manifest, plan):
    packages = manifest.get("packages")
    if not isinstance(packages, list) or not packages:
        raise ValueError("Candidate has no package inventory")
    installed = {}
    for package in packages:
        if not isinstance(package, dict):
            raise ValueError("Invalid candidate package inventory entry")
        name, version = package.get("name"), package.get("version")
        files = package.get("files")
        if (not isinstance(name, str) or not re.fullmatch(r"[a-z0-9_][a-z0-9_.-]*", name)
                or name in installed or not isinstance(version, str) or not version
                or not re.fullmatch(r"[0-9a-f]{64}", str(package.get("sha256", "")))
                or not isinstance(files, list) or not files
                or not all(isinstance(path, str) and path.startswith("/") and ".." not in Path(path).parts
                           for path in files)):
            raise ValueError("Invalid candidate package version, archive hash, or installed file inventory")
        installed[name] = version
    for package in plan["requirements"]:
        if installed.get(package["name"]) != package["version"]:
            raise ValueError("Candidate package inventory does not satisfy the current dependency plan")


def configuration(repo):
    path = Path(repo) / ".github/labview-ci.yml"
    data = yaml.safe_load(path.read_text(encoding="utf-8")) if path.exists() else {}
    if data is None:
        data = {}
    if not isinstance(data, dict) or not isinstance(data.get("config", {}), dict):
        raise ValueError("Pipeline configuration must be a mapping")
    config = data.get("config", {})
    container = config.get("container") or {}
    if not isinstance(container, dict):
        raise ValueError("config.container must be a mapping")
    return container, str(config.get("labviewVersion") or "2026")


def image_name(repository, override=None):
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
        raise ValueError("Expected repository OWNER/REPO")
    owner, name = repository.lower().split("/")
    override = override or f"{name}-labview"
    if not re.fullmatch(r"[a-z0-9][a-z0-9_.-]*", override.lower()):
        raise ValueError("Image name must be an untagged package name, not a registry path or image reference")
    return f"ghcr.io/{owner}/{override.lower()}"


def github_json(endpoint):
    if not (os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")):
        raise ValueError("Authenticated GitHub API access requires GH_TOKEN or GITHUB_TOKEN")
    result = subprocess.run(
        ["gh", "api", "--hostname", "github.com", "-H", "Accept: application/vnd.github+json", endpoint],
        check=True, capture_output=True, text=True,
    )
    return json.loads(result.stdout)


def current_plan(repo, year):
    with tempfile.TemporaryDirectory(prefix="lvpm-verify-") as temporary:
        subprocess.run(
            [sys.executable, "-I", str(Path(__file__).with_name("adapter.py")), "plan",
             "--repo", str(Path(repo).resolve()), "--out", temporary, "--labview-version", year],
            check=True, stdout=subprocess.PIPE, text=True,
        )
        return json.loads((Path(temporary) / "requirements.json").read_text(encoding="utf-8"))


def check_local_freshness(repo, manifest):
    if (manifest.get("schema") != 1 or manifest.get("installer") != "lvpm"
            or manifest.get("platform") != "linux" or manifest.get("architecture") != "amd64"
            or manifest.get("tool_revision") != TOOL_REVISION):
        raise ValueError("Candidate inventory lacks the required Linux amd64 tooling evidence")
    if manifest.get("verification") != {"status": "passed", "scope": "package-files", "activity_checks": "not_run",
                                       "hook_error_output": "not_observable_by_upstream"}:
        raise ValueError("Candidate does not contain the required package-file verification")
    _, year = configuration(repo)
    plan = current_plan(repo, year)
    fingerprint = manifest.get("requirements_fingerprint")
    if (manifest.get("labview_version") != year or plan.get("schema") != 1
            or not re.fullmatch(r"[0-9a-f]{64}", str(fingerprint))
            or plan.get("labview_version") != year or plan.get("requirements_fingerprint") != fingerprint):
        raise ValueError("LVPM candidate is stale: rebuild for the current Linux requirements and LabVIEW version")
    verify_packages(manifest, plan)
    if build_inputs_hash(repo) != manifest.get("build_inputs_sha256"):
        raise ValueError("LVPM candidate is stale: worker build inputs have changed")
    return plan


def local_freshness(repo, candidate, revision, manifest):
    result = {"status": "unknown", "revision": revision, "selection": candidate,
              "reason": "Local LVPM freshness is unavailable. Rebuild, reverify, and explicitly re-activate the candidate."}
    if not re.fullmatch(r"[0-9a-f]{40}", str(revision)) or not isinstance(manifest, dict):
        return result
    try:
        match = SELECTION.fullmatch(candidate)
        if not match or manifest.get("tag") != match[1] or manifest.get("image_digest") != match[3]:
            raise ValueError("Candidate inventory does not match the exact selected tag and digest")
        check_local_freshness(repo, manifest)
    except (OSError, KeyError, TypeError, json.JSONDecodeError, yaml.YAMLError, subprocess.CalledProcessError):
        return result
    except ValueError as error:
        return {**result, "status": "stale", "reason": str(error) + ". Rebuild, reverify, and explicitly re-activate the candidate."}
    except Exception:
        return result
    return {**result, "status": "current", "reason": "Local requirements and build inputs match. "
            "Registry, provenance, and runtime activities have not been verified by this freshness check."}


def verify(repo, candidate, repository, image_override=None):
    match = SELECTION.fullmatch(candidate)
    if not match:
        raise ValueError("LVPM selection must be linux-lvpm-<64-character candidate hash>@sha256:<64-character digest>")
    tag, _, digest = match.groups()
    image = image_name(repository, image_override)
    endpoint = f"repos/{repository}"
    metadata = github_json(endpoint)
    if metadata.get("full_name", "").lower() != repository.lower():
        raise ValueError("Repository identity mismatch")
    namespace = f"workers/linux-lvpm/{tag}/manifest.json"
    content = github_json(f"{endpoint}/contents/{namespace}?ref=gh-pages")
    if content.get("type") != "file" or content.get("encoding") != "base64" or content.get("path") != namespace:
        raise ValueError("Candidate is not a manifest in the canonical LVPM Pages namespace")
    manifest = json.loads(base64.b64decode(content["content"]).decode("utf-8"))
    if not isinstance(manifest, dict):
        raise ValueError("Candidate manifest must be an object")
    if (manifest.get("schema") != 1 or manifest.get("installer") != "lvpm"
            or manifest.get("platform") != "linux" or manifest.get("architecture") != "amd64"
            or manifest.get("tag") != tag
            or manifest.get("repository", "").lower() != repository.lower()
            or manifest.get("manifest_path") != namespace
            or manifest.get("tool_revision") != TOOL_REVISION
            or manifest.get("image_digest") != digest
            or manifest.get("image_ref") != f"{image}:{tag}@{digest}"):
        raise ValueError("Candidate manifest identity, fingerprint, image, or digest mismatch")
    plan = check_local_freshness(repo, manifest)
    fingerprint = plan["requirements_fingerprint"]
    identity = manifest.get("workflow_run", {})
    run_id = identity.get("id")
    attempt = identity.get("attempt")
    if (type(run_id) is not int or run_id < 1 or type(attempt) is not int or attempt < 1
            or identity.get("workflow_path") != WORKFLOW
            or not re.fullmatch(r"[0-9a-f]{40}", str(identity.get("head_sha", "")))):
        raise ValueError("Candidate has no canonical build run identity")
    base_digest = manifest.get("base_digest")
    inputs_hash = manifest.get("build_inputs_sha256")
    if (not re.fullmatch(r"sha256:[0-9a-f]{64}", str(base_digest))
            or not re.fullmatch(r"[0-9a-f]{64}", str(inputs_hash))
            or not re.fullmatch(r"nationalinstruments/labview:[a-z0-9.-]+-linux@" + re.escape(base_digest),
                                str(manifest.get("base_image", "")))
            or manifest["base_image"].startswith("nationalinstruments/labview:latest")
            or tag != candidate_tag(fingerprint, base_digest, inputs_hash, run_id, attempt)):
        raise ValueError("Candidate tag does not bind its dependency plan, base, build inputs, and run identity")
    run = github_json(f"{endpoint}/actions/runs/{run_id}")
    workflow = github_json(f"{endpoint}/actions/workflows/build-labview-lvpm-image.yml")
    if (workflow.get("path") != WORKFLOW or type(workflow.get("id")) is not int
            or run.get("workflow_id") != workflow.get("id")
            or run.get("path") != WORKFLOW or run.get("id") != run_id
            or run.get("run_attempt") != attempt or run.get("head_sha") != identity["head_sha"]
            or run.get("head_branch") != metadata.get("default_branch")
            or run.get("event") != "workflow_dispatch" or run.get("status") != "completed"
            or run.get("conclusion") != "success"
            or run.get("repository", {}).get("full_name", "").lower() != repository.lower()
            or run.get("head_repository", {}).get("full_name", "").lower() != repository.lower()):
        raise ValueError("Candidate build is not a successful canonical manual workflow run in this repository")
    verify_registry(image, tag, digest)
    return manifest


def resolve(repo, action, repository, image_override=None):
    container, _ = configuration(repo)
    actions = container.get("actions") or {}
    if not isinstance(actions, dict):
        raise ValueError("config.container.actions must be a mapping")
    linux_selection = container.get("linuxUse") or ""
    if linux_selection and (not isinstance(linux_selection, str) or not SELECTION.fullmatch(linux_selection)):
        raise ValueError("config.container.linuxUse requires a canonical digest-pinned LVPM selection")
    if action == "snapshots" and not linux_selection:
        return ""
    selected = actions.get(action) or linux_selection or container.get("use") or ""
    if not isinstance(selected, str) or (selected and not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.:@/-]*", selected)):
        raise ValueError("Container selection must be a scalar image tag/reference without shell syntax")
    normalized = selected.lower()
    if normalized.startswith(("linux-lvpm", "lvpm-")) or ":linux-lvpm" in normalized or ":lvpm-" in normalized:
        verify(repo, selected, repository, image_override)
    return selected


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for command in ("verify", "resolve"):
        subparser = commands.add_parser(command)
        subparser.add_argument("--repo", required=True, type=Path)
        subparser.add_argument("--repository", required=True)
        subparser.add_argument("--image-name", help="GHCR package name only (no registry, owner, tag, or digest)")
        subparser.add_argument("--candidate" if command == "verify" else "--action", required=True)
    freshness = commands.add_parser("freshness")
    freshness.add_argument("--repo", required=True, type=Path)
    freshness.add_argument("--candidate", required=True)
    freshness.add_argument("--revision", required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "freshness":
            print(json.dumps(local_freshness(args.repo, args.candidate, args.revision, json.load(sys.stdin)), sort_keys=True))
        elif args.command == "verify":
            print(json.dumps(verify(args.repo, args.candidate, args.repository, args.image_name), sort_keys=True))
        else:
            print(resolve(args.repo, args.action, args.repository, args.image_name))
    except (ValueError, KeyError, TypeError, OSError, subprocess.SubprocessError, yaml.YAMLError) as error:
        print(f"LVPM worker verification failed: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
