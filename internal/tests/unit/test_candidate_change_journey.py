"""Fix-the-build journey: the worker edits its own worktree, never the user's checkout."""
from __future__ import annotations

import base64
import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

from local_agent.config import load_repo_config
from local_agent.llm.client import ScriptedClient, tool_call
from local_agent.llm.protocol import ChatResponse
from local_agent.session.contracts import TaskOutcome
from local_agent.session.conversation_gateway import ConversationGateway
from local_agent.session.event_buffer import EventBuffer
from local_agent.session.intents import RULE_FIX_BUILD, RouteAction, decide_route
from local_agent.session.results import verdict_block_from_task_result
from local_agent.session.task_controller import TaskController
from local_agent.session.workspaces import GitWorkspaceManager, WorkspaceError

# Imports stay at module scope. test_session_import_boundary purges and re-imports
# local_agent modules; a function-local import can then bind a newer class than the
# one the controller raises or returns, and isinstance/raises checks stop matching.


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


def test_worker_empty_build_target_cannot_produce_verified_candidate(sandbox, tmp_path):
    turns = [
        ChatResponse(tool_calls=[tool_call("build_target", {"target": ""}, "c1")]),
        ChatResponse(tool_calls=[tool_call("submit_answer", {
            "claim": "success", "summary": "built", "evidence_ids": ["build_target:0"]}, "c2")]),
        ChatResponse(content="Built successfully."),
    ]
    controller, manager = _controller(sandbox.root, tmp_path, turns)

    result = controller.run(
        "fix the build", task_id=str(uuid4()), skill_name="fix-build-failure"
    )

    assert result.outcome is not TaskOutcome.PASS
    assert result.verified_at_completion is False
    assert result.reason_code == "missing_evidence"
    assert result.metrics["candidate"]["retained"] is False
    assert _worktrees(sandbox.root) == 1


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


def test_create_refusal_names_all_blocks_in_the_conversation(sandbox, tmp_path):
    config = sandbox.root / ".local-agent.toml"
    config.write_text(
        config.read_text(encoding="utf-8")
        .replace("allow_patch = true", "allow_patch = false")
        .replace("allow_test = true", "allow_test = false"),
        encoding="utf-8",
    )
    controller, manager = _controller(sandbox.root, tmp_path, [], allow_execution=False)
    runner = SimpleNamespace(run=lambda task, **kwargs: controller.run(
        task, skill_name=kwargs["skill"], route_source=kwargs["route_source"],
    ))
    gateway = ConversationGateway(
        ScriptedClient([]), controller, EventBuffer(uuid4().hex), task_runner=runner,
    )

    rendered = gateway.turn("Create a C++ file called aiden.cpp that prints hello")

    assert gateway.last_result is not None
    assert gateway.last_result.outcome is TaskOutcome.BLOCKED
    assert [item["code"] for item in gateway.last_result.metrics["candidate_blockers"]] == [
        "patch_disabled", "execution_disabled", "test_disabled",
    ]
    assert "allow_patch = false" in rendered
    assert "execution is not enabled" in rendered
    assert "allow_test = false" in rendered
    assert [turn.role for turn in gateway.session.turns] == ["user", "assistant"]
    reply = gateway.session.turns[-1].content
    assert "allow_patch = false" in reply
    assert "execution is not enabled" in reply
    assert "allow_test = false" in reply
    assert "Nothing was created" in reply
    assert "policy_denied" not in reply
    assert not (sandbox.root / "aiden.cpp").exists()
    assert _worktrees(sandbox.root) == 1
    assert list(manager.workspaces_root.glob("*.lease")) == []


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
    raw["candidate"]["patch_b64"] = base64.b64encode(b"diff --git a/x b/x\n").decode()
    record.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(WorkspaceError):
        manager.load(task_id)
    with pytest.raises(WorkspaceError):
        manager.load(str(uuid4()))


# ------------------------------------------------------------- /apply <task>


def _prepare(sandbox, tmp_path):
    sandbox.scenario("compile_error")
    candidate_task = str(uuid4())
    controller, manager = _controller(sandbox.root, tmp_path, _fixing_turns())
    prepared = controller.run("fix the build", task_id=candidate_task, skill_name="fix-build-failure")
    assert prepared.metrics["candidate"]["retained"] is True
    return controller, manager, candidate_task


def _apply(controller, candidate_task):
    request = f"User request:\n/apply {candidate_task}\n\nDeterministic route (controller-owned provenance):\nrule_id=apply-candidate/v1"
    result = controller.run(request, task_id=str(uuid4()), skill_name="apply-candidate")
    verdict_block_from_task_result(result)  # every outcome must be renderable
    return result


def _staged(root: Path) -> str:
    return subprocess.run(["git", "diff", "--cached"], cwd=root, capture_output=True,
                          text=True, check=True).stdout


def test_apply_imports_the_reviewed_fix_once_and_says_the_proof_covers_it(sandbox, tmp_path):
    controller, manager, candidate_task = _prepare(sandbox, tmp_path)

    result = _apply(controller, candidate_task)

    assert result.outcome is TaskOutcome.PASS, result.answer
    fixed = (sandbox.root / RING).read_text(encoding="utf-8")
    assert "++count_;" in fixed and "return count_ == 0; }" in fixed
    assert _staged(sandbox.root) == ""
    assert "build proof covers them" in result.answer
    assert result.metrics["candidate_import"]["checkout_matches_candidate_tree"] is True
    assert _worktrees(sandbox.root) == 1

    again = _apply(controller, candidate_task)
    assert again.outcome is TaskOutcome.BLOCKED, "a candidate must be single-use"


def test_apply_refuses_when_the_user_changed_a_touched_file(sandbox, tmp_path):
    controller, manager, candidate_task = _prepare(sandbox, tmp_path)
    mine = (sandbox.root / RING).read_text(encoding="utf-8") + "// my own edit\n"
    (sandbox.root / RING).write_text(mine, encoding="utf-8")

    result = _apply(controller, candidate_task)

    assert result.outcome is TaskOutcome.FAIL
    assert result.reason_code == "scope_changed"
    assert "Your checkout is exactly as it was" in result.answer
    assert (sandbox.root / RING).read_text(encoding="utf-8") == mine
    manager.load(candidate_task)  # still retained for the user to decide


def test_apply_with_unrelated_dirty_work_does_not_claim_the_build_proof(sandbox, tmp_path):
    controller, _manager, candidate_task = _prepare(sandbox, tmp_path)
    readme = sandbox.root / "README.md"
    readme.write_text((readme.read_text(encoding="utf-8") if readme.exists() else "") + "\nlocal note\n",
                      encoding="utf-8")
    if not subprocess.run(["git", "ls-files", "--error-unmatch", "README.md"], cwd=sandbox.root,
                          capture_output=True).returncode == 0:
        pytest.skip("fixture has no tracked README.md")

    result = _apply(controller, candidate_task)

    assert result.outcome is TaskOutcome.PASS
    assert result.metrics["candidate_import"]["checkout_matches_candidate_tree"] is False
    assert "does not cover it" in result.answer
    assert "local note" in readme.read_text(encoding="utf-8")


def test_apply_requires_one_full_candidate_task_id(sandbox, tmp_path):
    controller, _ = _controller(sandbox.root, tmp_path, [])
    unknown = _apply(controller, str(uuid4()))
    assert unknown.outcome is TaskOutcome.BLOCKED and unknown.reason_code == "invalid_input"
    vague = controller.run("User request:\n/apply the last one", task_id=str(uuid4()),
                           skill_name="apply-candidate")
    assert vague.outcome is TaskOutcome.BLOCKED and vague.reason_code == "invalid_input"


def test_apply_is_a_fingerprinted_controller_action_not_a_worker_skill(sandbox, tmp_path):
    controller, _ = _controller(sandbox.root, tmp_path, [])
    assert controller.resolve_skill("apply-candidate") == "apply-candidate"
    digest = controller.effective_skill_sha256("apply-candidate")
    assert len(digest) == 64 and int(digest, 16) >= 0
    decision = decide_route(f"/apply {uuid4()}", active_repo_count=1)
    assert decision.action is RouteAction.WORK and decision.skill == "apply-candidate"
    assert decide_route("/apply 1234abcd", active_repo_count=1).action is RouteAction.MODEL_FALLBACK


