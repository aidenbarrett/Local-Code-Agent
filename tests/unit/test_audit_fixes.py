"""Slice 3c: what the first real-model run taught the apparatus.

Each test here pins a defect the nine-row audit found, in the layer the audit
charged it to. None of them are about the model.
"""

from __future__ import annotations

from pathlib import Path

from local_agent.agent import Orchestrator, SkillLibrary
from local_agent.agent.outcome import Outcome
from local_agent.tools.base import Reason, Locus
from local_agent.agent.state import HaltCause
from local_agent.config import load_repo_config
from local_agent.llm.client import ScriptedClient, tool_call
from local_agent.llm.models import ChatResponse

REPO = Path(__file__).resolve().parent.parent.parent


def _orch(root: Path, client, **kwargs):
    repo = load_repo_config(root)
    registry, _, _ = build_registry(repo)
    skills = SkillLibrary.discover(REPO / "skills")
    return Orchestrator(repo, registry, client, skills, approval=lambda *a: True, **kwargs)


from local_agent.tools import build_registry  # noqa: E402


def _patch_id(messages):
    import json
    for m in reversed(messages):
        if m.get("role") == "tool":
            try:
                pid = (json.loads(m.get("content") or "{}").get("data") or {}).get("patch_id")
            except json.JSONDecodeError:
                continue
            if pid:
                return pid
    raise AssertionError("no patch_id")


def _submit(claim, evidence=None, summary="done"):
    return lambda messages: ChatResponse(tool_calls=[tool_call("submit_answer", {
        "claim": claim, "summary": summary, "evidence_ids": evidence or []}, "cs")])


# ------------------------------------------------- 7. repeat guard is mutation-aware


def test_edit_build_edit_build_is_not_a_repeat_loop(sandbox):
    sandbox.scenario("compile_error")
    turns = [
        ChatResponse(tool_calls=[tool_call("build_target", {}, "c1")]),
        ChatResponse(tool_calls=[tool_call("propose_patch", {"path": "src/ring_buffer.cpp", "find": "++count;", "replace": "++count_;"}, "c2")]),
        lambda m: ChatResponse(tool_calls=[tool_call("apply_patch", {"patch_id": _patch_id(m)}, "c3")]),
        ChatResponse(tool_calls=[tool_call("build_target", {}, "c4")]),
        ChatResponse(tool_calls=[tool_call("propose_patch", {"path": "src/ring_buffer.cpp", "find": "return count_ == 0 }", "replace": "return count_ == 0; }"}, "c5")]),
        lambda m: ChatResponse(tool_calls=[tool_call("apply_patch", {"patch_id": _patch_id(m)}, "c6")]),
        ChatResponse(tool_calls=[tool_call("build_target", {}, "c7")]),
        ChatResponse(tool_calls=[tool_call("build_target", {}, "c8")]),
        _submit("success", ["build_target:7"]),
        ChatResponse(content="fixed"),
    ]
    result = _orch(sandbox.root, ScriptedClient(turns)).run("Fix the compilation errors", skill_name="fix-build-failure")
    assert result.state.halt_cause is None, result.state.halt_reason
    assert result.state.mutation_epoch == 2
    assert result.state.verified is True
    assert result.outcome is Outcome.PASS
    builds = [h for h in result.state.history if h.name == "build_target"]
    assert [h.epoch for h in builds] == [0, 1, 2, 2]


def test_four_identical_builds_on_an_unchanged_tree_still_halt(sandbox):
    sandbox.scenario("compile_error")
    turns = [ChatResponse(tool_calls=[tool_call("build_target", {}, f"c{i}")]) for i in range(6)]
    result = _orch(sandbox.root, ScriptedClient(turns)).run("build it", skill_name="fix-build-failure")
    assert result.state.halt_cause is HaltCause.REPEAT_LOOP
    assert result.state.mutation_epoch == 0


