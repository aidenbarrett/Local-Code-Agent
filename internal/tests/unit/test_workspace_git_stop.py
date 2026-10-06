"""Stop reaches candidate workspace git, without ever cutting a write in half (#415).

Before this, a Stop during the candidate snapshot or settle could not end a running git
step: workspace git took no Stop token at all. Now a requested Stop refuses the next
step, ends a step that only reads, lets a step that writes finish, and never blocks
removing a worktree.
"""
from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import time
from uuid import uuid4

import psutil
import pytest

from local_agent.session import workspaces
from local_agent.session.workspaces import GitWorkspaceManager, WorkspaceStoppedError

_SLOW = """\
import pathlib, subprocess, sys, time
grandchild = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
marker = pathlib.Path(sys.argv[1])
marker.write_text(f"started {grandchild.pid}\\n")
time.sleep(float(sys.argv[2]))
marker.write_text(f"finished {grandchild.pid}\\n")
grandchild.kill()
"""


class _StopAfter:
    """A Stop token that becomes requested a fixed time after it is armed."""

    def __init__(self, seconds: float | None = None) -> None:
        self.at = None if seconds is None else time.monotonic() + seconds

    @property
    def requested(self) -> bool:
        return self.at is not None and time.monotonic() >= self.at


class _Stopped:
    requested = True


def _fake_git(tmp_path: Path, marker: Path, seconds: float) -> None:
    """A ``git`` on PATH that runs ``seconds`` with a grandchild, recording its progress."""
    script = tmp_path / "slow.py"
    script.write_text(_SLOW, encoding="utf-8")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    if os.name == "nt":
        (bin_dir / "git.cmd").write_text(
            f'@"{sys.executable}" "{script}" "{marker}" {seconds}\r\n', encoding="utf-8")
    else:
        fake = bin_dir / "git"
        fake.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{script}" "{marker}" {seconds}\n',
                        encoding="utf-8")
        fake.chmod(0o755)


def _gone(pid: int) -> bool:
    try:
        return psutil.Process(pid).status() == psutil.STATUS_ZOMBIE
    except psutil.NoSuchProcess:
        return True


def _manager(tmp_path: Path) -> GitWorkspaceManager:
    return GitWorkspaceManager(tmp_path / "ws", controller_commit="c" * 40)


def test_a_requested_stop_refuses_the_next_step_before_it_starts(tmp_path, monkeypatch):
    marker = tmp_path / "marker"
    _fake_git(tmp_path, marker, 0)
    manager = _manager(tmp_path)
    monkeypatch.setenv("PATH", str(tmp_path / "bin") + os.pathsep + os.environ["PATH"])
    with manager.stoppable(_Stopped()), pytest.raises(WorkspaceStoppedError, match="did not start"):
        manager._git(tmp_path, "status")
    assert not marker.exists(), "git ran after Stop was requested"


def test_stop_ends_a_reading_step_and_its_whole_tree(tmp_path, monkeypatch):
    marker = tmp_path / "marker"
    _fake_git(tmp_path, marker, 60)
    manager = _manager(tmp_path)
    monkeypatch.setenv("PATH", str(tmp_path / "bin") + os.pathsep + os.environ["PATH"])

    started = time.monotonic()
    with manager.stoppable(_StopAfter(1.5)), pytest.raises(WorkspaceStoppedError, match="Stop ended"):
        manager._git(tmp_path, "status")
    elapsed = time.monotonic() - started

    assert elapsed < 20, elapsed
    text = marker.read_text(encoding="utf-8")
    assert text.startswith("started"), text
    grandchild = int(text.split()[1])
    deadline = time.monotonic() + 5
    while not _gone(grandchild) and time.monotonic() < deadline:
        time.sleep(0.05)
    assert _gone(grandchild), "the stopped git step left its tree running"


