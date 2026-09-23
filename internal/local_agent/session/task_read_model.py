"""Deterministic read model for durable Session Hub task events.

The UI must be able to reconstruct task state after restart without consulting model
prose or process-local callbacks. This projector consumes only reviewed v1 durable event
facts. Impossible orderings fail closed instead of being massaged into a plausible card.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable
from uuid import UUID


class TaskReadModelError(RuntimeError):
    pass


@dataclass(frozen=True)
class ToolActivity:
    call_id: str
    tool_name: str
    execution: str | None = None
    domain: str | None = None
    reason_code: str | None = None
    exit_code: int | None = None
    duration_ms: int | None = None
    evidence_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class EndpointActivity:
    endpoint_id: str
    role: str
    state: str
    queue_position: int | None
    wait_ms: int
    lease_id: str | None


@dataclass(frozen=True)
class TaskActivity:
    sequence: int
    occurred_utc: str
    kind: str
    summary: str


@dataclass
class TaskSnapshot:
    task_id: str
    admitted_sequence: int
    last_sequence: int
    state: str
    execution_epoch: int
    origin_kind: str
    repository_id: str
    skill: str | None
    deadline_utc: str
    cancel_requested: bool = False
    verdict: str | None = None
    verdict_reason: str | None = None
    verdict_scope: str | None = None
    verdict_lines: tuple[str, ...] = ()
    evidence_ids: tuple[str, ...] = ()
    cleanup: str | None = None
    closed_sequence: int | None = None
    result_ref: dict | None = None
    result_answer: str | None = None
    result_verification_ran: bool | None = None
    result_verified_at_completion: bool | None = None
    open_tools: dict[str, ToolActivity] = field(default_factory=dict)
    last_tool: ToolActivity | None = None
    endpoint: EndpointActivity | None = None
    faults: list[tuple[str, str]] = field(default_factory=list)
    activity: list[TaskActivity] = field(default_factory=list)

    @property
    def terminal(self) -> bool:
        return self.closed_sequence is not None

    def frozen_open_tools(self) -> tuple[ToolActivity, ...]:
        return tuple(self.open_tools[key] for key in sorted(self.open_tools))


_TASK_KINDS = frozenset(
    {
        "task.admitted",
        "task.state_changed",
        "endpoint.state_changed",
        "tool.started",
        "tool.finished",
        "task.cancel_requested",
        "task.verdict",
        "task.closed",
        "fault.reported",
    }
)


def _require_task_id(task_id: str) -> None:
    try:
        UUID(task_id)
    except (TypeError, ValueError) as exc:
        raise ValueError("task_id must be a UUID") from exc


def _activity(sequence: int, occurred_utc: str, kind: str, summary: str) -> TaskActivity:
    return TaskActivity(sequence=sequence, occurred_utc=occurred_utc, kind=kind, summary=summary)


def _project_relevant(snapshot: TaskSnapshot, event: dict) -> None:
    sequence = int(event["sequence"])
    if sequence <= snapshot.last_sequence:
        raise TaskReadModelError("task events must have strictly increasing durable sequence")
    snapshot.last_sequence = sequence
    kind = str(event["kind"])
    payload = event["payload"]
    occurred_utc = str(event["occurred_utc"])

    if snapshot.terminal and kind != "fault.reported":
        raise TaskReadModelError("task lifecycle event appeared after durable task.closed")

    if kind == "task.state_changed":
        previous = str(payload["previous"])
        current = str(payload["current"])
        epoch = int(payload["execution_epoch"])
        if previous != snapshot.state:
            raise TaskReadModelError(
                f"state transition expected previous={snapshot.state!r}, got {previous!r}"
            )
        if epoch < snapshot.execution_epoch:
            raise TaskReadModelError("task state transition used a stale execution epoch")
        snapshot.execution_epoch = epoch
        snapshot.state = current
        snapshot.activity.append(
            _activity(sequence, occurred_utc, kind, f"{previous} -> {current}")
        )
        return

    if kind == "endpoint.state_changed":
        snapshot.endpoint = EndpointActivity(
            endpoint_id=str(payload["endpoint_id"]),
            role=str(payload["role"]),
            state=str(payload["state"]),
            queue_position=payload["queue_position"],
            wait_ms=int(payload["wait_ms"]),
            lease_id=None if payload["lease_id"] is None else str(payload["lease_id"]),
        )
        snapshot.activity.append(
            _activity(
                sequence,
                occurred_utc,
                kind,
                f"{snapshot.endpoint.role} endpoint {snapshot.endpoint.state}",
            )
        )
        return

    if kind == "tool.started":
        epoch = int(payload["execution_epoch"])
        if epoch != snapshot.execution_epoch:
            raise TaskReadModelError("tool start used a non-current execution epoch")
        call_id = str(payload["call_id"])
        if call_id in snapshot.open_tools:
            raise TaskReadModelError("duplicate durable tool.started call id")
        tool = ToolActivity(call_id=call_id, tool_name=str(payload["tool_name"]))
        snapshot.open_tools[call_id] = tool
        snapshot.activity.append(
            _activity(sequence, occurred_utc, kind, f"{tool.tool_name} started")
        )
        return

    if kind == "tool.finished":
        epoch = int(payload["execution_epoch"])
        if epoch != snapshot.execution_epoch:
            raise TaskReadModelError("tool finish used a non-current execution epoch")
        call_id = str(payload["call_id"])
        opened = snapshot.open_tools.get(call_id)
        if opened is None:
            raise TaskReadModelError("durable tool.finished has no matching open tool")
        tool_name = str(payload["tool_name"])
        if opened.tool_name != tool_name:
            raise TaskReadModelError("durable tool.finished changed the tool name")
        finished = ToolActivity(
            call_id=call_id,
            tool_name=tool_name,
            execution=str(payload["execution"]),
            domain=str(payload["domain"]),
            reason_code=str(payload["reason_code"]),
            exit_code=payload["exit_code"],
            duration_ms=int(payload["duration_ms"]),
            evidence_ids=tuple(str(value) for value in payload["evidence_ids"]),
        )
        del snapshot.open_tools[call_id]
        snapshot.last_tool = finished
        snapshot.activity.append(
            _activity(
                sequence,
                occurred_utc,
                kind,
                f"{tool_name} {finished.execution}/{finished.domain}",
            )
        )
        return

    if kind == "task.cancel_requested":
        epoch = int(payload["execution_epoch"])
        if epoch < snapshot.execution_epoch:
            raise TaskReadModelError("cancel request used a stale execution epoch")
        snapshot.cancel_requested = True
        snapshot.activity.append(
            _activity(sequence, occurred_utc, kind, f"cancel requested: {payload['reason_code']}")
        )
        return

    if kind == "task.verdict":
        completion = payload["completion"]
        if str(completion["task_id"]) != snapshot.task_id:
            raise TaskReadModelError("task.verdict completion names a different task")
        block = completion["verdict_block"]
        snapshot.verdict = str(block["verdict"])
        snapshot.verdict_reason = str(block["reason_code"])
        snapshot.verdict_scope = str(block["scope"])
        snapshot.verdict_lines = tuple(str(value) for value in block["rendered_lines"])
        snapshot.evidence_ids = tuple(str(value) for value in block["evidence_ids"])
        snapshot.activity.append(
            _activity(sequence, occurred_utc, kind, f"verdict {snapshot.verdict}")
        )
        return

    if kind == "task.closed":
        if snapshot.verdict is None:
            raise TaskReadModelError("task.closed appeared before task.verdict")
        snapshot.state = str(payload["status"])
        snapshot.cleanup = str(payload["cleanup"])
        snapshot.closed_sequence = sequence
        snapshot.result_ref = dict(payload["result_ref"])
        snapshot.activity.append(
            _activity(sequence, occurred_utc, kind, f"closed {snapshot.state}/{snapshot.cleanup}")
        )
        return

    if kind == "fault.reported":
        reason = str(payload["reason_code"])
        message = str(payload["message"])
        snapshot.faults.append((reason, message))
        snapshot.activity.append(_activity(sequence, occurred_utc, kind, f"fault {reason}"))
        return

    raise TaskReadModelError(f"unsupported task event kind {kind!r}")


def project_task(events: Iterable[dict], task_id: str) -> TaskSnapshot:
    """Project one task from a durable event stream.

    Events for other tasks and session-level events are ignored. The first relevant event
    must be ``task.admitted``; callers should not fabricate a card for an event orphan.
    """
    _require_task_id(task_id)
    relevant = [
        event
        for event in events
        if event.get("task_id") == task_id and event.get("kind") in _TASK_KINDS
    ]
    if not relevant:
        raise TaskReadModelError("no durable events exist for the requested task")
    relevant.sort(key=lambda event: int(event["sequence"]))
    admitted = relevant[0]
    if admitted.get("kind") != "task.admitted":
        raise TaskReadModelError("first durable task event must be task.admitted")
    payload = admitted["payload"]
    snapshot = TaskSnapshot(
        task_id=task_id,
        admitted_sequence=int(admitted["sequence"]),
        last_sequence=int(admitted["sequence"]),
        state="admitted",
        execution_epoch=int(payload["execution_epoch"]),
        origin_kind=str(payload["origin"]["kind"]),
        repository_id=str(payload["repository_id"]),
        skill=None if payload["skill"] is None else str(payload["skill"]),
        deadline_utc=str(payload["deadline_utc"]),
    )
    snapshot.activity.append(
        _activity(
            snapshot.admitted_sequence,
            str(admitted["occurred_utc"]),
            "task.admitted",
            f"admitted from {snapshot.origin_kind}",
        )
    )
    for event in relevant[1:]:
        _project_relevant(snapshot, event)
    return snapshot


def project_tasks(events: Iterable[dict]) -> list[TaskSnapshot]:
    """Project every admitted task in deterministic admission order."""
    materialised = list(events)
    task_ids: list[tuple[int, str]] = []
    seen: set[str] = set()
    for event in materialised:
        if event.get("kind") != "task.admitted":
            continue
        task_id = event.get("task_id")
        if not isinstance(task_id, str) or task_id in seen:
            if task_id in seen:
                raise TaskReadModelError("task has more than one durable admission event")
            raise TaskReadModelError("task.admitted must carry a task UUID")
        _require_task_id(task_id)
        seen.add(task_id)
        task_ids.append((int(event["sequence"]), task_id))
    task_ids.sort()
    return [project_task(materialised, task_id) for _sequence, task_id in task_ids]
