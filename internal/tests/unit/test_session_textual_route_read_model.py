from __future__ import annotations

from uuid import uuid4

import pytest

from local_agent.session.contracts import RouteSource
from local_agent.session.durable_routes import DurableRouteEvents
from local_agent.session.intents import TaskIntent
from local_agent.session.session_event_service import DurableSessionService
from local_agent.session.session_store import SQLiteSessionStore
from local_agent.session.textual_route_read_model import (
    RouteReadModelError,
    latest_route_summary,
    project_routes,
)


def _service(tmp_path) -> DurableSessionService:
    return DurableSessionService(
        SQLiteSessionStore(tmp_path / "session.db"),
        stream_id=str(uuid4()),
        session_id=str(uuid4()),
    )


def _turn_ref(index: int = 0) -> dict[str, object]:
    return {
        "conversation_id": "conversation-route-ui",
        "turn_index": index,
        "turn_sha256": f"{index + 1:064x}",
    }


def _intent(source: RouteSource, *, index: int = 0) -> TaskIntent:
    return TaskIntent(
        turn_ref=_turn_ref(index),
        objective="inspect repository",
        proposed_reference_ids=(),
        origin=source,
        rule_id="git-review/v1" if source == RouteSource.RULE else None,
    )


def test_model_proposal_projects_pending_route_then_user_acceptance(tmp_path):
    service = _service(tmp_path)
    try:
        routes = DurableRouteEvents(service)
        proposal = routes.propose(_intent(RouteSource.MODEL_PROPOSAL), skill="repo-navigation")

        pending = project_routes(service.replay())[-1]
        assert pending.route_id == proposal.route_id
        assert pending.pending is True
        assert pending.summary() == "model proposal · work · repo-navigation · awaiting acceptance"

        routes.resolve(proposal, source="user")
        resolved = project_routes(service.replay())[-1]
        assert resolved.pending is False
        assert resolved.resolution == "accepted"
        assert resolved.resolution_source == "user"
        assert latest_route_summary(service.replay()) == "model proposal · work · repo-navigation · accepted"
    finally:
        service.close()


def test_rule_route_summary_preserves_controller_owned_skill_and_acceptance(tmp_path):
    service = _service(tmp_path)
    try:
        routes = DurableRouteEvents(service)
        proposal = routes.propose(_intent(RouteSource.RULE), skill="git-review")
        routes.resolve(proposal, source="controller")

        snapshot = project_routes(service.replay())[-1]
        assert snapshot.source == "rule"
        assert snapshot.rule_id == "git-review/v1"
        assert snapshot.skill == "git-review"
        assert snapshot.summary() == "rule · work · git-review · accepted"
    finally:
        service.close()


def test_exact_duplicate_stable_proposal_is_idempotent_for_presentation(tmp_path):
    service = _service(tmp_path)
    try:
        routes = DurableRouteEvents(service)
        intent = _intent(RouteSource.USER_DIRECT)
        first = routes.propose(intent)
        second = routes.propose(intent)
        assert first.route_id == second.route_id

        projected = project_routes(service.replay())
        assert len(projected) == 1
        assert projected[0].summary() == "user · work"
    finally:
        service.close()


def test_projection_rejects_resolution_without_proposal():
    event = {
        "sequence": 1,
        "kind": "route.resolved",
        "payload": {
            "route_id": str(uuid4()),
            "revision": 0,
            "resolution": "accepted",
            "mode": "work",
            "source": "user",
            "skill": None,
        },
    }
    with pytest.raises(RouteReadModelError, match="no durable proposal"):
        project_routes([event])


def test_projection_rejects_out_of_order_durable_sequence():
    events = [
        {"sequence": 2, "kind": "session.opened", "payload": {}},
        {"sequence": 1, "kind": "session.opened", "payload": {}},
    ]
    with pytest.raises(RouteReadModelError, match="strictly increasing"):
        project_routes(events)