def test_stop_reaches_a_running_candidate_build_and_nothing_is_retained(sandbox, tmp_path):
    """Stop during the candidate's build ends it; no half-made candidate is kept."""
    import threading
    import time

    from local_agent.session.cancellation import CancellationToken

    sandbox.scenario("compile_error")
    marker = tmp_path / "build-started"
    config = sandbox.root / ".local-agent.toml"
    slow_build = json.dumps([
        sys.executable, "-c",
        f"from pathlib import Path; import time; Path({str(marker)!r}).write_text('x'); time.sleep(60)",
    ])
    text = config.read_text(encoding="utf-8")
    text = text.replace(
        'build = ["cmake", "--build", "build", "--config", "Debug", "--parallel", "4"]',
        f"build = {slow_build}",
    )
    text = text.replace('configure = ["cmake", "-S", ".", "-B", "build", "-DCMAKE_BUILD_TYPE=Debug"]', "")
    config.write_text(text, encoding="utf-8")
    subprocess.run(["git", "-c", "user.email=a@b.c", "-c", "user.name=t", "commit", "-qam", "slow build"],
                   cwd=sandbox.root, check=True)

    task_id = str(uuid4())
    token = CancellationToken(task_id, 0)
    controller, manager = _controller(sandbox.root, tmp_path, _fixing_turns())
    results = []
    worker = threading.Thread(target=lambda: results.append(controller.run(
        "fix the build", task_id=task_id, skill_name="fix-build-failure",
        cancellation_probe=token)))
    worker.start()
    deadline = time.monotonic() + 60
    while not marker.exists() and time.monotonic() < deadline:
        time.sleep(0.05)
    assert marker.exists(), "candidate build never started"
    stopped_at = time.monotonic()
    token.request()
    worker.join(30)

    assert not worker.is_alive(), "Stop did not reach the candidate build"
    assert time.monotonic() - stopped_at < 20
    result = results[0]
    assert result.outcome is TaskOutcome.BLOCKED and result.reason_code == "cancelled"
    assert result.verified_at_completion is False
    assert result.metrics["candidate"] == {
        "retained": False, "stopped": True,
        "workspace_existed": True, "workspace_removed": True,
    }
    assert "nothing is available to apply" in result.answer
    assert _worktrees(sandbox.root) == 1
    with pytest.raises(WorkspaceError):
        manager.load(task_id)
# ------------------------------------------------------ fault injection: /apply


def _fail_apply_after(manager, write):
    """Make the real (non --check) git apply write something, then report failure."""
    real = manager._git

    def faulty(cwd, *args, **kwargs):
        if args and args[0] == "apply" and not {"--check", "--summary"} & set(args):
            write()
            return subprocess.CompletedProcess(["git", *args], 1, b"", b"injected: disk full")
        return real(cwd, *args, **kwargs)

    return faulty


def test_partial_apply_is_rolled_back_to_exactly_the_pre_import_content(sandbox, tmp_path, monkeypatch):
    controller, manager, candidate_task = _prepare(sandbox, tmp_path)
    workspace, _candidate = manager.load(candidate_task)
    before = (sandbox.root / RING).read_bytes()
    post = (workspace.root / RING).read_bytes()
    monkeypatch.setattr(manager, "_git", _fail_apply_after(
        manager, lambda: (sandbox.root / RING).write_bytes(post)))

    result = _apply(controller, candidate_task)

    assert result.outcome is TaskOutcome.FAIL
    assert "restored" in result.answer and "back exactly as it was" in result.answer
    assert (sandbox.root / RING).read_bytes() == before
    assert _staged(sandbox.root) == ""
    manager.load(candidate_task)  # not consumed by a failed import


def test_partial_apply_never_overwrites_content_it_did_not_write(sandbox, tmp_path, monkeypatch):
    controller, manager, candidate_task = _prepare(sandbox, tmp_path)
    foreign = b"// written by something else mid-import\n"
    monkeypatch.setattr(manager, "_git", _fail_apply_after(
        manager, lambda: (sandbox.root / RING).write_bytes(foreign)))

    result = _apply(controller, candidate_task)

    assert result.outcome is TaskOutcome.NO_VERDICT
    assert result.reason_code == "cleanup_unknown"
    assert RING in result.answer and "need your attention" in result.answer
    assert result.metrics["candidate_import"]["unresolved"] == [RING]
    assert (sandbox.root / RING).read_bytes() == foreign


def test_unreadable_candidate_worktree_does_not_misreport_a_completed_import(sandbox, tmp_path):
    controller, manager, candidate_task = _prepare(sandbox, tmp_path)
    workspace, _candidate = manager.load(candidate_task)
    # Break the worktree's link to its repository rather than deleting the directory:
    # on Windows a build can still hold files open under it, and the fault under test is
    # "the candidate tree cannot be read", not "rmtree works".
    (workspace.root / ".git").unlink()

    result = _apply(controller, candidate_task)

    assert result.outcome is TaskOutcome.PASS, result.answer
    assert "++count_;" in (sandbox.root / RING).read_text(encoding="utf-8")
    assert result.metrics["candidate_import"]["checkout_matches_candidate_tree"] is None
    assert "could not be compared" in result.answer
    assert "covers them" not in result.answer


# ------------------------------------------------------- fix the failing tests


def _test_fixing_turns():
    return [
        ChatResponse(tool_calls=[tool_call("build_target", {}, "t1")]),
        ChatResponse(tool_calls=[tool_call("run_test", {}, "t2")]),
        ChatResponse(tool_calls=[tool_call("propose_patch", {
            "path": RING, "find": "count_ + 1 == slots_.size()", "replace": "count_ == slots_.size()"}, "t3")]),
        lambda m: ChatResponse(tool_calls=[tool_call("apply_patch", {"patch_id": _patch_id(m)}, "t4")]),
        ChatResponse(tool_calls=[tool_call("build_target", {}, "t5")]),
        ChatResponse(tool_calls=[tool_call("run_test", {}, "t6")]),
        lambda m: ChatResponse(tool_calls=[tool_call("submit_answer", {
            "claim": "success", "summary": "full() was off by one", "evidence_ids": ["run_test:5"]}, "t7")]),
        ChatResponse(content="Fixed RingBuffer::full(); the full test run passes."),
    ]


def test_route_sends_explicit_test_fix_phrasing_to_the_candidate_skill():
    from local_agent.session.intents import RULE_FIX_TESTS

    for text in ("fix the failing tests", "Fix the test failures", "make the tests pass"):
        decision = decide_route(text, active_repo_count=1)
        assert (decision.action, decision.skill, decision.rule_id) == (
            RouteAction.WORK, "fix-test-failure", RULE_FIX_TESTS)
    assert decide_route("fix the tests by deleting them", active_repo_count=1).action is RouteAction.MODEL_FALLBACK


def test_failing_test_is_fixed_and_proven_by_a_full_test_run_in_isolation(sandbox, tmp_path):
    sandbox.scenario("test_failure")
    broken = (sandbox.root / RING).read_bytes()
    task_id = str(uuid4())
    controller, manager = _controller(sandbox.root, tmp_path, _test_fixing_turns())

    result = controller.run("fix the failing tests", task_id=task_id, skill_name="fix-test-failure")

    assert result.outcome is TaskOutcome.PASS, result.answer
    assert result.verified_at_completion is True
    assert "A full test run of the candidate passed." in result.answer
    assert result.metrics["proof_binding"]["scope"] == "full_test"
    assert (sandbox.root / RING).read_bytes() == broken
    assert result.metrics["candidate"]["paths"] == [RING]

    applied = _apply(controller, task_id)
    assert applied.outcome is TaskOutcome.PASS, applied.answer
    assert "count_ == slots_.size()" in (sandbox.root / RING).read_text(encoding="utf-8")


def test_test_fix_is_refused_before_any_workspace_when_tests_are_not_allowed(sandbox, tmp_path):
    config = sandbox.root / ".local-agent.toml"
    config.write_text(
        config.read_text(encoding="utf-8").replace("allow_test = true", "allow_test = false"),
        encoding="utf-8",
    )
    controller, manager = _controller(sandbox.root, tmp_path, _test_fixing_turns())
    result = controller.run("fix the failing tests", task_id=str(uuid4()), skill_name="fix-test-failure")
    assert result.outcome is TaskOutcome.BLOCKED
    assert "allow_test" in result.answer
    assert list(manager.workspaces_root.glob("*.lease")) == []


