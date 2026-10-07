"""Settlement contract for owned command trees (#450 review on #452).

A descendant that was witnessed and conclusively ended before return is evidence, not
failure. Only an unknown settlement fails: a missing, malformed or late supervisor
report, or a tree that could not be shown gone. The runner never kills the Linux
supervisor before its own drain budget and a margin have passed.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time

import pytest

from local_agent.tools import process_runner
from local_agent.tools.process_runner import OwnedLifecycle, run_command, run_owned
from local_agent.tools.tool_primitives import Reason, ToolError

linux_only = pytest.mark.skipif(
    not sys.platform.startswith("linux"), reason="the child subreaper is Linux-only"
)
posix_only = pytest.mark.skipif(os.name == "nt", reason="the supervisor pipe is POSIX-only")


class _Exited:
    def __init__(self, code: int | None) -> None:
        self.code = code

    def poll(self) -> int | None:
        return self.code


_VALID = {"cleanup_confirmed": True, "leader_exit_code": 0, "setup_error": None,
          "stray_descendants": 2}


def _owner_with(payload: bytes, *, keep_writer: bool = False):
    owner = process_runner._PosixOwner.open()
    os.write(owner.write_fd, payload)
    if not keep_writer:
        owner.spawned()
    return owner


@posix_only
def test_a_valid_report_is_read_once_after_the_supervisor_exits():
    owner = _owner_with(json.dumps(_VALID).encode() + b"\n")
    report = owner.collect(_Exited(0))
    assert report is not None
    assert report.cleanup_confirmed is True and report.stray_descendants == 2
    assert owner.collect(_Exited(0)) is report
    owner.close()


@posix_only
def test_a_live_supervisor_is_never_read():
    owner = _owner_with(json.dumps(_VALID).encode())
    assert owner.collect(_Exited(None)) is None
    assert owner.read_fd >= 0, "the report was consumed before the supervisor exited"
    owner.close()


@posix_only
def test_a_write_end_still_held_open_is_unknown_not_a_hang():
    owner = _owner_with(b"", keep_writer=True)
    started = time.monotonic()
    assert owner.collect(_Exited(0)) is None
    assert time.monotonic() - started < 1.0
    owner.close()


@posix_only
@pytest.mark.parametrize("payload", [
    b"",                                                          # EOF, no report
    b"not json",
    b"[]",
    json.dumps({**_VALID, "extra": 1}).encode(),
    json.dumps({k: v for k, v in _VALID.items() if k != "setup_error"}).encode(),
    json.dumps({**_VALID, "cleanup_confirmed": "true"}).encode(),
    json.dumps({**_VALID, "stray_descendants": True}).encode(),
    json.dumps({**_VALID, "stray_descendants": -1}).encode(),
    json.dumps({**_VALID, "leader_exit_code": False}).encode(),
    json.dumps({**_VALID, "setup_error": 3}).encode(),
    json.dumps(_VALID).encode()                                   # oversized, even
    + b" " * process_runner._SUPERVISOR_REPORT_LIMIT,             # with a valid prefix
])
def test_a_missing_malformed_or_oversized_report_is_unknown(payload):
    owner = _owner_with(payload)
    assert owner.collect(_Exited(0)) is None
    owner.close()


def _stub_supervisor(tmp_path: Path, body: str) -> Path:
    stub = tmp_path / "stub_supervisor.py"
    stub.write_text(
        "import json, os, signal, subprocess, sys, time\n"
        "fd = int(os.environ['LCA_POSIX_SUPERVISOR_FD'])\n"
        "args = sys.argv[1:]\n"
        "args = args[args.index('--') + 1:]\n" + body,
        encoding="utf-8",
    )
    return stub


def _use_stub(monkeypatch, stub: Path) -> None:
    monkeypatch.setattr(
        process_runner._PosixOwner, "argv",
        lambda self, command: [sys.executable, str(stub), "--", *command],
    )


@linux_only
@pytest.mark.parametrize("body", [
    "raise SystemExit(subprocess.call(args))\n",                  # exits, never reports
    "os.write(fd, b'{\"cleanup_confirmed\": true}\\n')\n"          # partial report
    "raise SystemExit(subprocess.call(args))\n",
    "os.kill(os.getpid(), signal.SIGKILL)\n",                     # crashes
])
def test_an_unreported_settlement_fails_the_public_result_closed(tmp_path, monkeypatch, body):
    _use_stub(monkeypatch, _stub_supervisor(tmp_path, body))
    with pytest.raises(ToolError) as refused:
        run_command([sys.executable, "-c", "print('ok')"], tmp_path, tmp_path / "runs", 10)
    assert refused.value.reason is Reason.CLEANUP_UNKNOWN


@linux_only
def test_an_unreported_settlement_fails_workspace_git_closed(tmp_path, monkeypatch):
    from local_agent.session.workspaces import GitWorkspaceManager, WorkspaceError

    _use_stub(monkeypatch, _stub_supervisor(
        tmp_path, "raise SystemExit(subprocess.call(args))\n"))
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)  # noqa: S607
    manager = GitWorkspaceManager(tmp_path / "ws", controller_commit="test")
    with pytest.raises(WorkspaceError, match="could not be shown ended"):
        manager._git(tmp_path, "status")


@linux_only
def test_the_runner_never_kills_the_supervisor_inside_its_drain_budget(tmp_path, monkeypatch):
    """A drain slower than the old 2 s post-kill wait still finishes and reports."""
    drained = tmp_path / "drained"
    slow = 3.0
    assert slow > process_runner._POST_KILL_WAIT_S
    assert slow < process_runner._SUPERVISOR_DRAIN_S
    _use_stub(monkeypatch, _stub_supervisor(tmp_path, (
        "def stop(*_):\n"
        f"    time.sleep({slow})\n"
        "    os.write(fd, json.dumps({'cleanup_confirmed': True, 'leader_exit_code': None,"
        " 'setup_error': None, 'stray_descendants': 0}).encode())\n"
        f"    open({str(drained)!r}, 'w').write('done')\n"
        "    os._exit(143)\n"
        "signal.signal(signal.SIGTERM, stop)\n"
        "while True:\n"
        "    time.sleep(1)\n"
    )))
    timeout = 0.3
    started = time.monotonic()
    owned = run_owned(["true"], tmp_path, OwnedLifecycle(timeout), env=dict(os.environ))
    elapsed = time.monotonic() - started
    assert owned.timed_out is True
    assert drained.exists(), "the supervisor was killed in the middle of its drain"
    assert owned.cleanup_confirmed is True
    bound = (timeout + process_runner._SUPERVISOR_DRAIN_S
             + process_runner._SUPERVISOR_SETTLE_MARGIN_S + 1.0)
    assert elapsed < bound, f"Stop took {elapsed:.1f}s, past its documented bound"


def _repo_with_loose_objects(root: Path) -> None:
    def git(*args: str) -> None:
        subprocess.run(["git", *args], cwd=root, check=True,  # noqa: S603, S607
                       capture_output=True)

    git("init", "-q")
    git("config", "user.email", "t@example.invalid")
    git("config", "user.name", "t")
    blobs = root / ".blobs"
    blobs.mkdir()
    paths = []
    for index in range(1500):
        path = blobs / f"{index}"
        path.write_text(f"loose object {index}\n", encoding="utf-8")
        paths.append(str(path))
    subprocess.run(["git", "hash-object", "-w", "--stdin-paths"], cwd=root,  # noqa: S607
                   input="\n".join(paths), text=True, check=True, capture_output=True)
    shutil.rmtree(blobs)
    (root / "tracked.txt").write_text("x\n", encoding="utf-8")
    git("add", "tracked.txt")


@pytest.mark.skipif(shutil.which("git") is None, reason="git is required")
def test_a_commit_whose_detached_auto_gc_was_ended_is_not_reported_failed(tmp_path):
    """Reproduced on #452: HEAD advanced, and the step used to be refused."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _repo_with_loose_objects(repo)
    outcome = run_command(
        ["git", "-c", "gc.auto=1", "-c", "gc.autoDetach=true", "commit", "-qm", "c"],
        repo, tmp_path / "runs", 120,
    )
    head = subprocess.run(["git", "rev-parse", "--verify", "HEAD"], cwd=repo,  # noqa: S607
                          capture_output=True, text=True, check=False)
    assert head.returncode == 0, "the commit did not land"
    assert outcome.ok is True
    assert outcome.exit_code == 0
    if outcome.stray_descendants_at_exit:
        assert outcome.process_cleanup_confirmed is True
