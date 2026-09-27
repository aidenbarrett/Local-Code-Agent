#!/usr/bin/env python3
"""Print the download URL of an OpenVINO GenAI release archive.

The public storage index is a JSON file tree. Guessing archive names breaks
whenever the naming scheme shifts, so CI looks the name up instead and fails
loudly when no archive matches. Used only by .github/workflows/native-endpoint.yml.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from urllib.request import urlopen

STORAGE = "https://storage.openvinotoolkit.org"


def walk(node, prefix=""):
    name = node.get("name", "")
    path = f"{prefix}/{name}" if name else prefix
    children = node.get("children")
    if children is None:
        yield path
        return
    for child in children:
        yield from walk(child, path)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--release", required=True, help="for example 2026.3")
    parser.add_argument("--platform", required=True, choices=["ubuntu24", "windows"])
    parser.add_argument("--list", action="store_true", help="print every file in the release folder")
    args = parser.parse_args(argv)

    with urlopen(f"{STORAGE}/filetree.json", timeout=120) as response:  # noqa: S310 - fixed public URL
        tree = json.load(response)

    folder = f"/repositories/openvino_genai/packages/{args.release}/"
    if args.list:
        for p in sorted(p for p in walk(tree) if folder in p):
            print(p[p.index("/repositories/"):])
        return 0
    suffix = r"\.tar\.gz" if args.platform == "ubuntu24" else r"\.zip"
    # Anchored at the file name: the folder also holds pdb_openvino_genai_*
    # debug-symbol archives, which contain no headers or CMake package.
    pattern = re.compile(
        rf"/openvino_genai_{args.platform}_{re.escape(args.release)}(\.\d+)*_x86_64{suffix}$")
    matches = sorted(p for p in walk(tree) if folder in p and pattern.search(p))
    if not matches:
        print(f"no OpenVINO GenAI {args.release} {args.platform} archive in the storage index",
              file=sys.stderr)
        return 1
    # Several patch builds may exist; the last in version order is the newest.
    path = matches[-1]
    path = path[path.index("/repositories/"):]
    print(STORAGE + path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
