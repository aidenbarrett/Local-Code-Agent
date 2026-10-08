"""Each commit outcome is its own type; the facts a result records follow from the type."""
from __future__ import annotations

import dataclasses

import pytest

from local_agent.session.candidate_change import _commit_facts
from local_agent.session.workspaces import (
    CommitDrifted,
    CommitRefused,
    CommitResult,
    Committed,
)

TASK = "0f1e2d3c-4b5a-4968-8776-655443322110"
SHA = "a" * 40


@pytest.mark.parametrize(("done", "commit", "branch", "drifted"), [
    (Committed(SHA, "main", ("src/a.cpp",)), SHA, "main", []),
    (CommitDrifted("main", ("src/a.cpp", "src/b.cpp"), ("src/b.cpp",)), None, "main", ["src/b.cpp"]),
    (CommitRefused("HEAD is detached", ("src/a.cpp",)), None, None, []),
    (CommitRefused("already committed", ("src/a.cpp",), existing_commit=SHA), SHA, None, []),
])
def test_commit_facts_follow_the_outcome_type(
    done: CommitResult, commit: str | None, branch: str | None, drifted: list[str],
) -> None:
    facts = _commit_facts(TASK, done)
    assert facts == {
        "candidate_task_id": TASK,
        "commit": commit,
        "branch": branch,
        "paths": list(done.paths),
        "drifted": drifted,
    }


def test_outcomes_are_immutable_values() -> None:
    done = Committed(SHA, "main", ("src/a.cpp",))
    with pytest.raises(dataclasses.FrozenInstanceError):
        done.commit = "b" * 40  # type: ignore[misc]


def test_only_a_commit_carries_a_commit_id_field() -> None:
    # A refusal cannot be read as a commit: the attribute does not exist on it.
    assert not hasattr(CommitRefused("x"), "commit")
    assert not hasattr(CommitDrifted("main", (), ()), "commit")
