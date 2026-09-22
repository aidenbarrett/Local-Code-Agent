from __future__ import annotations

import json
from types import SimpleNamespace
from uuid import uuid4

from local_agent.llm.protocol import ChatResponse
from local_agent.session.contracts import RouteSource, TaskOutcome, TaskResult
from local_agent.session.conversation_gateway import ConversationGateway
from local_agent.session.durable_routes import DurableRouteEvents
from local_agent.session.event_buffer import EventBuffer
from local_agent.session.intents import TaskIntent
from local_agent.session.session_event_service import DurableSessionService
from local_agent.session.session_store import SQLiteSessionStore


class Chat:
    def __init__(self, *responses):
        self.responses = iter(responses)
        self.calls = 0

    def chat(self, messages, tools=None):
        self.calls += 1
        return next(self.responses)


def _reply(kind: str, text: str) -> ChatResponse:
    return ChatResponse(content=json.dumps({"kind": kind, "text": text}))


class Runner:
    def __init__(self):
        self.calls = []

    def run(self, task, **kwargs):
        self.calls.append((task, kwargs))
        return TaskResult("task-result", TaskOutcome.FAIL, "observed failure", False)


def _service(tmp_path):
    return DurableSessionService(
        SQLiteSessionStore(tmp_path / "session.db"),
        stream_id=str(uuid4()),
        session_id=str(uuid4()),
    )


def _gateway(service, chat, runner):
    return ConversationGateway(
        chat,
        SimpleNamespace(repo=object(), run=lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError())),
        EventBuffer(service.stream_id),
        task_runner=runner,
        route_events=DurableRouteEvents(service),
    )


def test_model_repository_proposal_waits_for_explicit_work_acceptance(tmp_path):
    service = _service(tmp_path)
    runner = Runner()
    gateway = _gateway(
        service,
        Chat(_reply("repository", "I can inspect the cache implementation.")),
        runner,
    )
    try:
        answer = gateway.turn("Could you inspect the cache implementation?")
        assert runner.calls == []
        assert "reply `work` to accept" in answer
        assert [event["kind"] for event in service.replay()] == ["route.proposed"]
        proposal_turn = gateway.last_turn_ref
        assert proposal_turn is not None
        assert [turn.role for turn in gateway.session.turns] == ["user", "assistant"]

        rendered = gateway.turn("work")
        assert "observed failure" in rendered
        assert len(runner.calls) == 1
        task, kwargs = runner.calls[0]
        assert task == "User request:\nCould you inspect the cache implementation?"
        assert "I can inspect" not in task
        assert kwargs["turn_ref"] == proposal_turn
        assert kwargs["route_source"] == RouteSource.MODEL_PROPOSAL
        assert [event["kind"] for event in service.replay()] == [
            "route.proposed",
            "route.resolved",
        ]
        resolved = service.replay()[-1]["payload"]
        assert resolved["resolution"] == "accepted"
        assert resolved["source"] == "user"
        assert resolved["mode"] == "work"
    finally:
        service.close()


def test_chat_correction_resolves_proposal_without_running_task(tmp_path):
    service = _service(tmp_path)
    runner = Runner()
    gateway = _gateway(
        service,
        Chat(_reply("repository", "I could inspect the repository.")),
        runner,
    )
    try:
        gateway.turn("Can you look into this repository issue?")
        answer = gateway.turn("chat")
        assert "conversation-only" in answer
        assert runner.calls == []
        events = service.replay()
        assert [event["kind"] for event in events] == ["route.proposed", "route.resolved"]
        resolved = events[-1]["payload"]
        assert resolved["resolution"] == "corrected"
        assert resolved["mode"] == "conversation"
        assert resolved["source"] == "user"
        assert resolved["skill"] is None
    finally:
        service.close()


def test_pending_model_route_blocks_unrelated_turn_until_user_decides(tmp_path):
    service = _service(tmp_path)
    runner = Runner()
    chat = Chat(_reply("repository", "I could inspect it."))
    gateway = _gateway(service, chat, runner)
    try:
        gateway.turn("Please inspect the cache")
        answer = gateway.turn("What about the scheduler instead?")
        assert "awaiting your decision" in answer
        assert chat.calls == 1
        assert runner.calls == []
        assert len(DurableRouteEvents(service).pending_acceptance()) == 1
    finally:
        service.close()


def test_accepted_route_recovery_stops_once_original_turn_has_a_task(tmp_path):
    service = _service(tmp_path)
    routes = DurableRouteEvents(service)
    try:
        intent = TaskIntent(
            turn_ref={
                "conversation_id": "conversation-1",
                "turn_index": 0,
                "turn_sha256": "1" * 64,
            },
            objective="inspect cache",
            proposed_reference_ids=(),
            origin=RouteSource.MODEL_PROPOSAL,
        )
        proposal = routes.propose(intent, skill="repo-navigation")
        routes.resolve(proposal, source="user")

        assert len(routes.accepted_unadmitted()) == 1
        service.store.task_ids_for_turn = lambda _ref: ["existing-task"]  # type: ignore[method-assign]
        assert routes.accepted_unadmitted() == ()
    finally:
        service.close()


def test_public_session_wires_durable_route_events_into_gateway():
    from pathlib import Path

    source = Path("internal/scripts/session-hub.py").read_text(encoding="utf-8")
    assert "from local_agent.session.durable_routes import DurableRouteEvents" in source
    assert "route_events=DurableRouteEvents(service)" in source
