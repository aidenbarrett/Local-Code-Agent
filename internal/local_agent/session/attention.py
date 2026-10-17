"""Evidence-backed Attention projection for the Session Hub.

Only durable task facts can create an intervention. Worker prose is never action
authority, and a candidate is actionable only when retained completion proof agrees
with the durable VERIFIED verdict.
"""
from __future__ import annotations

from collections.abc import Sequence

from .task_read_model import TaskSnapshot


def render_attention(tasks: Sequence[TaskSnapshot]) -> str:
    """Up to three durable facts that need the user, newest first; empty when none."""
    ordered = sorted(tasks, key=lambda item: item.last_sequence)
    # A candidate's state is its latest task: once it has been applied, refused,
    # undone, committed or discarded, the task that prepared it (or an earlier
    # refusal) no longer describes it.
    latest: dict[str, int] = {}
    for task in ordered:
        if task.candidate is not None:
            latest[task.candidate.candidate_task_id] = task.last_sequence
    # Likewise a configured check's state is its latest finished run in that
    # repository: a failure that a later run of the same check has replaced no
    # longer describes the checkout.
    latest_check: dict[tuple[str, str], int] = {}
    for task in ordered:
        if task.configured_check is not None and task.terminal:
            latest_check[(task.repository_id, task.configured_check)] = task.last_sequence
    items: list[str] = []
    for task in reversed(ordered):
        candidate = task.candidate
        if candidate is not None and latest[candidate.candidate_task_id] != task.last_sequence:
            continue
        check = (None if task.configured_check is None or not task.terminal
                 else (task.repository_id, task.configured_check))
        if check is not None and latest_check[check] != task.last_sequence:
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


__all__ = ["render_attention"]
