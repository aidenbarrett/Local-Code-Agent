"""Fix-the-build journey: the worker edits its own worktree, never the user's checkout."""
from __future__ import annotations

import json
import subprocess
from pathlib import Path
from uuid import uuid4

import pytest

from local_agent.config import load_repo_config
from local_agent.llm.client import ScriptedClient, tool_call
from local_agent.llm.protocol import ChatResponse
from local_agent.session.contracts import TaskOutcome
from local_agent.session.event_buffer import EventBuffer
from local_agent.session.intents import RULE_FIX_BUILD, RouteAction, decide_route
from local_agent.session.task_controller import TaskController
from local_agent.session.workspaces import GitWorkspaceManager


RING = "src/ring_buffer.cpp"


def _patch_id(messages):
    for m in reversed(messages):
        if m.get("role") == "tool":
            try:
                pid = (json.loads(m.get("content") or "{}").get("data") or {}).get("patch_id")
            except json.JSONDecodeError:
                continue
            if pid:
                return pid
    raise AssertionError("no patch_id")


def _fixing_turns():
    return [
        ChatResponse(tool_calls=[tool_call("build_target", {}, "c1")]),
        ChatResponse(tool_calls=[tool_call("propose_patch", {"path": RING, "find": "++count;", "replace": "++count_;"}, "c2")]),
        lambda m: ChatResponse(tool_calls=[tool_call("apply_patch", {"patch_id": _patch_id(m)}, "c3")]),
        ChatResponse(tool_calls=[tool_call("propose_patch", {"path": RING, "find": "return count_ == 0 }", "replace": "return count_ == 0; }"}, "c4")]),
        lambda m: ChatResponse(tool_calls=[tool_call("apply_patch", {"patch_id": _patch_id(m)}, "c5")]),
        ChatResponse(tool_calls=[tool_call("build_target", {}, "c6")]),
        lambda m: ChatResponse(tool_calls=[tool_call("submit_answer", {
            "claim": "success", "summary": "fixed two typos", "evidence_ids": ["build_target:5"]}, "c7")]),
        ChatResponse(content="Fixed the two compile errors in ring_buffer.cpp."),
    ]


def _controller(root: Path, tmp_path: Path, turns, *, allow_execution=True, workspaces="default"):
    manager = (
        GitWorkspaceManager(tmp_path / "lca-ws", controller_commit="c" * 40)
        if workspaces == "default" else workspaces
    )
    controller = TaskController(
        load_repo_config(root),
        lambda: ScriptedClient(list(turns)),
        EventBuffer(uuid4().hex),
        allow_execution=allow_execution,
        workspaces=manager,
    )
    return controller, manager


def _worktrees(root: Path) -> int:
    out = subprocess.run(["git", "worktree", "list", "--porcelain"], cwd=root,
                         capture_output=True, text=True, check=True).stdout
    return out.count("worktree ")


def test_route_sends_explicit_build_fix_phrasing_to_the_candidate_skill():
    for text in ("fix the build", "Fix the compilation errors", "make it compile"):
        decision = decide_route(text, active_repo_count=1)
        assert decision.action is RouteAction.WORK
        assert (decision.skill, decision.rule_id) == ("fix-build-failure", RULE_FIX_BUILD)
    assert decide_route('"fix the build"', active_repo_count=1).action is RouteAction.MODEL_FALLBACK
    assert decide_route("fix the build", active_repo_count=0).action is RouteAction.CLARIFY


def test_fix_is_prepared_and_proven_in_isolation_and_the_user_checkout_is_untouched(sandbox, tmp_path):
    sandbox.scenario("compile_error")
    broken = (sandbox.root / RING).read_bytes()
    status_before = subprocess.run(["git", "status", "--porcelain"], cwd=sandbox.root,
                                   capture_output=True, text=True, check=True).stdout
    task_id = str(uuid4())
    controller, manager = _controller(sandbox.root, tmp_path, _fixing_turns())

    result = controller.run("fix the build", task_id=task_id, skill_name="fix-build-failure")

    assert result.outcome is TaskOutcome.PASS, result.answer
    assert result.verified_at_completion is True
    assert "Your checkout has not been modified" in result.answer
    # The user's broken file is exactly as it was; nothing was staged or built there.
    assert (sandbox.root / RING).read_bytes() == broken
    assert subprocess.run(["git", "status", "--porcelain"], cwd=sandbox.root,
                          capture_output=True, text=True, check=True).stdout == status_before
    assert not (sandbox.root / "build").exists()

    candidate_facts = result.metrics["candidate"]
    assert candidate_facts["retained"] is True
    assert candidate_facts["paths"] == [RING]
    assert RING in candidate_facts["dirty_paths_in_base"]

    workspace, candidate = manager.load(task_id)
    assert candidate.sha256 == candidate_facts["patch_sha256"]
    assert b"++count_;" in candidate.patch and b"build/" not in candidate.patch
    assert result.metrics["proof_binding"]["scope"] == "full_build"

    # The separate import is exact and lands only the reviewed change.
    imported = manager.import_patch(workspace, candidate)
    assert imported.applied and imported.verified
    fixed = (sandbox.root / RING).read_text(encoding="utf-8")
    assert "++count_;" in fixed and "return count_ == 0; }" in fixed
    manager.discard(workspace)
    assert _worktrees(sandbox.root) == 1


