"""One policy for when a configured-check run replaces an older run."""
from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass


@dataclass(frozen=True)
class ExecutedCheckRun:
    task_id: str
    repository_id: str
    configured_check: str
    sequence: int


def check_run_is_superseded(
    runs: Iterable[ExecutedCheckRun],
    target: ExecutedCheckRun,
) -> bool:
    """Whether a later executed run owns this repository/check's current state."""
    return any(
        other.sequence > target.sequence
        and other.repository_id == target.repository_id
        and other.configured_check == target.configured_check
        for other in runs
    )


__all__ = ["ExecutedCheckRun", "check_run_is_superseded"]
