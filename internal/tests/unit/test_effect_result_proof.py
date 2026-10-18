"""Checkout effects are VERIFIED only with their authoritative receipt owner."""
from __future__ import annotations

from uuid import uuid4

import pytest

from local_agent.session.candidate_facts import project_candidate
from local_agent.session.contracts import TaskOutcome, TaskResult
from local_agent.session.task_controller import _action_result_with_owner


@pytest.mark.parametrize(
    "action", ["diff-candidate", "apply-candidate", "undo-candidate", "commit-candidate"],
)
def test_successful_owned_action_without_its_owner_fails_closed(action):
    result = TaskResult(
        str(uuid4()),
        TaskOutcome.PASS,
        "effect completed",
        True,
        verification_ran=True,
        reason_code="verification_passed",
    )

    checked = _action_result_with_owner(action, result)

    assert checked.outcome is TaskOutcome.NO_VERDICT
    assert checked.verified_at_completion is False
    assert checked.reason_code == "protocol_mismatch"
    assert "authoritative candidate receipt is missing or invalid" in checked.answer


@pytest.mark.parametrize(
    ("action", "metric", "facts"),
    [
        ("diff-candidate", "candidate_diff", {
            "paths": ["src/example.cpp"],
            "patch_sha256": "d" * 64,
        }),
        ("apply-candidate", "candidate_import", {
            "applied": True,
            "import_verified": True,
            "paths": ["src/example.cpp"],
            "patch_sha256": "a" * 64,
        }),
        ("undo-candidate", "candidate_undo", {
            "paths": ["src/example.cpp"],
        }),
        ("commit-candidate", "candidate_commit", {
            "paths": ["src/example.cpp"],
            "commit": "b" * 40,
        }),
    ],
)
def test_successful_effect_keeps_verified_only_with_matching_owner(action, metric, facts):
    facts = {"candidate_task_id": str(uuid4()), **facts}
    result = TaskResult(
        str(uuid4()),
        TaskOutcome.PASS,
        "effect completed",
        True,
        metrics={metric: facts},
        verification_ran=True,
        reason_code="verification_passed",
    )

    assert _action_result_with_owner(action, result) is result
    projected = project_candidate(result.task_id, True, result.metrics)
    assert projected is not None
    if action == "diff-candidate":
        assert projected == {
            "role": "reviewed",
            "candidate_task_id": facts["candidate_task_id"],
            "retained": False,
            "paths": ["src/example.cpp"],
            "patch_sha256": "d" * 64,
            "base_commit": None,
            "commit": None,
        }


@pytest.mark.parametrize(
    ("action", "metric", "facts"),
    [
        ("diff-candidate", "candidate_diff", {
            "candidate_task_id": None,
            "paths": ["src/example.cpp"],
            "patch_sha256": "d" * 64,
        }),
        ("apply-candidate", "candidate_import", {
            "candidate_task_id": None,
            "applied": True,
            "import_verified": True,
            "paths": ["src/example.cpp"],
            "patch_sha256": "a" * 64,
        }),
        ("undo-candidate", "candidate_undo", {
            "candidate_task_id": None,
            "paths": ["src/example.cpp"],
        }),
        ("commit-candidate", "candidate_commit", {
            "candidate_task_id": None,
            "paths": ["src/example.cpp"],
            "commit": "b" * 40,
        }),
    ],
)
def test_successful_effect_with_malformed_owner_fails_closed(action, metric, facts):
    result = TaskResult(
        str(uuid4()),
        TaskOutcome.PASS,
        "effect completed",
        True,
        metrics={metric: facts},
        verification_ran=True,
        reason_code="verification_passed",
    )

    checked = _action_result_with_owner(action, result)

    assert checked.outcome is TaskOutcome.NO_VERDICT
    assert checked.reason_code == "protocol_mismatch"


@pytest.mark.parametrize("facts", [
    {"paths": ["src/example.cpp"], "patch_sha256": None},
    {"paths": [], "patch_sha256": "d" * 64},
])
def test_successful_diff_with_incomplete_prepared_owner_fails_closed(facts):
    result = TaskResult(
        str(uuid4()), TaskOutcome.PASS, "diff shown", True,
        metrics={"candidate_diff": {"candidate_task_id": str(uuid4()), **facts}},
        verification_ran=True, reason_code="verification_passed",
    )

    checked = _action_result_with_owner("diff-candidate", result)

    assert checked.outcome is TaskOutcome.NO_VERDICT
    assert checked.reason_code == "protocol_mismatch"


def test_successful_diff_with_a_checkout_effect_receipt_fails_closed():
    result = TaskResult(
        str(uuid4()), TaskOutcome.PASS, "diff shown", True,
        metrics={"candidate_import": {
            "candidate_task_id": str(uuid4()), "applied": True, "import_verified": True,
            "paths": ["src/example.cpp"], "patch_sha256": "a" * 64,
        }},
        verification_ran=True, reason_code="verification_passed",
    )

    checked = _action_result_with_owner("diff-candidate", result)

    assert checked.outcome is TaskOutcome.NO_VERDICT
    assert checked.reason_code == "protocol_mismatch"