def test_no_edit_means_no_retained_candidate_and_no_orphan_worktree(sandbox, tmp_path):
    sandbox.scenario("compile_error")
    turns = [
        ChatResponse(tool_calls=[tool_call("build_target", {}, "c1")]),
        ChatResponse(content="I could not determine a safe fix."),
    ]
    task_id = str(uuid4())
    controller, manager = _controller(sandbox.root, tmp_path, turns)

    result = controller.run("fix the build", task_id=task_id, skill_name="fix-build-failure")

    assert result.metrics["candidate"]["retained"] is False
    assert result.verified_at_completion is False
    assert _worktrees(sandbox.root) == 1
    with pytest.raises(Exception):
        manager.load(task_id)


def test_worker_crash_discards_the_workspace(sandbox, tmp_path):
    class Exploding:
        def __init__(self):
            raise RuntimeError("worker endpoint construction failed")

    task_id = str(uuid4())
    manager = GitWorkspaceManager(tmp_path / "lca-ws", controller_commit="c" * 40)
    controller = TaskController(
        load_repo_config(sandbox.root), Exploding, EventBuffer(uuid4().hex),
        allow_execution=True, workspaces=manager,
    )
    result = controller.run("fix the build", task_id=task_id, skill_name="fix-build-failure")
    assert result.outcome is TaskOutcome.NO_VERDICT
    assert _worktrees(sandbox.root) == 1
    assert not (manager.workspaces_root / f"{task_id}.lease").exists()


def test_without_build_execution_no_workspace_is_created(sandbox, tmp_path):
    controller, manager = _controller(sandbox.root, tmp_path, _fixing_turns(), allow_execution=False)
    result = controller.run("fix the build", task_id=str(uuid4()), skill_name="fix-build-failure")
    assert result.outcome is TaskOutcome.BLOCKED
    assert "execution is not enabled" in result.answer
    assert _worktrees(sandbox.root) == 1
    assert list(manager.workspaces_root.glob("*.lease")) == []


def test_without_a_workspace_manager_the_fix_is_refused(sandbox, tmp_path):
    controller, _ = _controller(sandbox.root, tmp_path, _fixing_turns(), workspaces=None)
    result = controller.run("fix the build", task_id=str(uuid4()), skill_name="fix-build-failure")
    assert result.outcome is TaskOutcome.BLOCKED
    assert "not configured" in result.answer


def test_repository_policy_forbidding_patches_is_honoured(sandbox, tmp_path):
    config = sandbox.root / ".local-agent.toml"
    config.write_text(
        config.read_text(encoding="utf-8").replace("allow_patch = true", "allow_patch = false"),
        encoding="utf-8",
    )
    controller, _ = _controller(sandbox.root, tmp_path, _fixing_turns())
    result = controller.run("fix the build", task_id=str(uuid4()), skill_name="fix-build-failure")
    assert result.outcome is TaskOutcome.BLOCKED
    assert "allow_patch" in result.answer


def test_the_user_checkout_stays_read_only_for_every_other_skill(sandbox, tmp_path):
    controller, _ = _controller(sandbox.root, tmp_path, _fixing_turns())
    assert controller.repo.policy.allow_patch is False
    assert controller.repo.policy.allow_commit is False


def test_retained_candidate_record_is_tamper_evident(sandbox, tmp_path):
    sandbox.scenario("compile_error")
    task_id = str(uuid4())
    controller, manager = _controller(sandbox.root, tmp_path, _fixing_turns())
    controller.run("fix the build", task_id=task_id, skill_name="fix-build-failure")
    record = manager.workspaces_root / f"{task_id}.candidate.json"
    raw = json.loads(record.read_text(encoding="utf-8"))
    import base64
    raw["candidate"]["patch_b64"] = base64.b64encode(b"diff --git a/x b/x\n").decode()
    record.write_text(json.dumps(raw), encoding="utf-8")
    from local_agent.session.workspaces import WorkspaceError
    with pytest.raises(WorkspaceError):
        manager.load(task_id)
    with pytest.raises(WorkspaceError):
        manager.load(str(uuid4()))
