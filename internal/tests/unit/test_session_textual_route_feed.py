from __future__ import annotations

from uuid import uuid4

from local_agent.session.contracts import RouteSource
from local_agent.session.durable_routes import DurableRouteEvents
from local_agent.session.intents import TaskIntent
from local_agent.session.session_event_service import DurableSessionService
from local_agent.session.session_store import SQLiteSessionStore
from local_agent.session.textual_feed import DurableHubFeed


def _service(tmp_path) -> DurableSessionService:
    return DurableSessionService(
        SQLiteSessionStore(tmp_path / "session.db"),
        stream_id=str(uuid4()),
        session_id=str(uuid4()),
    )


def _turn_ref() -> dict[str, object]:
    return {
        "conversation_id": "conversation-route-feed",
        "turn_index": 0,
        "turn_sha256": "1" * 64,
    }


def test_feed_projects_pending_and_resolved_route_from_durable_events(tmp_path):
    service = _service(tmp_path)
    try:
        routes = DurableRouteEvents(service)
        proposal = routes.propose(
            TaskIntent(
                turn_ref=_turn_ref(),
                objective="inspect repository",
                proposed_reference_ids=(),
                origin=RouteSource.MODEL_PROPOSAL,
            ),
            skill="repo-navigation",
        )
        feed = DurableHubFeed(service)
        state = feed.start()
        assert state.route_summary == "model proposal · work · repo-navigation · awaiting acceptance"

        routes.resolve(proposal, source="user")
        assert feed.poll() is True
        assert feed.state().route_summary == "model proposal · work · repo-navigation · accepted"
    finally:
        service.close()


def test_explicit_route_summary_provider_remains_an_override(tmp_path):
    service = _service(tmp_path)
    try:
        routes = DurableRouteEvents(service)
        routes.propose(
            TaskIntent(
                turn_ref=_turn_ref(),
                objective="build",
                proposed_reference_ids=(),
                origin=RouteSource.USER_DIRECT,
            )
        )
        feed = DurableHubFeed(service, route_summary_provider=lambda: "fixture override")
        assert feed.start().route_summary == "fixture override"
    finally:
        service.close()
