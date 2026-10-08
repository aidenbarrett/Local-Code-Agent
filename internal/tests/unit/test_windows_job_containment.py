"""Windows Job Object ownership of configured-command process trees.

The Windows cases run only on native Windows CI. Layout and platform-gate cases run
everywhere so a structure or gating regression is caught on Linux as well.
"""
from __future__ import annotations

import ctypes
import os
from pathlib import Path
import subprocess
import sys
import time
from unittest.mock import Mock

import psutil
import pytest

from local_agent.tools import windows_job
from local_agent.tools.process_runner import _Tree, _start, run_command


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


def test_unadopted_process_is_killed_and_boundedly_reaped():
    child = Mock()
    assert windows_job.terminate_unadopted(child, timeout_s=3) is True
    child.kill.assert_called_once_with()
    child.wait.assert_called_once_with(timeout=3)


def test_unadopted_process_retries_kill_once_without_unbounded_wait():
    child = Mock()
    child.wait.side_effect = [
        subprocess.TimeoutExpired("child", 3),
        subprocess.TimeoutExpired("child", 3),
    ]

    assert windows_job.terminate_unadopted(child, timeout_s=3) is False
    assert child.kill.call_count == 2
    assert child.wait.call_count == 2


def test_runner_kills_unadopted_attempt_before_visible_tree_retry():
    job = Mock(spec=windows_job.ProcessTreeJob)
    job.adopt_suspended.side_effect = windows_job.JobContainmentError(5, "OpenProcess failed")
    suspended = Mock()
    suspended.pid = 1234
    retry = Mock()
    spawn = Mock(side_effect=[suspended, retry])
    tree = _Tree(job=job, containment="job_object")

    assert _start(spawn, tree, on_spawn=None) is retry

    suspended.kill.assert_called_once_with()
    suspended.wait.assert_called_once_with(timeout=2.0)
    job.close.assert_called_once_with()
    assert tree.job is None
    assert tree.containment == "visible_tree"
    assert spawn.call_args_list[0].args == (True,)
    assert spawn.call_args_list[1].args == (False,)


def test_runner_does_not_retry_when_unadopted_attempt_cannot_be_reaped(monkeypatch):
    job = Mock(spec=windows_job.ProcessTreeJob)
    job.adopt_suspended.side_effect = windows_job.JobContainmentError(5, "OpenProcess failed")
    suspended = Mock()
    suspended.pid = 1234
    spawn = Mock(return_value=suspended)
    tree = _Tree(job=job, containment="job_object")
    monkeypatch.setattr(windows_job, "terminate_unadopted", lambda process, timeout: False)

    with pytest.raises(windows_job.JobContainmentError, match="did not exit"):
        _start(spawn, tree, on_spawn=None)

    spawn.assert_called_once_with(True)
    job.close.assert_called_once_with()
    assert tree.job is None
    assert tree.containment == "visible_tree"


def test_callback_failure_before_adoption_uses_direct_cleanup_not_empty_job():
    job = Mock(spec=windows_job.ProcessTreeJob)
    job.active_processes.return_value = 0
    suspended = Mock()
    suspended.pid = 1234
    spawn = Mock(return_value=suspended)
    callback = Mock(side_effect=RuntimeError("durable start failed"))
    tree = _Tree(job=job, containment="job_object")

    with pytest.raises(RuntimeError, match="durable start failed"):
        _start(spawn, tree, on_spawn=callback)

    spawn.assert_called_once_with(True)
    job.adopt_suspended.assert_not_called()
    job.terminate_and_confirm.assert_not_called()
    job.active_processes.assert_not_called()
    suspended.kill.assert_called_once_with()
    suspended.wait.assert_called_once_with(timeout=2.0)
    job.close.assert_called_once_with()
    assert tree.job is None
    assert tree.containment == "visible_tree"
    assert tree.cleanup_confirmed is True


def test_callback_failure_does_not_let_empty_job_certify_timed_out_direct_reap():
    job = Mock(spec=windows_job.ProcessTreeJob)
    job.active_processes.return_value = 0
    suspended = Mock()
    suspended.pid = 1234
    suspended.wait.side_effect = [
        subprocess.TimeoutExpired("child", 2.0),
        subprocess.TimeoutExpired("child", 2.0),
    ]
    spawn = Mock(return_value=suspended)
    callback = Mock(side_effect=RuntimeError("durable start failed"))
    tree = _Tree(job=job, containment="job_object")

    with pytest.raises(RuntimeError, match="durable start failed"):
        _start(spawn, tree, on_spawn=callback)

    spawn.assert_called_once_with(True)
    job.adopt_suspended.assert_not_called()
    job.terminate_and_confirm.assert_not_called()
    job.active_processes.assert_not_called()
    assert suspended.kill.call_count == 2
    assert suspended.wait.call_count == 2
    job.close.assert_called_once_with()
    assert tree.job is None
    assert tree.containment == "visible_tree"
    assert tree.cleanup_confirmed is False


@posix_only
def test_posix_run_reports_the_available_ownership_boundary(tmp_path):
    if not sys.platform.startswith("linux"):
        from local_agent.tools.tool_primitives import Reason, ToolError

        with pytest.raises(ToolError) as refused:
            run_command([sys.executable, "-c", "print('ok')"], tmp_path, tmp_path / "runs", 10)
        assert refused.value.reason is Reason.CLEANUP_UNKNOWN
        return
    out = run_command([sys.executable, "-c", "print('ok')"], tmp_path, tmp_path / "runs", 10)
    assert out.ok
    assert out.containment == "child_subreaper"
    assert out.stray_descendants_at_exit == 0
    assert out.process_cleanup_confirmed is None
    assert "containment=child_subreaper" in (out.stdout_path.parent / "command.txt").read_text(
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
    # The job's own accounting confirmed the abandoned descendant ended before the run
    # returned. Under parallel load this used to time out silently (found by the xdist
    # probe on Windows CI): the run claimed containment with the descendant alive.
    assert out.process_cleanup_confirmed is True
    assert not _alive(grandchild), "a finished command left an owned descendant running"
    size = marker.stat().st_size
    time.sleep(0.4)
    assert marker.stat().st_size == size


@windows_only
def test_refused_adoption_reruns_uncontained_once_and_reports_unconfirmed(tmp_path, monkeypatch):
    runs = tmp_path / "runs.log"

    class RefusingJob(windows_job.ProcessTreeJob):
        def adopt_suspended(self, pid: int) -> None:
            # OpenProcess can fail before the job owns the child. The runner's direct
            # Popen fallback, not this empty job, must terminate the suspended attempt.
            raise windows_job.JobContainmentError(5, "refused for test")

    monkeypatch.setattr(windows_job, "ProcessTreeJob", RefusingJob)
    code = (
        "from pathlib import Path; "
        f"f=Path({str(runs)!r}).open('a', encoding='utf-8'); f.write('ran\\n'); f.close(); "
        "print('ok')"
    )
    from local_agent.tools.tool_primitives import Reason, ToolError

    # Without a Job Object the tree cannot be shown ended, so the public boundary
    # refuses the run as a result (#436 review) rather than reporting it ok.
    with pytest.raises(ToolError) as refused:
        run_command([sys.executable, "-c", code], tmp_path, tmp_path / "runs", 30)
    assert refused.value.reason is Reason.CLEANUP_UNKNOWN
    assert "containment=visible_tree" in str(refused.value)
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
