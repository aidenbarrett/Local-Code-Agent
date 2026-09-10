#!/usr/bin/env python3
"""Start OVMS detached on Windows using an argv JSON file.

PowerShell 5.1's Start-Process -ArgumentList can mangle embedded JSON quoting
(e.g. --plugin_config). Python's subprocess.list2cmdline handles Windows argv
quoting correctly, so the bootstrap writes a launch spec and delegates only the
process creation to this helper.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path


def main() -> int:
    if len(sys.argv) != 2:
        print("usage: start-ovms-detached.py <launch-spec.json>", file=sys.stderr)
        return 2

    spec_path = Path(sys.argv[1])
    with spec_path.open("r", encoding="utf-8-sig") as fh:
        spec = json.load(fh)

    exe = str(spec["exe"])
    args = [str(x) for x in spec.get("args", [])]
    stdout_path = Path(spec["stdout"])
    stderr_path = Path(spec["stderr"])
    stdout_path.parent.mkdir(parents=True, exist_ok=True)
    stderr_path.parent.mkdir(parents=True, exist_ok=True)

    out = stdout_path.open("ab", buffering=0)
    err = stderr_path.open("ab", buffering=0)
    flags = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS
    proc = subprocess.Popen(
        [exe, *args],
        stdin=subprocess.DEVNULL,
        stdout=out,
        stderr=err,
        creationflags=flags,
        close_fds=True,
    )
    print(proc.pid)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