def test_a_failed_mutation_does_not_open_a_new_epoch(sandbox):
    turns = [ChatResponse(tool_calls=[tool_call("propose_patch", {"path": "src/ring_buffer.cpp", "find": "THIS TEXT IS NOT THERE", "replace": "x"}, "c1")]), ChatResponse(content="gave up")]
    result = _orch(sandbox.root, ScriptedClient(turns)).run("fix", skill_name="fix-build-failure")
    assert result.state.mutation_epoch == 0


# ------------------------------------------ 8. verification belongs to the orchestrator


def test_a_correct_fix_with_a_bad_citation_is_still_verified(sandbox):
    sandbox.scenario("compile_error")
    turns = [
        ChatResponse(tool_calls=[tool_call("propose_patch", {"path": "src/ring_buffer.cpp", "find": "++count;", "replace": "++count_;"}, "c1")]),
        lambda m: ChatResponse(tool_calls=[tool_call("apply_patch", {"patch_id": _patch_id(m)}, "c2")]),
        ChatResponse(tool_calls=[tool_call("propose_patch", {"path": "src/ring_buffer.cpp", "find": "return count_ == 0 }", "replace": "return count_ == 0; }"}, "c3")]),
        lambda m: ChatResponse(tool_calls=[tool_call("apply_patch", {"patch_id": _patch_id(m)}, "c4")]),
        ChatResponse(tool_calls=[tool_call("build_target", {}, "c5")]),
        _submit("success", ["nonsense:99"]),
        ChatResponse(content="fixed"),
    ]
    result = _orch(sandbox.root, ScriptedClient(turns)).run("fix", skill_name="fix-build-failure")
    assert result.state.verified is True
    assert result.outcome is Outcome.PASS
    assert result.state.cited_correctly is False
    assert result.state.cited_unknown == ["nonsense:99"]


def test_a_success_claim_with_no_passing_build_is_still_not_verified(sandbox):
    sandbox.scenario("compile_error")
    turns = [ChatResponse(tool_calls=[tool_call("build_target", {}, "c1")]), _submit("success", ["build_target:0"]), ChatResponse(content="fixed (it was not)")]
    result = _orch(sandbox.root, ScriptedClient(turns)).run("fix", skill_name="fix-build-failure")
    assert result.state.verified is False
    assert result.outcome is Outcome.FAIL


def test_a_mutation_after_verification_makes_it_stale(sandbox):
    turns = [
        ChatResponse(tool_calls=[tool_call("build_target", {}, "c1")]),
        ChatResponse(tool_calls=[tool_call("propose_patch", {"path": "src/ring_buffer.cpp", "find": "++count_;", "replace": "++count_; // touched"}, "c2")]),
        lambda m: ChatResponse(tool_calls=[tool_call("apply_patch", {"patch_id": _patch_id(m)}, "c3")]),
        _submit("success", ["build_target:0"]),
        ChatResponse(content="done"),
    ]
    result = _orch(sandbox.root, ScriptedClient(turns)).run("fix", skill_name="fix-build-failure")
    assert result.state.verified is False
    assert result.outcome is Outcome.FAIL


# ------------------------------------------- 9. read-only skills may claim success


def test_navigation_may_say_success_without_a_build(sandbox):
    turns = [ChatResponse(tool_calls=[tool_call("read_file", {"path": "include/sandbox/ring_buffer.hpp"}, "c1")]), _submit("success", [], "RingBuffer is declared in include/sandbox/ring_buffer.hpp"), ChatResponse(content="answered")]
    result = _orch(sandbox.root, ScriptedClient(turns)).run("where is RingBuffer", skill_name="repo-navigation")
    assert result.outcome is Outcome.PASS
    assert result.state.verified is False


# -------------------------------------- 10. a partial build is not verification


def test_a_targeted_build_does_not_satisfy_the_contract(sandbox):
    sandbox.scenario("link_error")
    turns = [ChatResponse(tool_calls=[tool_call("build_target", {"target": "sandbox"}, "c1")]), _submit("success", ["build_target:0"]), ChatResponse(content="builds")]
    result = _orch(sandbox.root, ScriptedClient(turns)).run("fix", skill_name="fix-build-failure")
    assert result.state.history[0].ok is True
    assert result.state.verified is False
    assert result.state.verification_attempted is True
    assert result.outcome is Outcome.FAIL


