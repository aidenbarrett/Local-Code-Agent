"""Evidence-backed Attention projection for the Session Hub.

Only durable task facts can create an intervention. Worker prose is never action
authority, and a candidate is actionable only when retained completion proof agrees
with the durable VERIFIED verdict.
"""
from __future__ import annotations

from typing import Sequence

from .task_read_model import TaskSnapshot


def render_attention(tasks: Sequence[TaskSnapshot]) -> str:
    items: list[str] = []
    for task in reversed(tuple(tasks)):
        candidate = task.candidate
        if (
            candidate is not None
            and candidate.role == "prepared"
            and candidate.retained
            and task.verdict == "VERIFIED"
            and task.result_verified_at_completion is True
        ):
            items.append(
                f"Verified candidate ready · {task.task_id}\n"
                f"Review: /diff {task.task_id} · Apply: /apply {task.task_id}"
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