def test_stop_during_the_final_write_lets_it_finish_and_is_reported_by_that_call(
    tmp_path, monkeypatch,
):
    """No second git call is needed to notice a Stop that arrived during a write.

    Old code relied on the next step to refuse; when the write was an operation's last
    step, the Stop was lost and the operation returned success (#423 review).
    """
    marker = tmp_path / "marker"
    _fake_git(tmp_path, marker, 3)
    manager = _manager(tmp_path)
    monkeypatch.setenv("PATH", str(tmp_path / "bin") + os.pathsep + os.environ["PATH"])

    with manager.stoppable(_StopAfter(0.5)), pytest.raises(WorkspaceStoppedError) as stopped:
        manager._git(tmp_path, "apply", "-")  # a write: runs to the end
    # The write was never cut: its own marker says it finished.
    assert marker.read_text(encoding="utf-8").startswith("finished")
    assert "it finished and nothing after it ran" in str(stopped.value)
    assert stopped.value.writes_finished == ("apply",)
    assert stopped.value.process_cleanup_confirmed is None


def test_outside_a_stoppable_block_nothing_is_stopped(tmp_path, monkeypatch):
    marker = tmp_path / "marker"
    _fake_git(tmp_path, marker, 0)
    manager = _manager(tmp_path)
    monkeypatch.setenv("PATH", str(tmp_path / "bin") + os.pathsep + os.environ["PATH"])
    with manager.stoppable(_Stopped()):
        pass
    assert manager._git(tmp_path, "status").returncode == 0


def _repo(tmp_path: Path) -> Path:
    root = tmp_path / "user"
    root.mkdir()
    (root / "a.txt").write_text("a\n", encoding="utf-8")
    for args in (("init", "-q"), ("add", "-A"), ("commit", "-qm", "base")):
        subprocess.run(["git", "-c", "user.email=t@e.invalid", "-c", "user.name=t", *args],
                       cwd=root, check=True, capture_output=True)
    return root


def test_removing_a_worktree_is_never_stoppable(tmp_path):
    root = _repo(tmp_path)
    manager = _manager(tmp_path)
    ws = manager.create(root, str(uuid4()))
    with manager.stoppable(_Stopped()):
        manager.discard(ws)
    assert not ws.root.exists()
    assert not (manager.workspaces_root / f"{ws.task_id}.lease").exists()
    listed = subprocess.run(["git", "worktree", "list", "--porcelain"], cwd=root,
                            capture_output=True, text=True, check=True).stdout
    assert str(ws.root) not in listed


def _status(root: Path) -> str:
    return subprocess.run(["git", "status", "--porcelain=v1"], cwd=root,
                          capture_output=True, text=True, check=True).stdout


def _stop_during(monkeypatch, stop: _Armed, verb: str, where: Path | None = None) -> None:
    """Press Stop while git ``verb`` is executing: after its process started, before
    the call returns. The step itself is real git."""
    real = workspaces.run_owned

    def during(argv, cwd, lifecycle, **kwargs):
        if verb in argv and (where is None or Path(cwd) != where):
            if lifecycle.cancellation_probe is None:
                # A write: it runs to completion; Stop lands while it is running.
                run = real(argv, cwd, lifecycle, **kwargs)
                stop.requested = True
                return run
            stop.requested = True  # a read: Stop is seen by the owned runner itself
        return real(argv, cwd, lifecycle, **kwargs)

    monkeypatch.setattr(workspaces, "run_owned", during)


class _Armed:
    requested = False


class _CountingWorker:
    def __init__(self, turns) -> None:
        from test_candidate_change_journey import ScriptedClient

        self.started = 0
        self._turns = turns
        self._client = ScriptedClient

    def __call__(self):
        self.started += 1
        return self._client(list(self._turns))


def _public(sandbox, tmp_path):
    from test_candidate_change_journey import _controller, _fixing_turns

    sandbox.scenario("compile_error")
    controller, manager = _controller(sandbox.root, tmp_path, _fixing_turns())
    worker = _CountingWorker(_fixing_turns())
    controller.worker_factory = worker
    return controller, manager, worker


def _leftovers(manager: GitWorkspaceManager) -> list[Path]:
    return [p for p in manager.workspaces_root.iterdir()
            if p.is_dir() and p.name != ".no-hooks"]


