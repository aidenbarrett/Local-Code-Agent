from __future__ import annotations

from dataclasses import fields

import pytest

from local_agent.session.contracts import RouteSource
from local_agent.session.intents import (
    CorrectionStatus,
    ExplicitMode,
    PendingRouteRef,
    RouteAction,
    RULE_BUILD_AND_TEST,
    RULE_GIT_REVIEW,
    RULE_MUTATION_UNAVAILABLE,
    RULE_SELF_CHECK,
    RULE_TASK_DIAGNOSTIC,
    TaskIntent,
    correct_pending_route,
    decide_route,
    task_intent_from_decision,
)


def _turn_ref() -> dict[str, object]:
    return {
        "conversation_id": "conversation-1",
        "turn_index": 3,
        "turn_sha256": "a" * 64,
    }


def test_control_commands_win_before_explicit_mode():
    decision = decide_route("/quit", explicit_mode=ExplicitMode.WORK)
    assert decision.action == RouteAction.CONTROL
    assert decision.control_id == "quit"


def test_explicit_work_bypasses_classification_and_preserves_original_text():
    text = "  Inspect src/Foo.cpp exactly as written  "
    decision = decide_route(text, explicit_mode="work")
    assert decision.action == RouteAction.WORK
    assert decision.source == RouteSource.USER_DIRECT
    assert decision.objective == text
    assert decision.rule_id is None


def test_explicit_chat_bypasses_work_rules():
    decision = decide_route("Build it", explicit_mode="chat")
    assert decision.action == RouteAction.CHAT
    assert decision.objective == "Build it"


@pytest.mark.parametrize(
    "text,rule_id,skill,kwargs",
    [
        (
            "What changed on my branch?",
            RULE_GIT_REVIEW,
            "git-review",
            {"active_repo_count": 1},
        ),
        ("build it", RULE_BUILD_AND_TEST, "build-and-test", {"active_repo_count": 1}),
        ("/check", RULE_SELF_CHECK, "self-check", {}),
    ],
)
def test_anchored_named_rules_route_without_model(text, rule_id, skill, kwargs):
    decision = decide_route(text, **kwargs)
    assert decision.action == RouteAction.WORK
    assert decision.source == RouteSource.RULE
    assert decision.rule_id == rule_id
    assert decision.skill == skill


@pytest.mark.parametrize(
    "text",
    [
        '"build it"',
        "don't build it",
        "do not build it",
        "explain what build it means",
        "the docs say build it",
        "if I say build it, what happens?",
        "what changed on my branch if I run the tool?",
        "why did that fail in your example?",
        "please fix it later",
    ],
)
def test_keywords_inside_other_language_do_not_gain_rule_authority(text):
    decision = decide_route(text, active_repo_count=1, eligible_task_ids=("task-1",))
    assert decision.action == RouteAction.MODEL_FALLBACK
    assert decision.source is None
    assert decision.rule_id is None


@pytest.mark.parametrize("text", ["Build it", "What changed on my branch?"])
def test_repository_rules_require_explicit_exactly_one_active_repository(text):
    unknown = decide_route(text)
    assert unknown.action == RouteAction.CLARIFY
    assert unknown.reason_code == "active_repository_unknown"
    assert decide_route(text, active_repo_count=0).reason_code == "no_active_repository"
    assert decide_route(text, active_repo_count=2).reason_code == "ambiguous_active_repository"
    assert decide_route(text, active_repo_count=1).action == RouteAction.WORK


def test_diagnostic_rule_requires_exactly_one_eligible_task_reference():
    none = decide_route("Why did that fail?", eligible_task_ids=())
    assert none.action == RouteAction.CLARIFY
    assert none.reason_code == "no_eligible_task_reference"

    many = decide_route("Why did that fail?", eligible_task_ids=("task-1", "task-2"))
    assert many.action == RouteAction.CLARIFY
    assert many.reason_code == "ambiguous_task_reference"

    one = decide_route("Why did that fail?", eligible_task_ids=("task-1",))
    assert one.action == RouteAction.WORK
    assert one.source == RouteSource.RULE
    assert one.rule_id == RULE_TASK_DIAGNOSTIC
    assert one.skill == "task-diagnostic"
    assert one.reference_ids == ("task-1",)


def test_duplicate_referent_input_fails_closed_instead_of_hiding_store_bug():
    with pytest.raises(ValueError, match="unique"):
        decide_route("Why did that fail?", eligible_task_ids=("task-1", "task-1"))


