#!/usr/bin/env python3
"""Translate toolkit declarations and verify a pinned upstream LVPM installation."""

import argparse
import configparser
import hashlib
import io
import json
import os
from pathlib import Path
import platform
import re
import shutil
import sqlite3
import subprocess
import sys
import zipfile
from xml.etree import ElementTree

import yaml

try:
    import tomllib
except ModuleNotFoundError:
    import tomli as tomllib


TOOL_REVISION = "8e8bd7e390b7d4a77558af12cfa52f79079df409"
NAME = r"[A-Za-z0-9_][A-Za-z0-9_.-]*"
VERSION = r"[0-9][A-Za-z0-9_.+-]*"
CORE = ".github/labview/vipm/ci-tooling.vipc"
MAX_SPEC = 8 * 1024 * 1024
MAX_ARCHIVE = 128 * 1024 * 1024
MAX_CLOSURE = 512 * 1024 * 1024
MAX_PACKAGES = 512


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def digest(value):
    return hashlib.sha256(value).hexdigest()


def repository_path(root, relative):
    if not isinstance(relative, str) or "\\" in relative:
        raise ValueError("Dependency paths must be repository-relative POSIX paths")
    path = (root / relative).resolve()
    if Path(relative).is_absolute() or not path.is_relative_to(root.resolve()) or not path.is_file():
        raise ValueError(f"Dependency file is missing or outside the repository: {relative}")
    return path


def container_config(root):
    path = root / ".github/labview-ci.yml"
    data = yaml.safe_load(path.read_text()) if path.exists() else {}
    if not isinstance(data, dict):
        raise ValueError("The pipeline configuration must be a mapping")
    config = data.get("config") or {}
    if not isinstance(config, dict) or not isinstance(config.get("container", {}), dict):
        raise ValueError("config.container must be a mapping")
    return config.get("container") or {}, str(config.get("labviewVersion") or "2026")


def selected_files(root, config, kind):
    if kind in config:
        entries = config[kind]
        if not isinstance(entries, list):
            raise ValueError(f"container.{kind} must be a list")
        selected = []
        for entry in entries:
            if not isinstance(entry, dict) or not isinstance(entry.get("path"), str):
                raise ValueError(f"Invalid container.{kind} declaration")
            platforms = entry.get("monitorOn")
            if isinstance(platforms, str):
                platforms = platforms.split("+")
            if platforms is not None and not isinstance(platforms, list):
                raise ValueError("monitorOn must be a platform list or + separated string")
            if entry.get("monitor", False) is True and (platforms is None or "linux" in platforms):
                selected.append(entry["path"])
        return selected
    skipped = {".git", ".github", "ci-out", "build", "_lvci", ".lv-venv", "__pycache__"}
    return [path.relative_to(root).as_posix() for path in root.rglob(f"*.{kind}")
            if not any(part in skipped for part in path.relative_to(root).parts)]


def package_pin(name, version, source):
    if not re.fullmatch(NAME, name) or not re.fullmatch(VERSION, version):
        raise ValueError(f"LVPM requires an exact package version: {name}={version!r} in {source}")
    return {"name": name.lower(), "version": version, "source_files": [source]}


