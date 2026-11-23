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
    selected = sorted(set(policy.selected_files(root, config, "vipc")
                          + policy.selected_files(root, config, "dragon")))
    sources = []
    requirements = {}
    for relative in selected:
        path = safe_path(root, relative)
        if path.suffix.lower() not in (".vipc", ".dragon"):
            raise ValueError("Selected dependency must have the .vipc or .dragon extension")
        if path.suffix.lower() == ".dragon":
            dragon = policy.tomllib.loads(path.read_text(encoding="utf-8"))
            target = (dragon.get("project") or {}).get("labview-version")
            if target is not None and str(target) != year:
                raise ValueError(f"Dragon LabVIEW target differs from configured year: {relative}")
            if (dragon.get("vipm") or {}).get("feeds"):
                raise ValueError(f"Custom Dragon VIPM feeds are not supported: {relative}")
        packages = policy.declarations(path, relative)
        for package in packages:
            previous = requirements.setdefault(package["name"], package["version"])
            if previous != package["version"]:
                raise ValueError(f"Conflicting dependency versions: {package['name']}")
        sources.append({"path": relative, "sha256": policy.digest(path.read_bytes()),
                        "staged_name": "project-" + policy.digest(relative.encode()) + ".vipc",
                        "requirements": packages})
    destination.mkdir(parents=True, exist_ok=True)
    for old in destination.glob("project-*.vipc"):
        old.unlink()
    for source in sources:
        original = safe_path(root, source["path"])
        staged = destination / source["staged_name"]
        if policy.digest(original.read_bytes()) != source["sha256"]:
            raise ValueError("Dependency bytes changed during staging")
        if original.suffix.lower() == ".dragon":
            tree = ElementTree.Element("VI_Package_Configuration")
            for package in source["requirements"]:
                item = ElementTree.SubElement(tree, "Package")
                ElementTree.SubElement(item, "Name").text = package["name"] + "-" + package["version"]
            with zipfile.ZipFile(staged, "w") as archive:
                # Fixed metadata keeps the generated build input reproducible.
                archive.writestr(zipfile.ZipInfo("config.xml"), ElementTree.tostring(tree, encoding="utf-8"))
        else:
            shutil.copyfile(original, staged)
        source["staged_sha256"] = policy.digest(staged.read_bytes())
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


def vipc_specs(path):
    with zipfile.ZipFile(path) as archive:
        tree = ElementTree.fromstring(archive.read("config.xml"))
    packages = list(tree.iter("Package"))
    if not packages:
        raise ValueError(f"No declared project packages: {path.name}")
    result = []
    for package in packages:
        label = (package.findtext("Name") or "").strip()
        match = re.fullmatch(r"([A-Za-z0-9_][A-Za-z0-9_.-]*)-([0-9]+(?:\.[0-9]+)+(?:[A-Za-z0-9_.+-]*))", label)
        if not match:
            raise ValueError(f"Invalid or unpinned VIPC package: {label}")
        result.append((match[1].lower(), match[2]))
    return result


def verify_project_pins(value, directory):
    installed = {package["name"]: package["version"] for package in value["packages"]}
    # Project pins override the tooling baseline, but every effective requirement
    # must be present, including baseline-only packages.
    required = {}
    for path in sorted(directory.glob("*.vipc"), key=lambda p: (p.name != "ci-tooling.vipc", p.name)):
        required.update(vipc_specs(path))
    for name, version in required.items():
        if installed.get(name) != version:
            raise ValueError(f"Selected package was not observed exactly: {name}-{version}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=("stage", "plan", "observe", "validate", "specs"))
    parser.add_argument("--vipc", type=Path)
    parser.add_argument("--root", type=Path)
    parser.add_argument("--destination", type=Path)
    parser.add_argument("--dependency-sha")
    parser.add_argument("--year", default="2026")
    parser.add_argument("--required", action="store_true")
    parser.add_argument("--failed", action="store_true")
    parser.add_argument("--raw", type=Path)
    parser.add_argument("--vipc-dir", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.mode == "specs":
        if not args.vipc:
            parser.error("specs requires --vipc")
        for name, version in vipc_specs(args.vipc):
            print(name + "@" + version)
        return
    if not args.output:
        parser.error("--output is required")
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