# ------------------------------------------------------------- /undo <task>


def _undo(controller, candidate_task):
    request = f"User request:\n/undo {candidate_task}\n\nDeterministic route (controller-owned provenance):\nrule_id=undo-candidate/v1"
    result = controller.run(request, task_id=str(uuid4()), skill_name="undo-candidate")
    verdict_block_from_task_result(result)
    return result


def test_undo_restores_exactly_the_pre_import_bytes_once(sandbox, tmp_path):
    controller, manager, candidate_task = _prepare(sandbox, tmp_path)
    before = (sandbox.root / RING).read_bytes()
    applied = _apply(controller, candidate_task)
    assert f"/undo {candidate_task}" in applied.answer
    assert (sandbox.root / RING).read_bytes() != before

    undone = _undo(controller, candidate_task)

    assert undone.outcome is TaskOutcome.PASS, undone.answer
    assert (sandbox.root / RING).read_bytes() == before
    assert _staged(sandbox.root) == ""
    again = _undo(controller, candidate_task)
    assert again.outcome is TaskOutcome.BLOCKED, "undo must be single-use"


def test_undo_refuses_to_overwrite_work_done_after_the_apply(sandbox, tmp_path):
    controller, manager, candidate_task = _prepare(sandbox, tmp_path)
    _apply(controller, candidate_task)
    mine = (sandbox.root / RING).read_text(encoding="utf-8") + "// kept\n"
    (sandbox.root / RING).write_text(mine, encoding="utf-8")

    undone = _undo(controller, candidate_task)

    assert undone.outcome is TaskOutcome.FAIL and undone.reason_code == "scope_changed"
    assert "Nothing was undone" in undone.answer
    assert (sandbox.root / RING).read_text(encoding="utf-8") == mine


def test_undo_requires_a_recorded_apply_and_a_full_id(sandbox, tmp_path):
    controller, _ = _controller(sandbox.root, tmp_path, [])
    assert _undo(controller, str(uuid4())).outcome is TaskOutcome.BLOCKED
    vague = controller.run("User request:\n/undo the last one", task_id=str(uuid4()),
                           skill_name="undo-candidate")
    assert vague.outcome is TaskOutcome.BLOCKED and vague.reason_code == "invalid_input"
    decision = decide_route(f"/undo {uuid4()}", active_repo_count=1)
    assert (decision.action, decision.skill) == (RouteAction.WORK, "undo-candidate")


# ------------------------------------------------------------- /commit <task>


def _commit(controller, candidate_task):
    request = f"User request:\n/commit {candidate_task}\n\nDeterministic route (controller-owned provenance):\nrule_id=commit-candidate/v1"
    result = controller.run(request, task_id=str(uuid4()), skill_name="commit-candidate")
    verdict_block_from_task_result(result)
    return result