def test_public_stop_during_the_final_worktree_add_never_starts_the_worker(
    sandbox, tmp_path, monkeypatch,
):
    """`worktree add` is creation's last step and a write: Stop during it must still
    stop the task, remove the new worktree and never invoke the model worker."""
    controller, manager, worker = _public(sandbox, tmp_path)
    stop = _Armed()
    _stop_during(monkeypatch, stop, "worktree")
    before = _status(sandbox.root)

    result = controller.run("fix the build", task_id=str(uuid4()),
                            skill_name="fix-build-failure", cancellation_probe=stop)

    assert result.outcome.value == "blocked" and result.reason_code == "cancelled", result.answer
    assert worker.started == 0, "the worker ran after Stop"
    evidence = result.metrics["candidate"]["stop"]
    assert evidence["step"] == "worktree"
    assert "worktree" in evidence["writes_finished"]
    assert evidence["worktree_removed"] is True
    assert not any(manager.workspaces_root.glob("*.lease"))
    assert not _leftovers(manager)
    listed = subprocess.run(["git", "worktree", "list", "--porcelain"], cwd=sandbox.root,
                            capture_output=True, text=True, check=True).stdout
    assert listed.count("worktree ") == 1
    assert _status(sandbox.root) == before
    assert "unreferenced objects" in result.answer


def test_public_stop_with_unconfirmed_cleanup_is_reported_as_unconfirmed(
    sandbox, tmp_path, monkeypatch,
):
    """A stopped read whose tree end could not be confirmed is not 'cleanly cancelled'."""
    from local_agent.tools.process_runner import OwnedRun

    controller, manager, worker = _public(sandbox, tmp_path)
    stop = _Armed()
    real = workspaces.run_owned

    def unconfirmed(argv, cwd, lifecycle, **kwargs):
        if "diff" in argv and lifecycle.cancellation_probe is not None:
            stop.requested = True
            return OwnedRun(
                exit_code=130, stdout=b"", stderr=b"", elapsed_s=0.1, timed_out=False,
                cancel_requested=True, cleanup_confirmed=False,
                containment="process_group", stray_descendants_at_exit=None,
                strays_unconfirmed=False,
            )
        return real(argv, cwd, lifecycle, **kwargs)

    monkeypatch.setattr(workspaces, "run_owned", unconfirmed)
    result = controller.run("fix the build", task_id=str(uuid4()),
                            skill_name="fix-build-failure", cancellation_probe=stop)

    assert (result.outcome.value, result.reason_code) == ("no_verdict", "cleanup_unknown"), result.answer
    assert worker.started == 0
    assert result.metrics["candidate"]["stop"]["process_cleanup_confirmed"] is False
    assert "could not be confirmed" in result.answer
    assert "No candidate exists" in result.answer
    # Retry is possible: nothing holds the next attempt's lease or worktree slot.
    monkeypatch.setattr(workspaces, "run_owned", real)
    assert not any(manager.workspaces_root.glob("*.lease"))
    ws = manager.create(sandbox.root, str(uuid4()))
    manager.discard(ws)


def test_public_stop_when_the_half_made_copy_cannot_be_removed_keeps_its_lease(
    sandbox, tmp_path, monkeypatch,
):
    controller, manager, worker = _public(sandbox, tmp_path)
    stop = _Armed()
    _stop_during(monkeypatch, stop, "worktree")
    monkeypatch.setattr(manager, "_remove_worktree", lambda *_args: False)

    result = controller.run("fix the build", task_id=str(uuid4()),
                            skill_name="fix-build-failure", cancellation_probe=stop)

    assert (result.outcome.value, result.reason_code) == ("no_verdict", "cleanup_unknown"), result.answer
    assert worker.started == 0
    assert result.metrics["candidate"]["stop"]["worktree_removed"] is False
    assert "could not be shown removed" in result.answer
    # The lease stays so orphan cleanup can decide; it is never silently dropped.
    assert any(manager.workspaces_root.glob("*.lease"))


