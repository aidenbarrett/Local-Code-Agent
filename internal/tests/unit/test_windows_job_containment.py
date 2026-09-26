"""Windows Job Object ownership of configured-command process trees.

The Windows cases run only on native Windows CI. Layout and platform-gate cases run
everywhere so a structure or gating regression is caught on Linux as well.
"""
from __future__ import annotations

import ctypes
import os
from pathlib import Path
import sys
import time

import psutil
import pytest

from local_agent.tools import windows_job
from local_agent.tools.process_runner import run_command


windows_only = pytest.mark.skipif(os.name != "nt", reason="Job Objects are Windows-only")
posix_only = pytest.mark.skipif(os.name == "nt", reason="POSIX containment contract")

_CREATE_BREAKAWAY_FROM_JOB = 0x01000000
_DETACHED_PROCESS = 0x00000008
_CREATE_NEW_PROCESS_GROUP = 0x00000200


def _wait_for(path: Path, timeout_s: float = 20.0) -> None:
    deadline = time.monotonic() + timeout_s
    while not path.exists() and time.monotonic() < deadline:
        time.sleep(0.02)
    assert path.exists(), f"{path.name} never appeared"


def _alive(pid: int) -> bool:
    try:
        return psutil.Process(pid).is_running() and psutil.Process(pid).status() != psutil.STATUS_ZOMBIE
    except psutil.NoSuchProcess:
        return False


def _ticker(marker: Path) -> str:
    return (
        "from pathlib import Path; import time; "
        f"f=Path({str(marker)!r}).open('a', encoding='utf-8'); "
        "\nwhile True:\n f.write('tick\\n'); f.flush(); time.sleep(0.05)"
    )


@pytest.mark.skipif(ctypes.sizeof(ctypes.c_void_p) != 8, reason="64-bit layout check")
def test_job_structures_match_windows_x64_layout():
    # Sizes from the Windows SDK for x64. A wrong layout would make the kernel read
    # LimitFlags or ActiveProcesses from the wrong offset.
    assert ctypes.sizeof(windows_job.JOBOBJECT_BASIC_LIMIT_INFORMATION) == 64
    assert ctypes.sizeof(windows_job.JOBOBJECT_EXTENDED_LIMIT_INFORMATION) == 144
    assert ctypes.sizeof(windows_job.JOBOBJECT_BASIC_ACCOUNTING_INFORMATION) == 48
    assert windows_job.JOBOBJECT_BASIC_LIMIT_INFORMATION.LimitFlags.offset == 16
    assert windows_job.JOBOBJECT_BASIC_ACCOUNTING_INFORMATION.ActiveProcesses.offset == 40


@posix_only
def test_job_objects_refuse_to_exist_off_windows():
    assert windows_job.supported() is False
    with pytest.raises(windows_job.JobContainmentError):
        windows_job.ProcessTreeJob()


@posix_only
def test_posix_run_reports_escapable_group_containment(tmp_path):
    out = run_command([sys.executable, "-c", "print('ok')"], tmp_path, tmp_path / "runs", 10)
    assert out.ok
    assert out.containment == "process_group"
    assert out.stray_descendants_at_exit is None
    assert "containment=process_group" in (out.stdout_path.parent / "command.txt").read_text(
        encoding="utf-8"
    )


@windows_only
def test_normal_command_runs_inside_job_with_no_strays(tmp_path):
    out = run_command([sys.executable, "-c", "print('ok')"], tmp_path, tmp_path / "runs", 30)
    assert out.ok
    assert out.containment == "job_object"
    assert out.stray_descendants_at_exit == 0
    assert out.process_cleanup_confirmed is None
    assert out.stdout_path.read_text(encoding="utf-8").strip() == "ok"


@windows_only
def test_descendant_cannot_break_away_from_job(tmp_path):
    marker = tmp_path / "breakaway.log"
    outcome_path = tmp_path / "breakaway.txt"
    parent = tmp_path / "breakaway_parent.py"
    parent.write_text(
        "import subprocess, sys, time\n"
        "from pathlib import Path\n"
        f"out=Path({str(outcome_path)!r})\n"
        "try:\n"
        f"    p=subprocess.Popen([sys.executable, '-c', {_ticker(marker)!r}],"
        f" creationflags={_CREATE_BREAKAWAY_FROM_JOB | _DETACHED_PROCESS})\n"
        "    out.write_text(f'escaped {p.pid}')\n"
        "except OSError as exc:\n"
        "    out.write_text(f'denied {exc.winerror}')\n"
        "time.sleep(60)\n",
        encoding="utf-8",
    )

    out = run_command([sys.executable, str(parent)], tmp_path, tmp_path / "runs", 3)
    _wait_for(outcome_path)
    verdict = outcome_path.read_text(encoding="utf-8")

    assert out.timed_out is True
    assert out.containment == "job_object"
    assert out.process_cleanup_confirmed is True
    # The job never grants breakaway, so the OS must refuse the request.
    assert verdict.startswith("denied"), verdict