def _allow_commits(root: Path) -> None:
    """Enable commits and commit the broken scenario, so the fix is a real change to HEAD."""
    config = root / ".local-agent.toml"
    config.write_text(config.read_text(encoding="utf-8").replace("allow_commit = false", "allow_commit = true"),
                      encoding="utf-8")
    subprocess.run(["git", "-c", "user.email=a@b.c", "-c", "user.name=t", "commit", "-qam", "allow commits"],
                   cwd=root, check=True)
    subprocess.run(["git", "config", "user.email", "dev@example.invalid"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.name", "Dev"], cwd=root, check=True)


def _git_out(root: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=root, capture_output=True, text=True, check=True).stdout


def test_commit_records_exactly_the_applied_change_and_leaves_other_staging_alone(sandbox, tmp_path):
    sandbox.scenario("compile_error")
    _allow_commits(sandbox.root)
    controller, manager, candidate_task = _prepare(sandbox, tmp_path)
    applied = _apply(controller, candidate_task)
    assert f"/commit {candidate_task}" in applied.answer
    # Unrelated work the user has staged must stay staged and out of the commit.
    notes = sandbox.root / "NOTES.md"
    notes.write_text("my notes\n", encoding="utf-8")
    subprocess.run(["git", "add", "NOTES.md"], cwd=sandbox.root, check=True)
    head_before = _git_out(sandbox.root, "rev-parse", "HEAD").strip()

    result = _commit(controller, candidate_task)

    assert result.outcome is TaskOutcome.PASS, result.answer
    commit = result.metrics["candidate_commit"]["commit"]
    assert _git_out(sandbox.root, "rev-parse", f"{commit}^").strip() == head_before
    assert _git_out(sandbox.root, "diff-tree", "--no-commit-id", "--name-only", "-r", commit).split() == [RING]
    assert f"LCA-Task: {candidate_task}" in _git_out(sandbox.root, "log", "-1", "--format=%B")
    assert _git_out(sandbox.root, "diff", "--cached", "--name-only").split() == ["NOTES.md"]
    assert "Nothing was pushed" in result.answer

    again = _commit(controller, candidate_task)
    assert again.outcome is TaskOutcome.BLOCKED, "commit is single-use"
    # The refusal names the existing commit and never reports a new one as made.
    assert commit[:12] in again.answer
    assert again.metrics["candidate_commit"]["commit"] == commit
    assert _git_out(sandbox.root, "rev-parse", "HEAD").strip() == commit
    undo = _undo(controller, candidate_task)
    assert undo.outcome is TaskOutcome.BLOCKED and "committed" in undo.answer
    assert "++count_;" in (sandbox.root / RING).read_text(encoding="utf-8")


def test_commit_is_refused_when_policy_forbids_it(sandbox, tmp_path):
    controller, _manager, candidate_task = _prepare(sandbox, tmp_path)
    _apply(controller, candidate_task)
    head_before = _git_out(sandbox.root, "rev-parse", "HEAD")
    result = _commit(controller, candidate_task)
    assert result.outcome is TaskOutcome.BLOCKED and result.reason_code == "policy_denied"
    assert _git_out(sandbox.root, "rev-parse", "HEAD") == head_before


def test_commit_refuses_drift_and_detached_head_without_committing(sandbox, tmp_path):
    sandbox.scenario("compile_error")
    _allow_commits(sandbox.root)
    controller, _manager, candidate_task = _prepare(sandbox, tmp_path)
    _apply(controller, candidate_task)
    head_before = _git_out(sandbox.root, "rev-parse", "HEAD")

    (sandbox.root / RING).write_text((sandbox.root / RING).read_text(encoding="utf-8") + "// later\n",
                                     encoding="utf-8")
    drifted = _commit(controller, candidate_task)
    assert drifted.outcome is TaskOutcome.FAIL and drifted.reason_code == "scope_changed"
    assert drifted.metrics["candidate_commit"]["drifted"] == [RING]
    assert drifted.metrics["candidate_commit"]["commit"] is None

    subprocess.run(["git", "checkout", "-q", "--detach"], cwd=sandbox.root, check=True)
    detached = _commit(controller, candidate_task)
    assert detached.outcome is TaskOutcome.BLOCKED and "detached" in detached.answer
    assert _git_out(sandbox.root, "rev-parse", "HEAD") == head_before


def test_commit_route_requires_a_full_task_id():
    assert decide_route(f"/commit {uuid4()}", active_repo_count=1).skill == "commit-candidate"
    assert decide_route("/commit abc12345", active_repo_count=1).action is RouteAction.MODEL_FALLBACK
    # A bare commit request gains no commit authority: it is refused outright.
    everything = decide_route("commit everything", active_repo_count=1)
    assert (everything.action, everything.reason_code) == (RouteAction.REFUSE, "commit_needs_candidate")


def test_commit_says_nothing_to_commit_when_the_fix_restores_head(sandbox, tmp_path):
    # Broken working tree over a good HEAD: the fix makes the files equal to HEAD again.
    _allow_commits(sandbox.root)
    controller, _manager, candidate_task = _prepare(sandbox, tmp_path)
    _apply(controller, candidate_task)
    head_before = _git_out(sandbox.root, "rev-parse", "HEAD")
    result = _commit(controller, candidate_task)
    assert result.outcome is TaskOutcome.BLOCKED
    assert "nothing to commit" in result.answer
    assert _git_out(sandbox.root, "rev-parse", "HEAD") == head_before


def test_a_test_fix_proven_only_by_a_build_is_not_reported_as_proven(sandbox, tmp_path):
    sandbox.scenario("test_failure")
    turns = [
        ChatResponse(tool_calls=[tool_call("propose_patch", {
            "path": RING, "find": "count_ + 1 == slots_.size()", "replace": "count_ + 2 == slots_.size()"}, "b1")]),
        lambda m: ChatResponse(tool_calls=[tool_call("apply_patch", {"patch_id": _patch_id(m)}, "b2")]),
        ChatResponse(tool_calls=[tool_call("build_target", {}, "b3")]),
        lambda m: ChatResponse(tool_calls=[tool_call("submit_answer", {
            "claim": "success", "summary": "built", "evidence_ids": ["build_target:2"]}, "b4")]),
        ChatResponse(content="Fixed, the build passes."),
    ]
    controller, _manager = _controller(sandbox.root, tmp_path, turns)

    result = controller.run("fix the failing tests", task_id=str(uuid4()), skill_name="fix-test-failure")

    # The worker's build is the wrong proof for a test fix, so the controller runs
    # the full test run itself; the wrong fix fails it and is never called proven.
    assert result.verified_at_completion is False
    assert result.outcome is TaskOutcome.FAIL and result.reason_code == "verification_failed"
    assert result.metrics["controller_check"] == {"check": "run-tests", "decided": True}
    assert "NOT proven: no full test run passed" in result.answer
    assert "full build and test run on the candidate: it failed." in result.answer
    verdict_block_from_task_result(result)


# --------------------------------------------------- change: <any source change>


_CLEAR_TEST = """#include "sandbox/ring_buffer.hpp"

#include <cstdio>

int main() {
    sandbox::RingBuffer buffer(2);
    buffer.push(1);
    buffer.push(2);
    buffer.clear();
    if (!buffer.empty() || buffer.size() != 0 || !buffer.push(3) || buffer.pop().value() != 3) {
        std::fprintf(stderr, "clear() did not reset the buffer\\n");
        return 1;
    }
    return 0;
}
"""


def _implement_clear_turns():
    header, source = "include/sandbox/ring_buffer.hpp", RING
    return [
        ChatResponse(tool_calls=[tool_call("propose_patch", {
            "path": header, "find": "    std::optional<int> pop();\n",
            "replace": "    std::optional<int> pop();\n    void clear();\n"}, "c1")]),
        lambda m: ChatResponse(tool_calls=[tool_call("apply_patch", {"patch_id": _patch_id(m)}, "c2")]),
        ChatResponse(tool_calls=[tool_call("propose_patch", {
            "path": source, "find": "bool RingBuffer::empty() const",
            "replace": "void RingBuffer::clear() {\n    head_ = 0;\n    tail_ = 0;\n    count_ = 0;\n}\n\n"
                       "bool RingBuffer::empty() const"}, "c3")]),
        lambda m: ChatResponse(tool_calls=[tool_call("apply_patch", {"patch_id": _patch_id(m)}, "c4")]),
        ChatResponse(tool_calls=[tool_call("propose_file", {
            "path": "tests/test_ring_clear.cpp", "content": _CLEAR_TEST}, "c5")]),
        lambda m: ChatResponse(tool_calls=[tool_call("apply_patch", {"patch_id": _patch_id(m)}, "c6")]),
        ChatResponse(tool_calls=[tool_call("propose_patch", {
            "path": "CMakeLists.txt", "find": "foreach(name ring_buffer text_util",
            "replace": "foreach(name ring_buffer ring_clear text_util"}, "c7")]),
        lambda m: ChatResponse(tool_calls=[tool_call("apply_patch", {"patch_id": _patch_id(m)}, "c8")]),
        ChatResponse(tool_calls=[tool_call("build_target", {}, "c9")]),
        ChatResponse(tool_calls=[tool_call("run_test", {}, "c10")]),
        lambda m: ChatResponse(tool_calls=[tool_call("submit_answer", {
            "claim": "success", "summary": "added RingBuffer::clear with a test",
            "evidence_ids": ["run_test:9"]}, "c11")]),
        ChatResponse(content="Added RingBuffer::clear() and a test for it."),
    ]


def test_change_route_needs_the_explicit_prefix():
    from local_agent.session.intents import RULE_IMPLEMENT_CHANGE

    for text in ("change: add a clear() method to RingBuffer", "/change rename x to y"):
        decision = decide_route(text, active_repo_count=1)
        assert (decision.action, decision.skill, decision.rule_id) == (
            RouteAction.WORK, "implement-change", RULE_IMPLEMENT_CHANGE)
    for text in ("change:", "changed my mind", "please change: x"):
        assert decide_route(text, active_repo_count=1).action is not RouteAction.WORK


def test_a_new_feature_with_a_new_test_is_built_tested_and_applied(sandbox, tmp_path):
    task_id = str(uuid4())
    before = {p: (sandbox.root / p).read_bytes() for p in (RING, "CMakeLists.txt", "include/sandbox/ring_buffer.hpp")}
    controller, manager = _controller(sandbox.root, tmp_path, _implement_clear_turns())

    result = controller.run("change: add a clear() method to RingBuffer that empties it",
                            task_id=task_id, skill_name="implement-change")

    assert result.outcome is TaskOutcome.PASS, result.answer
    assert result.verified_at_completion is True
    assert result.metrics["proof_binding"]["scope"] == "full_test"
    assert "full build and full test run of the candidate passed" in result.answer
    assert sorted(result.metrics["candidate"]["paths"]) == sorted([
        "CMakeLists.txt", "include/sandbox/ring_buffer.hpp", RING, "tests/test_ring_clear.cpp"])
    assert {p: (sandbox.root / p).read_bytes() for p in before} == before
    assert not (sandbox.root / "tests" / "test_ring_clear.cpp").exists()

    applied = _apply(controller, task_id)
    assert applied.outcome is TaskOutcome.PASS, applied.answer
    assert (sandbox.root / "tests" / "test_ring_clear.cpp").read_text(encoding="utf-8").replace("\r\n", "\n") == _CLEAR_TEST
    assert "void clear();" in (sandbox.root / "include/sandbox/ring_buffer.hpp").read_text(encoding="utf-8")


def test_propose_file_refuses_existing_files_and_the_instrument(sandbox, tmp_path):
    from local_agent.tools import build_registry
    from local_agent.tools.tool_primitives import ToolError

    registry, _ctx, _store = build_registry(load_repo_config(sandbox.root))
    propose = registry.get("propose_file").handler
    with pytest.raises(ToolError):
        propose(path=RING, content="x")
    with pytest.raises(Exception):
        propose(path="build/.local-agent-build-ok", content="forged")
    with pytest.raises(Exception):
        propose(path="../outside.txt", content="x")


def test_journal_revert_removes_a_file_the_agent_created(tmp_path):
    from local_agent.agent.journal import MutationJournal

    created = tmp_path / "new.cpp"
    created.write_text("int n();\n", encoding="utf-8")
    journal = MutationJournal()
    journal.record_write(created, None, "int n();\n", "apply_patch")
    assert journal.revert()["reverted"] == [str(created)]
    assert not created.exists()


# ------------------------------------------ durable candidate facts (task-result/2)


def _round_trip(result):
    import json as _json

    from local_agent.session.session_event_service import DurableTaskExecutor
    from local_agent.session.task_history import DurableTaskHistory
    from local_agent.session.terminal_completion import _retained_result

    _ref, payload = DurableTaskExecutor._result_artifact(result)
    raw = _json.loads(payload)
    assert raw["schema"] == "lca.task-result/2"
    _retained_result(result.task_id, payload)
    DurableTaskHistory._parse_result(payload, task_id=result.task_id)
    return raw["candidate"]


def test_candidate_lifecycle_crosses_the_durable_result_boundary(sandbox, tmp_path):
    sandbox.scenario("compile_error")
    subprocess.run(["git", "-c", "user.email=a@b.c", "-c", "user.name=t", "commit", "-qam", "broken"],
                   cwd=sandbox.root, check=True)
    config = sandbox.root / ".local-agent.toml"
    config.write_text(config.read_text(encoding="utf-8").replace("allow_commit = false", "allow_commit = true"),
                      encoding="utf-8")
    subprocess.run(["git", "-c", "user.email=a@b.c", "-c", "user.name=t", "commit", "-qam", "commits"],
                   cwd=sandbox.root, check=True)
    subprocess.run(["git", "config", "user.email", "dev@example.invalid"], cwd=sandbox.root, check=True)
    subprocess.run(["git", "config", "user.name", "Dev"], cwd=sandbox.root, check=True)
    task_id = str(uuid4())
    controller, _manager = _controller(sandbox.root, tmp_path, _fixing_turns())

    prepared = _round_trip(controller.run("fix the build", task_id=task_id, skill_name="fix-build-failure"))
    assert prepared["role"] == "prepared" and prepared["retained"] is True
    assert prepared["candidate_task_id"] == task_id and prepared["paths"] == [RING]
    assert len(prepared["patch_sha256"]) == 64 and prepared["commit"] is None

    applied = _round_trip(_apply(controller, task_id))
    assert (applied["role"], applied["candidate_task_id"], applied["retained"]) == ("applied", task_id, False)
    assert applied["patch_sha256"] == prepared["patch_sha256"]

    committed = _round_trip(_commit(controller, task_id))
    assert committed["role"] == "committed" and len(committed["commit"]) == 40

    refused = _round_trip(_undo(controller, task_id))
    assert refused is None, "a refused undo changes no candidate state"


def test_a_stopped_or_ordinary_task_carries_no_appliable_candidate():
    from local_agent.session.contracts import TaskResult

    stopped = TaskResult(str(uuid4()), TaskOutcome.BLOCKED, "Stopped.", False,
                         metrics={"candidate": {"retained": False, "stopped": True}}, reason_code="cancelled")
    block = _round_trip(stopped)
    assert block["role"] == "prepared" and block["retained"] is False and block["paths"] == []
    assert _round_trip(TaskResult(str(uuid4()), TaskOutcome.PASS, "built", True)) is None


@pytest.mark.parametrize("mutate", [
    lambda b: b.update(role="merged"),
    lambda b: b.update(retained=True, role="applied"),
    lambda b: b.update(role="committed"),
    lambda b: b.update(candidate_task_id="not-a-uuid"),
    lambda b: b.update(patch_sha256="xyz"),
    lambda b: b.update(extra=1),
    lambda b: b.update(retained=True, paths=[]),
])
def test_malformed_candidate_facts_are_refused(mutate):
    from local_agent.session.candidate_facts import validate_candidate

    block = {"role": "prepared", "candidate_task_id": str(uuid4()), "retained": True,
             "paths": ["src/a.cpp"], "patch_sha256": "a" * 64, "base_commit": None, "commit": None}
    validate_candidate(dict(block))
    mutate(block)
    with pytest.raises(ValueError):
        validate_candidate(block)


def test_an_untracked_source_file_the_user_builds_is_part_of_the_candidate(sandbox, tmp_path):
    # The user has written src/extra.cpp and added it to CMake but not to git. The
    # candidate must build the same tree, or it would prove a tree the user doesn't have.
    sandbox.scenario("compile_error")
    (sandbox.root / "src" / "extra.cpp").write_text("int extra_value() { return 7; }\n", encoding="utf-8")
    cmake = sandbox.root / "CMakeLists.txt"
    cmake.write_text(cmake.read_text(encoding="utf-8").replace(
        "  src/text_util.cpp\n", "  src/text_util.cpp\n  src/extra.cpp\n"), encoding="utf-8")
    task_id = str(uuid4())
    controller, manager = _controller(sandbox.root, tmp_path, _fixing_turns())

    result = controller.run("fix the build", task_id=task_id, skill_name="fix-build-failure")

    assert result.outcome is TaskOutcome.PASS, result.answer
    assert result.metrics["candidate"]["paths"] == [RING]
    assert "Included 1 untracked file(s)" in result.answer
    applied = _apply(controller, task_id)
    assert applied.metrics["candidate_import"]["checkout_matches_candidate_tree"] is True
    assert "build proof covers them" in applied.answer


def test_an_unready_machine_refuses_before_any_worktree_or_model_call(sandbox, tmp_path, monkeypatch):
    from local_agent.session.workspaces import WorkspaceReadiness

    calls = []

    def factory():
        calls.append(1)
        return ScriptedClient([])

    manager = GitWorkspaceManager(tmp_path / "lca-ws", controller_commit="c" * 40)
    monkeypatch.setattr(manager, "readiness", lambda root: WorkspaceReadiness(
        False, "git version 2.20.1", ("git version 2.20.1 is too old; candidate changes need git 2.25 or newer",)))
    controller = TaskController(load_repo_config(sandbox.root), factory, EventBuffer(uuid4().hex),
                                allow_execution=True, workspaces=manager)

    result = controller.run("fix the build", task_id=str(uuid4()), skill_name="fix-build-failure")

    assert result.outcome is TaskOutcome.BLOCKED and result.reason_code == "missing_dependency"
    assert "too old" in result.answer
    assert calls == [] and _worktrees(sandbox.root) == 1
    assert list(manager.workspaces_root.glob("*.lease")) == []
    verdict_block_from_task_result(result)


# ------------------------------------------------------------- /diff <task>


def _diff(controller, candidate_task):
    request = f"User request:\n/diff {candidate_task}\n\nDeterministic route (controller-owned provenance):\nrule_id=diff-candidate/v1"
    result = controller.run(request, task_id=str(uuid4()), skill_name="diff-candidate")
    verdict_block_from_task_result(result)
    return result


def test_the_candidate_answer_shows_what_changed_and_diff_shows_all_of_it(sandbox, tmp_path):
    sandbox.scenario("compile_error")
    task_id = str(uuid4())
    controller, manager = _controller(sandbox.root, tmp_path, _fixing_turns())
    prepared = controller.run("fix the build", task_id=task_id, skill_name="fix-build-failure")

    assert f"  {RING} | +2 -2" in prepared.answer
    assert "+    ++count_;" in prepared.answer and "-    ++count;" in prepared.answer
    assert f"Review: /diff {task_id}   Apply: /apply {task_id}" in prepared.answer

    before = (sandbox.root / RING).read_bytes()
    shown = _diff(controller, task_id)
    assert shown.outcome is TaskOutcome.PASS, shown.answer
    _workspace, candidate = manager.load(task_id)
    assert candidate.sha256 in shown.answer
    assert candidate.patch.decode() in shown.answer
    assert (sandbox.root / RING).read_bytes() == before, "/diff must not change anything"
    assert _diff(controller, task_id).outcome is TaskOutcome.PASS, "/diff is repeatable"
    manager.load(task_id)  # still retained for /apply


def test_diff_refuses_unknown_candidates_and_vague_requests(sandbox, tmp_path):
    controller, _ = _controller(sandbox.root, tmp_path, [])
    assert _diff(controller, str(uuid4())).outcome is TaskOutcome.BLOCKED
    vague = controller.run("User request:\n/diff the last one", task_id=str(uuid4()), skill_name="diff-candidate")
    assert vague.outcome is TaskOutcome.BLOCKED and vague.reason_code == "invalid_input"


def test_readable_patch_hides_binary_payloads_and_bounds_long_text():
    from local_agent.session.candidate_change import bounded, diffstat, readable_patch

    patch = (b"diff --git a/img.png b/img.png\nGIT binary patch\nliteral 12\nzcmZ?wbhEHb\n\n"
             b"diff --git a/a.cpp b/a.cpp\n--- a/a.cpp\n+++ b/a.cpp\n@@ -1 +1 @@\n-int a;\n+int b;\n")
    text = readable_patch(patch)
    assert "zcmZ" not in text and "(binary content not shown)" in text and "+int b;" in text
    assert diffstat(patch) == ["  img.png | +0 -0", "  a.cpp | +1 -1"]
    cut, truncated = bounded("line\n" * 100, 50)
    assert truncated and len(cut) <= 50 and cut.endswith("\n")


# ------------------------------------------------ disk use and /discard <task>


def test_a_retained_candidate_keeps_no_build_output_and_still_applies(sandbox, tmp_path):
    sandbox.scenario("compile_error")
    task_id = str(uuid4())
    controller, manager = _controller(sandbox.root, tmp_path, _fixing_turns())
    prepared = controller.run("fix the build", task_id=task_id, skill_name="fix-build-failure")
    assert prepared.verified_at_completion is True
    workspace, _candidate = manager.load(task_id)
    assert workspace.root.is_dir()
    assert not (workspace.root / "build").exists(), "proven candidate kept its build directory"
    assert _diff(controller, task_id).outcome is TaskOutcome.PASS
    applied = _apply(controller, task_id)
    assert applied.outcome is TaskOutcome.PASS, applied.answer
    assert "build proof covers them" in applied.answer


def test_discard_removes_an_unapplied_candidate_and_is_durable(sandbox, tmp_path):
    sandbox.scenario("compile_error")
    task_id = str(uuid4())
    controller, manager = _controller(sandbox.root, tmp_path, _fixing_turns())
    controller.run("fix the build", task_id=task_id, skill_name="fix-build-failure")
    before = (sandbox.root / RING).read_bytes()

    request = f"User request:\n/discard {task_id}"
    discarded = controller.run(request, task_id=str(uuid4()), skill_name="discard-candidate")

    assert discarded.outcome is TaskOutcome.PASS, discarded.answer
    assert (sandbox.root / RING).read_bytes() == before
    assert _worktrees(sandbox.root) == 1
    assert _apply(controller, task_id).outcome is TaskOutcome.BLOCKED
    facts = _round_trip(discarded)
    assert (facts["role"], facts["candidate_task_id"], facts["retained"]) == ("discarded", task_id, False)
    again = controller.run(request, task_id=str(uuid4()), skill_name="discard-candidate")
    assert again.outcome is TaskOutcome.BLOCKED
    assert decide_route(f"/discard {task_id}", active_repo_count=1).skill == "discard-candidate"


# ------------------------------------------- the controller proves the candidate


def _edits_then(*finish, fixes=2):
    """Worker turns that apply the ring buffer fixes and never build or test."""
    edits = [("++count;", "++count_;"), ("return count_ == 0 }", "return count_ == 0; }")][:fixes]
    turns = []
    for i, (find, replace) in enumerate(edits):
        turns.append(ChatResponse(tool_calls=[tool_call(
            "propose_patch", {"path": RING, "find": find, "replace": replace}, f"p{i}")]))
        turns.append(lambda m, i=i: ChatResponse(tool_calls=[tool_call(
            "apply_patch", {"patch_id": _patch_id(m)}, f"a{i}")]))
    return turns + list(finish)


def _claims(claim):
    return ChatResponse(tool_calls=[tool_call("submit_answer", {
        "claim": claim, "summary": "edited ring_buffer.cpp", "evidence_ids": []}, "s1")])


def test_unproven_fix_is_proven_by_the_controller_running_the_full_build(sandbox, tmp_path):
    sandbox.scenario("compile_error")
    broken = (sandbox.root / RING).read_bytes()
    task_id = str(uuid4())
    turns = _edits_then(_claims("success"), ChatResponse(content="Fixed it."))
    controller, manager = _controller(sandbox.root, tmp_path, turns)

    result = controller.run("fix the build", task_id=task_id, skill_name="fix-build-failure")

    assert result.outcome is TaskOutcome.PASS, result.answer
    assert result.verified_at_completion is True and result.verification_ran is True
    assert result.reason_code == "verification_passed"
    assert result.metrics["controller_check"] == {"check": "run-build", "decided": True}
    assert "The controller ran the configured full build on the candidate: it passed." in result.answer
    assert result.metrics["candidate"]["retained"] is True
    assert result.metrics["proof_binding"]["scope"] == "full_build"
    cited = result.metrics["proof_binding"]["evidence_ids"]
    assert len(cited) == 1 and cited[0].startswith("build_target:") and cited[0] in result.evidence_ids
    verdict_block_from_task_result(result)
    assert (sandbox.root / RING).read_bytes() == broken
    workspace, candidate = manager.load(task_id)
    assert b"++count_;" in candidate.patch
    manager.discard(workspace)


def test_a_worker_that_ends_in_prose_after_editing_is_still_checked(sandbox, tmp_path):
    sandbox.scenario("compile_error")
    turns = _edits_then(ChatResponse(content="Fixed it."), ChatResponse(content="Fixed it, honest."))
    controller, manager = _controller(sandbox.root, tmp_path, turns)

    result = controller.run("fix the build", task_id=str(uuid4()), skill_name="fix-build-failure")

    assert result.outcome is TaskOutcome.PASS, result.answer
    assert result.metrics["controller_check"]["decided"] is True


def test_unproven_incomplete_fix_fails_the_controller_build(sandbox, tmp_path):
    sandbox.scenario("compile_error")
    task_id = str(uuid4())
    turns = _edits_then(_claims("success"), ChatResponse(content="Fixed it."), fixes=1)
    controller, manager = _controller(sandbox.root, tmp_path, turns)

    result = controller.run("fix the build", task_id=task_id, skill_name="fix-build-failure")

    assert result.outcome is TaskOutcome.FAIL, result.answer
    assert result.reason_code == "verification_failed"
    assert result.verified_at_completion is False and result.verification_ran is True
    assert result.metrics["controller_check"] == {"check": "run-build", "decided": True}
    assert "The controller ran the configured full build on the candidate: it failed." in result.answer
    # Kept for review like any unproven candidate, and said plainly to be unproven.
    assert "NOT proven: no full build passed" in result.answer
    assert "A full build of the candidate passed." not in result.answer
    manager.discard(manager.load(task_id)[0])


@pytest.mark.parametrize("claim", ["failure", "diagnosis", "needs_action"])
def test_a_worker_that_reports_no_success_is_not_overruled_by_a_controller_check(sandbox, tmp_path, claim):
    sandbox.scenario("compile_error")
    turns = _edits_then(_claims(claim), ChatResponse(content="Not done."))
    controller, _ = _controller(sandbox.root, tmp_path, turns)

    result = controller.run("fix the build", task_id=str(uuid4()), skill_name="fix-build-failure")

    assert "controller_check" not in result.metrics
    assert result.outcome is not TaskOutcome.PASS
    assert result.verified_at_completion is False
    assert "NOT proven" in result.answer
    assert "The controller" not in result.answer


def test_a_worker_proven_fix_needs_no_controller_check(sandbox, tmp_path):
    sandbox.scenario("compile_error")
    controller, manager = _controller(sandbox.root, tmp_path, _fixing_turns())

    result = controller.run("fix the build", task_id=str(uuid4()), skill_name="fix-build-failure")

    assert result.outcome is TaskOutcome.PASS
    assert "controller_check" not in result.metrics


def test_test_fix_proven_only_by_a_build_gets_the_controller_full_test_run(sandbox, tmp_path):
    sandbox.scenario("test_failure")
    task_id = str(uuid4())
    turns = [
        ChatResponse(tool_calls=[tool_call("propose_patch", {
            "path": RING, "find": "count_ + 1 == slots_.size()", "replace": "count_ == slots_.size()"}, "t1")]),
        lambda m: ChatResponse(tool_calls=[tool_call("apply_patch", {"patch_id": _patch_id(m)}, "t2")]),
        ChatResponse(tool_calls=[tool_call("build_target", {}, "t3")]),
        ChatResponse(tool_calls=[tool_call("submit_answer", {
            "claim": "success", "summary": "full() was off by one", "evidence_ids": ["build_target:2"]}, "t4")]),
        ChatResponse(content="Fixed RingBuffer::full()."),
    ]
    controller, manager = _controller(sandbox.root, tmp_path, turns)

    result = controller.run("fix the failing tests", task_id=task_id, skill_name="fix-test-failure")

    assert result.outcome is TaskOutcome.PASS, result.answer
    assert result.metrics["controller_check"] == {"check": "run-tests", "decided": True}
    assert result.metrics["proof_binding"]["scope"] == "full_test"
    assert "full build and test run on the candidate: it passed." in result.answer
    assert result.metrics["candidate"]["retained"] is True
    verdict_block_from_task_result(result)
    manager.discard(manager.load(task_id)[0])


def test_stop_during_the_controller_check_discards_the_candidate(sandbox, tmp_path):
    import threading
    import time

    from local_agent.session.cancellation import CancellationToken

    sandbox.scenario("compile_error")
    marker = tmp_path / "build-started"
    config = sandbox.root / ".local-agent.toml"
    slow_build = json.dumps([
        sys.executable, "-c",
        f"from pathlib import Path; import time; Path({str(marker)!r}).write_text('x'); time.sleep(60)",
    ])
    text = config.read_text(encoding="utf-8")
    text = text.replace(
        'build = ["cmake", "--build", "build", "--config", "Debug", "--parallel", "4"]',
        f"build = {slow_build}",
    )
    text = text.replace('configure = ["cmake", "-S", ".", "-B", "build", "-DCMAKE_BUILD_TYPE=Debug"]', "")
    config.write_text(text, encoding="utf-8")
    subprocess.run(["git", "-c", "user.email=a@b.c", "-c", "user.name=t", "commit", "-qam", "slow build"],
                   cwd=sandbox.root, check=True)

    task_id = str(uuid4())
    token = CancellationToken(task_id, 0)
    turns = _edits_then(_claims("success"), ChatResponse(content="Fixed it."))
    controller, manager = _controller(sandbox.root, tmp_path, turns)
    results = []
    worker = threading.Thread(target=lambda: results.append(controller.run(
        "fix the build", task_id=task_id, skill_name="fix-build-failure",
        cancellation_probe=token)))
    worker.start()
    deadline = time.monotonic() + 60
    while not marker.exists() and time.monotonic() < deadline:
        time.sleep(0.05)
    assert marker.exists(), "the controller check never started its build"
    token.request()
    worker.join(30)

    assert not worker.is_alive(), "Stop did not reach the controller check"
    result = results[0]
    assert result.outcome is TaskOutcome.BLOCKED and result.reason_code == "cancelled"
    assert result.metrics["candidate"] == {
        "retained": False, "stopped": True,
        "workspace_existed": True, "workspace_removed": True,
    }
    assert _worktrees(sandbox.root) == 1
    with pytest.raises(WorkspaceError):
        manager.load(task_id)


# ----------------------------------------- a proposal that was never applied (PTL J11)


TEXT_UTIL = "src/text_util.cpp"
_TOP = '#include "sandbox/text_util.hpp"'


def _propose_top_comment(call_id="j1"):
    return ChatResponse(tool_calls=[tool_call("propose_patch", {
        "path": TEXT_UTIL, "find": _TOP,
        "replace": "// String helpers used by the sandbox.\n" + _TOP}, call_id)])


def _apply_latest(call_id):
    return lambda m: ChatResponse(tool_calls=[tool_call("apply_patch", {"patch_id": _patch_id(m)}, call_id)])


def test_a_submitted_answer_with_an_unapplied_proposal_is_sent_back_once(sandbox, tmp_path):
    """PTL J11: read, propose, submit. The edit is applied after one reminder and
    the controller's own full build and test run proves it."""
    seen = {}

    def record_then_apply(m):
        seen["reminder"] = next(
            (x.get("content") or "" for x in reversed(m) if x.get("role") == "tool"), "")
        return _apply_latest("j3")(m)

    turns = [
        ChatResponse(tool_calls=[tool_call("read_file", {"path": TEXT_UTIL}, "j0")]),
        _propose_top_comment("j1"),
        ChatResponse(tool_calls=[tool_call("submit_answer", {
            "claim": "success", "summary": "added the comment", "evidence_ids": []}, "j2")]),
        record_then_apply,
        ChatResponse(tool_calls=[tool_call("submit_answer", {
            "claim": "success", "summary": "added the comment", "evidence_ids": []}, "j4")]),
        ChatResponse(content="Added the comment."),
    ]
    task_id = str(uuid4())
    controller, manager = _controller(sandbox.root, tmp_path, turns)

    result = controller.run(
        "change: add a one-line comment at the top of src/text_util.cpp saying what the file contains",
        task_id=task_id, skill_name="implement-change")

    assert "never applied it" in seen["reminder"] and '"accepted": false' in seen["reminder"]
    assert result.outcome is TaskOutcome.PASS, result.answer
    assert result.metrics["candidate"]["paths"] == [TEXT_UTIL]
    assert result.metrics["controller_check"] == {"check": "run-tests", "decided": True}
    verdict_block_from_task_result(result)
    manager.discard(manager.load(task_id)[0])


def test_a_prose_finish_with_an_unapplied_proposal_gets_the_reminder(sandbox, tmp_path):
    seen = {}

    def record_then_apply(m):
        seen["reminder"] = m[-1].get("content") or ""
        return _apply_latest("k2")(m)

    turns = [
        _propose_top_comment("k1"),
        ChatResponse(content="Done, I added the comment."),
        record_then_apply,
        ChatResponse(tool_calls=[tool_call("submit_answer", {
            "claim": "success", "summary": "added the comment", "evidence_ids": []}, "k3")]),
        ChatResponse(content="Added the comment."),
    ]
    task_id = str(uuid4())
    controller, manager = _controller(sandbox.root, tmp_path, turns)

    result = controller.run("change: add a comment at the top of src/text_util.cpp",
                            task_id=task_id, skill_name="implement-change")

    assert "never applied it" in seen["reminder"]
    assert result.outcome is TaskOutcome.PASS, result.answer
    manager.discard(manager.load(task_id)[0])


def test_the_unapplied_proposal_reminder_is_given_once_and_never_applies_anything(sandbox, tmp_path):
    turns = [
        _propose_top_comment("u1"),
        ChatResponse(tool_calls=[tool_call("submit_answer", {
            "claim": "success", "summary": "added it", "evidence_ids": []}, "u2")]),
        ChatResponse(tool_calls=[tool_call("submit_answer", {
            "claim": "success", "summary": "added it", "evidence_ids": []}, "u3")]),
        ChatResponse(content="Added it."),
    ]
    controller, _ = _controller(sandbox.root, tmp_path, turns)

    result = controller.run("change: add a comment at the top of src/text_util.cpp",
                            task_id=str(uuid4()), skill_name="implement-change")

    # The second submission is accepted as it stands: the product never applies the
    # model's proposal on its behalf, and an unchanged candidate proves nothing.
    assert result.outcome is not TaskOutcome.PASS
    assert result.verified_at_completion is False
    assert result.metrics["candidate"]["retained"] is False
    assert "controller_check" not in result.metrics


def test_an_applied_proposal_gets_no_reminder(sandbox, tmp_path):
    turns = [
        _propose_top_comment("a1"),
        _apply_latest("a2"),
        ChatResponse(content="Added it."),
        ChatResponse(tool_calls=[tool_call("submit_answer", {
            "claim": "success", "summary": "added it", "evidence_ids": []}, "a3")]),
        ChatResponse(content="Added it."),
    ]
    client_messages = []

    def spy(m):
        client_messages.extend(m)
        return turns[3]

    turns_with_spy = turns[:3] + [spy] + turns[4:]
    task_id = str(uuid4())
    controller, manager = _controller(sandbox.root, tmp_path, turns_with_spy)

    result = controller.run("change: add a comment at the top of src/text_util.cpp",
                            task_id=task_id, skill_name="implement-change")

    assert not any("never applied it" in (m.get("content") or "") for m in client_messages)
    assert result.outcome is TaskOutcome.PASS, result.answer
    manager.discard(manager.load(task_id)[0])


def test_an_empty_find_says_how_to_insert_at_the_start(sandbox, tmp_path):
    seen = {}

    def record(m):
        seen["result"] = next(x.get("content") or "" for x in reversed(m) if x.get("role") == "tool")
        return ChatResponse(content="stopping")

    turns = [
        ChatResponse(tool_calls=[tool_call("propose_patch", {
            "path": TEXT_UTIL, "find": "", "replace": "// comment\n"}, "e1")]),
        record,
        ChatResponse(content="stopping"),
    ]
    controller, _ = _controller(sandbox.root, tmp_path, turns)
    controller.run("change: add a comment at the top of src/text_util.cpp",
                   task_id=str(uuid4()), skill_name="implement-change")
    assert "`find` is empty" in seen["result"] and "appears" not in seen["result"]


# ------------------------------- an editing task that changed nothing (PTL 09d4d71)


def _submit(claim, call_id):
    return ChatResponse(tool_calls=[tool_call("submit_answer", {
        "claim": claim, "summary": "done", "evidence_ids": []}, call_id)])


def _last_tool_content(m):
    return next((x.get("content") or "" for x in reversed(m) if x.get("role") == "tool"), "")


def test_a_success_claim_with_nothing_changed_is_sent_back_once_then_the_fix_lands(sandbox, tmp_path):
    """PTL J10: reads and searches, no patch, then "the fix was applied". The claim is
    held back once from recorded state; the model then edits and the controller proves it."""
    sandbox.scenario("test_failure")
    seen = {}

    def record_then_propose(m):
        seen["reminder"] = _last_tool_content(m)
        return ChatResponse(tool_calls=[tool_call("propose_patch", {
            "path": RING, "find": "count_ + 1 == slots_.size()",
            "replace": "count_ == slots_.size()"}, "n2")])

    turns = [
        ChatResponse(tool_calls=[tool_call("read_file", {"path": RING}, "n0")]),
        _submit("success", "n1"),
        record_then_propose,
        lambda m: ChatResponse(tool_calls=[tool_call("apply_patch", {"patch_id": _patch_id(m)}, "n3")]),
        _submit("success", "n4"),
        ChatResponse(content="Fixed RingBuffer::full()."),
    ]
    task_id = str(uuid4())
    controller, manager = _controller(sandbox.root, tmp_path, turns)

    result = controller.run("fix the failing tests", task_id=task_id, skill_name="fix-test-failure")

    assert "No file has been changed yet" in seen["reminder"] and '"accepted": false' in seen["reminder"]
    assert result.outcome is TaskOutcome.PASS, result.answer
    assert result.metrics["controller_check"] == {"check": "run-tests", "decided": True}
    verdict_block_from_task_result(result)
    manager.discard(manager.load(task_id)[0])


def test_a_prose_finish_with_nothing_changed_gets_the_reminder(sandbox, tmp_path):
    """PTL J11: search, read, stop."""
    sandbox.scenario("compile_error")
    seen = {}

    def record(m):
        seen["reminder"] = m[-1].get("content") or ""
        return ChatResponse(content="I looked at it.")

    turns = [
        ChatResponse(tool_calls=[tool_call("read_file", {"path": RING}, "p0")]),
        ChatResponse(content="The build is fixed."),
        record,
        _submit("failure", "p1"),
        ChatResponse(content="Not fixed."),
    ]
    controller, _ = _controller(sandbox.root, tmp_path, turns)

    result = controller.run("fix the build", task_id=str(uuid4()), skill_name="fix-build-failure")

    assert "No file has been changed yet" in seen["reminder"]
    assert result.outcome is not TaskOutcome.PASS
    assert result.metrics["candidate"]["retained"] is False


@pytest.mark.parametrize("claim", ["failure", "diagnosis", "needs_action"])
def test_a_worker_reporting_no_success_without_edits_is_not_reminded(sandbox, tmp_path, claim):
    sandbox.scenario("compile_error")
    seen = []

    def spy(m):
        seen.extend(x.get("content") or "" for x in m)
        return ChatResponse(content="Stopping.")

    turns = [
        ChatResponse(tool_calls=[tool_call("read_file", {"path": RING}, "f0")]),
        _submit(claim, "f1"),
        spy,
    ]
    controller, _ = _controller(sandbox.root, tmp_path, turns)

    result = controller.run("fix the build", task_id=str(uuid4()), skill_name="fix-build-failure")

    assert not any("No file has been changed yet" in text for text in seen)
    assert result.outcome is not TaskOutcome.PASS


def test_the_nothing_changed_reminder_is_given_once_and_changes_nothing(sandbox, tmp_path):
    sandbox.scenario("compile_error")
    turns = [
        ChatResponse(tool_calls=[tool_call("read_file", {"path": RING}, "o0")]),
        _submit("success", "o1"),
        _submit("success", "o2"),
        ChatResponse(content="Done."),
    ]
    controller, _ = _controller(sandbox.root, tmp_path, turns)

    result = controller.run("fix the build", task_id=str(uuid4()), skill_name="fix-build-failure")

    assert result.outcome is not TaskOutcome.PASS
    assert result.verified_at_completion is False
    assert result.metrics["candidate"]["retained"] is False
    assert "controller_check" not in result.metrics


def test_the_edit_reminder_never_applies_to_read_only_or_unnarrowed_runs():
    from types import SimpleNamespace

    from local_agent.agent.orchestrator import _edit_reminder

    read_only = SimpleNamespace(toolset=["read_file", "search_text", "submit_answer"],
                                history=[], mutation_epoch=0)
    unnarrowed = SimpleNamespace(toolset=[], history=[], mutation_epoch=0)
    editing = SimpleNamespace(toolset=["read_file", "propose_patch", "apply_patch"],
                              history=[], mutation_epoch=0)
    assert _edit_reminder(read_only, None) is None
    assert _edit_reminder(unnarrowed, "success") is None
    assert _edit_reminder(editing, "success") is not None
    assert _edit_reminder(editing, "diagnosis") is None
    editing.mutation_epoch = 1
    assert _edit_reminder(editing, "success") is None


def _bracket_file_turns():
    return [
        ChatResponse(tool_calls=[tool_call("propose_file", {
            "path": "note[1].txt", "content": "candidate note\n"}, "n1")]),
        lambda m: ChatResponse(tool_calls=[tool_call("apply_patch", {"patch_id": _patch_id(m)}, "n2")]),
        ChatResponse(tool_calls=[tool_call("build_target", {}, "n3")]),
        lambda m: ChatResponse(tool_calls=[tool_call("submit_answer", {
            "claim": "success", "summary": "added note[1].txt", "evidence_ids": ["build_target:2"]}, "n4")]),
        ChatResponse(content="Added note[1].txt."),
    ]


def test_public_commit_of_a_bracket_named_candidate_leaves_the_matching_user_file(sandbox, tmp_path):
    """#393 through the public /commit: Git would read note[1].txt as a pattern matching note1.txt."""
    sandbox.scenario("clean")
    (sandbox.root / "note1.txt").write_text("original\n", encoding="utf-8")
    subprocess.run(["git", "add", "note1.txt"], cwd=sandbox.root, check=True)
    _allow_commits(sandbox.root)
    controller, _manager = _controller(sandbox.root, tmp_path, _bracket_file_turns())
    candidate_task = str(uuid4())
    prepared = controller.run("change: add note[1].txt", task_id=candidate_task,
                              skill_name="implement-change")
    assert prepared.metrics["candidate"]["retained"] is True, prepared.answer
    assert _apply(controller, candidate_task).outcome is TaskOutcome.PASS
    # The user's unrelated work on the file the pattern would match, staged and unstaged.
    note1 = sandbox.root / "note1.txt"
    note1.write_text("user staged\n", encoding="utf-8")
    subprocess.run(["git", "add", "note1.txt"], cwd=sandbox.root, check=True)
    note1.write_text("user unstaged\n", encoding="utf-8")
    head_blob = _git_out(sandbox.root, "rev-parse", "HEAD:note1.txt")
    index_entry = _git_out(sandbox.root, "ls-files", "--stage", "--", "note1.txt")

    result = _commit(controller, candidate_task)

    assert result.outcome is TaskOutcome.PASS, result.answer
    commit = result.metrics["candidate_commit"]["commit"]
    assert _git_out(sandbox.root, "diff-tree", "--no-commit-id", "--name-only", "-r", "-z",
                    commit).split("\0")[:-1] == ["note[1].txt"]
    assert _git_out(sandbox.root, "rev-parse", "HEAD:note1.txt") == head_blob
    assert _git_out(sandbox.root, "ls-files", "--stage", "--", "note1.txt") == index_entry
    assert note1.read_text(encoding="utf-8") == "user unstaged\n"
def test_public_commit_refused_by_git_leaves_head_and_index_alone(sandbox, tmp_path):
    """#395: a genuine git failure (a signer that always fails) is a refusal with no effect."""
    sandbox.scenario("compile_error")
    _allow_commits(sandbox.root)
    controller, manager, candidate_task = _prepare(sandbox, tmp_path)
    _apply(controller, candidate_task)
    for key, value in (("commit.gpgsign", "true"), ("gpg.format", "openpgp"),
                       ("gpg.program", "false")):
        subprocess.run(["git", "config", key, value], cwd=sandbox.root, check=True)
    head = _git_out(sandbox.root, "rev-parse", "HEAD")
    index = _git_out(sandbox.root, "ls-files", "--stage")
    status = _git_out(sandbox.root, "status", "--porcelain=v1")

    result = _commit(controller, candidate_task)

    assert result.outcome is TaskOutcome.BLOCKED, result.answer
    assert "Nothing was committed" in result.answer and "git commit failed" in result.answer
    assert _git_out(sandbox.root, "rev-parse", "HEAD") == head
    assert _git_out(sandbox.root, "ls-files", "--stage") == index
    assert _git_out(sandbox.root, "status", "--porcelain=v1") == status