def test_fix_it_is_a_deterministic_refusal_until_mutation_exists():
    decision = decide_route("Fix it")
    assert decision.action == RouteAction.REFUSE
    assert decision.rule_id == RULE_MUTATION_UNAVAILABLE
    assert decision.reason_code == "mutation_workflow_unavailable"
    assert decision.source is None


def test_unmatched_text_stops_at_model_fallback_boundary():
    decision = decide_route("Could you look into the weird cache issue?")
    assert decision.action == RouteAction.MODEL_FALLBACK
    assert decision.objective == "Could you look into the weird cache issue?"
    assert decision.source is None


def test_empty_input_requires_clarification_not_model_guessing():
    decision = decide_route("   ")
    assert decision.action == RouteAction.CLARIFY
    assert decision.reason_code == "empty_input"


def test_work_decision_becomes_narrow_untrusted_task_intent():
    decision = decide_route("Why did that fail?", eligible_task_ids=("task-7",))
    intent = task_intent_from_decision(decision, _turn_ref())
    assert intent == TaskIntent(
        turn_ref=_turn_ref(),
        objective="Why did that fail?",
        proposed_reference_ids=("task-7",),
        origin=RouteSource.RULE,
        rule_id=RULE_TASK_DIAGNOSTIC,
    )


def test_task_intent_normalises_caller_owned_reference_inputs():
    turn_ref = _turn_ref()
    references = ["task-7"]
    intent = TaskIntent(turn_ref, "inspect", references, "user_direct")
    turn_ref["turn_sha256"] = "b" * 64
    references.append("task-8")
    assert intent.turn_ref["turn_sha256"] == "a" * 64
    assert intent.proposed_reference_ids == ("task-7",)
    assert intent.origin == RouteSource.USER_DIRECT


def test_task_intent_contract_has_no_execution_authority_fields():
    assert {field.name for field in fields(TaskIntent)} == {
        "turn_ref",
        "objective",
        "proposed_reference_ids",
        "origin",
        "rule_id",
    }
    forbidden = {
        "root",
        "argv",
        "allowlist",
        "approval",
        "verification_policy",
        "experimental_condition",
        "success",
    }
    assert forbidden.isdisjoint({field.name for field in fields(TaskIntent)})


def test_task_intent_validates_turn_ref_and_rule_identity():
    bad_ref = _turn_ref() | {"turn_sha256": "A" * 64}
    with pytest.raises(ValueError, match="lowercase SHA-256"):
        TaskIntent(bad_ref, "build", (), RouteSource.USER_DIRECT)
    with pytest.raises(ValueError, match="rule identity"):
        TaskIntent(_turn_ref(), "build", (), RouteSource.RULE)


def test_only_work_decision_can_become_task_intent():
    with pytest.raises(ValueError, match="only a work route"):
        task_intent_from_decision(decide_route("hello"), _turn_ref())


def test_one_word_route_correction_targets_one_explicit_pending_route():
    pending = (PendingRouteRef("route-1", 4),)
    correction = correct_pending_route(" work ", pending)
    assert correction.status == CorrectionStatus.APPLIED
    assert correction.route_id == "route-1"
    assert correction.revision == 5
    assert correction.mode == ExplicitMode.WORK


def test_route_correction_never_guesses_across_multiple_pending_routes():
    correction = correct_pending_route(
        "chat",
        (PendingRouteRef("route-1", 0), PendingRouteRef("route-2", 2)),
    )
    assert correction.status == CorrectionStatus.CLARIFY
    assert correction.reason_code == "ambiguous_pending_route"
    assert correction.route_id is None


def test_route_correction_requires_a_pending_route():
    correction = correct_pending_route("work", ())
    assert correction.status == CorrectionStatus.CLARIFY
    assert correction.reason_code == "no_pending_route"


def test_other_text_is_not_a_route_correction():
    correction = correct_pending_route("please work on this", (PendingRouteRef("route-1", 0),))
    assert correction.status == CorrectionStatus.NOT_A_CORRECTION


def test_invalid_route_inputs_fail_closed():
    with pytest.raises(ValueError, match="nonnegative"):
        decide_route("build it", active_repo_count=-1)
    with pytest.raises(ValueError):
        decide_route("x" * 8001)
    with pytest.raises(ValueError, match="nonnegative"):
        PendingRouteRef("route-1", -1)
    with pytest.raises(ValueError, match="PendingRouteRef"):
        correct_pending_route("work", ["route-1"])
