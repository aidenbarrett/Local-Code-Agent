"""Candidate checks never run a program named in Git configuration (#398).

`_worktree_blob` used `hash-object --path`, which runs a configured clean filter: a
precondition check executed a repository-configured program.
"""
from __future__ import annotations

from pathlib import Path
import subprocess
import sys
from uuid import uuid4

import pytest

from local_agent.session import workspaces
from local_agent.session.workspaces import (
    CandidateRefusedError,
    CommitRefused,
    GitWorkspaceManager,
    WorkspaceError,
)


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", "-c", "user.email=t@e.invalid", "-c", "user.name=t", *args],
                   cwd=str(cwd), check=True, capture_output=True)


def _repo(tmp_path: Path) -> Path:
    root = tmp_path / "user"
    root.mkdir()
    (root / "base.txt").write_text("base\n", encoding="utf-8")
    (root / "code.cpp").write_text("int x;\n", encoding="utf-8")
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "t@e.invalid")
    _git(root, "config", "user.name", "t")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "base")
    return root


def _install_filter(root: Path, marker: Path) -> None:
    """A clean/smudge filter that leaves a marker whenever git runs it."""
    script = root.parent / "filter.py"
    script.write_text(
        "import sys\n"
        f"open({str(marker)!r}, 'a').write('ran\\n')\n"
        "sys.stdout.write(sys.stdin.read())\n",
        encoding="utf-8",
    )
    command = f'"{Path(sys.executable).as_posix()}" "{script.as_posix()}"'
    _git(root, "config", "filter.audit.clean", command)
    _git(root, "config", "filter.audit.smudge", command)
    (root / ".gitattributes").write_text("*.txt filter=audit\n", encoding="utf-8")


def _manager(tmp_path: Path) -> GitWorkspaceManager:
    return GitWorkspaceManager(tmp_path / "ws", controller_commit="c" * 40)


def test_a_repository_using_a_filter_is_not_ready_and_nothing_runs(tmp_path):
    root = _repo(tmp_path)
    marker = tmp_path / "marker"
    _install_filter(root, marker)
    manager = _manager(tmp_path)

    readiness = manager.readiness(root)

    assert not readiness.ready
    assert any("Git filter(s) audit" in problem for problem in readiness.problems)
    with pytest.raises(WorkspaceError, match="audit"):
        manager.create(root, str(uuid4()))
    assert not marker.exists(), "a candidate check ran the repository's filter"


def test_apply_undo_and_commit_refuse_once_a_filter_appears_without_running_it(tmp_path):
    root = _repo(tmp_path)
    manager = _manager(tmp_path)
    task_id = str(uuid4())
    ws = manager.create(root, task_id)
    (ws.root / "base.txt").write_text("changed\n", encoding="utf-8")
    candidate = manager.candidate_patch(ws)
    marker = tmp_path / "marker"
    _install_filter(root, marker)  # the user configures a filter after preparation

    imported = manager.import_patch(ws, candidate)
    assert imported.applied is False and "audit" in (imported.refused_reason or "")
    assert (root / "base.txt").read_text(encoding="utf-8") == "base\n"
    assert not marker.exists()
    manager.close(ws)


def test_commit_and_undo_refuse_when_a_filter_is_added_after_apply(tmp_path):
    root = _repo(tmp_path)
    manager = _manager(tmp_path)
    task_id = str(uuid4())
    ws = manager.create(root, task_id)
    (ws.root / "code.cpp").write_text("int y;\n", encoding="utf-8")
    candidate = manager.candidate_patch(ws)
    result = manager.import_patch(ws, candidate)
    manager.record_applied(task_id, root, candidate, result)
    manager.discard(ws)
    marker = tmp_path / "marker"
    _install_filter(root, marker)
    _git(root, "add", ".gitattributes")
    _git(root, "commit", "-qm", "attributes", "--no-verify")
    marker.unlink(missing_ok=True)  # the user's own commit may run it; ours must not

    committed = manager.commit_applied(task_id, root, "candidate")
    undone = manager.undo_applied(task_id, root)

    assert isinstance(committed, CommitRefused) and "audit" in committed.reason
    assert undone.undone is False and "audit" in (undone.refused_reason or "")
    assert not marker.exists()