def declarations(path, source):
    if path.suffix.lower() == ".vipc":
        with zipfile.ZipFile(path) as archive:
            if archive.getinfo("config.xml").file_size > MAX_SPEC:
                raise ValueError(f"Oversized VIPC configuration: {source}")
            tree = ElementTree.fromstring(archive.read("config.xml"))
        result = []
        for package in tree.iter("Package"):
            label = (package.findtext("Name") or "").strip()
            match = re.fullmatch(rf"({NAME})-([0-9]+(?:\.[0-9]+)+(?:[A-Za-z0-9_.+-]*))", label)
            if not match:
                raise ValueError(f"Invalid or unpinned VIPC package {label!r} in {source}")
            result.append(package_pin(*match.groups(), source))
        if not result:
            raise ValueError(f"No packages declared in {source}")
        return result
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    if (data.get("nipm") or {}).get("dependencies"):
        raise ValueError(f"LVPM cannot install NIPM dependencies from {source}; use Linux-native packages")
    project = data.get("project") or {}
    if project.get("vipc"):
        raise ValueError(f"Embedded Dragon VIPC references are not supported: {source}")
    result = []
    for name, value in ((data.get("vipm") or {}).get("dependencies") or {}).items():
        if isinstance(value, dict):
            if set(value) != {"version"}:
                raise ValueError(f"Unsupported Dragon dependency settings for {name} in {source}")
            value = value["version"]
        if not isinstance(value, str):
            raise ValueError(f"Expected a quoted version for {name} in {source}")
        result.append(package_pin(name, value, source))
    return result


def manifest_text(packages, local=False):
    lines = ["[sources]", "defaults = false"]
    if not local:
        lines += ['ni = "https://download.ni.com/evaluation/labview/lvtn/vipm/"',
                  'jki = "https://www.jkisoft.com/packages/"']
    lines += ["", "[dependencies]"]
    lines += [f'{json.dumps(package["name"])} = {json.dumps(package["version"])}'
              for package in packages]
    return "\n".join(lines) + "\n"


def make_plan(root, labview_version=None):
    root = root.resolve()
    config, configured_version = container_config(root)
    year = labview_version or configured_version
    if not re.fullmatch(r"20\d{2}", year):
        raise ValueError("LabVIEW version must be a four-digit year")
    paths = sorted(set([CORE] + selected_files(root, config, "vipc") + selected_files(root, config, "dragon")))
    files = []
    packages = {}
    for source in paths:
        path = repository_path(root, source)
        files.append({"path": source, "sha256": digest(path.read_bytes())})
        for package in declarations(path, source):
            existing = packages.get(package["name"])
            if existing and existing["version"] != package["version"]:
                raise ValueError(f'Conflicting versions of {package["name"]}: {existing["version"]} '
                                 f'({", ".join(existing["source_files"])}) versus {package["version"]} ({source})')
            if existing:
                existing["source_files"].append(source)
            else:
                packages[package["name"]] = package
    plan = {"schema": 1, "installer": "lvpm", "tool_revision": TOOL_REVISION,
            "policy_sha256": digest(Path(__file__).read_bytes()),
            "labview_version": year, "files": files,
            "requirements": sorted(packages.values(), key=lambda package: package["name"])}
    plan["requirements_fingerprint"] = digest(canonical(plan))
    return plan


def run_lvpm(arguments, environment, discovery=False):
    if discovery and ("install" not in arguments or "--dry-run" not in arguments):
        raise ValueError("Package discovery must be read-only")
    result = subprocess.run(["lvpm", *arguments], env=environment, text=True,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=3600)
    print(result.stdout, flush=True)
    rejected = r"FAILED" if discovery else r"unresolved|FAILED|absent from archive"
    if result.returncode or re.search(rejected, result.stdout, re.IGNORECASE):
        raise ValueError("LVPM failed or reported incomplete installation; see the installation log")
    return result.stdout


def resolved_packages(output):
    match = re.search(r"^resolved (\d+) packages?:\n((?:  [^\n]+\n)+)", output, re.MULTILINE)
    if not match:
        raise ValueError("The pinned LVPM install-plan format was not recognized")
    packages = []
    for line in match[2].splitlines():
        entry = re.fullmatch(rf"  ({NAME}) ({VERSION})", line)
        if not entry:
            raise ValueError("Unrecognized package in LVPM resolution plan")
        packages.append({"name": entry[1].lower(), "version": entry[2]})
    if len(packages) != int(match[1]) or len({package["name"] for package in packages}) != len(packages):
        raise ValueError("Incomplete or conflicting LVPM resolution plan")
    return packages


