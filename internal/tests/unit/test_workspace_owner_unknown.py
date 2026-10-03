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


@pytest.mark.parametrize("owner", [None, "", "not-a-token", "123", "abc:1.0", "12:not-a-time", 7,
                                   "LIVE:nan", "LIVE:inf", "LIVE:-1.000000", "LIVE:1.0", "LIVE:1e3"])
def test_a_missing_or_malformed_owner_keeps_its_workspace(tmp_path, owner):
    """Sol's #410 review: a non-canonical token for a LIVE pid must not read as 'gone'."""
    manager, task_id, ws = _setup(tmp_path)
    if isinstance(owner, str) and owner.startswith("LIVE:"):
        owner = owner.replace("LIVE", str(os.getpid()), 1)
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


def test_the_real_live_owner_keeps_its_workspace(tmp_path):
    """On the real process table. Kept is unconditional. Whether the owner reads as live
    or unknown depends on the host's process permissions (Sol's restricted host cannot
    read its own start time); both are correct, and the deterministic test above proves
    a readable live owner is never unknown."""
    manager, task_id, ws = _setup(tmp_path)
    assert manager.reap_orphans() == ()
    assert _kept(manager, task_id, ws)
    readable = GitWorkspaceManager._owner_token(os.getpid()) is not None
    assert manager.unreconciled_leases == (() if readable else (task_id,))


class _Processes:
    """A deterministic process table for psutil.Process: pid -> start time or error."""

    def __init__(self, table):
        self.table = table

    def __call__(self, pid):
        entry = self.table.get(pid, psutil.NoSuchProcess(pid))
        if isinstance(entry, BaseException):
            raise entry
        return _FakeProcess(entry)


class _FakeProcess:
    def __init__(self, started):
        self._started = started

    def create_time(self):
        return self._started


def test_a_readable_live_owner_is_live_not_unknown(tmp_path, monkeypatch):
    """Sol's #410 re-review: live-owner observation independent of the host's process
    permissions. The real-process test below cannot be 'not unknown' everywhere."""
    monkeypatch.setattr(psutil, "Process", _Processes({os.getpid(): 1700000000.25}))
    manager, task_id, ws = _setup(tmp_path)
    assert manager.reap_orphans() == ()
    assert _kept(manager, task_id, ws)
    assert manager.unreconciled_leases == ()


def test_a_deterministically_gone_owner_is_reaped(tmp_path, monkeypatch):
    processes = _Processes({os.getpid(): 1700000000.25})
    monkeypatch.setattr(psutil, "Process", processes)
    manager, task_id, ws = _setup(tmp_path)
    del processes.table[os.getpid()]
    assert manager.reap_orphans() == (task_id,)
    assert not ws.root.exists()
    assert manager.unreconciled_leases == ()


@pytest.mark.parametrize("error", [psutil.AccessDenied(1), psutil.Error(), OSError("EIO"),
                                   PermissionError("denied")])
def test_a_transient_observation_error_keeps_the_workspace(tmp_path, monkeypatch, error):
    processes = _Processes({os.getpid(): 1700000000.25})
    monkeypatch.setattr(psutil, "Process", processes)
    manager, task_id, ws = _setup(tmp_path)
    processes.table[os.getpid()] = error
    assert manager.reap_orphans() == ()
    assert _kept(manager, task_id, ws)
    assert manager.unreconciled_leases == (task_id,)


@pytest.mark.parametrize("error", [psutil.AccessDenied(1), psutil.Error(), OSError("EIO")])
def test_a_workspace_created_without_a_readable_owner_is_kept_as_unknown(
    tmp_path, monkeypatch, error,
):
    """Creation: an unreadable own start time is recorded as unknown, never guessed."""
    processes = _Processes({os.getpid(): error})
    monkeypatch.setattr(psutil, "Process", processes)
    manager, task_id, ws = _setup(tmp_path)
    processes.table[os.getpid()] = 1700000000.25  # readable again at reap time
    assert manager.reap_orphans() == ()
    assert _kept(manager, task_id, ws)
    assert manager.unreconciled_leases == (task_id,)


def test_session_hub_startup_keeps_an_unknown_workspace_and_says_so(sandbox, tmp_path, capsys):
    """The public composition: the Hub's own startup reap over its own workspace root."""
    from test_session_hub_product_path import _load_hub

    from local_agent.config import ModelConfig, load_repo_config
    from local_agent.session.conversation_gateway import conversation_budgets
    from local_agent.session.conversation_store import (
        conversation, create_session, ensure_runtime, new_session,
    )
    from local_agent.session.runtime_facts import RuntimeFacts

    hub = _load_hub()
    runtime = tmp_path / "runtime"
    left = GitWorkspaceManager(runtime / "ws", controller_commit="c" * 40)
    task_id = str(uuid4())
    ws = left.create(sandbox.root, task_id)
    _set_owner(left, task_id, None)
    capsys.readouterr()

    chat = ModelConfig()
    budgets = conversation_budgets(chat.context_budget_tokens)
    session = new_session("fixture", chat.model, chat.device, budget_chars=budgets["request_chars"])
    create_session(runtime, session)
    facts = RuntimeFacts.observe("fixture", chat, execution_enabled=True,
                                 fetch=lambda _url: b'{"data":[{"id":"m"}]}')
    with conversation(runtime, session.conversation_id) as opened:
        runtime_index = ensure_runtime(opened.session, "fixture", chat.model, chat.device)
        service, _recovered = hub._open_durable_service(runtime, session.conversation_id)
        try:
            graph = hub.compose_session_graph(
                service, load_repo_config(sandbox.root), chat, chat,
                runtime_facts=facts, opened=opened, runtime_index=runtime_index,
                runtime_root=runtime, allow_execution=True, budgets=budgets,
            )
        finally:
            service.close()

    err = capsys.readouterr().err
    assert "1 candidate workspace(s)" in err and "were kept" in err, err
    assert ws.root.exists()
    assert graph.workspaces.unreconciled_leases == (task_id,)
