from __future__ import annotations

from uuid import uuid4

import pytest

from local_agent.session.contracts import RouteSource
from local_agent.session.durable_routes import DurableRouteError, DurableRouteEvents
from local_agent.session.intents import TaskIntent
from local_agent.session.session_event_service import DurableSessionService
from local_agent.session.session_store import SQLiteSessionStore


def _service(tmp_path):
    return DurableSessionService(
        SQLiteSessionStore(tmp_path / "session.db"),
        stream_id=str(uuid4()),
        session_id=str(uuid4()),
    )


def _turn_ref(index: int = 0):
    return {
        "conversation_id": "conv-routes",
        "turn_index": index,
        "turn_sha256": f"{index + 1:064x}",
    }


def test_rule_work_route_persists_proposal_then_controller_acceptance(tmp_path):
    service = _service(tmp_path)
    try:
        routes = DurableRouteEvents(service)
        intent = TaskIntent(
            turn_ref=_turn_ref(),
            objective="Build it",
            proposed_reference_ids=(),
            origin=RouteSource.RULE,
            rule_id="build-and-test/v1",
        )

        proposal = routes.propose(intent, skill="build-and-test")
        resolved = routes.resolve(proposal, source="controller")

        events = service.replay()
        assert [event["kind"] for event in events] == ["route.proposed", "route.resolved"]
        proposed = events[0]["payload"]
        accepted = events[1]["payload"]
        assert proposed["route_id"] == accepted["route_id"] == proposal.route_id
        assert proposed["source"] == "rule"
        assert proposed["rule_id"] == "build-and-test/v1"
        assert proposed["skill"] == "build-and-test"
        assert proposed["requires_acceptance"] is False
        assert "Build it" not in proposed["explanation"]
        assert accepted == {
            "route_id": proposal.route_id,
            "revision": 0,
            "resolution": "accepted",
            "mode": "work",
            "source": "controller",
            "skill": "build-and-test",
        }
        assert resolved.revision == 0
    finally:
        service.close()


def test_model_proposed_work_cannot_be_controller_accepted(tmp_path):
    service = _service(tmp_path)
    try:
        routes = DurableRouteEvents(service)
        intent = TaskIntent(
            turn_ref=_turn_ref(),
            objective="inspect the repository",
            proposed_reference_ids=(),
            origin=RouteSource.MODEL_PROPOSAL,
        )
        proposal = routes.propose(intent, skill="repo-navigation")

        with pytest.raises(DurableRouteError, match="explicit user acceptance"):
            routes.resolve(proposal, source="controller")

        assert [event["kind"] for event in service.replay()] == ["route.proposed"]
        accepted = routes.resolve(proposal, source="user")
        assert accepted.requires_acceptance is False
        events = service.replay()
        assert events[-1]["payload"]["source"] == "user"
        assert events[-1]["payload"]["resolution"] == "accepted"
    finally:
        service.close()


def test_user_correction_increments_revision_and_clears_work_skill(tmp_path):
    service = _service(tmp_path)
    try:
        routes = DurableRouteEvents(service)
        intent = TaskIntent(
            turn_ref=_turn_ref(1),
            objective="do repository work",
            proposed_reference_ids=(),
            origin=RouteSource.MODEL_PROPOSAL,
        )
        proposal = routes.propose(intent, skill="repo-navigation")
        corrected = routes.resolve(
            proposal,
            resolution="corrected",
            source="user",
            mode="conversation",
        )

        assert corrected.revision == 1
        assert corrected.skill is None
        payload = service.replay()[-1]["payload"]
        assert payload["revision"] == 1
        assert payload["mode"] == "conversation"
        assert payload["skill"] is None
    finally:
        service.close()


def test_route_id_is_stable_for_same_stream_turn_and_typed_intent(tmp_path):
    service = _service(tmp_path)
    try:
        routes = DurableRouteEvents(service)
        intent = TaskIntent(
            turn_ref=_turn_ref(),
            objective="What changed on my branch?",
            proposed_reference_ids=(),
            origin=RouteSource.RULE,
            rule_id="git-review/v1",
        )
        first = routes.propose(intent, skill="git-review")
        second = routes.propose(intent, skill="git-review")
        assert first.route_id == second.route_id
    finally:
        service.close()


def test_correction_and_rejection_cannot_be_attributed_to_controller(tmp_path):
    service = _service(tmp_path)
    try:
        routes = DurableRouteEvents(service)
        intent = TaskIntent(
            turn_ref=_turn_ref(),
            objective="inspect",
            proposed_reference_ids=(),
            origin=RouteSource.USER_DIRECT,
        )
        proposal = routes.propose(intent)
        with pytest.raises(DurableRouteError, match="must come from the user"):
            routes.resolve(proposal, resolution="rejected", source="controller")
        assert [event["kind"] for event in service.replay()] == ["route.proposed"]
    finally:
        service.close()


def test_pending_acceptance_recovers_only_unresolved_model_routes_across_pages(tmp_path):
    service = _service(tmp_path)
    try:
        routes = DurableRouteEvents(service)
        first = routes.propose(
            TaskIntent(
                turn_ref=_turn_ref(0),
                objective="inspect cache",
                proposed_reference_ids=(),
                origin=RouteSource.MODEL_PROPOSAL,
            ),
            skill="repo-navigation",
        )
        second = routes.propose(
            TaskIntent(
                turn_ref=_turn_ref(1),
                objective="inspect scheduler",
                proposed_reference_ids=(),
                origin=RouteSource.MODEL_PROPOSAL,
            ),
            skill="repo-navigation",
        )
        routes.resolve(first, resolution="rejected", source="user", mode="clarify")

        pending = routes.pending_acceptance(replay_page=1)
        assert pending == (second,)
        assert pending[0].turn_ref == _turn_ref(1)
        assert pending[0].skill == "repo-navigation"
        assert pending[0].requires_acceptance is True
    finally:
        service.close()


def test_pending_acceptance_ignores_non_user_gated_and_deduplicates_exact_proposal(tmp_path):
    service = _service(tmp_path)
    try:
        routes = DurableRouteEvents(service)
        rule = TaskIntent(
            turn_ref=_turn_ref(0),
            objective="Build it",
            proposed_reference_ids=(),
            origin=RouteSource.RULE,
            rule_id="build-and-test/v1",
        )
        routes.propose(rule, skill="build-and-test")

        model = TaskIntent(
            turn_ref=_turn_ref(1),
            objective="inspect cache",
            proposed_reference_ids=(),
            origin=RouteSource.MODEL_PROPOSAL,
        )
        first = routes.propose(model, skill="repo-navigation")
        repeated = routes.propose(model, skill="repo-navigation")
        assert first == repeated

        assert routes.pending_acceptance() == (first,)
    finally:
        service.close()


def test_pending_acceptance_fails_closed_when_replay_bound_is_exceeded(tmp_path):
    service = _service(tmp_path)
    try:
        routes = DurableRouteEvents(service)
        for index in range(2):
            routes.propose(
                TaskIntent(
                    turn_ref=_turn_ref(index),
                    objective=f"inspect target {index}",
                    proposed_reference_ids=(),
                    origin=RouteSource.MODEL_PROPOSAL,
                )
            )

        with pytest.raises(DurableRouteError, match="bounded event history"):
            routes.pending_acceptance(max_events=1)
    finally:
        service.close()
