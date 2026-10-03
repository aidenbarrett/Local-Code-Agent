"""`build it` and `run the tests` run the configured check with no model deciding.

The controller drives the ordinary worker with a fixed plan, so verification, proof
binding and the durable facts that `fix it` reads have one owner. Build, test, git
and the durable store are real; no model is available to these tasks at all.
"""
from __future__ import annotations

import json
from uuid import uuid4

import pytest

from local_agent.config import load_repo_config
from local_agent.llm.client import ScriptedClient, tool_call
from local_agent.llm.protocol import ChatResponse
from local_agent.session.cancellable_task_executor import CancellableDurableTaskExecutor
from local_agent.session.configured_checks import (
    RUN_BUILD_CHECK,
    RUN_TEST_CHECK,
    ConfiguredCheckPlan,
    configured_check_sha256,
)
from local_agent.session.contracts import TaskOutcome
from local_agent.session.conversation_gateway import ConversationGateway
from local_agent.session.durable_routes import DurableRouteEvents
from local_agent.session.durable_task_controller import AdmittedDurableTaskController
from local_agent.session.event_buffer import EventBuffer
from local_agent.session.intents import RouteAction, decide_route
from local_agent.session.session_event_service import DurableSessionService
from local_agent.session.session_store import SQLiteSessionStore
from local_agent.session.task_admission import DurableTaskAdmissionRunner
from local_agent.session.task_controller import TaskController
from local_agent.session.task_history import DurableTaskHistory
from local_agent.session.workspaces import GitWorkspaceManager


class NoModel:
    """Any model call from a configured check is a defect."""

    def chat(self, messages, tools=None, max_tokens=None):
        raise AssertionError("a configured check reached a model")


def _tool_message(name: str, execution: str, result: str) -> dict[str, object]:
    return {"role": "tool", "name": name, "tool_call_id": "x",
            "content": json.dumps({"ok": result == "pass", "summary": f"{name} {result}",
                                   "execution": execution, "result": result})}


def _submitted(response: ChatResponse) -> dict[str, object]:
    (call,) = response.tool_calls
    assert call.name == "submit_answer"
    return call.arguments


# ------------------------------------------------------------------ the plan itself


def test_the_build_plan_runs_the_build_then_reports_what_it_saw():
    plan = ConfiguredCheckPlan(RUN_BUILD_CHECK)
    (first,) = plan.chat([{"role": "user", "content": "build it"}]).tool_calls
    assert (first.name, first.arguments) == ("build_target", {})
    passed = _submitted(plan.chat([_tool_message("build_target", "ok", "pass")]))
    assert passed == {"claim": "success", "summary": "build_target: build_target pass",
                      "evidence_ids": ["build_target:0"]}
    failed = _submitted(plan.chat([_tool_message("build_target", "ok", "fail")]))
    assert (failed["claim"], failed["evidence_ids"]) == ("failure", ["build_target:0"])


def test_the_test_plan_builds_first_and_stops_at_a_failed_build():
    plan = ConfiguredCheckPlan(RUN_TEST_CHECK)
    (after_build,) = plan.chat([_tool_message("build_target", "ok", "pass")]).tool_calls
    assert after_build.name == "run_test"
    stopped = _submitted(plan.chat([_tool_message("build_target", "ok", "fail")]))
    assert stopped["claim"] == "failure"
    assert stopped["evidence_ids"] == ["build_target:0"]


def test_a_check_that_could_not_run_claims_nothing():
    plan = ConfiguredCheckPlan(RUN_BUILD_CHECK)
    blocked = _submitted(plan.chat([_tool_message("build_target", "blocked", "unknown")]))
    assert blocked["claim"] == "diagnosis"


def test_an_unknown_check_is_refused():
    with pytest.raises(ValueError):
        ConfiguredCheckPlan("build-and-deploy")
    with pytest.raises(ValueError):
        configured_check_sha256("build-and-deploy", "0" * 64)


def test_the_admission_fingerprint_binds_the_check_and_the_skill_bytes():
    a = configured_check_sha256(RUN_BUILD_CHECK, "0" * 64)
    assert a != configured_check_sha256(RUN_TEST_CHECK, "0" * 64)
    assert a != configured_check_sha256(RUN_BUILD_CHECK, "1" * 64)


# ------------------------------------------------------------------ routing


@pytest.mark.parametrize(("text", "skill"), [
    ("build it", RUN_BUILD_CHECK), ("Build the repo.", RUN_BUILD_CHECK),
    ("run the tests", RUN_TEST_CHECK), ("run tests", RUN_TEST_CHECK), ("test it!", RUN_TEST_CHECK),
])
def test_exact_build_and_test_requests_route_to_configured_checks(text, skill):
    decision = decide_route(text, active_repo_count=1)
    assert (decision.action, decision.skill) == (RouteAction.WORK, skill)


