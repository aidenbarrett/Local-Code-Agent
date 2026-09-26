"""Fix-the-build journey: the worker edits its own worktree, never the user's checkout."""
from __future__ import annotations

import base64
import json
import subprocess
import sys
from pathlib import Path
from uuid import uuid4

import pytest

from local_agent.config import load_repo_config
from local_agent.llm.client import ScriptedClient, tool_call
from local_agent.llm.protocol import ChatResponse
from local_agent.session.contracts import TaskOutcome
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
    assert result.metrics["candidate"] == {"retained": False, "stopped": True}
    assert "nothing is available to apply" in result.answer
    assert _worktrees(sandbox.root) == 1
    with pytest.raises(WorkspaceError):
        manager.load(task_id)
# ------------------------------------------------------ fault injection: /apply


def _fail_apply_after(manager, write):
    """Make the real (non --check) git apply write something, then report failure."""
    real = manager._git

    def faulty(cwd, *args, **kwargs):
        if args and args[0] == "apply" and "--check" not in args:
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
