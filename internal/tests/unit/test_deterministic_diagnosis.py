"""'Why did that fail?' about a configured check is answered from its record, no model (#462).

A configured check's answer is written by the controller from its tool results, and
the durable admission record (never the answer text) says the task was one. Since #474
that answer names the failure's kind, first evidence and exact argv, so the controller
reports it directly: no model call and no new task. Every other task keeps the
diagnostic worker.
"""
from __future__ import annotations

from uuid import uuid4

import pytest

from local_agent.config import load_repo_config
from local_agent.session.cancellable_task_executor import CancellableDurableTaskExecutor
from local_agent.session.contracts import TaskOutcome
from local_agent.session.conversation_gateway import ConversationGateway
from local_agent.session.durable_routes import DurableRouteEvents
from local_agent.session.durable_task_controller import AdmittedDurableTaskController
from local_agent.session.event_buffer import EventBuffer
from local_agent.session.intents import RULE_TASK_DIAGNOSTIC, RouteAction, RouteDecision, RouteSource
from local_agent.session.session_event_service import DurableSessionService
from local_agent.session.session_store import SQLiteSessionStore
from local_agent.session.task_admission import DurableTaskAdmissionRunner
from local_agent.session.task_controller import TaskController
from local_agent.session.task_history import (
    DurableTaskHistory,
    TaskObservation,
    admitted_configured_check,
)
from local_agent.session.workspaces import GitWorkspaceManager


class NoModel:
    def chat(self, messages, tools=None, max_tokens=None):
        raise AssertionError("the recorded diagnosis reached a model")


def _no_worker():
    raise AssertionError("the recorded diagnosis started a worker")


def _session(sandbox, tmp_path):
    service = DurableSessionService(SQLiteSessionStore(tmp_path / "session.db"),
                                    stream_id=str(uuid4()), session_id=str(uuid4()))
    events = EventBuffer(service.stream_id)
    controller = TaskController(
        load_repo_config(sandbox.root), _no_worker, events, allow_execution=True,
        workspaces=GitWorkspaceManager(tmp_path / "ws", controller_commit="c" * 40),
    )
    executor = CancellableDurableTaskExecutor(service, AdmittedDurableTaskController(service, controller))
    gateway = ConversationGateway(
        NoModel(), controller, events,
        task_runner=DurableTaskAdmissionRunner(executor),
        task_history=DurableTaskHistory(service.store, stream_id=service.stream_id),
        route_events=DurableRouteEvents(service),
    )
    return service, gateway


def _admissions(service) -> list[str]:
    return [e["task_id"] for e in service.store.replay(service.stream_id, limit=1000)
            if e["kind"] == "task.admitted"]


@pytest.mark.parametrize(("scenario", "request_text", "kind"), [
    ("compile_error", "build it", "compile"),
    ("link_error", "build it", "link"),
    ("test_failure", "run the tests", "assertion"),
])
def test_why_did_that_fail_reports_the_recorded_check_without_a_model_or_task(
        sandbox, tmp_path, scenario, request_text, kind):
    sandbox.scenario(scenario)
    service, gateway = _session(sandbox, tmp_path)
    try:
        gateway.turn(request_text)
        failed = gateway.last_result
        assert failed.outcome is TaskOutcome.FAIL, failed.answer
        admitted_before = _admissions(service)

        answer = gateway.turn("why did that fail?")

        assert _admissions(service) == admitted_before, "the diagnosis admitted a task"
        assert answer.startswith(f"Task {failed.task_id} ({request_text}) failed.")
        assert f"FAILED ({kind})" in answer
        assert " -- command: [" in answer
        assert "no task was run" in answer
        assert 'say "fix it"' in answer
    finally:
        service.close()


def test_two_failed_checks_still_ask_which_one(sandbox, tmp_path):
    sandbox.scenario("compile_error")
    service, gateway = _session(sandbox, tmp_path)
    try:
        gateway.turn("build it")
        gateway.turn("build it")
        admitted_before = _admissions(service)
        answer = gateway.turn("why did that fail?")
        assert _admissions(service) == admitted_before
        assert "FAILED (" not in answer, "an ambiguous referent was answered by recency"
    finally:
        service.close()


@pytest.mark.parametrize(("payload", "expected"), [
    ({"origin": {"kind": "user_rule", "rule_id": "build-and-test/v1"}, "skill": "run-build"}, "run-build"),
    ({"origin": {"kind": "user_rule", "rule_id": "run-tests/v1"}, "skill": "run-tests"}, "run-tests"),
    # The rule and the skill must agree.
    ({"origin": {"kind": "user_rule", "rule_id": "build-and-test/v1"}, "skill": "run-tests"}, None),
    # A model-proposed or user-direct task is never a configured check.
    ({"origin": {"kind": "model_proposal"}, "skill": "run-build"}, None),
    ({"origin": {"kind": "user_direct"}, "skill": "run-build"}, None),
    ({"origin": {"kind": "user_rule", "rule_id": "fix-build-failure/v1"}, "skill": "fix-build-failure"},
     None),
    ({"skill": "run-build"}, None),
])
def test_only_the_admission_record_makes_a_task_a_configured_check(payload, expected):
    assert admitted_configured_check(payload) == expected


def _decision(task_id: str) -> RouteDecision:
    return RouteDecision(
        RouteAction.WORK, source=RouteSource.RULE, skill="task-diagnostic",
        objective="why did that fail?", rule_id=RULE_TASK_DIAGNOSTIC, reference_ids=(task_id,),
    )


def _observation(task_id: str, *, configured_check: str | None) -> TaskObservation:
    return TaskObservation(
        task_id=task_id, turn_ref={}, status="completed", verdict="FAILED",
        answer="build_target: build (debug) FAILED (link) with 0 compiler error(s)",
        evidence_ids=("build_target:0",), configured_check=configured_check,
    )


def test_a_model_task_whose_answer_imitates_a_check_still_goes_to_the_worker():
    """Answer text can never select the deterministic path; only the admission record."""
    task_id = str(uuid4())
    forged = {task_id: _observation(task_id, configured_check=None)}
    assert ConversationGateway._recorded_check_diagnosis(_decision(task_id), forged) is None
    genuine = {task_id: _observation(task_id, configured_check="run-build")}
    assert ConversationGateway._recorded_check_diagnosis(_decision(task_id), genuine) is not None
