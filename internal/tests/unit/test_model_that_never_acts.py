"""A model that only ever talks, and never calls a tool, ends the task truthfully.

A weak or misconfigured model can answer every turn with prose and no tool call.
The task must then finish with a renderable verdict, not crash the Hub while the
controller projects its result.
"""
from __future__ import annotations

from uuid import uuid4

import pytest

from local_agent.agent.orchestrator import RunResult
from local_agent.agent.outcome import Outcome
from local_agent.agent.state import AgentState, HaltCause
from local_agent.config import load_repo_config
from local_agent.llm.protocol import ChatResponse
from local_agent.session.cancellable_task_executor import CancellableDurableTaskExecutor
from local_agent.session.contracts import TaskOutcome, TaskResult
from local_agent.session.conversation_gateway import ConversationGateway
from local_agent.session.durable_routes import DurableRouteEvents
from local_agent.session.durable_task_controller import AdmittedDurableTaskController
from local_agent.session.event_buffer import EventBuffer
from local_agent.session.results import verdict_block_from_task_result
from local_agent.session.session_event_service import DurableSessionService
from local_agent.session.session_store import SQLiteSessionStore
from local_agent.session.task_admission import DurableTaskAdmissionRunner
from local_agent.session.task_controller import TaskController
from local_agent.session.task_history import DurableTaskHistory
from local_agent.session.workspaces import GitWorkspaceManager


class NoModel:
    def chat(self, messages, tools=None, max_tokens=None):
        raise AssertionError("the conversation model is not needed here")


class OnlyTalks:
    """Long prose and never a tool call, until the context budget runs out."""

    def __init__(self) -> None:
        self.calls = 0

    def chat(self, messages, tools=None, max_tokens=None):
        self.calls += 1
        return ChatResponse(content="lorem ipsum dolor sit amet " * 800)


def _session(sandbox, tmp_path):
    service = DurableSessionService(SQLiteSessionStore(tmp_path / "session.db"),
                                    stream_id=str(uuid4()), session_id=str(uuid4()))
    events = EventBuffer(service.stream_id)
    controller = TaskController(
        load_repo_config(sandbox.root), OnlyTalks, events, allow_execution=True,
        context_budget_tokens=7500,
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


@pytest.mark.parametrize(("scenario", "check"), [("compile_error", "build it"),
                                                  ("test_failure", "run the tests")])
def test_a_fix_by_a_model_that_never_acts_finishes_with_a_verdict(sandbox, tmp_path, scenario, check):
    sandbox.scenario(scenario)
    service, gateway = _session(sandbox, tmp_path)
    try:
        gateway.turn(check)
        observed = gateway.last_result
        gateway.turn("fix it")
        fixed = gateway.last_result
        assert fixed.task_id != observed.task_id
        assert not fixed.outcome.succeeded and fixed.verified_at_completion is False
    finally:
        service.close()


def test_a_fix_that_ran_out_of_context_is_blocked_and_says_so(sandbox, tmp_path):
    sandbox.scenario("compile_error")
    service, gateway = _session(sandbox, tmp_path)
    try:
        gateway.turn("build it")
        gateway.turn("fix it")
        fixed = gateway.last_result
        assert (fixed.outcome, fixed.reason_code) == (TaskOutcome.BLOCKED, "unavailable_capability")
        assert "context budget exhausted" in fixed.answer
    finally:
        service.close()


@pytest.mark.parametrize("cause", [None, *HaltCause])
@pytest.mark.parametrize("worker_outcome", list(Outcome))
@pytest.mark.parametrize(("attempted", "verified"), [(False, False), (True, False), (True, True)])
def test_every_worker_ending_projects_to_a_renderable_verdict(tmp_path, cause, worker_outcome,
                                                              attempted, verified):
    # The controller's outcome and reason code are projected independently; any
    # pair the verdict contract rejects crashes the Hub at durable completion.
    state = AgentState(task="t", repo_root=tmp_path)
    state.halt_cause, state.verification_attempted, state.verified = cause, attempted, verified
    run = RunResult(answer="", state=state, outcome=worker_outcome)
    outcome = TaskController._product_outcome(run)
    ok = bool(outcome.succeeded and state.verified)
    if outcome.succeeded and not ok:
        return  # the controller never builds this pair: success requires proof
    result = TaskResult(str(uuid4()), outcome, "answer", ok, (), {},
                        verification_ran=attempted,
                        reason_code=TaskController._reason_code(run, outcome))
    verdict_block_from_task_result(result)
