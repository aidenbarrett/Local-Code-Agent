"""Checkout effects are VERIFIED only with their authoritative receipt owner."""
from __future__ import annotations

from uuid import uuid4

import pytest

from local_agent.session.contracts import TaskOutcome, TaskResult
from local_agent.session.task_controller import _effect_result_with_owner


@pytest.mark.parametrize("action", ["apply-candidate", "undo-candidate", "commit-candidate"])
def test_successful_effect_without_its_owner_fails_closed(action):
    result = TaskResult(
        str(uuid4()),
        TaskOutcome.PASS,
        "effect completed",
        True,
        verification_ran=True,
        reason_code="verification_passed",
    )

    checked = _effect_result_with_owner(action, result)

    assert checked.outcome is TaskOutcome.NO_VERDICT
    assert checked.verified_at_completion is False
    assert checked.reason_code == "protocol_mismatch"
    assert "authoritative effect receipt is missing or invalid" in checked.answer


@pytest.mark.parametrize(
    ("action", "metric", "facts"),
    [
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

    assert _effect_result_with_owner(action, result) is result


@pytest.mark.parametrize(
    ("action", "metric", "facts"),
    [
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

    checked = _effect_result_with_owner(action, result)

    assert checked.outcome is TaskOutcome.NO_VERDICT
    assert checked.reason_code == "protocol_mismatch"