def test_configure_alone_is_not_verification(sandbox):
    sandbox.scenario("compile_error")
    turns = [ChatResponse(tool_calls=[tool_call("configure_project", {}, "c1")]), _submit("success", ["configure_project:0"]), ChatResponse(content="configured")]
    result = _orch(sandbox.root, ScriptedClient(turns)).run("fix", skill_name="fix-build-failure")
    assert result.state.verified is False


# -------------------------------------------------- 11. diagnose and fix are split


def test_diagnose_skill_has_no_patch_tools_and_fix_skill_does():
    skills = SkillLibrary.discover(REPO / "skills")
    assert "propose_patch" not in skills.get("diagnose-build-failure").tools
    assert "apply_patch" not in skills.get("diagnose-build-failure").tools
    assert {"propose_patch", "apply_patch"} <= set(skills.get("fix-build-failure").tools)


def test_router_sends_fix_tasks_to_fix_and_locate_tasks_to_diagnose():
    skills = SkillLibrary.discover(REPO / "skills")
    cases = {
        "Fix the compilation errors in this repository, then prove the build succeeds.": "fix-build-failure",
        "The build is broken. Find the first compiler error and explain the cause.": "diagnose-build-failure",
        "The build fails at link time. What is missing and where should it be defined?": "diagnose-build-failure",
    }
    for task, expected in cases.items():
        name, score, confident = skills.route_with_confidence(task)
        assert (name, confident) == (expected, True), (task, name, score, confident)


def test_diagnose_toolset_cannot_mutate(sandbox):
    turns = [ChatResponse(tool_calls=[tool_call("apply_patch", {"patch_id": "x"}, "c1")]), ChatResponse(content="oh")]
    result = _orch(sandbox.root, ScriptedClient(turns)).run("find the error", skill_name="diagnose-build-failure")
    assert result.state.history[0].reason == "tool_not_allowed"
    assert result.state.mutation_epoch == 0


def test_an_invented_tool_and_a_forbidden_one_are_different_failures(sandbox):
    turns = [ChatResponse(tool_calls=[tool_call("frobnicate", {}, "c1")]), ChatResponse(tool_calls=[tool_call("apply_patch", {"patch_id": "x"}, "c2")]), ChatResponse(content="fine")]
    result = _orch(sandbox.root, ScriptedClient(turns)).run("find the error", skill_name="diagnose-build-failure")
    assert [h.reason for h in result.state.history] == ["unknown_tool", "tool_not_allowed"]
    assert Reason.UNKNOWN_TOOL.locus is Locus.MODEL
    assert Reason.TOOL_NOT_ALLOWED.locus is Locus.MODEL
    assert all(h.verdict == "unknown" for h in result.state.history)


# --------------------------------------------------------- tool feedback quality


def test_run_test_on_an_unbuilt_tree_says_so(sandbox):
    repo = load_repo_config(sandbox.root)
    registry, _, _ = build_registry(repo)
    result = registry.get("run_test").handler()
    assert result.ok is False
    assert "not configured or not built" in result.summary
    assert "build_target" in result.summary


# ------------------------------------------------- citations


def test_canonical_evidence_id_is_exposed_to_the_model(sandbox):
    import json
    seen = {}

    def submit(messages):
        tool_message = next(m for m in reversed(messages) if m.get("role") == "tool")
        payload = json.loads(tool_message["content"])
        evidence_id = payload["data"]["evidence_id"]
        seen["id"] = evidence_id
        return ChatResponse(tool_calls=[tool_call("submit_answer", {"claim": "diagnosis", "summary": "observed", "evidence_ids": [evidence_id]}, "c2")])

    turns = [ChatResponse(tool_calls=[tool_call("run_test", {"name_filter": "ring_buffer"}, "c1")]), submit]
    result = _orch(sandbox.root, ScriptedClient(turns)).run("why does ring_buffer fail", skill_name="diagnose-test-failure")
    assert seen["id"] == "run_test:0"
    assert result.state.cited_unknown == []
    assert result.state.citation_schemes["chosen"] == "canonical"


