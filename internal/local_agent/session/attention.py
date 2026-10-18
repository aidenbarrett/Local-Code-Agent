"""Evidence-backed Attention projection for the Session Hub.

Only durable task facts can create an intervention. Worker prose is never action
authority, and a candidate is actionable only when retained completion proof agrees
with the durable VERIFIED verdict.
"""
from __future__ import annotations

from collections.abc import Sequence

from .task_read_model import TaskSnapshot


def _executed_check(task: TaskSnapshot) -> bool:
    """A finished configured-check run whose durable result says the check ran."""
    return (task.configured_check is not None and task.terminal
            and task.verdict in {"VERIFIED", "FAILED"}
            and task.result_verification_ran is True)


def superseded_check(tasks: Sequence[TaskSnapshot], task: TaskSnapshot) -> bool:
    """Whether a later run of the same configured check in the same repository, one
    that actually executed the check, has replaced ``task`` as that check's state."""
    if not _executed_check(task):
        return False
    return any(
        _executed_check(other) and other.last_sequence > task.last_sequence
        and other.repository_id == task.repository_id
        and other.configured_check == task.configured_check
        for other in tasks
    )


def render_attention(tasks: Sequence[TaskSnapshot]) -> str:
    """Up to three durable facts that need the user, newest first; empty when none."""
    ordered = sorted(tasks, key=lambda item: item.last_sequence)
    # A candidate's lifecycle state is its latest state-changing task.  Review is
    # read-only evidence about the prepared candidate, so it must not hide the
    # still-unapplied prepared task (and its completion proof) from Attention.
    # Apply, refusal, undo, commit and discard do supersede earlier lifecycle state.
    latest: dict[str, int] = {}
    for task in ordered:
        if task.candidate is not None and task.candidate.role != "reviewed":
            latest[task.candidate.candidate_task_id] = task.last_sequence
    # Likewise a configured check's state is its latest run in that repository that
    # actually executed the check: a failure that such a run has replaced no longer
    # describes the checkout. A refused, cancelled or unrecorded run executed nothing
    # and replaces nothing.
    latest_check: dict[tuple[str, str], int] = {}
    for task in ordered:
        if _executed_check(task):
            latest_check[(task.repository_id, str(task.configured_check))] = task.last_sequence
    items: list[str] = []
    for task in reversed(ordered):
        candidate = task.candidate
        if (candidate is not None
                and latest.get(candidate.candidate_task_id) != task.last_sequence):
            continue
        check = (task.repository_id, str(task.configured_check))
        if _executed_check(task) and latest_check[check] != task.last_sequence:
            continue
        if (
            candidate is not None
            and candidate.role == "prepared"
            and candidate.retained
            and task.verdict == "VERIFIED"
            and task.result_verified_at_completion is True
        ):
            candidate_id = candidate.candidate_task_id
            items.append(
                f"Verified candidate ready · {candidate_id}\n"
                f"Review: /diff {candidate_id} · Apply: /apply {candidate_id}"
            )
            continue
        if task.faults:
            reason, message = task.faults[-1]
            items.append(f"Fault · {task.task_id}\n{reason}: {message}")
            continue
        if task.terminal and task.verdict in {"FAILED", "NO_VERDICT"}:
            reason = task.verdict_reason or "reason unavailable"
            items.append(f"{task.verdict} · {task.task_id}\n{reason}")
    return "\n\n".join(items[:3])


__all__ = ["render_attention", "superseded_check"]
