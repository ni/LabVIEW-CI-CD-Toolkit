#!/usr/bin/env python3
"""Merge independently-uploaded snapshot artifacts into the Pages source tree."""

from __future__ import annotations

import argparse
import shutil
from pathlib import Path


STORES = ("snapshots-classic", "snapshots-2-linux", "snapshots-2-windows", "snapshots-windows", "snapshots-linux")


def copy_contents(source: Path, destination: Path) -> None:
    if not source.is_dir():
        return
    destination.mkdir(parents=True, exist_ok=True)
    for child in source.iterdir():
        target = destination / child.name
        if child.is_dir():
            shutil.copytree(child, target, dirs_exist_ok=True)
        else:
            shutil.copy2(child, target)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--reports", default="ci-out/gitlab-pages")
    args = parser.parse_args()
    reports = Path(args.reports)
    destination = reports / "vi-snapshots"
    for store in STORES:
        root = reports / store
        copy_contents(root / "vi-snapshots", destination)
        if root.exists():
            shutil.rmtree(root)


if __name__ == "__main__":
    main()
