"""Actionable Session Hub attention derived only from durable task facts.

Attention is presentation, not authority. It cannot grant capabilities, infer outcomes
from worker prose, or turn an unresolved condition into success.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

from .task_read_model import TaskSnapshot


@dataclass(frozen=True)
class AttentionItem:
    task_id: str
    kind: str
    summary: str
    action: str | None = None


def project_attention(tasks: Sequence[TaskSnapshot]) -> tuple[AttentionItem, ...]:
    """Return actionable durable conditions, newest first."""
    items: list[AttentionItem] = []
    for task in reversed(tuple(tasks)):
        candidate = task.candidate
        if candidate is not None and candidate.role == "prepared" and candidate.retained:
            items.append(
                AttentionItem(
                    task.task_id,
                    "candidate_ready",
                    "Verified candidate is ready but has not been applied.",
                    f"/diff {candidate.candidate_task_id} · /apply {candidate.candidate_task_id}",
                )
            )
            continue
        if task.faults:
            reason, message = task.faults[-1]
            items.append(AttentionItem(task.task_id, "fault", f"{reason}: {message}"))
            continue
        if task.verdict == "FAILED":
            items.append(
                AttentionItem(
                    task.task_id,
                    "failed",
                    f"Verification failed ({task.verdict_reason or 'reason unavailable'}).",
                    f"fix task {task.task_id}",
                )
            )
        elif task.verdict == "NO_VERDICT":
            items.append(
                AttentionItem(
                    task.task_id,
                    "unknown",
                    f"Final outcome is unknown ({task.verdict_reason or 'reason unavailable'}).",
                )
            )
        elif task.verdict == "REFUSED":
            items.append(
                AttentionItem(
                    task.task_id,
                    "refused",
                    f"Request was refused ({task.verdict_reason or 'reason unavailable'}).",
                )
            )
    return tuple(items)


def render_attention(tasks: Sequence[TaskSnapshot]) -> str:
    items = project_attention(tasks)
    if not items:
        return "Nothing needs attention."
    lines: list[str] = []
    for item in items:
        lines.append(f"{item.kind.upper()} · {item.task_id}")
        lines.append(f"  {item.summary}")
        if item.action is not None:
            lines.append(f"  Next: {item.action}")
    return "\n".join(lines)


__all__ = ["AttentionItem", "project_attention", "render_attention"]
