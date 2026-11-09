#!/usr/bin/env python3
"""Stage exact Linux dependency sources and record target-bound VIPM evidence."""

import argparse
import importlib.util
import json
from pathlib import Path, PurePosixPath
import re
import shutil
from xml.etree import ElementTree
import zipfile


def adapter():
    location = Path(__file__).resolve().parents[1] / "lvpm/adapter.py"
    spec = importlib.util.spec_from_file_location("linux_dependency_adapter", location)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def safe_path(root, relative):
    if (not isinstance(relative, str) or not relative or "\\" in relative
            or any(ord(character) < 32 for character in relative)
            or PurePosixPath(relative).is_absolute()
            or any(part in ("", ".", "..") for part in relative.split("/"))):
        raise ValueError(f"Unsafe dependency path: {relative!r}")
    path = root
    for part in relative.split("/"):
        path = path / part
        if path.is_symlink():
            raise ValueError(f"Dependency symlink is forbidden: {relative}")
    if not path.is_file() or not path.resolve().is_relative_to(root):
        raise ValueError(f"Missing dependency file: {relative}")
    return path


def stage(root, destination, revision):
    if not re.fullmatch(r"[0-9a-fA-F]{40}", revision):
        raise ValueError("Dependency revision must be a full SHA")
    root = root.resolve()
    policy = adapter()
    safe_path(root, ".github/labview-ci.yml")
    config, year = policy.container_config(root)
    selected = sorted(set(policy.selected_files(root, config, "vipc")))
    dragons = policy.selected_files(root, config, "dragon")
    if dragons:
        raise ValueError("Native Linux VIPM cannot apply Dragon sources; select the LVPM worker")
    sources = []
    requirements = {}
    for relative in selected:
        path = safe_path(root, relative)
        if path.suffix.lower() != ".vipc":
            raise ValueError("Selected VIPC must have the .vipc extension")
        for package in policy.declarations(path, relative):
            previous = requirements.setdefault(package["name"], package["version"])
            if previous != package["version"]:
                raise ValueError(f"Conflicting dependency versions: {package['name']}")
        sources.append({"path": relative, "sha256": policy.digest(path.read_bytes()),
                        "staged_name": "project-" + policy.digest(relative.encode()) + ".vipc"})
    destination.mkdir(parents=True, exist_ok=True)
    for old in destination.glob("project-*.vipc"):
        old.unlink()
    for source in sources:
        shutil.copyfile(safe_path(root, source["path"]), destination / source["staged_name"])
        if policy.digest((destination / source["staged_name"]).read_bytes()) != source["sha256"]:
            raise ValueError("Dependency bytes changed during staging")
    config_path = safe_path(root, ".github/labview-ci.yml")
    return {"schema": 1, "dependency_sha": revision.lower(), "labview_version": year,
            "config_sha256": policy.digest(config_path.read_bytes()), "files": sources}


def safe_plan(root):
    root = root.resolve()
    policy = adapter()
    safe_path(root, ".github/labview-ci.yml")
    config, year = policy.container_config(root)
    selected = [policy.CORE] + policy.selected_files(root, config, "vipc") + policy.selected_files(root, config, "dragon")
    for relative in selected:
        safe_path(root, relative)
    plan = policy.make_plan(root, year)
    for source in plan["files"]:
        if policy.digest(safe_path(root, source["path"]).read_bytes()) != source["sha256"]:
            raise ValueError("Dependency bytes changed during planning")
    return plan


def observation(raw, year, required, succeeded=True):
    targets = re.findall(r"^[ \t]*(?:Using )?LabVIEW[ \t]+(20\d{2})[ \t]+\((32|64)-bit\)[ \t\r]*$",
                         raw, re.MULTILINE)
    verified = bool(succeeded and targets and set(targets) == {(year, "64")})
    packages = {}
    if verified:
        for line in raw.splitlines():
            match = re.fullmatch(r"\s*(?:[^()]+\()?([A-Za-z0-9_][A-Za-z0-9_.-]*)\s+v?([0-9][A-Za-z0-9_.+-]*)\)?\s*", line)
            if match:
                name, version = match.groups()
                name = name.lower()
                if name in packages and packages[name] != version:
                    raise ValueError("Conflicting installed package versions")
                packages[name] = version
    observed = verified and bool(packages)
    return {"schema": 1, "platform": "linux", "labview_version": year,
            "labview_bitness": "64", "source": "vipm list --installed",
            "target_verified": bool(observed), "required_project": required,
            "status": "observed" if observed else "unavailable", "raw": raw,
            "packages": [{"name": name, "version": version} for name, version in sorted(packages.items())]
            if observed else []}


def validate(value, year, required):
    expected = observation(value.get("raw", ""), year, required,
                           value.get("status") == "observed")
    if value != expected:
        raise ValueError("Inventory does not match target-bound CLI evidence")
    if required and (value["status"] != "observed" or not value["target_verified"]):
        raise ValueError("Project inventory requires a verified LabVIEW target")


def verify_project_pins(value, directory):
    installed = {package["name"]: package["version"] for package in value["packages"]}
    for path in directory.glob("project-*.vipc"):
        with zipfile.ZipFile(path) as archive:
            tree = ElementTree.fromstring(archive.read("config.xml"))
        packages = list(tree.iter("Package"))
        if not packages:
            raise ValueError(f"No declared project packages: {path.name}")
        for package in packages:
            label = (package.findtext("Name") or "").strip()
            match = re.fullmatch(r"([A-Za-z0-9_][A-Za-z0-9_.-]*)-([0-9]+(?:\.[0-9]+)+(?:[A-Za-z0-9_.+-]*))", label)
            if not match or installed.get(match[1].lower()) != match[2]:
                raise ValueError(f"Selected project package was not observed exactly: {label}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=("stage", "plan", "observe", "validate"))
    parser.add_argument("--root", type=Path)
    parser.add_argument("--destination", type=Path)
    parser.add_argument("--dependency-sha")
    parser.add_argument("--year", default="2026")
    parser.add_argument("--required", action="store_true")
    parser.add_argument("--failed", action="store_true")
    parser.add_argument("--raw", type=Path)
    parser.add_argument("--vipc-dir", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.mode == "validate":
        value = json.loads(args.output.read_text(encoding="utf-8-sig"))
        validate(value, args.year, args.required)
        if args.required and args.vipc_dir:
            verify_project_pins(value, args.vipc_dir)
        return
    if args.mode == "stage":
        value = stage(args.root, args.destination, args.dependency_sha)
    elif args.mode == "plan":
        value = safe_plan(args.root)
    else:
        value = observation(args.raw.read_text(encoding="utf-8-sig"), args.year,
                            args.required, not args.failed)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")
    if args.mode == "observe":
        validate(value, args.year, args.required)
        if args.required and args.vipc_dir:
            verify_project_pins(value, args.vipc_dir)


if __name__ == "__main__":
    main()