def test_public_stop_while_settle_writes_retains_nothing(sandbox, tmp_path, monkeypatch):
    """Stop lands while settle's `add -A` runs in the candidate worktree (a write)."""
    controller, manager, worker = _public(sandbox, tmp_path)
    stop = _Armed()
    _stop_during(monkeypatch, stop, "add", where=sandbox.root)

    result = controller.run("fix the build", task_id=str(uuid4()),
                            skill_name="fix-build-failure", cancellation_probe=stop)

    assert result.outcome.value == "blocked" and result.reason_code == "cancelled", result.answer
    assert worker.started == 1
    candidate = result.metrics["candidate"]
    assert candidate["retained"] is False and candidate["workspace_removed"] is True
    assert candidate["stop"]["step"] == "add"
    assert not any(manager.workspaces_root.glob("*.candidate.json"))
    assert not any(manager.workspaces_root.glob("*.lease"))
    assert not _leftovers(manager)


def test_stop_after_a_final_read_exits_is_not_lost(tmp_path, monkeypatch):
    """Stop lands after a read process exited normally but before _git returns.
    Old code only checked the runner's snapshot, so the call returned success."""
    root = _repo(tmp_path)
    manager = _manager(tmp_path)
    stop = _Armed()
    real = workspaces.run_owned

    def read_then_stop(argv, cwd, lifecycle, **kwargs):
        run = real(argv, cwd, lifecycle, **kwargs)
        stop.requested = True  # pressed just as the read finished
        return run

    monkeypatch.setattr(workspaces, "run_owned", read_then_stop)
    with manager.stoppable(stop), pytest.raises(WorkspaceStoppedError) as stopped:
        manager._git(root, "status", "--porcelain")
    assert stopped.value.step == "status"
    assert stopped.value.process_cleanup_confirmed is None  # the read exited by itself
    assert "finished reading" in str(stopped.value)


def _unremovable(manager: GitWorkspaceManager, monkeypatch) -> None:
    """git refuses to remove the worktree and the directory cannot be deleted."""
    import subprocess as sp

    real_git = manager._git

    def refusing(cwd, *args, **kwargs):
        if args[:2] == ("worktree", "remove"):
            return sp.CompletedProcess(["git", *args], 1, b"", b"permission denied")
        return real_git(cwd, *args, **kwargs)

    monkeypatch.setattr(manager, "_git", refusing)
    monkeypatch.setattr(workspaces.shutil, "rmtree", lambda *_a, **_k: None)


def test_discarding_an_established_workspace_that_cannot_be_removed_keeps_its_lease(
    tmp_path, monkeypatch,
):
    """Old code unlinked the lease and returned success with the worktree still there."""
    root = _repo(tmp_path)
    manager = _manager(tmp_path)
    ws = manager.create(root, str(uuid4()))
    _unremovable(manager, monkeypatch)
    with pytest.raises(workspaces.WorkspaceError, match="could not be shown removed"):
        manager.discard(ws)
    assert ws.root.exists()
    assert (manager.workspaces_root / f"{ws.task_id}.lease").exists()
    monkeypatch.undo()
    manager.discard(ws)


def test_public_stop_during_settle_with_an_unremovable_candidate_is_cleanup_unknown(
    sandbox, tmp_path, monkeypatch,
):
    controller, manager, worker = _public(sandbox, tmp_path)
    stop = _Armed()
    _stop_during(monkeypatch, stop, "add", where=sandbox.root)
    real_git = manager._git

    def refusing(cwd, *args, **kwargs):
        if args[:2] == ("worktree", "remove"):
            import subprocess as sp
            return sp.CompletedProcess(["git", *args], 1, b"", b"permission denied")
        return real_git(cwd, *args, **kwargs)

    monkeypatch.setattr(manager, "_git", refusing)
    monkeypatch.setattr(workspaces.shutil, "rmtree", lambda *_a, **_k: None)

    result = controller.run("fix the build", task_id=str(uuid4()),
                            skill_name="fix-build-failure", cancellation_probe=stop)

    assert (result.outcome.value, result.reason_code) == ("no_verdict", "cleanup_unknown")
    assert result.metrics["candidate"]["workspace_removed"] is False
    assert any(manager.workspaces_root.glob("*.lease")), "the lease was dropped"
    assert not any(manager.workspaces_root.glob("*.candidate.json"))