def test_a_configured_but_unused_filter_and_autocrlf_stay_supported(tmp_path):
    """A global git-lfs style filter that no file here uses is not a reason to refuse."""
    root = _repo(tmp_path)
    marker = tmp_path / "marker"
    _install_filter(root, marker)
    (root / ".gitattributes").unlink()
    _git(root, "config", "core.autocrlf", "input")
    manager = _manager(tmp_path)
    assert manager.filter_drivers_in_use(root) == ()
    assert manager.readiness(root).ready, manager.readiness(root).problems
    ws = manager.create(root, str(uuid4()))
    manager.close(ws)
    assert not marker.exists()


def test_a_repository_without_filters_is_unaffected(tmp_path):
    root = _repo(tmp_path)
    assert _manager(tmp_path).filter_drivers_in_use(root) == ()


def _install_unused_filter(root: Path, marker: Path) -> None:
    """The audit filter configured, but no file of the repository uses it."""
    _install_filter(root, marker)
    (root / ".gitattributes").unlink()


def test_a_candidate_that_adds_filter_attributes_is_refused_before_staging(tmp_path):
    """Sol's #411 review: the worker's own .gitattributes ran the filter in `git add -A`."""
    root = _repo(tmp_path)
    marker = tmp_path / "marker"
    _install_unused_filter(root, marker)
    manager = _manager(tmp_path)
    ws = manager.create(root, str(uuid4()))
    (ws.root / ".gitattributes").write_text("*.txt filter=audit\n", encoding="utf-8")
    (ws.root / "base.txt").write_text("changed\n", encoding="utf-8")

    with pytest.raises(CandidateRefusedError, match="Git filter\\(s\\) audit"):
        manager.candidate_patch(ws)
    assert not marker.exists(), "staging the candidate ran the repository's filter"


def test_a_candidate_that_changes_attributes_is_refused_at_settle_and_import(tmp_path, monkeypatch):
    root = _repo(tmp_path)
    manager = _manager(tmp_path)
    ws = manager.create(root, str(uuid4()))
    (ws.root / "sub").mkdir()
    (ws.root / "sub" / ".gitattributes").write_text("*.txt eol=crlf\n", encoding="utf-8")
    (ws.root / "base.txt").write_text("changed\n", encoding="utf-8")

    with pytest.raises(CandidateRefusedError, match="changes Git attributes.*sub/.gitattributes"):
        manager.candidate_patch(ws)

    # A candidate retained before this check existed is still refused at import.
    monkeypatch.setattr(workspaces, "_attribute_files", lambda _paths: ())
    candidate = manager.candidate_patch(ws)
    monkeypatch.undo()
    before = (root / "base.txt").read_bytes()
    result = manager.import_patch(ws, candidate)
    assert result.applied is False
    assert "changes Git attributes" in (result.refused_reason or "")
    assert (root / "base.txt").read_bytes() == before
    assert not (root / "sub").exists()


@pytest.mark.parametrize("failing", ["config", "ls-files", "check-attr"])
def test_filter_discovery_that_cannot_be_read_refuses(tmp_path, monkeypatch, failing):
    """Unknown is not 'no filter in use' (Sol's #411 review)."""
    root = _repo(tmp_path)
    _install_unused_filter(root, tmp_path / "marker")
    manager = _manager(tmp_path)
    real = manager._git

    def broken(cwd, *args, **kwargs):
        if args and args[0] == failing:
            if kwargs.get("check", True):
                raise WorkspaceError(f"git {failing} failed (128): injected")
            return subprocess.CompletedProcess(["git", *args], 128, b"", b"injected")
        return real(cwd, *args, **kwargs)

    monkeypatch.setattr(manager, "_git", broken)
    readiness = manager.readiness(root)
    assert not readiness.ready
    assert any("could not be checked" in problem for problem in readiness.problems), readiness


@pytest.mark.parametrize(
    "answer",
    [
        b"",
        b"base.txt\0filter\0unspecified\0",
        b"base.txt\0filter\0unspecified\0base.txt\0filter\0unspecified\0",
        b"base.txt\0diff\0unspecified\0code.cpp\0filter\0unspecified\0",
        b"other.txt\0filter\0unspecified\0code.cpp\0filter\0unspecified\0",
    ],
    ids=["empty", "omitted", "duplicate", "wrong-attribute", "wrong-path"],
)
def test_filter_discovery_refuses_a_complete_but_untrustworthy_answer(
    tmp_path, monkeypatch, answer,
):
    """Every requested path must have exactly one matching filter answer.

    A syntactically complete response that omits or substitutes a path is still unknown;
    it must not be converted into "no filter in use".
    """
    root = _repo(tmp_path)
    _install_unused_filter(root, tmp_path / "marker")
    manager = _manager(tmp_path)
    real = manager._git

    def malformed(cwd, *args, **kwargs):
        if args and args[0] == "check-attr":
            return subprocess.CompletedProcess(["git", *args], 0, answer, b"")
        return real(cwd, *args, **kwargs)

    monkeypatch.setattr(manager, "_git", malformed)

    with pytest.raises(WorkspaceError, match="complete answer for every requested path"):
        manager.filter_drivers_in_use(root)
    readiness = manager.readiness(root)
    assert not readiness.ready
    assert any("could not be checked" in problem for problem in readiness.problems), readiness


