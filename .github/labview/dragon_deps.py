#!/usr/bin/env python3
"""Parse JKI Dragon (``.dragon``) dependency files into a normalized model.

A ``.dragon`` file is plain-text TOML (NOT JSON or a binary container), so it can
be read statically on any platform without Dragon, VIPM, NIPM, or LabVIEW. JKI
documents these primary tables::

    [project]
    [nipm.feeds]
    [nipm.dependencies]
    [vipm.dependencies]

Dependency values are either a bare version string::

    oglib_error = "6.0.0.26"

or an inline TOML table::

    oglib_array = { version = "6.0.1.20" }
    ni-daqmx = { version = "23.8.0", feed = "daqmx-feed" }

The parser is intentionally lenient (real files from newer Dragon versions differ
slightly from JKI's documented examples):

* accepts both ``labview-version`` and ``labview_version``
* accepts an integer or string LabVIEW year
* accepts string and inline-table dependency values
* tolerates empty ``[vipm]`` / ``[nipm]`` parent tables
* ignores unknown / future fields instead of rejecting the file

An actual TOML library is used (``tomllib`` on Python 3.11+, falling back to the
third-party ``tomli``); regular expressions are deliberately NOT used. When no
TOML library is available the module still imports, but parsing raises
``DragonParseError`` so callers can degrade gracefully.

The normalized dependency model is::

    {
      "manager": "vipm",                 # "vipm" | "nipm"
      "package_id": "oglib_array",
      "declared_version": "6.0.1.20",
      "feed_name": null,                 # str | None (NIPM named feed)
      "source_file": "example/Example.dragon"
    }
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import sys
from pathlib import Path

_TOML_BACKEND = None
try:  # Python 3.11+
    import tomllib as _toml  # type: ignore

    _TOML_BACKEND = "tomllib"
except ModuleNotFoundError:  # pragma: no cover - exercised on older runners
    try:
        import tomli as _toml  # type: ignore

        _TOML_BACKEND = "tomli"
    except ModuleNotFoundError:
        _toml = None  # type: ignore


class DragonParseError(Exception):
    """Raised when a ``.dragon`` file cannot be parsed."""


MANAGERS = ("vipm", "nipm")


def toml_available() -> bool:
    """True when a TOML backend is importable."""
    return _toml is not None


def _loads(text: str) -> dict:
    if _toml is None:
        raise DragonParseError(
            "no TOML library available (need Python 3.11+ tomllib or the 'tomli' package)"
        )
    try:
        return _toml.loads(text)
    except Exception as exc:  # tomllib.TOMLDecodeError and friends
        raise DragonParseError(f"invalid TOML: {exc}") from exc


def _normalize_value(val):
    """A dependency value is a version string or an inline table.

    Returns ``(declared_version, feed_name)``. Unknown shapes are stringified so
    a surprising-but-valid TOML value never aborts the whole file.
    """
    if isinstance(val, str):
        return val.strip(), None
    if isinstance(val, dict):
        ver = val.get("version")
        ver = str(ver).strip() if ver is not None else ""
        feed = val.get("feed")
        feed = str(feed).strip() if feed not in (None, "") else None
        return ver, feed
    if isinstance(val, bool):
        return str(val).lower(), None
    return str(val).strip(), None


def parse_dragon_text(text: str, source_file: str = "") -> dict:
    """Parse ``.dragon`` TOML text into the normalized model.

    Raises :class:`DragonParseError` on malformed TOML or a missing TOML backend.
    """
    data = _loads(text)
    if not isinstance(data, dict):
        raise DragonParseError("top-level TOML is not a table")

    project = data.get("project")
    project = project if isinstance(project, dict) else {}
    lv = project.get("labview-version", project.get("labview_version"))
    if lv is not None:
        lv = str(lv).strip()

    nipm = data.get("nipm")
    nipm = nipm if isinstance(nipm, dict) else {}
    vipm = data.get("vipm")
    vipm = vipm if isinstance(vipm, dict) else {}

    feeds: dict[str, str] = {}
    nipm_feeds = nipm.get("feeds")
    if isinstance(nipm_feeds, dict):
        for name, url in nipm_feeds.items():
            feeds[str(name)] = str(url)

    deps: list[dict] = []
    for manager, block in (("vipm", vipm), ("nipm", nipm)):
        table = block.get("dependencies") if isinstance(block, dict) else None
        if not isinstance(table, dict):
            continue
        for pkg, val in table.items():
            version, feed = _normalize_value(val)
            deps.append(
                {
                    "manager": manager,
                    "package_id": str(pkg),
                    "declared_version": version,
                    "feed_name": feed,
                    "source_file": source_file,
                }
            )
    deps.sort(key=lambda d: (d["manager"], d["package_id"].lower()))

    return {
        "source_file": source_file,
        "labview_version": lv,
        "feeds": feeds,
        "dependencies": deps,
    }


def parse_dragon_file(path: str) -> dict:
    """Parse a ``.dragon`` file from disk into the normalized model."""
    with open(path, "r", encoding="utf-8") as fh:
        return parse_dragon_text(fh.read(), source_file=path.replace("\\", "/"))

def windows_install_plan(
    paths: list[str], repository_root: str | None = None,
    target_year: str = "2026", target_bitness: str = "64",
) -> dict:
    """Validate the supported Windows subset and bind exact specs to source bytes."""
    if not re.fullmatch(r"\d{4}", target_year) or target_bitness not in ("32", "64"):
        raise DragonParseError("invalid Windows Dragon target LabVIEW year/bitness")
    parsed = []
    files = []
    staged_names = set()
    for path in sorted(paths, key=str.casefold):
        source = Path(path)
        name = source.name
        if repository_root is not None:
            root = Path(repository_root).absolute()
            try:
                source.resolve().relative_to(root.resolve())
                relative = source.absolute().relative_to(root)
            except ValueError as exc:
                raise DragonParseError(f"{path}: selected Dragon source is outside the repository") from exc
            if ".." in relative.parts:
                raise DragonParseError(f"{path}: parent traversal in selected Dragon source")
            current = root
            for component in relative.parts:
                current = current / component
                if current.is_symlink() or (
                    current.exists() and
                    getattr(current.lstat(), "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT
                ):
                    raise DragonParseError(f"{path}: symlink/junction Dragon sources are unsupported")
        if source.is_symlink() or not source.is_file():
            raise DragonParseError(f"{path}: selected Dragon source must be a regular file")
        if not name.lower().endswith(".dragon") or name.casefold() in staged_names:
            raise DragonParseError(f"{path}: duplicate or invalid staged Dragon filename")
        staged_names.add(name.casefold())
        content = source.read_bytes()
        try:
            text = content.decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            raise DragonParseError(f"{path}: Dragon source must be UTF-8") from exc
        data = _loads(text)
        if set(data) - {"project", "vipm", "nipm"}:
            raise DragonParseError(f"{path}: unsupported Windows Dragon table")
        for key in ("project", "vipm", "nipm"):
            if key in data and not isinstance(data[key], dict):
                raise DragonParseError(f"{path}: {key} must be a TOML table")
        project = data.get("project", {})
        if set(project) - {"labview-version", "labview_version", "vipc"}:
            raise DragonParseError(f"{path}: unsupported Windows Dragon project setting")
        if project.get("vipc", "") != "":
            raise DragonParseError(f"{path}: project.vipc is unsupported; select the VIPC separately")
        year_values = [str(project[key]) for key in ("labview-version", "labview_version") if key in project]
        if any(not re.fullmatch(r"\d{4}", value) for value in year_values) or len(set(year_values)) > 1:
            raise DragonParseError(f"{path}: invalid or conflicting LabVIEW year")
        if year_values and year_values[0] != target_year:
            raise DragonParseError(f"{path}: declared LabVIEW year differs from Windows target {target_year}")
        nipm = data.get("nipm", {})
        if set(nipm) - {"feeds", "dependencies"} or any(
            not isinstance(value, dict) or value for value in nipm.values()
        ):
            raise DragonParseError(
                f"{path}: NIPM dependencies/feeds are not supported by the Windows Dragon VIPM installer"
            )
        vipm = data.get("vipm", {})
        if set(vipm) - {"dependencies"} or not isinstance(vipm.get("dependencies", {}), dict):
            raise DragonParseError(f"{path}: unsupported Windows VIPM setting")
        specs = []
        for package, declaration in vipm.get("dependencies", {}).items():
            if not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.-]*", package):
                raise DragonParseError(f"{path}: invalid VIPM package ID {package!r}")
            if isinstance(declaration, dict):
                if set(declaration) != {"version"}:
                    raise DragonParseError(f"{path}: unsupported VIPM declaration for {package}")
                version = declaration["version"]
            else:
                version = declaration
            if not isinstance(version, str) or not re.fullmatch(r"\d+(?:\.\d+)+(?:-\d+)?", version):
                raise DragonParseError(f"{path}: {package} requires an exact numeric VIPM version")
            specs.append(f"{package}@{version}")
        source_file = relative.as_posix() if repository_root is not None else path.replace("\\", "/")
        model = parse_dragon_text(text, source_file=source_file)
        parsed.append(model)
        files.append({
            "path": name,
            "source_file": model["source_file"],
            "sha256": hashlib.sha256(content).hexdigest(),
            "labview_version": year_values[0] if year_values else "",
            "specs": sorted(specs, key=str.casefold),
        })
    conflicts = preflight(parsed)["conflicts"]
    if conflicts:
        raise DragonParseError("conflicting Windows Dragon declarations: " + json.dumps(conflicts))
    return {
        "schema": 1,
        "platform": "windows",
        "labview_version": target_year,
        "labview_bitness": target_bitness,
        "files": files,
        "specs": sorted({spec for item in files for spec in item["specs"]}, key=str.casefold),
    }


def windows_monitored_paths(config_path: str, kind: str) -> tuple[list[str], bool]:
    """Read Windows monitor selections using safe YAML, preserving explicit opt-outs."""
    import yaml

    path = Path(config_path)
    if not path.exists():
        return [], False
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise DragonParseError("dependency monitor configuration must be a YAML mapping")
    config = data.get("config", data)
    if not isinstance(config, dict):
        raise DragonParseError("config must be a YAML mapping")
    container = config.get("container", {})
    if not isinstance(container, dict):
        raise DragonParseError("config.container must be a YAML mapping")
    if kind not in container:
        return [], False
    entries = container[kind]
    if not isinstance(entries, list):
        raise DragonParseError(f"config.container.{kind} must be a YAML list (use [] to opt out)")
    selected = []
    for entry in entries:
        if not isinstance(entry, dict) or not isinstance(entry.get("path"), str) or not entry["path"].strip():
            raise DragonParseError(f"config.container.{kind} entries require a nonempty string path")
        monitor = entry.get("monitor", False)
        if not isinstance(monitor, bool):
            raise DragonParseError(f"{entry['path']}: monitor must be a YAML boolean, not a string")
        platforms = entry.get("monitorOn", ["windows", "linux"])
        if isinstance(platforms, str):
            platforms = platforms.split("+")
        if not isinstance(platforms, list) or any(
            not isinstance(value, str) or value.strip().lower() not in ("windows", "linux")
            for value in platforms
        ):
            raise DragonParseError(f"{entry['path']}: monitorOn must name windows/linux as a string or list")
        if monitor and "windows" in [value.strip().lower() for value in platforms]:
            selected.append(entry["path"].replace("\\", "/"))
    return selected, True


def stage_windows_sources(
    paths: list[str], destination: str, repository_root: str,
    target_year: str = "2026", target_bitness: str = "64",
) -> dict:
    """Stage selected sources and their validated plan as one closed worker input."""
    plan = windows_install_plan(paths, repository_root, target_year, target_bitness)
    contents = {}
    for source, item in zip(sorted(paths, key=str.casefold), plan["files"]):
        content = Path(source).read_bytes()
        if hashlib.sha256(content).hexdigest() != item["sha256"]:
            raise DragonParseError(f"{source}: source changed while staging")
        contents[item["path"]] = content
    dest = Path(destination)
    if dest.is_symlink():
        raise DragonParseError("Dragon staging directory must not be a symlink")
    dest.mkdir(parents=True, exist_ok=True)
    for previous in dest.rglob("*"):
        if previous.suffix.lower() == ".dragon" and (previous.is_file() or previous.is_symlink()):
            previous.unlink()
    for name, content in contents.items():
        (dest / name).write_bytes(content)
    (dest / "dragon-plan.json").write_text(
        json.dumps(plan, indent=2, ensure_ascii=True) + "\n", encoding="utf-8"
    )
    return plan


def discover_dragon_files(root: str = ".") -> list:
    """Return all ``*.dragon`` files under ``root`` (deterministic lexical order)."""
    skip = {".git", "ci-out", "build", "_lvci", "__pycache__", "node_modules"}
    out = []
    for current, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if d not in skip]
        for name in files:
            if name.lower().endswith(".dragon"):
                path = os.path.join(current, name).replace("\\", "/")
                if path.startswith("./"):
                    path = path[2:]
                out.append(path)
    return sorted(out, key=str.lower)


def preflight(parsed_files: list) -> dict:
    """Build a deduplicated install set and detect cross-file conflicts.

    Returns ``{"install_set": [...], "conflicts": [...]}`` where each install-set
    entry is a normalized dependency augmented with ``source_files`` (every file
    that declares it) and conflicts flag: same package/different version, same
    NIPM feed name/different URL, and differing declared LabVIEW years.
    """
    # (manager, pkg_lower) -> version -> {source files}
    by_key: dict = {}
    canonical: dict = {}  # (manager, pkg_lower) -> a representative dep dict
    feed_urls: dict = {}  # feed name -> url -> {source files}
    lv_years: dict = {}   # year -> {source files}

    for pf in parsed_files:
        year = pf.get("labview_version")
        if year:
            lv_years.setdefault(year, set()).add(pf.get("source_file", ""))
        for name, url in (pf.get("feeds") or {}).items():
            feed_urls.setdefault(name, {}).setdefault(url, set()).add(pf.get("source_file", ""))
        for dep in pf.get("dependencies") or []:
            key = (dep["manager"], dep["package_id"].lower())
            by_key.setdefault(key, {}).setdefault(dep["declared_version"], set()).add(
                dep["source_file"]
            )
            canonical.setdefault(key, dep)

    conflicts = []
    install_set = []
    for key in sorted(by_key, key=lambda k: (k[0], k[1])):
        manager, pkg_lower = key
        versions = by_key[key]
        rep = canonical[key]
        sources = sorted({f for fs in versions.values() for f in fs})
        is_conflict = len(versions) > 1
        if is_conflict:
            conflicts.append(
                {
                    "type": "version",
                    "manager": manager,
                    "package_id": rep["package_id"],
                    "versions": sorted(versions.keys()),
                    "source_files": sources,
                }
            )
        install_set.append(
            {
                "manager": manager,
                "package_id": rep["package_id"],
                "declared_version": sorted(versions.keys())[0],
                "declared_versions": sorted(versions.keys()),
                "feed_name": rep.get("feed_name"),
                "source_files": sources,
                "conflict": is_conflict,
            }
        )

    for name in sorted(feed_urls):
        urls = feed_urls[name]
        if len(urls) > 1:
            conflicts.append(
                {
                    "type": "feed",
                    "feed_name": name,
                    "urls": sorted(urls.keys()),
                    "source_files": sorted({f for fs in urls.values() for f in fs}),
                }
            )

    if len(lv_years) > 1:
        conflicts.append(
            {
                "type": "labview_version",
                "years": sorted(lv_years.keys()),
                "source_files": sorted({f for fs in lv_years.values() for f in fs}),
            }
        )

    return {"install_set": install_set, "conflicts": conflicts}


def build_inventory(root: str = ".") -> dict:
    """Discover, parse, and preflight every ``.dragon`` file under ``root``.

    A file that fails to parse is recorded with an ``error`` instead of aborting
    the whole inventory.
    """
    files = []
    for path in discover_dragon_files(root):
        try:
            files.append(parse_dragon_file(path))
        except DragonParseError as exc:
            files.append(
                {
                    "source_file": path.replace("\\", "/"),
                    "labview_version": None,
                    "feeds": {},
                    "dependencies": [],
                    "error": str(exc),
                }
            )
    pre = preflight([f for f in files if not f.get("error")])
    return {
        "files": files,
        "install_set": pre["install_set"],
        "conflicts": pre["conflicts"],
        "toml_backend": _TOML_BACKEND,
    }


def _main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Parse repository .dragon files.")
    parser.add_argument(
        "--root", default=".", help="Repository root to scan for *.dragon files."
    )
    parser.add_argument(
        "--out", default="", help="Write the inventory JSON here (default: stdout)."
    )
    parser.add_argument("--windows-stage", default="", help="Stage discovered Windows Dragon sources and plan here.")
    parser.add_argument("--year", default="2026", help="Requested Windows LabVIEW year.")
    parser.add_argument("--bitness", default="64", choices=("32", "64"))
    args = parser.parse_args(argv)

    if not toml_available():
        sys.stderr.write(
            "dragon_deps: no TOML backend (need Python 3.11+ or 'pip install tomli')\n"
        )
        return 2

    if args.windows_stage:
        root = Path(args.root).absolute()
        paths = sorted(
            str(path) for path in root.rglob("*")
            if path.suffix.lower() == ".dragon" and ".github" not in path.relative_to(root).parts
        )
        inventory = stage_windows_sources(paths, args.windows_stage, str(root), args.year, args.bitness)
    else:
        inventory = build_inventory(args.root)
    text = json.dumps(inventory, indent=2, ensure_ascii=True)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            fh.write(text + "\n")
    else:
        print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
