"""Derive terminal cleanup facts from durable execution activity.

Terminal state is not allowed to imply cleanup. Cleanup is evidence about the exact
execution epoch: process-spawning tools must have a matching durable finish and must not
have timed out before `not_needed` is truthful. Open tool activity is also surfaced so a
controller cannot return a success-shaped result while its durable lifecycle is still
open.
"""
from __future__ import annotations

from dataclasses import dataclass
import json
from typing import Any

from .durable_activity import OK_EXECUTION_REASON_CODES, TOOL_FAILURE_SCHEMAS


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
    process_calls: dict[str, dict[str, Any] | None] = {}

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

    # A call the controller recorded as having started no process leaves nothing to
    # clean up, whatever else went wrong with it (a refused argument, a policy block).
    # Anything else, including a failure record without that fact, stays unknown.
    process_calls = {
        call_id: finish for call_id, finish in process_calls.items()
        if finish is None or not _started_no_process(service, finish)
    }
    process_open = any(finish is None for finish in process_calls.values())
    if process_open:
        cleanup = "unknown"
    elif not process_calls:
        cleanup = "not_needed"
    elif all(finish is not None and _ended_normally(finish) for finish in process_calls.values()):
        cleanup = "not_needed"
    elif all(
        finish is not None and (_ended_normally(finish) or _stop_confirmed(service, finish))
        for finish in process_calls.values()
    ):
        # Every spawned tree either ended on its own or was stopped and confirmed gone
        # by the runner's typed answer (subreaper or Job settlement), not its prose.
        cleanup = "confirmed"
    elif any(
        finish is not None and finish.get("reason_code") in {"tool_timeout", "cancelled"}
        for finish in process_calls.values()
    ):
        # The command runner ended the tree on timeout or Stop without a durable typed
        # confirmation that it is gone, so terminal truth cannot claim it.
        cleanup = "attempted"
    else:
        cleanup = "unknown"

    return TerminalActivityTruth(
        cleanup=cleanup,
        open_call_ids=tuple(sorted(open_calls)),
    )


def _ended_normally(finish: dict[str, Any]) -> bool:
    return (finish.get("execution") == "ok"
            and finish.get("reason_code") in OK_EXECUTION_REASON_CODES)


def _failure_record(service: Any, finish: dict[str, Any]) -> dict[str, Any] | None:
    """The retained typed failure record of a call that did not execute cleanly."""
    ref = finish.get("result_ref")
    if finish.get("execution") == "ok" or not isinstance(ref, dict):
        return None
    record = json.loads(service.store.artifact_bytes(ref))
    if not isinstance(record, dict) or record.get("schema") not in TOOL_FAILURE_SCHEMAS:
        return None
    return record


def _started_no_process(service: Any, finish: dict[str, Any]) -> bool:
    """True only when the retained failure record says the call spawned nothing."""
    record = _failure_record(service, finish)
    return record is not None and record.get("process_started") is False


def _stop_confirmed(service: Any, finish: dict[str, Any]) -> bool:
    """True only for a Stop whose retained record carries the runner's typed confirmation."""
    if finish.get("reason_code") != "cancelled":
        return False
    record = _failure_record(service, finish)
    return record is not None and record.get("process_cleanup_confirmed") is True
