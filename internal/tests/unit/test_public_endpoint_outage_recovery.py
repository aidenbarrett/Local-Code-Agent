"""A mid-task model outage is durable, truthful, and does not poison the next turn."""
from __future__ import annotations

from uuid import uuid4

from local_agent.config import load_repo_config
from local_agent.llm.client import tool_call
from local_agent.llm.protocol import ChatResponse, LLMTransportError
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


class NoConversationModel:
    def chat(self, messages, tools=None):
        raise AssertionError("deterministic repository routing reached the conversation model")


class RecoveringWorkerClient:
    """Drop the second call of the first task, then serve the next task normally."""

    def __init__(self) -> None:
        self.calls = 0

    def chat(self, messages, tools=None):
        self.calls += 1
        if self.calls in (1, 3):
            return ChatResponse(tool_calls=[tool_call("git_status", {}, f"status-{self.calls}")])
        if self.calls == 2:
            raise LLMTransportError(
                "APIConnectionError: model server dropped mid-task",
                cause="APIConnectionError",
            )
        if self.calls == 4:
            return ChatResponse(tool_calls=[tool_call(
                "submit_answer",
                {
                    "claim": "success",
                    "summary": "Repository status inspected after the model recovered.",
                    "evidence_ids": ["git_status:0"],
                },
                "answer-4",
            )])
        raise AssertionError(f"unexpected worker call {self.calls}")


def test_mid_task_endpoint_outage_is_retained_and_next_turn_recovers(sandbox, tmp_path):
    service = DurableSessionService(
        SQLiteSessionStore(tmp_path / "session.db"),
        stream_id=str(uuid4()), session_id=str(uuid4()),
    )
    events = EventBuffer(service.stream_id)
    client = RecoveringWorkerClient()
    controller = TaskController(
        load_repo_config(sandbox.root), lambda: client, events, allow_execution=False,
    )
    executor = CancellableDurableTaskExecutor(
        service, AdmittedDurableTaskController(service, controller),
    )
    history = DurableTaskHistory(service.store, stream_id=service.stream_id)
    gateway = ConversationGateway(
        NoConversationModel(), controller, events,
        task_runner=DurableTaskAdmissionRunner(executor),
        task_history=history,
        route_events=DurableRouteEvents(service),
    )
    try:
        first_answer = gateway.turn("Inspect this repo")
        failed = gateway.last_result
        assert failed is not None
        assert failed.outcome is TaskOutcome.NO_VERDICT
        assert failed.reason_code == "endpoint_unavailable"
        assert failed.verified_at_completion is False
        assert "model server dropped mid-task" in first_answer
        assert "inference server unavailable" in first_answer
        assert "Repository status inspected after the model recovered" not in first_answer

        retained = history.result_for_task(failed.task_id)
        assert retained is not None
        assert retained.status == "unknown"
        assert retained.verdict == "NO_VERDICT"
        assert retained.verified_at_completion is False
        assert retained.answer == failed.answer
        record = service.store.task_record(failed.task_id)
        assert record is not None and record["terminal"] is True
        first_events = [event for event in service.replay() if event["task_id"] == failed.task_id]
        assert [event["kind"] for event in first_events][-2:] == ["task.verdict", "task.closed"]
        assert first_events[-1]["payload"]["status"] == "unknown"

        second_answer = gateway.turn("Inspect this repo")
        recovered = gateway.last_result
        assert recovered is not None
        # Read-only inspection is useful but is not build/test proof, so the product
        # remains NO_VERDICT / missing_evidence. Recovery here means the model call,
        # tool call, typed answer and durable terminal all complete normally.
        assert recovered.outcome is TaskOutcome.NO_VERDICT
        assert recovered.reason_code == "missing_evidence"
        assert recovered.verified_at_completion is False
        assert "Repository status inspected after the model recovered" in second_answer
        assert "unavailable" not in second_answer.lower()
        assert client.calls == 4
        recovered_record = service.store.task_record(recovered.task_id)
        assert recovered_record is not None and recovered_record["terminal"] is True
    finally:
        service.close()
