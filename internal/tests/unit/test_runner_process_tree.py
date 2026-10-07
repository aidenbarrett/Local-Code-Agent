"""Regression coverage for command timeout process ownership."""
from __future__ import annotations

import json
import os
from pathlib import Path
import signal
import sys
import time

import pytest

from local_agent.tools.process_runner import run_command


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
    # Windows owns the tree through a Job Object. Linux owns it through a dedicated
    # child subreaper. Other POSIX process groups remain signalling-only and fail closed.
    if os.name == "nt":
        assert out.containment == "job_object"
        assert out.process_cleanup_confirmed is True
    elif sys.platform.startswith("linux"):
        assert out.containment == "child_subreaper"
        assert out.process_cleanup_confirmed is True
    else:
        assert out.containment == "process_group"
        assert out.process_cleanup_confirmed is False

    size_after_return = marker.stat().st_size if marker.exists() else 0
    time.sleep(0.4)
    size_later = marker.stat().st_size if marker.exists() else 0
    assert size_later == size_after_return, "a grandchild kept running after run_command returned"
    log = out.stderr_path.read_text(encoding="utf-8")
    expected = "true" if os.name == "nt" or sys.platform.startswith("linux") else "false"
    assert f"process-tree cleanup confirmed={expected}" in log


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
        assert out.process_cleanup_confirmed is True
        assert out.containment == "child_subreaper"
        assert elapsed < 4.0, "an escaped child kept timeout collection waiting for inherited output EOF"

        stdout_before = out.stdout_path.read_bytes()
        stderr_before = out.stderr_path.read_bytes()
        combined_before = out.combined_path.read_bytes()
        assert b"process-tree cleanup confirmed=true" in stderr_before

        _assert_pid_gone(escaped_pid)
        # Public evidence remains a frozen snapshot with no live capture file inside
        # the run-artifact directory.
        time.sleep(0.3)
        assert out.stdout_path.read_bytes() == stdout_before
        assert out.stderr_path.read_bytes() == stderr_before
        assert out.combined_path.read_bytes() == combined_before
        assert {path.name for path in out.stdout_path.parent.iterdir()} == {
            "stdout.log", "stderr.log", "combined.log", "command.txt"
        }
    finally:
        if escaped_pid is not None:
            try:
                os.kill(escaped_pid, signal.SIGKILL)
            except ProcessLookupError:
                pass


def _normal_exit_escape(tmp_path: Path, run) -> tuple[int, Path, object]:
    escaped_pid_path = tmp_path / "normal-escape.pid"
    delayed_marker = tmp_path / "delayed-mutation"
    child_code = (
        "import os, time; from pathlib import Path; "
        "os.setsid(); "
        f"Path({str(escaped_pid_path)!r}).write_text(str(os.getpid())); "
        "time.sleep(0.8); "
        f"Path({str(delayed_marker)!r}).write_text('late')"
    )
    parent = tmp_path / "normal_escape_parent.py"
    parent.write_text(
        "import subprocess, sys, time\n"
        "from pathlib import Path\n"
        f"pid_path = Path({str(escaped_pid_path)!r})\n"
        f"subprocess.Popen([sys.executable, '-c', {child_code!r}], "
        "stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, "
        "stderr=subprocess.DEVNULL)\n"
        "deadline = time.monotonic() + 5\n"
        "while not pid_path.exists() and time.monotonic() < deadline:\n"
        "    time.sleep(0.01)\n",
        encoding="utf-8",
    )
    outcome = run([sys.executable, str(parent)])
    return int(escaped_pid_path.read_text(encoding="utf-8")), delayed_marker, outcome


def _assert_pid_gone(pid: int) -> None:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return
    raise AssertionError(f"escaped process {pid} is still alive")


def test_linux_subreaper_ends_a_setsid_descendant_after_normal_parent_exit(tmp_path):
    if not sys.platform.startswith("linux"):
        pytest.skip("the child subreaper is Linux-only")
    from local_agent.tools.process_runner import OwnedLifecycle, run_owned

    pid, marker, owned = _normal_exit_escape(
        tmp_path,
        lambda command: run_owned(
            command, tmp_path, OwnedLifecycle(10), env=dict(os.environ)
        ),
    )
    assert owned.containment == "child_subreaper"
    assert owned.stray_descendants_at_exit == 1
    assert owned.cleanup_confirmed is True
    assert owned.strays_unconfirmed is False
    _assert_pid_gone(pid)
    time.sleep(1.0)
    assert not marker.exists(), "the escaped descendant mutated after the terminal result"


def test_public_command_does_not_accept_an_abandoned_setsid_descendant(tmp_path):
    if not sys.platform.startswith("linux"):
        pytest.skip("the child subreaper is Linux-only")

    pid, marker, outcome = _normal_exit_escape(
        tmp_path,
        lambda command: run_command(command, tmp_path, tmp_path / "runs", 10),
    )
    assert outcome.containment == "child_subreaper"
    assert outcome.stray_descendants_at_exit == 1
    assert outcome.process_cleanup_confirmed is True
    assert outcome.ok is False
    _assert_pid_gone(pid)
    time.sleep(1.0)
    assert not marker.exists(), "the escaped descendant mutated after the terminal result"


def _nested_pid_namespace_available() -> bool:
    import shutil
    import subprocess

    unshare = shutil.which("unshare")
    if unshare is None:
        return False
    probe = subprocess.run(  # noqa: S603 - fixed argv probe
        [unshare, "--user", "--pid", "--fork", "true"],
        capture_output=True, timeout=10, check=False,
    )
    return probe.returncode == 0


def test_linux_subreaper_ends_a_nested_pid_namespace_init(tmp_path):
    """A reparented nested-namespace init is signalled at the supervisor's own depth.

    Its innermost NSpid is 1. Signalling that PID targets the supervisor's own
    namespace init (SIGKILL ignored), so the nested tree is never signalled: it keeps
    running through cleanup, and either exits on its own (and is then "confirmed") or
    outlives the drain bound and survives the run.
    """
    if not sys.platform.startswith("linux") or not _nested_pid_namespace_available():
        pytest.skip("unprivileged nested PID namespaces are not available on this host")
    from local_agent.tools.process_runner import OwnedLifecycle, run_owned

    marker = tmp_path / "nested-mutation"
    inner = f"sleep 0.8; echo late > {str(marker)!r}"
    script = f"unshare --user --pid --fork sh -c {inner!r} & sleep 0.3; exit 0"
    started = time.monotonic()
    owned = run_owned(["sh", "-c", script], tmp_path, OwnedLifecycle(10), env=dict(os.environ))
    elapsed = time.monotonic() - started
    assert owned.containment == "child_subreaper"
    assert owned.cleanup_confirmed is True
    assert owned.strays_unconfirmed is False
    assert owned.stray_descendants_at_exit is not None and owned.stray_descendants_at_exit >= 1
    assert elapsed < 5.0, "the drain ran to its bound instead of ending the nested tree"
    time.sleep(1.2)
    assert not marker.exists(), "the nested-namespace descendant was never signalled"
