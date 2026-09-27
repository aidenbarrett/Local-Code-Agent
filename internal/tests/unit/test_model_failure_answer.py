"""A task that could not use the model says why, in the transport's own words.

``endpoint_unavailable`` alone cannot tell a user whether the server was down,
refused the connection, or the client could not even be built, and on the target
machine that is the difference between a five-second fix and an evening lost.
Build, git, the durable store and the candidate workspace are real; only the model
client is a stub that fails the way a transport does.
"""
from __future__ import annotations

from uuid import uuid4

from local_agent.config import load_repo_config
from local_agent.llm.protocol import LLMTransportError
from local_agent.session.cancellable_task_executor import CancellableDurableTaskExecutor
from local_agent.session.contracts import TaskOutcome
from local_agent.session.conversation_gateway import ConversationGateway
from local_agent.session.durable_routes import DurableRouteEvents
from local_agent.session.durable_task_controller import AdmittedDurableTaskController
from local_agent.session.event_buffer import EventBuffer
from local_agent.session.session_event_service import DurableSessionService
from local_agent.session.session_store import SQLiteSessionStore
from local_agent.session.task_admission import DurableTaskAdmissionRunner
from local_agent.session.task_controller import TaskController
from local_agent.session.task_history import DurableTaskHistory
from local_agent.session.workspaces import GitWorkspaceManager

CAUSE = "ModuleNotFoundError: No module named 'jiter'"


class NoModel:
    def chat(self, messages, tools=None, max_tokens=None):
        raise AssertionError("the conversation model is not needed here")


class BrokenTransport:
    def __init__(self, kind: str) -> None:
        self.kind = kind

    def chat(self, messages, tools=None, max_tokens=None):
        raise LLMTransportError(CAUSE, cause="ModuleNotFoundError", kind=self.kind)


def _session(sandbox, tmp_path, kind: str):
    service = DurableSessionService(SQLiteSessionStore(tmp_path / "session.db"),
                                    stream_id=str(uuid4()), session_id=str(uuid4()))
    events = EventBuffer(service.stream_id)
    controller = TaskController(
        load_repo_config(sandbox.root), lambda: BrokenTransport(kind), events, allow_execution=True,
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


def test_a_fix_that_could_not_reach_the_model_names_the_transport_error(sandbox, tmp_path):
    sandbox.scenario("compile_error")
    service, gateway = _session(sandbox, tmp_path, "unavailable")
    try:
        gateway.turn("build it")
        assert gateway.last_result.reason_code == "verification_failed"
        gateway.turn("fix it")
        fixed = gateway.last_result
        assert (fixed.outcome, fixed.reason_code) == (TaskOutcome.NO_VERDICT, "endpoint_unavailable")
        assert fixed.verified_at_completion is False
        assert f"inference server unavailable: {CAUSE}" in fixed.answer
        # The candidate summary is still there; the cause is added, not substituted.
        assert "nothing to apply" in fixed.answer
    finally:
        service.close()


def test_a_stalled_model_says_it_stalled(sandbox, tmp_path):
    sandbox.scenario("compile_error")
    service, gateway = _session(sandbox, tmp_path, "stalled")
    try:
        gateway.turn("build it")
        gateway.turn("fix it")
        fixed = gateway.last_result
        assert fixed.reason_code == "inference_timeout"
        assert f"inference stalled: {CAUSE}" in fixed.answer
    finally:
        service.close()


def test_a_check_that_never_used_the_model_carries_no_transport_note(sandbox, tmp_path):
    # Configured checks never touch the model; nothing about a model is added.
    sandbox.scenario("compile_error")
    service, gateway = _session(sandbox, tmp_path, "unavailable")
    try:
        gateway.turn("build it")
        assert "inference" not in gateway.last_result.answer
    finally:
        service.close()
