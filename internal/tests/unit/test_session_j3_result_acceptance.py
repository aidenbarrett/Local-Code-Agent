from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

from local_agent.session.contracts import RouteSource, TaskOutcome, TaskResult
from local_agent.session.conversation_gateway import ConversationGateway
from local_agent.session.durable_routes import DurableRouteEvents
from local_agent.session.event_buffer import EventBuffer
from local_agent.session.intents import RULE_REPO_NAVIGATION
from local_agent.session.session_event_service import DurableSessionService
from local_agent.session.session_store import SQLiteSessionStore


class NoConversationModel:
    def __init__(self) -> None:
        self.calls = 0

    def chat(self, messages, tools=None):
        self.calls += 1
        raise AssertionError("J3 deterministic read-only routing must not call the conversation model")


class ResultRunner:
    def __init__(self, result: TaskResult) -> None:
        self.result = result
        self.calls = []

    def run(self, task, **kwargs):
        self.calls.append((task, kwargs))
        return self.result


def _service(tmp_path):
    return DurableSessionService(
        SQLiteSessionStore(tmp_path / "session.db"),
        stream_id=str(uuid4()),
        session_id=str(uuid4()),
    )


def _gateway(service, runner, chat):
    return ConversationGateway(
        chat,
        SimpleNamespace(
            repo=object(),
            run=lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError()),
        ),
        EventBuffer(service.stream_id),
        task_runner=runner,
        route_events=DurableRouteEvents(service),
    )


def test_j3_public_route_preserves_source_scope_and_missing_coverage(tmp_path):
    result = TaskResult(
        "task-j3",
        TaskOutcome.NO_VERDICT,
        "Sources: src/scheduler.cpp:42-67\n"
        "Scope: src/scheduler.cpp lines 35-75\n"
        "Missing coverage: callers were not searched.",
        False,
        evidence_ids=("tool:0", "tool:1"),
        reason_code="missing_coverage",
    )
    service = _service(tmp_path)
    chat = NoConversationModel()
    runner = ResultRunner(result)
    gateway = _gateway(service, runner, chat)
    try:
        rendered = gateway.turn("Where is Scheduler defined?")

        assert chat.calls == 0
        assert len(runner.calls) == 1
        task, kwargs = runner.calls[0]
        assert "User request:\nWhere is Scheduler defined?" in task
        assert f"rule_id={RULE_REPO_NAVIGATION}" in task
        assert "skill=repo-navigation" in task
        assert kwargs["route_source"] == RouteSource.RULE
        assert kwargs["rule_id"] == RULE_REPO_NAVIGATION
        assert kwargs["skill"] == "repo-navigation"

        assert "Sources: src/scheduler.cpp:42-67" in rendered
        assert "Scope: src/scheduler.cpp lines 35-75" in rendered
        assert "Missing coverage: callers were not searched." in rendered
        assert "Controller: no_verdict" in rendered
        assert "reason: missing_coverage" in rendered
        assert "Evidence IDs: tool:0, tool:1" in rendered
    finally:
        service.close()


def test_j3_public_route_keeps_invalid_result_non_success_shaped(tmp_path):
    result = TaskResult(
        "task-j3-invalid",
        TaskOutcome.NO_VERDICT,
        "Repository result could not be accepted because the result payload was invalid.",
        False,
        reason_code="invalid_result",
    )
    service = _service(tmp_path)
    chat = NoConversationModel()
    runner = ResultRunner(result)
    gateway = _gateway(service, runner, chat)
    try:
        rendered = gateway.turn("Inspect this repo")

        assert chat.calls == 0
        assert len(runner.calls) == 1
        assert "Controller: no_verdict" in rendered
        assert "reason: invalid_result" in rendered
        assert "Controller: pass" not in rendered
        assert "Controller: escalated_pass" not in rendered
    finally:
        service.close()
