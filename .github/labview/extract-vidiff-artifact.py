#!/usr/bin/env python3

import os
from pathlib import Path
import stat
import sys
import zipfile


def fail(message: str) -> None:
    raise ValueError(message)


def is_symlink(entry: zipfile.ZipInfo) -> bool:
    mode = (entry.external_attr >> 16) & 0o177777
    return stat.S_IFMT(mode) == stat.S_IFLNK


def target_path(destination: Path, name: str) -> Path:
    text = str(name or "")
    if text == "":
        fail("ZIP contains an empty path")
    normalized = text.replace("\\", "/")
    if normalized.startswith("/"):
        fail(f"ZIP entry uses an absolute path: {text}")
    if len(normalized) >= 3 and normalized[1] == ":" and normalized[2] == "/":
        fail(f"ZIP entry uses a drive-absolute path: {text}")
    parts = [part for part in normalized.split("/") if part not in {"", "."}]
    if any(part == ".." for part in parts):
        fail(f"ZIP entry escapes destination with parent traversal: {text}")
    destination_resolved = destination.resolve()
    target = destination_resolved.joinpath(*parts)
    target_resolved = target.resolve()
    try:
        target_resolved.relative_to(destination_resolved)
    except ValueError:
        fail(f"ZIP entry resolves outside destination: {text}")
    return target_resolved


def extract_archive(archive: Path, destination: Path) -> None:
    destination.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(archive) as zipped:
        for entry in zipped.infolist():
            if is_symlink(entry):
                fail(f"ZIP entry is a symlink and is not allowed: {entry.filename}")
            target = target_path(destination, entry.filename)
            if entry.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with zipped.open(entry, "r") as source, open(target, "wb") as sink:
                while True:
                    chunk = source.read(1024 * 1024)
                    if not chunk:
                        break
                    sink.write(chunk)


def main() -> int:
    if len(sys.argv) != 3:
        print("usage: extract-vidiff-artifact.py <artifact.zip> <destination-dir>", file=sys.stderr)
        return 2
    archive = Path(sys.argv[1])
    destination = Path(sys.argv[2])
    if not archive.is_file():
        fail(f"Missing ZIP archive: {archive}")
    extract_archive(archive, destination)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1)