@windows_only
def test_timeout_kills_detached_grandchild_the_visible_tree_would_miss(tmp_path):
    marker = tmp_path / "detached.log"
    pid_path = tmp_path / "detached.pid"
    parent = tmp_path / "detached_parent.py"
    # The middle process exits immediately, orphaning the grandchild. Enumerating the
    # direct child's descendants cannot find it; only the job can.
    parent.write_text(
        "import subprocess, sys\n"
        "from pathlib import Path\n"
        f"p=subprocess.Popen([sys.executable, '-c', {_ticker(marker)!r}],"
        f" creationflags={_DETACHED_PROCESS | _CREATE_NEW_PROCESS_GROUP})\n"
        f"Path({str(pid_path)!r}).write_text(str(p.pid))\n",
        encoding="utf-8",
    )
    outer = tmp_path / "outer.py"
    outer.write_text(
        "import subprocess, sys, time\n"
        f"subprocess.run([sys.executable, {str(parent)!r}], check=True)\n"
        "time.sleep(60)\n",
        encoding="utf-8",
    )

    out = run_command([sys.executable, str(outer)], tmp_path, tmp_path / "runs", 4)
    grandchild = int(pid_path.read_text(encoding="utf-8"))

    assert out.timed_out is True
    assert out.process_cleanup_confirmed is True
    assert not _alive(grandchild)
    size = marker.stat().st_size if marker.exists() else 0
    time.sleep(0.4)
    assert (marker.stat().st_size if marker.exists() else 0) == size


@windows_only
def test_normal_exit_terminates_and_counts_abandoned_descendant(tmp_path):
    marker = tmp_path / "abandoned.log"
    pid_path = tmp_path / "abandoned.pid"
    parent = tmp_path / "abandon_parent.py"
    parent.write_text(
        "import subprocess, sys, time\n"
        "from pathlib import Path\n"
        f"marker=Path({str(marker)!r})\n"
        f"p=subprocess.Popen([sys.executable, '-c', {_ticker(marker)!r}],"
        f" creationflags={_DETACHED_PROCESS})\n"
        f"Path({str(pid_path)!r}).write_text(str(p.pid))\n"
        "deadline=time.monotonic()+20\n"
        "while not marker.exists() and time.monotonic() < deadline:\n"
        "    time.sleep(0.02)\n"
        "print('parent done')\n",
        encoding="utf-8",
    )

    out = run_command([sys.executable, str(parent)], tmp_path, tmp_path / "runs", 60)
    grandchild = int(pid_path.read_text(encoding="utf-8"))

    assert out.ok is True
    assert out.exit_code == 0
    assert out.containment == "job_object"
    assert out.stray_descendants_at_exit is not None and out.stray_descendants_at_exit >= 1
    assert not _alive(grandchild), "a finished command left an owned descendant running"
    size = marker.stat().st_size
    time.sleep(0.4)
    assert marker.stat().st_size == size


@windows_only
def test_refused_adoption_reruns_uncontained_once_and_reports_unconfirmed(tmp_path, monkeypatch):
    runs = tmp_path / "runs.log"

    class RefusingJob(windows_job.ProcessTreeJob):
        def adopt_suspended(self, pid: int) -> None:
            # Mirror the real contract: the suspended child is terminated before the
            # refusal escapes.
            psutil.Process(pid).kill()
            raise windows_job.JobContainmentError(5, "refused for test")

    monkeypatch.setattr(windows_job, "ProcessTreeJob", RefusingJob)
    code = (
        "from pathlib import Path; "
        f"f=Path({str(runs)!r}).open('a', encoding='utf-8'); f.write('ran\\n'); f.close(); "
        "print('ok')"
    )
    out = run_command([sys.executable, "-c", code], tmp_path, tmp_path / "runs", 30)

    assert out.ok
    assert out.containment == "visible_tree"
    assert out.stray_descendants_at_exit is None
    # The suspended first attempt never executed; the command ran exactly once.
    assert runs.read_text(encoding="utf-8") == "ran\n"


@windows_only
def test_unavailable_job_falls_back_to_unconfirmed_visible_tree(tmp_path, monkeypatch):
    def refuse() -> windows_job.ProcessTreeJob:
        raise windows_job.JobContainmentError(5, "refused for test")

    monkeypatch.setattr(windows_job, "ProcessTreeJob", refuse)
    parent = tmp_path / "sleeper.py"
    parent.write_text("import time\ntime.sleep(60)\n", encoding="utf-8")

    out = run_command([sys.executable, str(parent)], tmp_path, tmp_path / "runs", 1)

    assert out.timed_out is True
    assert out.containment == "visible_tree"
    assert out.process_cleanup_confirmed is False
