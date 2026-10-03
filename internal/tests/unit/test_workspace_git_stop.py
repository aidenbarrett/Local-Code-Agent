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


def test_stop_never_cuts_a_writing_step_and_refuses_the_next(tmp_path, monkeypatch):
    marker = tmp_path / "marker"
    _fake_git(tmp_path, marker, 3)
    manager = _manager(tmp_path)
    monkeypatch.setenv("PATH", str(tmp_path / "bin") + os.pathsep + os.environ["PATH"])

    with manager.stoppable(_StopAfter(0.5)):
        done = manager._git(tmp_path, "apply", "-")  # a write: runs to the end
        assert done.returncode == 0
        assert marker.read_text(encoding="utf-8").startswith("finished")
        with pytest.raises(WorkspaceStoppedError, match="did not start"):
            manager._git(tmp_path, "status")


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


class _StopOnGit:
    """A Stop token the user presses as soon as git runs ``verb`` in ``where``."""

    def __init__(self, manager: GitWorkspaceManager, verb: str, where=None) -> None:
        self.requested = False
        real = manager._git

        def watched(cwd, *args, **kwargs):
            if args and args[0] == verb and (where is None or Path(cwd) != where):
                self.requested = True
            return real(cwd, *args, **kwargs)

        manager._git = watched


def test_public_stop_during_the_candidate_snapshot_creates_nothing(sandbox, tmp_path):
    """Through the public candidate route: Stop while the checkout is being snapshotted."""
    from test_candidate_change_journey import _controller, _fixing_turns

    sandbox.scenario("compile_error")
    controller, manager = _controller(sandbox.root, tmp_path, _fixing_turns())
    stop = _StopOnGit(manager, "diff")  # the snapshot's dirty-path diff, before worktree add
    status = subprocess.run(["git", "status", "--porcelain=v1"], cwd=sandbox.root,
                            capture_output=True, text=True, check=True).stdout

    result = controller.run("fix the build", task_id=str(uuid4()),
                            skill_name="fix-build-failure", cancellation_probe=stop)

    assert result.outcome.value == "blocked" and result.reason_code == "cancelled", result.answer
    assert "Nothing was created" in result.answer
    assert not any(manager.workspaces_root.glob("*.lease"))
    assert not [p for p in manager.workspaces_root.iterdir() if p.is_dir() and p.name != ".no-hooks"]
    assert subprocess.run(["git", "status", "--porcelain=v1"], cwd=sandbox.root,
                          capture_output=True, text=True, check=True).stdout == status


def test_public_stop_during_settle_retains_nothing(sandbox, tmp_path):
    """Stop while the finished candidate is being snapshotted for review."""
    from test_candidate_change_journey import _controller, _fixing_turns

    sandbox.scenario("compile_error")
    controller, manager = _controller(sandbox.root, tmp_path, _fixing_turns())
    # `add -A` runs in the candidate worktree only at settle. It writes, so it finishes;
    # the next step is refused.
    stop = _StopOnGit(manager, "add", where=sandbox.root)

    result = controller.run("fix the build", task_id=str(uuid4()),
                            skill_name="fix-build-failure", cancellation_probe=stop)

    assert result.outcome.value == "blocked" and result.reason_code == "cancelled", result.answer
    assert result.metrics["candidate"] == {"retained": False, "stopped": True}
    assert not any(manager.workspaces_root.glob("*.candidate.json"))
    assert not any(manager.workspaces_root.glob("*.lease"))
