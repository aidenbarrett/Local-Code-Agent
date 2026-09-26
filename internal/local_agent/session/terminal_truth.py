"""Derive terminal cleanup facts from durable execution activity.

Terminal state is not allowed to imply cleanup. Cleanup is evidence about the exact
execution epoch: process-spawning tools must have a matching durable finish and must not
have timed out before `not_needed` is truthful. Open tool activity is also surfaced so a
controller cannot return a success-shaped result while its durable lifecycle is still
open.
"""
from __future__ import annotations

from dataclasses import dataclass


class DurableWriteFailed(RuntimeError):
    reason_code = "durable_write_failed"


class CancelUnreconciled(RuntimeError):
    reason_code = "cancel_unreconciled"


@dataclass(frozen=True)
class TerminalActivityTruth:
    cleanup: str
    open_call_ids: tuple[str, ...]


def derive_terminal_activity_truth(
    service,
    *,
    task_id: str,
    execution_epoch: int,
    process_spawning_tools: frozenset[str] | set[str],
) -> TerminalActivityTruth:
    """Project cleanup only from committed tool lifecycle events in this epoch."""
    record = service.store.task_record(task_id)
    if record is None:
        raise ValueError("terminal truth requires an admitted durable task")
    cursor = int(record["admitted_sequence"]) - 1
    open_calls: dict[str, str] = {}
    process_calls: dict[str, dict | None] = {}

    while True:
        batch = service.replay(after=cursor, limit=1000)
        if not batch:
            break
        for event in batch:
            cursor = int(event["sequence"])
            if event.get("task_id") != task_id:
                continue
            payload = event.get("payload") or {}
            if int(payload.get("execution_epoch", -1)) != execution_epoch:
                continue
            kind = event.get("kind")
            if kind == "tool.started":
                call_id = str(payload["call_id"])
                tool_name = str(payload["tool_name"])
                open_calls[call_id] = tool_name
                if tool_name in process_spawning_tools:
                    process_calls[call_id] = None
            elif kind == "tool.finished":
                call_id = str(payload["call_id"])
                open_calls.pop(call_id, None)
                if call_id in process_calls:
                    process_calls[call_id] = dict(payload)
        if len(batch) < 1000:
            break

    process_open = any(finish is None for finish in process_calls.values())
    if process_open:
        cleanup = "unknown"
    elif not process_calls:
        cleanup = "not_needed"
    elif all(
        finish is not None
        and finish.get("execution") == "ok"
        and finish.get("reason_code") == "completed"
        for finish in process_calls.values()
    ):
        cleanup = "not_needed"
    elif any(
        finish is not None and finish.get("reason_code") in {"tool_timeout", "cancelled"}
        for finish in process_calls.values()
    ):
        # The command runner ended the tree on timeout or Stop. Windows Job Object
        # accounting can confirm that locally, but the confirmation is not yet carried
        # in the durable tool.finished contract, so terminal truth cannot claim it.
        cleanup = "attempted"
    else:
        cleanup = "unknown"

    return TerminalActivityTruth(
        cleanup=cleanup,
        open_call_ids=tuple(sorted(open_calls)),
    )