def require_root_pins(packages, plan):
    versions = {package["name"]: package["version"] for package in packages}
    for package in plan["requirements"]:
        if versions.get(package["name"]) != package["version"]:
            raise ValueError(f'Exact declared dependency was not resolved: {package["name"]}={package["version"]}')


def version_key(version):
    if not re.fullmatch(r"\d+(?:[.-]\d+)*", version):
        raise ValueError(f"Unsupported dependency version comparison: {version}")
    components = tuple(int(component) for component in re.split(r"[.-]", version))
    return components + (0,) * max(0, 8 - len(components))


def check_requirements(text, packages):
    for requirement in filter(None, (part.strip() for part in text.split(","))):
        match = re.fullmatch(rf"({NAME})\s*(?:(>=|==|=)\s*({VERSION}))?", requirement)
        if not match:
            raise ValueError(f"Unsupported package dependency constraint: {requirement}")
        name, operator, requested = match.groups()
        installed = packages.get(name.lower())
        if installed is None:
            raise ValueError(f"Missing transitive dependency: {requirement}")
        if requested:
            matches = (version_key(installed) >= version_key(requested) if operator == ">="
                       else version_key(installed) == version_key(requested))
            if not matches:
                raise ValueError(f"Dependency conflict: {requirement}, resolved {installed}")


def spec_text(data):
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        names = [entry for entry in archive.infolist() if entry.filename.lower() == "spec"]
        if len(names) != 1 or names[0].file_size > MAX_SPEC:
            raise ValueError("Package archive has no unique, bounded spec")
        raw = archive.read(names[0])
        try:
            text = raw.decode("utf-8-sig")
        except UnicodeDecodeError:
            text = raw.decode("cp437")
    return text


def read_spec(data):
    parser = configparser.ConfigParser(interpolation=None, strict=False)
    parser.read_string(spec_text(data))
    return parser


def archive_identity(spec):
    metadata = spec["Package"] if spec.has_section("Package") else spec["Package Name"]
    name = metadata.get("Name", "").strip('"').lower()
    version = metadata.get("Version", "").strip('"')
    release = metadata.get("Release", "").strip('"')
    if release:
        version += "-" + release
    package_pin(name, version, "package archive")
    return name, version


def inactive_groups(spec, target):
    excluded = {}
    for section in spec.sections():
        match = re.fullmatch(r"File Group (\d+)", section)
        gate = spec[section].get("Exclusive_LabVIEW_Version", "").strip('"').strip()
        if match and gate:
            if target is None:
                raise ValueError("Conditional file groups require a detected LabVIEW version")
            if not labview_matches(gate, target):
                excluded[int(match[1])] = gate
    return excluded


def normalize_archive(data, target=None):
    if len(data) > MAX_ARCHIVE:
        raise ValueError("Oversized package archive")
    text = spec_text(data)
    spec = read_spec(data)
    excluded = inactive_groups(spec, target)
    groups = sorted(int(match[1]) for section in spec.sections()
                    if (match := re.fullmatch(r"File Group (\d+)", section)))
    if groups and groups != list(range(len(groups))):
        raise ValueError("Package file groups must be contiguous")
    selected = {original: index for index, original in enumerate(group for group in groups if group not in excluded)}
    if excluded:
        lines = []
        section = ""
        for line in text.splitlines(keepends=True):
            header = re.fullmatch(r"\[([^\]]+)\]", line.strip())
            if header:
                section = header[1]
            group = re.fullmatch(r"File Group (\d+)", section)
            if group:
                index = int(group[1])
                if index in excluded:
                    continue
                if header:
                    line = f"[File Group {selected[index]}]\n"
            elif section == "Files" and re.match(r"Num File Groups\s*=", line):
                line = f"Num File Groups={len(selected)}\n"
            lines.append(line)
        text = "".join(lines)
    normalized = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(data)) as original, zipfile.ZipFile(normalized, "w") as output:
        entries = original.infolist()
        if len(entries) > 100000 or sum(entry.file_size for entry in entries) > MAX_CLOSURE:
            raise ValueError("Oversized expanded package archive")
        names = set()
        for entry in entries:
            name = entry.filename.replace("\\", "/")
            group = re.match(r"File Group (\d+)(/.*)?$", name)
            if group and excluded:
                index = int(group[1])
                if index in excluded:
                    continue
                if index not in selected:
                    raise ValueError("Archive contains an undeclared file group")
                name = f"File Group {selected[index]}{group[2] or ''}"
            if (name in names or name.startswith("/") or ":" in name or ".." in name.split("/")
                    or entry.flag_bits & 1 or entry.external_attr >> 16 & 0o170000 == 0o120000):
                raise ValueError(f"Unsafe or duplicate package archive entry: {name}")
            names.add(name)
            content = text.encode("utf-8") if name.lower() == "spec" else original.read(entry)
            entry.filename = name
            output.writestr(entry, content)
    return normalized.getvalue()


