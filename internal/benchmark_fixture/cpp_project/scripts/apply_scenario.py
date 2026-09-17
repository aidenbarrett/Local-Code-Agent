#!/usr/bin/env python3
"""Apply one failure scenario to the sandbox, or restore the baseline."""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MANIFEST = ROOT / "scenarios" / "manifest.json"


def load() -> dict:
    return json.loads(MANIFEST.read_text(encoding="utf-8"))


def restore_baseline(manifest: dict) -> None:
    baseline = ROOT / "scenarios" / "clean"
    for rel in manifest["managed_files"]:
        src = baseline / rel
        dst = ROOT / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, dst)


def apply(name: str) -> int:
    manifest = load()
    if name not in manifest["scenarios"]:
        print(f"unknown scenario {name!r}; known: {', '.join(manifest['scenarios'])}")
        return 2
    restore_baseline(manifest)
    for rel in manifest["scenarios"][name]:
        src = ROOT / "scenarios" / name / rel
        dst = ROOT / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, dst)
    print(f"scenario applied: {name}")
    if manifest["scenarios"][name]:
        print("  " + "\n  ".join(manifest["scenarios"][name]))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("scenario", nargs="?", default="clean")
    parser.add_argument("--list", action="store_true")
    args = parser.parse_args()
    if args.list:
        for name, files in load()["scenarios"].items():
            print(f"{name:16s} {len(files)} file(s) replaced")
        return 0
    return apply(args.scenario)


if __name__ == "__main__":
    sys.exit(main())
