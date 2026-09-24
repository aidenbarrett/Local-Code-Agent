from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest

from local_agent.session.contracts import RouteSource, TaskOutcome, TaskResult
from local_agent.session.conversation_gateway import ConversationGateway
from local_agent.session.durable_routes import DurableRouteEvents
from local_agent.session.event_buffer import EventBuffer
from local_agent.session.intents import (
    RULE_REPO_NAVIGATION,
    RouteAction,
    decide_route,
)
from local_agent.session.session_event_service import DurableSessionService
from local_agent.session.session_store import SQLiteSessionStore


@pytest.mark.parametrize(
    "text",
    [
        "Inspect this repo",
        "Inspect this repository.",
        "Where is X defined?",
        "Where is Scheduler::run defined?",
    ],
)
def test_supported_read_only_requests_route_directly(text):
    decision = decide_route(text, active_repo_count=1)
    assert decision.action == RouteAction.WORK
    assert decision.source == RouteSource.RULE
    assert decision.rule_id == RULE_REPO_NAVIGATION
    assert decision.skill == "repo-navigation"
    assert decision.objective == text


@pytest.mark.parametrize("text", ["Inspect this repo", "Where is X defined?"])
def test_read_only_routes_require_exactly_one_active_repository(text):
    assert decide_route(text).reason_code == "active_repository_unknown"
    assert decide_route(text, active_repo_count=0).reason_code == "no_active_repository"
    assert decide_route(text, active_repo_count=2).reason_code == "ambiguous_active_repository"


@pytest.mark.parametrize(
    "text",
    [
        '"Inspect this repo"',
        "don't inspect this repo",
        "explain what inspect this repo means",
        "Where is X defined in the C++ standard?",
        "Where is the cache implementation defined?",
    ],
)
def test_read_only_rule_does_not_gain_authority_from_near_matches(text):
    decision = decide_route(text, active_repo_count=1)
    assert decision.action == RouteAction.MODEL_FALLBACK
    assert decision.source is None
    assert decision.rule_id is None


class NoConversationModel:
    def __init__(self):
        self.calls = 0

    def chat(self, messages, tools=None):
        self.calls += 1
        raise AssertionError("deterministic read-only routing must not call the conversation model")


class Runner:
    def __init__(self):
        self.calls = []

    def run(self, task, **kwargs):
        self.calls.append((task, kwargs))
        return TaskResult("task-result", TaskOutcome.FAIL, "observed read-only result", False)


def _service(tmp_path):
    return DurableSessionService(
        SQLiteSessionStore(tmp_path / "session.db"),
        stream_id=str(uuid4()),
        session_id=str(uuid4()),
    )


def _gateway(service, chat, runner):
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


@pytest.mark.parametrize("text", ["Inspect this repo", "Where is Scheduler defined?"])
def test_direct_read_only_request_needs_no_work_confirmation(tmp_path, text):
    service = _service(tmp_path)
    chat = NoConversationModel()
    runner = Runner()
    gateway = _gateway(service, chat, runner)
    try:
        answer = gateway.turn(text)
        assert "observed read-only result" in answer
        assert chat.calls == 0
        assert len(runner.calls) == 1
        task, kwargs = runner.calls[0]
        assert f"User request:\n{text}" in task
        assert f"rule_id={RULE_REPO_NAVIGATION}" in task
        assert "skill=repo-navigation" in task
        assert kwargs["route_source"] == RouteSource.RULE
        assert kwargs["rule_id"] == RULE_REPO_NAVIGATION
        assert kwargs["skill"] == "repo-navigation"
        assert service.replay() == []
    finally:
        service.close()