def check_platform(spec):
    for section in spec.sections():
        gate = spec[section].get("Exclusive_OS", "ALL").strip('"').strip()
        if gate and not any(token.strip().lower() in ("all", "linux", "linux x64", "linux64")
                            for token in gate.split(",")):
            raise ValueError(f"Unsupported Linux package/file-group OS restriction: {section}: {gate}")


def labview_matches(gate, target):
    accepted = False
    for constraint in gate.split(","):
        match = re.fullmatch(r"(?:LabVIEW)?\s*(>=|<=|==|=|>|<)?\s*(\d+(?:\.\d+)*)", constraint.strip())
        if not match:
            raise ValueError(f"Unsupported LabVIEW version restriction: {gate}")
        requested = version_key(match[2])
        actual = version_key(target)
        operator = match[1] or "="
        accepted = accepted or {">=": actual >= requested, "<=": actual <= requested,
                                "=": actual == requested, "==": actual == requested,
                                ">": actual > requested, "<": actual < requested}[operator]
    return accepted


def check_labview(spec, target):
    for section in spec.sections():
        gate = spec[section].get("Exclusive_LabVIEW_Version", "").strip('"').strip()
        if gate and not labview_matches(gate, target) and not re.fullmatch(r"File Group \d+", section):
            raise ValueError(f"Package/file-group requires {gate}; target LabVIEW is {target}")


