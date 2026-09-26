"""Durable tool activity for one already-admitted Session Hub task.

This module is deliberately narrower than the process-local EventBuffer. It records
reviewed tool lifecycle facts that must survive restart. A tool-start event commits
before the caller is allowed to perform the effect; a tool-finish event is built only
from typed execution/domain data, never by parsing summary prose.
"""
from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID, uuid5


class DurableActivityError(RuntimeError):
    """The durable tool lifecycle cannot be represented truthfully."""


_TOOL_REASON_TO_EVENT_REASON = {
    "policy_denied": "policy_denied",
    "approval_declined": "user_denied",
    "missing_executable": "missing_dependency",
    "spawn_failure": "unavailable_capability",
    "orchestrator_timeout": "tool_timeout",
    "bad_arguments": "invalid_input",
    "sandbox_violation": "policy_denied",
    "unknown_tool": "unavailable_capability",
    "tool_not_allowed": "policy_denied",
    "not_found": "invalid_input",
    "internal_error": "unavailable_capability",
    "server_unavailable": "endpoint_unavailable",
    "server_stalled": "inference_timeout",
    "stale_binary": "stale_evidence",
    "no_build_record": "missing_evidence",
    "profile_mismatch": "scope_changed",
    "protected_path": "policy_denied",
    "command_cancelled": "cancelled",
}
_ALLOWED_EXECUTION = frozenset({"ok", "blocked", "error", "interrupted", "unknown"})
_ALLOWED_DOMAIN = frozenset({"pass", "fail", "unknown"})


def durable_tool_reason(*, execution: str, reason: str | None) -> str:
    """Project typed tool reasons onto the reviewed durable-event vocabulary.

    Successful execution has an independent domain axis, so a command that ran and
    observed a failure still has reason ``completed``. Any non-OK execution must carry
    a known typed reason; silently guessing a new mapping would weaken the contract.
    """
    if execution not in _ALLOWED_EXECUTION:
        raise DurableActivityError(f"unknown tool execution status {execution!r}")
    if execution == "ok":
        if reason is not None:
            raise DurableActivityError("successful tool execution cannot carry an error reason")
        return "completed"
    if reason is None:
        raise DurableActivityError("non-OK tool execution requires a typed reason")
    try:
        return _TOOL_REASON_TO_EVENT_REASON[reason]
    except KeyError as exc:
        raise DurableActivityError(f"unmapped typed tool reason {reason!r}") from exc


@dataclass(frozen=True)
class OpenToolCall:
    call_id: str
    tool_name: str


class DurableToolActivity:
    """Fail-closed durable lifecycle publisher for a single task execution epoch."""

    def __init__(
        self,
        service,
        *,
        task_id: str,
        execution_epoch: int,
        deadline_utc: str,
    ) -> None:
        UUID(task_id)
        if not isinstance(execution_epoch, int) or isinstance(execution_epoch, bool) or execution_epoch < 0:
            raise ValueError("execution_epoch must be a non-negative integer")
        if not isinstance(deadline_utc, str) or not deadline_utc.strip():
            raise ValueError("deadline_utc must be nonempty")
        self.service = service
        self.task_id = task_id
        self.execution_epoch = execution_epoch
        self.deadline_utc = deadline_utc
        self._ordinal = 0
        self._open: OpenToolCall | None = None

    @classmethod
    def from_task(cls, service, task_id: str) -> "DurableToolActivity":
        """Recover the exact epoch/deadline already committed at admission."""
        record = service.store.task_record(task_id)
        if record is None:
            raise DurableActivityError("durable activity requires an admitted task")
        if record["stream_id"] != service.stream_id:
            raise DurableActivityError("task belongs to a different durable stream")
        admitted_sequence = int(record["admitted_sequence"])
        events = service.replay(after=admitted_sequence - 1, limit=1)
        if (
            len(events) != 1
            or events[0].get("kind") != "task.admitted"
            or events[0].get("task_id") != task_id
        ):
            raise DurableActivityError("task admission event is unavailable or inconsistent")
        payload = events[0]["payload"]
        if int(payload["execution_epoch"]) != int(record["execution_epoch"]):
            raise DurableActivityError("task index and admission epoch disagree")
        return cls(
            service,
            task_id=task_id,
            execution_epoch=int(payload["execution_epoch"]),
            deadline_utc=str(payload["deadline_utc"]),
        )

    @property
    def open_call(self) -> OpenToolCall | None:
        return self._open

    def start_tool(self, tool_name: str) -> OpenToolCall:
        """Commit ``tool.started`` before the caller performs the tool effect."""
        if self._open is not None:
            raise DurableActivityError("a previous tool call is still open")
        if not isinstance(tool_name, str) or not tool_name.strip():
            raise ValueError("tool_name must be nonempty")
        call_id = str(
            uuid5(
                UUID(self.task_id),
                f"tool:{self.execution_epoch}:{self._ordinal}:{tool_name}",
            )
        )
        receipt = self.service.append(
            "tool.started",
            {
                "call_id": call_id,
                "tool_name": tool_name,
                "arguments_ref": None,
                "execution_epoch": self.execution_epoch,
                "deadline_utc": self.deadline_utc,
            },
            task_id=self.task_id,
        )
        receipt.wait(30)
        opened = OpenToolCall(call_id=call_id, tool_name=tool_name)
        self._open = opened
        self._ordinal += 1
        return opened

    def finish_tool(
        self,
        *,
        call_id: str,
        tool_name: str,
        execution: str,
        domain: str,
        reason: str | None,
        exit_code: int | None,
        duration_ms: int,
        evidence_ids: tuple[str, ...] | list[str] = (),
    ) -> None:
        """Commit a typed finish for exactly the currently-open durable call."""
        opened = self._open
        if opened is None:
            raise DurableActivityError("tool finish has no matching durable start")
        if call_id != opened.call_id or tool_name != opened.tool_name:
            raise DurableActivityError("tool finish does not match the open durable call")
        if execution not in _ALLOWED_EXECUTION:
            raise DurableActivityError(f"unknown tool execution status {execution!r}")
        if domain not in _ALLOWED_DOMAIN:
            raise DurableActivityError(f"unknown tool domain status {domain!r}")
        if not isinstance(duration_ms, int) or isinstance(duration_ms, bool) or duration_ms < 0:
            raise ValueError("duration_ms must be a non-negative integer")
        durable_reason = durable_tool_reason(execution=execution, reason=reason)
        ids = tuple(str(value) for value in evidence_ids)
        if any(not value for value in ids):
            raise ValueError("evidence ids must be nonempty strings")
        receipt = self.service.append(
            "tool.finished",
            {
                "call_id": call_id,
                "tool_name": tool_name,
                "execution": execution,
                "domain": domain,
                "reason_code": durable_reason,
                "exit_code": exit_code,
                "duration_ms": duration_ms,
                "evidence_ids": list(ids),
                "result_ref": None,
                "execution_epoch": self.execution_epoch,
            },
            task_id=self.task_id,
        )
        receipt.wait(30)
        self._open = None
