"""Every workspace git process is an owned process tree (#415).

Workspace git used ``subprocess.run(..., timeout=...)``. On timeout that kills only the
direct child; a descendant (a commit signer and anything it started) keeps the output
pipe open, so the call then blocked until the descendant chose to exit, and the
descendant outlived the task. Now the whole tree is ended and the call returns.
"""
from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import time

import psutil
import pytest

from local_agent.session import workspaces
from local_agent.session.workspaces import GitWorkspaceManager, WorkspaceError

_CHILD = """\
import pathlib, subprocess, sys, time
grandchild = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
pathlib.Path(sys.argv[1]).write_text(str(grandchild.pid))
time.sleep(60)
"""


def _fake_git(bin_dir: Path, child: Path, pid_file: Path) -> None:
    """A ``git`` on PATH that starts a long-lived grandchild sharing its output."""
    bin_dir.mkdir()
    if os.name == "nt":
        (bin_dir / "git.cmd").write_text(
            f'@"{sys.executable}" "{child}" "{pid_file}"\r\n', encoding="utf-8")
    else:
        script = bin_dir / "git"
        script.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{child}" "{pid_file}"\n',
                          encoding="utf-8")
        script.chmod(0o755)


def _gone(pid: int) -> bool:
    try:
        return psutil.Process(pid).status() == psutil.STATUS_ZOMBIE
    except psutil.NoSuchProcess:
        return True


def _wait_for_pid(pid_file: Path) -> int:
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        if pid_file.exists() and pid_file.read_text().strip():
            return int(pid_file.read_text())
        time.sleep(0.05)
    raise AssertionError("the fake git never started its grandchild")


def test_a_git_tree_past_its_timeout_is_ended_and_the_call_returns(tmp_path, monkeypatch):
    child = tmp_path / "child.py"
    child.write_text(_CHILD, encoding="utf-8")
    pid_file = tmp_path / "grandchild.pid"
    _fake_git(tmp_path / "bin", child, pid_file)
    monkeypatch.setenv("PATH", str(tmp_path / "bin") + os.pathsep + os.environ["PATH"])
    monkeypatch.setattr(workspaces, "_GIT_TIMEOUT_S", 3)
    manager = GitWorkspaceManager(tmp_path / "ws", controller_commit="test")

    started = time.monotonic()
    with pytest.raises(WorkspaceError) as refused:
        manager._git(tmp_path, "status")
    elapsed = time.monotonic() - started

    grandchild = _wait_for_pid(pid_file)
    try:
        # Old code: the grandchild held the pipe, so this took the grandchild's 60 s.
        assert elapsed < 20, elapsed
        deadline = time.monotonic() + 5
        while not _gone(grandchild) and time.monotonic() < deadline:
            time.sleep(0.05)
        assert _gone(grandchild), "the git process tree outlived its timeout"
        assert "did not finish within 3 s" in str(refused.value)
    finally:
        if not _gone(grandchild):
            psutil.Process(grandchild).kill()


def test_git_output_beyond_the_limit_is_refused_not_truncated(tmp_path, monkeypatch):
    subprocess.run(["git", "init", "-q", str(tmp_path / "repo")], check=True)
    monkeypatch.setattr(workspaces, "_GIT_OUTPUT_LIMIT", 8)
    manager = GitWorkspaceManager(tmp_path / "ws", controller_commit="test")
    with pytest.raises(WorkspaceError, match="over the 8-byte limit"):
        manager._git(tmp_path / "repo", "version")


def test_stdin_reaches_git_and_bytes_come_back(tmp_path):
    repo = tmp_path / "repo"
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    manager = GitWorkspaceManager(tmp_path / "ws", controller_commit="test")
    done = manager._git(repo, "hash-object", "--stdin", stdin=b"hello\n")
    assert done.returncode == 0
    assert done.stdout.strip() == b"ce013625030ba8dba906f756967f9e9ca394464a"


@pytest.mark.skipif(os.name == "nt", reason="the POSIX signer script is a shell script")
def test_a_hung_commit_signer_is_ended_and_nothing_is_committed(tmp_path, monkeypatch):
    """The real case: /commit honours the user's signer, which can hang."""
    repo = tmp_path / "repo"
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    child = tmp_path / "child.py"
    child.write_text(_CHILD, encoding="utf-8")
    pid_file = tmp_path / "grandchild.pid"
    signer = tmp_path / "signer"
    signer.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{child}" "{pid_file}"\n',
                      encoding="utf-8")
    signer.chmod(0o755)
    for key, value in (("user.name", "t"), ("user.email", "t@e.invalid"),
                       ("commit.gpgsign", "true"), ("gpg.format", "openpgp"),
                       ("gpg.program", str(signer))):
        subprocess.run(["git", "config", key, value], cwd=repo, check=True)
    (repo / "a.txt").write_text("a\n", encoding="utf-8")
    subprocess.run(["git", "add", "a.txt"], cwd=repo, check=True)
    monkeypatch.setattr(workspaces, "_GIT_TIMEOUT_S", 3)
    manager = GitWorkspaceManager(tmp_path / "ws", controller_commit="test")

    started = time.monotonic()
    with pytest.raises(WorkspaceError) as refused:
        manager._git(repo, "commit", "-q", "-m", "signed")
    elapsed = time.monotonic() - started

    grandchild = _wait_for_pid(pid_file)
    try:
        assert elapsed < 20, elapsed
        deadline = time.monotonic() + 5
        while not _gone(grandchild) and time.monotonic() < deadline:
            time.sleep(0.05)
        assert _gone(grandchild), "the signer's descendant outlived the commit"
        head = subprocess.run(["git", "rev-parse", "--verify", "-q", "HEAD"], cwd=repo,
                              capture_output=True, check=False)
        assert head.returncode != 0, "a commit was made"
        assert "did not finish within 3 s" in str(refused.value)
    finally:
        if not _gone(grandchild):
            psutil.Process(grandchild).kill()