def test_noncanonical_ordinal_is_not_guessed(sandbox):
    turns = [
        ChatResponse(tool_calls=[tool_call("run_test", {"name_filter": "ring_buffer"}, "c1")]),
        ChatResponse(tool_calls=[tool_call("submit_answer", {"claim": "diagnosis", "summary": "observed", "evidence_ids": ["run_test:1"]}, "c2")]),
    ]
    result = _orch(sandbox.root, ScriptedClient(turns)).run("why does ring_buffer fail", skill_name="diagnose-test-failure")
    assert result.state.cited_unknown == ["run_test:1"]
    assert result.state.cited_correctly is False


def test_a_diagnosis_can_cite_correctly_without_a_passing_test(sandbox):
    turns = [
        ChatResponse(tool_calls=[tool_call("run_test", {"name_filter": "ring_buffer"}, "c1")]),
        ChatResponse(tool_calls=[tool_call("submit_answer", {"claim": "diagnosis", "summary": "full() is off by one.", "evidence_ids": ["run_test:0"]}, "c2")]),
    ]
    result = _orch(sandbox.root, ScriptedClient(turns)).run("why does ring_buffer fail", skill_name="diagnose-test-failure")
    assert result.state.claim == "diagnosis"
    assert result.state.cited_unknown == []
    assert result.state.cited_correctly is True
    assert result.state.verified is False


def test_a_success_claim_still_needs_a_passing_verification_cited(sandbox):
    turns = [
        ChatResponse(tool_calls=[tool_call("run_test", {"name_filter": "ring_buffer"}, "c1")]),
        ChatResponse(tool_calls=[tool_call("submit_answer", {"claim": "success", "summary": "fixed it.", "evidence_ids": ["run_test:0"]}, "c2")]),
    ]
    result = _orch(sandbox.root, ScriptedClient(turns)).run("fix the ring buffer", skill_name="fix-test-failure")
    assert result.state.cited_unknown == []
    assert result.state.cited_correctly is False


# ------------------------------------------------- the success contract


def test_every_eval_case_declares_its_own_success_contract():
    import dataclasses
    import sys
    sys.path.insert(0, str(REPO / "evaluation"))
    from task_contracts import CASES, EvalCase
    fields = {f.name: f for f in dataclasses.fields(EvalCase)}
    contract = fields["verification_required"]
    assert contract.default is dataclasses.MISSING
    assert contract.default_factory is dataclasses.MISSING
    for case in CASES:
        assert isinstance(case.verification_required, bool), case.name


def test_the_case_contract_matches_the_skill_it_pins():
    import sys
    sys.path.insert(0, str(REPO / "evaluation"))
    from task_contracts import CASES
    skills = SkillLibrary.discover(REPO / "skills")
    for case in CASES:
        if not case.skill:
            continue
        skill = skills.get(case.skill)
        assert skill is not None, case.skill
        assert case.verification_required == skill.verification_required, (f"{case.name}: case says {case.verification_required}, skill {case.skill} says {skill.verification_required}")


def test_a_read_only_task_is_not_asked_to_prove_itself_with_a_build(sandbox):
    turns = [
        ChatResponse(tool_calls=[tool_call("read_file", {"path": "include/sandbox/ring_buffer.hpp"}, "c1")]),
        ChatResponse(tool_calls=[tool_call("submit_answer", {"claim": "diagnosis", "summary": "RingBuffer lives in include/sandbox/ring_buffer.hpp."}, "c2")]),
    ]
    result = _orch(sandbox.root, ScriptedClient(turns)).run("where does RingBuffer live", skill_name="repo-navigation", verification_required=False)
    assert result.outcome is Outcome.PASS
    assert result.state.verification_attempted is False
    assert result.state.halt_cause is None
