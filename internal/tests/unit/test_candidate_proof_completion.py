"""Current proof, rather than another model turn, can finish a narrow repair."""
from pathlib import Path

import pytest

from local_agent.agent.state import AgentState, HaltCause, ToolCallRecord
from local_agent.session.candidate_change import candidate_proof_satisfied
from local_agent.session.task_controller import _repair_completion


def _state(proofs, *, epoch=1):
    return AgentState(
        task="fix the failure", repo_root=Path("."), mutation_epoch=epoch, verified=True,
        history=[ToolCallRecord("check", {}, "allow", True, "observed", epoch=e, proof=p)
                 for e, p in proofs],
    )


@pytest.mark.parametrize("skill,proofs,expected", [
    ("fix-build-failure", [(1, "full_build_pass")], True),
    ("fix-build-failure", [(1, "full_test_pass")], True),
    ("fix-test-failure", [(1, "full_test_pass")], True),
    ("fix-test-failure", [(1, "full_build_pass")], False),
    ("fix-build-failure", [(0, "full_build_pass")], False),
    ("fix-build-failure", [(1, "targeted_build_pass")], False),
    ("fix-test-failure", [(1, "targeted_test_pass")], False),
    ("fix-test-failure", [(1, "full_test_pass"), (1, "observed_test_fail"),
                          (1, "full_build_pass")], False),
    ("implement-change", [(1, "full_test_pass"), (1, "observed_test_fail"),
                          (1, "full_build_pass")], False),
    ("fix-test-failure", [(1, "full_test_pass"), (1, "observed_test_fail"),
                          (1, "full_test_pass")], True),
    ("fix-build-failure", [(1, "full_build_pass"), (1, "observed_test_fail"),
                           (1, "full_build_pass")], True),
    ("fix-build-failure", [(1, "full_build_pass"), (1, "observed_build_fail")], False),
])
def test_only_qualifying_proof_after_the_last_current_contradiction_satisfies_a_candidate(
    skill, proofs, expected,
):
    assert candidate_proof_satisfied(skill, _state(proofs)) is expected


@pytest.mark.parametrize("field,value", [
    ("verified", False), ("mutation_epoch", 0), ("halt_cause", HaltCause.STOPPED),
    ("claim", "failure"), ("claim", "diagnosis"), ("claim", "needs_action"),
])
def test_repair_completion_refuses_unfinished_or_contradicted_state(field, value):
    state = _state([(1, "full_build_pass")])
    setattr(state, field, value)
    assert _repair_completion("fix-build-failure", state) is None


def test_cleanup_uncertainty_is_not_repaired_by_a_later_passing_check():
    state = _state([(0, "no_current_proof"), (1, "full_build_pass")])
    state.history[0].reason = "cleanup_unknown"
    assert _repair_completion("fix-build-failure", state) is None


def test_a_general_change_still_requires_the_worker_to_finish_its_request():
    state = _state([(1, "full_test_pass")])
    assert candidate_proof_satisfied("implement-change", state)
    assert _repair_completion("implement-change", state) is None


def test_completion_does_not_invent_a_model_claim_or_citations():
    state = _state([(1, "full_build_pass")])
    assert _repair_completion("fix-build-failure", state)
    assert state.claim is None
    assert not state.cited_evidence
    assert state.cited_correctly is None