def _public_change(sandbox, tmp_path, turns, prepare=None):
    from test_candidate_change_journey import _controller

    controller, manager = _controller(sandbox.root, tmp_path, turns)
    if prepare is not None:
        prepare(controller, manager)
    before = subprocess.run(["git", "status", "--porcelain=v1"], cwd=sandbox.root,
                            capture_output=True, text=True, check=True).stdout
    result = controller.run("change: the attributes", task_id=str(uuid4()),
                            skill_name="implement-change")
    after = subprocess.run(["git", "status", "--porcelain=v1"], cwd=sandbox.root,
                           capture_output=True, text=True, check=True).stdout
    assert after == before, "the user's checkout changed"
    return result, manager


def test_public_change_cannot_write_git_attributes_and_no_filter_runs(sandbox, tmp_path):
    """Through the public `change:` route the worker tries to write .gitattributes naming
    a configured filter. The write tool refuses, and no git call ever runs the filter
    (the worker loop's own fingerprint diff ran it in Sol's #411 repro)."""
    from local_agent.llm.client import tool_call
    from local_agent.llm.protocol import ChatResponse

    sandbox.scenario("clean")
    marker = tmp_path / "marker"
    _install_unused_filter(sandbox.root, marker)
    seen = []

    def after_refusal(messages):
        seen.append(str(messages[-1].get("content", "")))
        return ChatResponse(tool_calls=[tool_call("submit_answer", {
            "claim": "failure", "summary": "cannot change attributes", "evidence_ids": []},
            "a2")])

    turns = [
        ChatResponse(tool_calls=[tool_call("propose_file", {
            "path": ".gitattributes", "content": "* filter=audit\n"}, "a1")]),
        after_refusal,
        ChatResponse(content="I cannot change Git attributes."),
    ]
    result, manager = _public_change(sandbox, tmp_path, turns)

    assert seen and "Git attributes file" in seen[0], seen
    assert result.metrics["candidate"]["retained"] is False
    assert not marker.exists(), "the configured filter ran"
    assert not any(manager.workspaces_root.glob("*.candidate.json"))


def test_public_change_refused_at_settle_is_blocked_and_discarded(sandbox, tmp_path, monkeypatch):
    """The settle-time check composed through the public route: a refused candidate is a
    blocked task with the reason, nothing retained, the checkout untouched."""
    from test_candidate_change_journey import _bracket_file_turns

    sandbox.scenario("clean")

    def prepare(controller, manager):
        # The exact class the controller's settle path catches. Another test may have
        # re-imported the session modules, so the one imported here can differ.
        # Read through the controller's own function globals, not sys.modules.
        settle = type(controller).run.__globals__["settle_candidate"]
        refused = settle.__globals__["CandidateRefusedError"]

        def refuse(workspace):
            raise refused("in the candidate, injected refusal")

        monkeypatch.setattr(manager, "candidate_patch", refuse)

    result, manager = _public_change(sandbox, tmp_path, _bracket_file_turns(), prepare)

    # By value: another test may have re-imported the session modules (and their enums).
    assert result.outcome.value == "blocked", result.answer
    assert result.reason_code == "policy_denied"
    assert result.verified_at_completion is False
    assert "discarded: in the candidate, injected refusal" in result.answer
    assert result.metrics["candidate"]["retained"] is False
    assert not any(manager.workspaces_root.glob("*.candidate.json"))
    assert not any(p.is_dir() for p in manager.workspaces_root.iterdir()
                   if p.name not in {".no-hooks"})


@pytest.mark.parametrize("path", [".gitattributes", "sub/.gitattributes", "a/b/.gitattributes"])
def test_the_write_authority_refuses_every_attributes_file(tmp_path, path):
    from local_agent.tools.tool_primitives import ProtectedPathError, assert_writable

    with pytest.raises(ProtectedPathError, match="Git attributes file"):
        assert_writable(tmp_path, tmp_path / path, (".git",))
    assert_writable(tmp_path, tmp_path / "sub" / "gitattributes.txt", (".git",))
