"""The Linux supervisor's start-up stays off every command's critical path (#455).

#452 made each owned command start two interpreters that each loaded psutil,
dataclasses, pathlib and json: about 140 ms per command where there had been 3 ms.
"""
from __future__ import annotations

import os
from pathlib import Path
import statistics
import subprocess
import sys
import time

import pytest

from local_agent.tools import process_runner
from local_agent.tools.process_runner import OwnedLifecycle, run_owned

linux_only = pytest.mark.skipif(not sys.platform.startswith("linux"),
                                reason="the child subreaper is Linux-only")

_SUPERVISOR = Path(process_runner.__file__).with_name("posix_supervisor.py")
# Modules whose import cost 5-25 ms each in the #455 measurement (threading is not
# listed: ctypes, which prctl needs, imports it).
_HEAVY = {"psutil", "dataclasses", "pathlib", "json", "typing", "subprocess",
          "inspect", "re"}


def test_the_supervisor_starts_isolated_without_site_packages():
    argv = process_runner._PosixOwner(-1, -1).argv(["true"])
    assert argv[:3] == [sys.executable, "-I", "-S"]
    assert argv[3] == str(_SUPERVISOR)


def _modules_after_running(script: Path) -> set[str]:
    probe = (
        "import runpy, sys\n"
        f"runpy.run_path({str(script)!r}, run_name='lca_supervisor_probe')\n"
        "print('\\n'.join(sorted(sys.modules)))\n"
    )
    done = subprocess.run(  # noqa: S603 - this interpreter, fixed probe
        [sys.executable, "-I", "-S", "-c", probe],
        capture_output=True, text=True, check=True, timeout=60,
    )
    return set(done.stdout.split())


def test_the_supervisor_loads_no_heavy_module_at_start_up(tmp_path):
    empty = tmp_path / "empty.py"
    empty.write_text("", encoding="utf-8")
    # Whatever runpy itself loads is the probe's cost, not the supervisor's.
    added = _modules_after_running(_SUPERVISOR) - _modules_after_running(empty)
    assert not _HEAVY & added, sorted(_HEAVY & added)


@linux_only
def test_owned_command_overhead_stays_bounded(tmp_path):
    """A generous wall-clock bound: about 30 ms here after #455, 140 ms before it."""
    env = dict(os.environ)
    run_owned(["true"], tmp_path, OwnedLifecycle(30), env=env)        # warm caches
    samples = []
    for _ in range(9):
        started = time.monotonic()
        run = run_owned(["true"], tmp_path, OwnedLifecycle(30), env=env)
        samples.append(time.monotonic() - started)
        assert run.exit_code == 0 and run.containment == "child_subreaper"
    assert statistics.median(samples) < 0.1, samples
