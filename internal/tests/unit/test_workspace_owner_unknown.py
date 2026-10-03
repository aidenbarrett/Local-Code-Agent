"""Unknown is not authority to delete a workspace (#397).

`_owner_token` turns AccessDenied into None, and the reaper deleted any lease whose
owner did not equal that token, so a live workspace whose owner could not be read was
force-removed (the `test_live_owner_is_never_reaped` failure on restricted runners).
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
from uuid import uuid4

import psutil
import pytest

from local_agent.session.workspaces import GitWorkspaceManager


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", "-c", "user.email=t@e.invalid", "-c", "user.name=t", *args],
                   cwd=str(cwd), check=True, capture_output=True)


def _setup(tmp_path: Path):
    user = tmp_path / "user"
    user.mkdir()
    (user / "a.txt").write_text("a\n", encoding="utf-8")
    _git(user, "init", "-q")
    _git(user, "add", "-A")
    _git(user, "commit", "-qm", "base")
    manager = GitWorkspaceManager(tmp_path / "ws", controller_commit="c" * 40)
    task_id = str(uuid4())
    ws = manager.create(user, task_id)
    return manager, task_id, ws


def _set_owner(manager, task_id, owner) -> None:
    lease = manager.workspaces_root / f"{task_id}.lease"
    record = json.loads(lease.read_text(encoding="utf-8"))
    record["owner"] = owner
    lease.write_text(json.dumps(record), encoding="utf-8")


def _kept(manager, task_id, ws) -> bool:
    return ws.root.exists() and (manager.workspaces_root / f"{task_id}.lease").exists()


def test_an_owner_that_cannot_be_read_keeps_its_workspace(tmp_path, monkeypatch):
    manager, task_id, ws = _setup(tmp_path)

    def denied(pid):
        raise psutil.AccessDenied(pid)

    monkeypatch.setattr(psutil, "Process", denied)
    assert manager.reap_orphans() == ()
    assert _kept(manager, task_id, ws)
    assert manager.unreconciled_leases == (task_id,)


@pytest.mark.parametrize("owner", [None, "", "not-a-token", "123", "abc:1.0", "12:not-a-time", 7])
def test_a_missing_or_malformed_owner_keeps_its_workspace(tmp_path, owner):
    manager, task_id, ws = _setup(tmp_path)
    _set_owner(manager, task_id, owner)
    assert manager.reap_orphans() == ()
    assert _kept(manager, task_id, ws)
    assert manager.unreconciled_leases == (task_id,)


def test_an_exited_owner_is_reaped(tmp_path):
    manager, task_id, ws = _setup(tmp_path)
    child = subprocess.Popen([sys.executable, "-c", "pass"])
    token = GitWorkspaceManager._owner_token(child.pid)
    child.wait()
    if token is None:
        pytest.skip("the child exited before its start time could be read")
    _set_owner(manager, task_id, token)
    assert manager.reap_orphans() == (task_id,)
    assert not ws.root.exists()
    assert manager.unreconciled_leases == ()


def test_a_reused_pid_is_a_different_owner_and_is_reaped(tmp_path):
    manager, task_id, ws = _setup(tmp_path)
    _set_owner(manager, task_id, f"{os.getpid()}:1.000000")  # this PID, another start time
    assert manager.reap_orphans() == (task_id,)
    assert not ws.root.exists()


def test_the_live_owner_keeps_its_workspace_and_is_not_unknown(tmp_path):
    manager, task_id, ws = _setup(tmp_path)
    assert manager.reap_orphans() == ()
    assert _kept(manager, task_id, ws)
    assert manager.unreconciled_leases == ()