# ------------------------------------------------------------------ the public path


def _session(sandbox, tmp_path, worker_factory):
    service = DurableSessionService(SQLiteSessionStore(tmp_path / "session.db"),
                                    stream_id=str(uuid4()), session_id=str(uuid4()))
    events = EventBuffer(service.stream_id)
    controller = TaskController(
        load_repo_config(sandbox.root), worker_factory, events, allow_execution=True,
        workspaces=GitWorkspaceManager(tmp_path / "ws", controller_commit="c" * 40),
    )
    executor = CancellableDurableTaskExecutor(service, AdmittedDurableTaskController(service, controller))
    history = DurableTaskHistory(service.store, stream_id=service.stream_id)
    gateway = ConversationGateway(
        NoModel(), controller, events,
        task_runner=DurableTaskAdmissionRunner(executor),
        task_history=history, route_events=DurableRouteEvents(service),
    )
    return service, gateway, history


def _no_model_factory():
    raise AssertionError("a configured check asked for a model")


def test_build_it_passes_on_a_clean_tree_with_full_build_proof_and_no_model(sandbox, tmp_path):
    sandbox.scenario("clean")
    service, gateway, _history = _session(sandbox, tmp_path, _no_model_factory)
    try:
        gateway.turn("build it")
        result = gateway.last_result
        assert result.outcome is TaskOutcome.PASS, result.answer
        assert result.verified_at_completion is True
        assert result.metrics["proof_binding"]["scope"] == "full_build"
        assert result.evidence_ids == ("build_target:0",)
    finally:
        service.close()


def test_build_it_observes_a_compile_error_without_a_model(sandbox, tmp_path):
    sandbox.scenario("compile_error")
    service, gateway, history = _session(sandbox, tmp_path, _no_model_factory)
    try:
        gateway.turn("build it")
        result = gateway.last_result
        assert (result.outcome, result.reason_code) == (TaskOutcome.FAIL, "verification_failed")
        assert result.metrics["proof_binding"]["scope"] == "observed_build_failure"
        assert history.failure_kind(result.task_id) == "build"
    finally:
        service.close()


def test_run_the_tests_observes_a_test_failure_and_fix_it_picks_the_test_fix(sandbox, tmp_path):
    sandbox.scenario("test_failure")
    # Only the fix is a model's job. Refuse it here: this test is about which fix
    # the failure selects, and the refusal proves the route reached the worker.
    fix_attempts: list[str] = []

    def factory():
        fix_attempts.append("fix")
        return ScriptedClient([ChatResponse(tool_calls=[tool_call("submit_answer", {
            "claim": "diagnosis", "summary": "not attempted", "evidence_ids": []}, "d1")])])

    service, gateway, history = _session(sandbox, tmp_path, factory)
    try:
        gateway.turn("run the tests")
        tested = gateway.last_result
        assert (tested.outcome, tested.reason_code) == (TaskOutcome.FAIL, "verification_failed"), tested.answer
        assert [e.split(":")[0] for e in tested.evidence_ids] == ["build_target", "run_test"]
        assert history.failure_kind(tested.task_id) == "test"
        assert fix_attempts == []

        gateway.turn("fix it")
        assert fix_attempts == ["fix"]
        fix_task = gateway.last_result
        assert fix_task.task_id != tested.task_id
        admitted = [e for e in service.store.replay(service.stream_id, limit=1000)
                    if e["kind"] == "task.admitted" and e["task_id"] == fix_task.task_id]
        assert [e["payload"]["skill"] for e in admitted] == ["fix-test-failure"]
    finally:
        service.close()


def test_run_the_tests_is_policy_denied_even_after_its_prerequisite_build_passes(sandbox, tmp_path):
    sandbox.scenario("clean")
    config = sandbox.root / ".local-agent.toml"
    config.write_text(
        config.read_text(encoding="utf-8").replace("allow_test = true", "allow_test = false"),
        encoding="utf-8",
    )
    service, gateway, _history = _session(sandbox, tmp_path, _no_model_factory)
    try:
        answer = gateway.turn("run the tests")
        result = gateway.last_result
        assert (result.outcome, result.reason_code) == (TaskOutcome.BLOCKED, "policy_denied")
        assert result.verified_at_completion is False
        assert "allow_test" in answer
        assert [e.split(":")[0] for e in result.evidence_ids] == ["build_target", "run_test"]
    finally:
        service.close()
