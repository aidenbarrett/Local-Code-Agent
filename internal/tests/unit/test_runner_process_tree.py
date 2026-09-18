"""Regression coverage for command timeout process ownership."""
from __future__ import annotations

import json
import os
from pathlib import Path
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
    if os.name == "nt":
        # psutil cleanup is useful, but enumeration is not containment. This must
        # stay false until a Job Object owns the tree from process creation.
        assert out.process_cleanup_confirmed is False
    else:
        assert out.process_cleanup_confirmed is True

    size_after_return = marker.stat().st_size if marker.exists() else 0
    time.sleep(0.4)
    size_later = marker.stat().st_size if marker.exists() else 0
    assert size_later == size_after_return, "a grandchild kept running after run_command returned"
    log = out.stderr_path.read_text(encoding="utf-8")
    assert "process-tree cleanup confirmed=" in log


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
