#!/usr/bin/env python3
"""Start OVMS detached on Windows using an argv JSON file.

PowerShell 5.1's Start-Process -ArgumentList can mangle embedded JSON quoting
(e.g. --plugin_config). Python's subprocess.list2cmdline handles Windows argv
quoting correctly, so the bootstrap writes a launch spec and delegates only the
process creation to this helper.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path


def launch(spec_path: Path) -> subprocess.Popen:
    """Shared argv-safe launcher for OVMS and llama-server, Windows and Linux."""
    with spec_path.open("r", encoding="utf-8-sig") as fh:
        spec = json.load(fh)

    exe = str(spec["exe"])
    args = [str(x) for x in spec.get("args", [])]
    stdout_path = Path(spec["stdout"])
    stderr_path = Path(spec["stderr"])
    stdout_path.parent.mkdir(parents=True, exist_ok=True)
    stderr_path.parent.mkdir(parents=True, exist_ok=True)

    options = ({"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS}
               if os.name == "nt" else {"start_new_session": True})
    # Detach from the invoking Windows terminal; stop attempts CTRL_BREAK before
    # forcing termination if no console is available.
    # No shell parsing, including for the embedded plugin JSON argument.
    env = dict(os.environ)
    for key in spec.get("unset_env", []):
        env.pop(key, None)
    env.update(spec.get("env", {}))
    with stdout_path.open("ab", buffering=0) as out, stderr_path.open("ab", buffering=0) as err:
        return subprocess.Popen(
            [exe, *args], stdin=subprocess.DEVNULL, stdout=out, stderr=err,
            cwd=spec.get("cwd"), env=env, close_fds=True, **options,
        )


def main() -> int:
    if len(sys.argv) != 2:
        print("usage: start-ovms-detached.py <launch-spec.json>", file=sys.stderr)
        return 2
    proc = launch(Path(sys.argv[1]))
    print(proc.pid)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
