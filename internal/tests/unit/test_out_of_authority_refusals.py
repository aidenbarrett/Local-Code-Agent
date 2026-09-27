"""Push, bare commit and delete imperatives are refused deterministically.

Found on the Panther Lake run (J06): "push this branch to origin" reached the
conversation model, which answered "I cannot interact with your local repository"
and proposed repository work. The product never pushes, and only commits an applied
candidate, so it says so itself, names the supported path, and calls no model.
"""
from __future__ import annotations

from uuid import uuid4

import pytest

from local_agent.session.conversation_gateway import ConversationGateway
from local_agent.session.durable_routes import DurableRouteEvents
from local_agent.session.event_buffer import EventBuffer
from local_agent.session.intents import RouteAction, decide_route
from local_agent.session.session_event_service import DurableSessionService
from local_agent.session.session_store import SQLiteSessionStore

TASK = "12345678-1234-1234-1234-123456789abc"


@pytest.mark.parametrize(("text", "reason"), [
    ("push this branch to origin", "push_not_supported"),
    ("git push --force", "push_not_supported"),
    ("Please push.", "push_not_supported"),
    ("commit that", "commit_needs_candidate"),
    ("git commit -am wip", "commit_needs_candidate"),
    ("delete the src directory and commit that", "change_needs_prefix"),
    ("remove the unused include in foo.cpp", "change_needs_prefix"),
    ("rm -rf build", "change_needs_prefix"),
])
def test_out_of_authority_imperatives_are_refused(text, reason):
    decision = decide_route(text, active_repo_count=1)
    assert (decision.action, decision.reason_code) == (RouteAction.REFUSE, reason)


@pytest.mark.parametrize("text", [
    f"/commit {TASK}", f"commit {TASK}",
    "change: remove the unused include in foo.cpp",
    "what does git push do?", "pushing to origin: is that safe?", "should I commit this?",
])
def test_supported_commands_and_questions_keep_their_route(text):
    assert decide_route(text, active_repo_count=1).action != RouteAction.REFUSE


class NoModel:
    def chat(self, messages, tools=None, max_tokens=None):
        raise AssertionError("a refusal must not reach the conversation model")


class NoController:
    pass


@pytest.mark.parametrize(("text", "phrase"), [
    ("push this branch to origin", "never pushes"),
    ("delete the src directory and commit that", "change:"),
    ("commit that", "/commit <task-id>"),
])
def test_the_gateway_refuses_without_a_model_or_a_proposal(tmp_path, text, phrase):
    service = DurableSessionService(SQLiteSessionStore(tmp_path / "s.db"),
                                    stream_id=str(uuid4()), session_id=str(uuid4()))
    try:
        gateway = ConversationGateway(NoModel(), NoController(), EventBuffer(service.stream_id),
                                      route_events=DurableRouteEvents(service))
        answer = gateway.turn(text)
        assert phrase in answer and answer.endswith("No task was run.")
        assert "reply `work`" not in answer
        assert [e["kind"] for e in service.replay()] == []
    finally:
        service.close()