def cached_archives(cache, packages, destination, labview_version=None):
    database = cache / "lvpm-cache.db"
    if not database.is_file():
        raise ValueError("LVPM did not populate its package cache")
    connection = sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)
    destination.mkdir(parents=True, exist_ok=True)
    records = []
    originals = {}
    specifications = {}
    total_bytes = 0

    def collect(data, expected_name, expected_version, source=None, depth=0):
        nonlocal total_bytes
        if depth > 8 or len(data) > MAX_ARCHIVE:
            raise ValueError("Oversized or excessively nested package archive")
        sha256 = digest(data)
        spec = read_spec(data)
        name, version = archive_identity(spec)
        if (name, version) != (expected_name, expected_version):
            raise ValueError("Cached or embedded package identity does not match its declaration")
        if name in originals:
            if originals[name]["version"] != version or originals[name]["sha256"] != sha256:
                raise ValueError(f"Conflicting embedded package: {name}")
            return
        total_bytes += len(data)
        if len(originals) >= MAX_PACKAGES or total_bytes > MAX_CLOSURE:
            raise ValueError("Oversized package dependency closure")
        check_platform(spec)
        if labview_version:
            check_labview(spec, labview_version)
        record = {"name": name, "version": version, "sha256": sha256}
        if source:
            record["embedded_source"] = source
        originals[name] = {**record, "data": data}
        specifications[name] = spec
        children = spec.get("Files", "Sub-Packages", fallback="").strip('"')
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            for child in filter(None, (part.strip() for part in children.split(","))):
                if not re.fullmatch(rf"{NAME}-{VERSION}", child):
                    raise ValueError(f"Unsupported embedded package declaration: {child}")
                path = f"Packages/{child}.vip"
                matches = [entry for entry in archive.infolist() if entry.filename.lower() == path.lower()]
                if len(matches) != 1 or matches[0].file_size > MAX_ARCHIVE:
                    raise ValueError(f"Missing, ambiguous, or oversized embedded package: {child}")
                child_data = archive.read(matches[0])
                child_name, child_version = archive_identity(read_spec(child_data))
                if f"{child_name}-{child_version}" != child.lower():
                    raise ValueError(f"Embedded package identity does not match its declaration: {child}")
                collect(child_data, child_name, child_version,
                        {"parent_sha256": sha256, "archive_path": matches[0].filename}, depth + 1)

    try:
        for package in packages:
            rows = connection.execute("SELECT DISTINCT sha256 FROM packages WHERE lower(name)=? AND version=?",
                                      (package["name"], package["version"])).fetchall()
            if len(rows) != 1 or not re.fullmatch(r"[a-f0-9]{64}", rows[0][0]):
                raise ValueError(f'No unambiguous cached archive for {package["name"]}')
            sha256 = rows[0][0]
            data = (cache / "content/sha256" / sha256[:2] / sha256).read_bytes()
            if digest(data) != sha256:
                raise ValueError(f'Corrupt package cache for {package["name"]}')
            collect(data, package["name"], package["version"])
        versions = {name: package["version"] for name, package in originals.items()}
        for name, package in originals.items():
            spec = specifications[name]
            check_requirements(spec.get("Dependencies", "Requires", fallback="").strip('"'), versions)
            data = package["data"]
            normalized = normalize_archive(data, labview_version)
            version = package["version"]
            path = destination / f'{name}-{version}.vip'
            path.write_bytes(normalized)
            sources = destination.parent / "source-packages"
            sources.mkdir(exist_ok=True)
            (sources / path.name).write_bytes(data)
            records.append({key: value for key, value in package.items() if key != "data"}
                           | {"install_sha256": digest(normalized),
                              "inactive_file_groups": inactive_groups(spec, labview_version)})
    finally:
        connection.close()
    return records


def verify_inventory(plan, records, installed):
    expected = {package["name"]: package for package in records}
    actual = {}
    for path in installed.glob("*.json"):
        package = json.loads(path.read_text())
        name = package.get("name", "").lower()
        if name not in expected:
            raise ValueError(f"Unexpected package installed: {name}")
        if package.get("version") != expected[name]["version"]:
            raise ValueError(f"Installed version mismatch: {name}")
        files = package.get("files")
        if not isinstance(files, list) or not files or not all(Path(file).is_file() for file in files):
            raise ValueError(f"Required installed files are missing: {name}")
        if package.get("skipped_hooks") or package.get("skipped_groups") or not package.get("relinked"):
            raise ValueError(f"Package hooks, groups, or relinking are incomplete: {name}")
        actual[name] = {**expected[name], "files": files}
    if actual.keys() != expected.keys():
        raise ValueError("The installed inventory does not match the resolved dependency closure")
    for package in plan["requirements"]:
        if actual.get(package["name"], {}).get("version") != package["version"]:
            raise ValueError(f'Declared dependency was not satisfied: {package["name"]}')
    return sorted(actual.values(), key=lambda package: package["name"])


def supported_architecture():
    if platform.system() != "Linux" or platform.machine().lower() not in ("x86_64", "amd64"):
        raise ValueError("LVPM candidate installation requires Linux x86-64 (linux/amd64)")
    return "amd64"


