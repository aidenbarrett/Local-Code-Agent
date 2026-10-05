"""Adversarial lifecycle tests for the reusable owned-process runner (#415)."""
from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

import psutil
import pytest

from local_agent.tools.process_runner import (
    CommandOutputLimitError,
    OwnedLifecycle,
    run_owned,
)


def _env() -> dict[str, str]:
    return dict(os.environ)


def _gone(pid: int) -> bool:
    try:
        return psutil.Process(pid).status() == psutil.STATUS_ZOMBIE
    except psutil.NoSuchProcess:
        return True


def _wait_pid(path: Path) -> int:
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if path.exists() and path.read_text(encoding="utf-8").strip():
            return int(path.read_text(encoding="utf-8"))
        time.sleep(0.02)
    raise AssertionError("child pid was never recorded")


def _assert_gone(pid: int) -> None:
    deadline = time.monotonic() + 5
    while not _gone(pid) and time.monotonic() < deadline:
        time.sleep(0.02)
    assert _gone(pid), f"owned descendant {pid} survived the API return"


def test_normal_exit_cannot_leave_a_process_group_child_alive(tmp_path: Path) -> None:
    pid_file = tmp_path / "child.pid"
    parent = tmp_path / "parent.py"
    child = (
        "import os,time; from pathlib import Path; "
        f"Path({str(pid_file)!r}).write_text(str(os.getpid()), encoding='utf-8'); "
        "time.sleep(60)"
    )
    parent.write_text(
        "import subprocess,sys,time\n"
        f"subprocess.Popen([sys.executable, '-c', {child!r}])\n"
        "time.sleep(0.2)\n",
        encoding="utf-8",
    )

    run = run_owned(
        [sys.executable, str(parent)], tmp_path, OwnedLifecycle(10), env=_env(),
    )
    pid = _wait_pid(pid_file)
    try:
        _assert_gone(pid)
        assert run.exit_code == 0
        assert run.stray_descendants_at_exit not in (None, 0)
        if os.name == "nt":
            assert run.cleanup_confirmed is True
            assert run.strays_unconfirmed is False
        else:
            # A POSIX process group can be escaped with setsid(), so killing observed
            # members is useful but never promoted to proof of whole-tree containment.
            assert run.cleanup_confirmed is False
            assert run.strays_unconfirmed is True
    finally:
        if not _gone(pid):
            psutil.Process(pid).kill()


def test_on_spawn_exception_never_leaks_the_created_process(tmp_path: Path) -> None:
    sleeper = tmp_path / "unique-owned-sleeper.py"
    sleeper.write_text("import time; time.sleep(60)\n", encoding="utf-8")

    class SpawnReceiptError(RuntimeError):
        pass

    def fail_receipt() -> None:
        raise SpawnReceiptError("receipt write failed")

    with pytest.raises(SpawnReceiptError, match="receipt write failed"):
        run_owned(
            [sys.executable, str(sleeper)],
            tmp_path,
            OwnedLifecycle(10, on_spawn=fail_receipt),
            env=_env(),
        )

    needle = str(sleeper)
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        matches = []
        for process in psutil.process_iter(["cmdline"]):
            try:
                if needle in (process.info["cmdline"] or []):
                    matches.append(process.pid)
            except psutil.Error:
                continue
        if not matches:
            break
        time.sleep(0.02)
    assert not matches, f"on_spawn failure leaked process(es): {matches}"


def test_aggregate_output_limit_is_enforced_while_tree_is_running(tmp_path: Path) -> None:
    pid_file = tmp_path / "writer.pid"
    parent = tmp_path / "writer_parent.py"
    child = (
        "import os,sys,time; from pathlib import Path; "
        f"Path({str(pid_file)!r}).write_text(str(os.getpid()), encoding='utf-8'); "
        "chunk='x'*4096; "
        "\nwhile True:\n"
        " sys.stdout.write(chunk); sys.stdout.flush(); "
        "sys.stderr.write(chunk); sys.stderr.flush(); time.sleep(0.001)"
    )
    parent.write_text(
        "import subprocess,sys,time\n"
        f"subprocess.Popen([sys.executable, '-c', {child!r}])\n"
        "time.sleep(60)\n",
        encoding="utf-8",
    )

    started = time.monotonic()
    with pytest.raises(CommandOutputLimitError) as raised:
        run_owned(
            [sys.executable, str(parent)],
            tmp_path,
            OwnedLifecycle(30, output_limit=64 * 1024),
            env=_env(),
        )
    elapsed = time.monotonic() - started
    pid = _wait_pid(pid_file)
    try:
        _assert_gone(pid)
        assert elapsed < 5
        assert raised.value.observed_bytes > raised.value.limit
        # Polling allows a bounded overshoot, but not seconds/minutes of disk growth.
        assert raised.value.observed_bytes < raised.value.limit + 2 * 1024 * 1024
    finally:
        if not _gone(pid):
            psutil.Process(pid).kill()


def test_output_limit_is_aggregate_across_stdout_and_stderr(tmp_path: Path) -> None:
    code = (
        "import sys,time; "
        "sys.stdout.write('a'*40000); sys.stdout.flush(); "
        "sys.stderr.write('b'*40000); sys.stderr.flush(); time.sleep(60)"
    )
    with pytest.raises(CommandOutputLimitError) as raised:
        run_owned(
            [sys.executable, "-c", code],
            tmp_path,
            OwnedLifecycle(30, output_limit=64 * 1024),
            env=_env(),
        )
    assert raised.value.observed_bytes >= 80000
    assert raised.value.limit == 64 * 1024
