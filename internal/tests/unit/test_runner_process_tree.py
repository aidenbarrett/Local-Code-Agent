"""Regression coverage for command timeout process ownership."""
from __future__ import annotations

import json
import os
from pathlib import Path
import signal
import sys
import time

from local_agent.tools.runner import run_command


def test_normal_completion_needs_no_cleanup_claim(tmp_path):
    out = run_command(
        [sys.executable, "-c", "print('ok')"],
        tmp_path,
        tmp_path / "runs",
        10,
    )
    assert out.ok is True
    assert out.timed_out is False
    assert out.process_cleanup_confirmed is None
    assert out.stdout_path.read_text(encoding="utf-8").strip() == "ok"


def test_timeout_stops_grandchild_and_reports_only_provable_cleanup(tmp_path):
    marker = tmp_path / "grandchild.log"
    parent = tmp_path / "parent.py"
    child_code = (
        "from pathlib import Path; import time; "
        f"p=Path({str(marker)!r}); "
        "f=p.open('a', encoding='utf-8'); "
        "\nwhile True:\n f.write('tick\\n'); f.flush(); time.sleep(0.05)"
    )
    parent.write_text(
        "import subprocess, sys, time\n"
        f"subprocess.Popen([sys.executable, '-c', {child_code!r}])\n"
        "time.sleep(60)\n",
        encoding="utf-8",
    )

    out = run_command(
        [sys.executable, str(parent)],
        tmp_path,
        tmp_path / "runs",
        1,
    )
    assert out.timed_out is True
    assert out.exit_code == 124
    # Killing the inherited POSIX process group and reconciling a visible
    # Windows tree are both useful cleanup, but neither is containment. A child
    # may call setsid()/setpgid() on POSIX or race enumeration on Windows.
    assert out.process_cleanup_confirmed is False

    size_after_return = marker.stat().st_size if marker.exists() else 0
    time.sleep(0.4)
    size_later = marker.stat().st_size if marker.exists() else 0
    assert size_later == size_after_return, "a grandchild kept running after run_command returned"
    log = out.stderr_path.read_text(encoding="utf-8")
    assert "process-tree cleanup confirmed=false" in log


def test_posix_child_is_launched_as_process_group_leader(tmp_path):
    if os.name == "nt":
        return
    record = tmp_path / "pg.json"
    code = (
        "import json, os; from pathlib import Path; "
        f"Path({str(record)!r}).write_text(json.dumps({{'pid':os.getpid(),'pgid':os.getpgrp()}}))"
    )
    out = run_command([sys.executable, "-c", code], tmp_path, tmp_path / "runs", 10)
    assert out.ok
    data = json.loads(record.read_text(encoding="utf-8"))
    assert data["pid"] == data["pgid"]


def test_posix_setsid_escape_cannot_hold_runner_or_mutate_public_evidence(tmp_path):
    if os.name == "nt":
        return

    escaped_pid_path = tmp_path / "escaped.pid"
    parent = tmp_path / "escape_parent.py"
    child_code = (
        "import os, sys, time; from pathlib import Path; "
        "os.setsid(); "
        f"Path({str(escaped_pid_path)!r}).write_text(str(os.getpid())); "
        "deadline=time.monotonic()+8.0; i=0; "
        "\nwhile time.monotonic() < deadline:\n"
        " print(f'out-{i}', flush=True); print(f'err-{i}', file=sys.stderr, flush=True); "
        "i+=1; time.sleep(0.05)"
    )
    parent.write_text(
        "import subprocess, sys, time\n"
        "from pathlib import Path\n"
        f"pid_path=Path({str(escaped_pid_path)!r})\n"
        f"subprocess.Popen([sys.executable, '-c', {child_code!r}])\n"
        "deadline=time.monotonic()+5.0\n"
        "while not pid_path.exists() and time.monotonic() < deadline:\n"
        " time.sleep(0.01)\n"
        "time.sleep(60)\n",
        encoding="utf-8",
    )

    escaped_pid: int | None = None
    try:
        started = time.monotonic()
        out = run_command(
            [sys.executable, str(parent)],
            tmp_path,
            tmp_path / "runs",
            1,
        )
        elapsed = time.monotonic() - started
        if escaped_pid_path.exists():
            escaped_pid = int(escaped_pid_path.read_text(encoding="utf-8"))

        assert escaped_pid is not None, "the regression child never reached setsid()"
        assert out.timed_out is True
        assert out.exit_code == 124
        assert out.process_cleanup_confirmed is False
        assert elapsed < 4.0, "an escaped child kept timeout collection waiting for inherited output EOF"

        stdout_before = out.stdout_path.read_bytes()
        stderr_before = out.stderr_path.read_bytes()
        combined_before = out.combined_path.read_bytes()
        assert b"process-tree cleanup confirmed=false" in stderr_before

        # The escaped child is still writing to its inherited descriptor here.
        # Public evidence must be a frozen snapshot, not the live capture inode.
        time.sleep(0.3)
        assert out.stdout_path.read_bytes() == stdout_before
        assert out.stderr_path.read_bytes() == stderr_before
        assert out.combined_path.read_bytes() == combined_before
        assert not (out.stdout_path.parent / ".stdout.capture").exists()
        assert not (out.stderr_path.parent / ".stderr.capture").exists()
    finally:
        if escaped_pid is not None:
            try:
                os.kill(escaped_pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