def install(plan_path, output):
    architecture = supported_architecture()
    plan = json.loads(plan_path.read_text())
    fingerprint = plan.pop("requirements_fingerprint")
    if digest(canonical(plan)) != fingerprint or plan.get("tool_revision") != TOOL_REVISION:
        raise ValueError("Unrecognized or modified dependency plan")
    plan["requirements_fingerprint"] = fingerprint
    output.mkdir(parents=True, exist_ok=True)
    environment = {**os.environ, "LV_RTE_HEADLESS": "1", "LVPM_CONFIG_CACHE": str(output / "cache")}
    initial = output / "lvpm.toml"
    initial.write_text(manifest_text(plan["requirements"]))
    flags = ["--global", "--labview-version", plan["labview_version"]]
    targets = run_lvpm(["targets"], environment)
    target = re.search(rf'LabVIEW {plan["labview_version"]} \(64-bit\)\s+v(\d+\.\d+)', targets)
    if not target:
        raise ValueError("The requested 64-bit LabVIEW target was not detected")
    roots = run_lvpm([*flags, "install", "--manifest", str(initial), "--no-deps", "--dry-run"], environment, discovery=True)
    root_packages = resolved_packages(roots)
    require_root_pins(root_packages, plan)
    resolution = run_lvpm([*flags, "install", "--manifest", str(initial), "--dry-run"], environment, discovery=True)
    discovered = {package["name"]: package for package in resolved_packages(resolution)}
    discovered.update({package["name"]: package for package in root_packages})
    packages = list(discovered.values())
    records = cached_archives(output / "cache", packages, output / "packages", target[1])
    require_root_pins(records, plan)
    frozen = output / "frozen.toml"
    frozen.write_text(manifest_text(records, local=True))
    environment["LVPM_CONFIG_SOURCES_LOCAL"] = str(output / "packages")
    verified = run_lvpm([*flags, "install", "--manifest", str(frozen), "--no-deps", "--dry-run"], environment)
    expected = {package["name"]: package["version"] for package in records}
    if {package["name"]: package["version"] for package in resolved_packages(verified)} != expected:
        raise ValueError("The local install plan does not match the complete verified package closure")
    run_lvpm([*flags, "install", "--manifest", str(frozen), "--no-deps", "--hooks", "--relink"], environment)
    inventory = verify_inventory(plan, records, Path(f'/var/lib/lvpm/LabVIEW-{plan["labview_version"]}-64bit/installed'))
    report = {"schema": 1, "installer": "lvpm", "tool_revision": TOOL_REVISION,
              "platform": "linux", "architecture": architecture,
              "requirements_fingerprint": fingerprint, "labview_version": plan["labview_version"],
              "packages": inventory, "verification": {"status": "passed", "scope": "package-files",
              "activity_checks": "not_run", "hook_error_output": "not_observable_by_upstream"}}
    (output / "inventory.json").write_text(json.dumps(report, indent=2) + "\n")
    if plan_path.resolve() != (output / "requirements.json").resolve():
        shutil.copyfile(plan_path, output / "requirements.json")
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    plan = commands.add_parser("plan")
    plan.add_argument("--repo", type=Path, default=Path("."))
    plan.add_argument("--labview-version")
    plan.add_argument("--out", type=Path, required=True)
    apply = commands.add_parser("install")
    apply.add_argument("--plan", type=Path, required=True)
    apply.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    try:
        if args.command == "plan":
            result = make_plan(args.repo, args.labview_version)
            args.out.mkdir(parents=True, exist_ok=True)
            (args.out / "requirements.json").write_text(json.dumps(result, indent=2) + "\n")
            (args.out / "lvpm.toml").write_text(manifest_text(result["requirements"]))
        else:
            result = install(args.plan, args.out)
        print(json.dumps(result))
    except (ValueError, OSError, KeyError, zipfile.BadZipFile, ElementTree.ParseError,
            configparser.Error, subprocess.TimeoutExpired) as error:
        print(f"LVPM dependency verification failed: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
