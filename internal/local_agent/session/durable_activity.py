"""Durable tool activity for one already-admitted Session Hub task.

This module is deliberately narrower than the process-local EventBuffer. It records
reviewed tool lifecycle facts that must survive restart. A tool-start event commits
before the caller is allowed to perform the effect; a tool-finish event is built only
from typed execution/domain data, never by parsing summary prose.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from typing import Any
from uuid import UUID, uuid4, uuid5

from .session_event_service import DURABLE_WRITE_TIMEOUT_S


class DurableActivityError(RuntimeError):
    """The durable tool lifecycle cannot be represented truthfully."""


_TOOL_REASON_TO_EVENT_REASON = {
    "policy_denied": "policy_denied",
    "approval_declined": "user_denied",
    "missing_executable": "missing_dependency",
    "spawn_failure": "unavailable_capability",
    "orchestrator_timeout": "tool_timeout",
    "bad_arguments": "invalid_input",
    "invalid_model_response": "invalid_input",
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
# A test run can execute cleanly and still say nothing about the current tree: the
# binaries were stale, never built, or built for another profile. The tool reports
# execution OK, domain UNKNOWN and one of these reasons, and the reason is the fact
# a reader needs, so it is kept rather than flattened to ``completed``.
_UNPROVEN_OK_REASONS = frozenset({"stale_binary", "no_build_record", "profile_mismatch"})
# Durable reason codes a finished OK execution may carry: the command exited on its own.
OK_EXECUTION_REASON_CODES = frozenset(
    {"completed"} | {_TOOL_REASON_TO_EVENT_REASON[r] for r in _UNPROVEN_OK_REASONS}
)
_ALLOWED_EXECUTION = frozenset({"ok", "blocked", "error", "interrupted", "unknown"})
_ALLOWED_DOMAIN = frozenset({"pass", "fail", "unknown"})
# A call that did not execute cleanly retains the tool's typed reason and its own
# short message as a result artifact, because the reviewed event vocabulary folds
# several typed reasons together (``not_found`` and ``bad_arguments`` are both
# ``invalid_input``). The message is tool-authored evidence, never authority.
TOOL_FAILURE_SCHEMA = "lca.tool-failure/2"
TOOL_FAILURE_MEDIA_TYPE = "application/json"
MAX_FAILURE_DETAIL_CHARS = 300


def tool_failure_artifact(
    *,
    tool_name: str,
    tool_reason: str,
    failure_detail: str,
    process_started: bool | None = None,
) -> tuple[dict[str, Any], bytes]:
    """The retained result reference and bytes for one failed tool call.

    ``process_started`` is the controller's own count of processes the call spawned
    (None when the call cannot spawn any). It is what lets terminal truth tell a call
    refused before anything ran from one that may have left a process behind.
    """
    detail = " ".join(failure_detail.split())[:MAX_FAILURE_DETAIL_CHARS]
    payload = json.dumps({
        "schema": TOOL_FAILURE_SCHEMA,
        "tool_name": tool_name,
        "tool_reason": tool_reason,
        "detail": detail,
        "process_started": process_started,
    }, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ref = {
        "artifact_id": str(uuid4()),
        "sha256": hashlib.sha256(payload).hexdigest(),
        "media_type": TOOL_FAILURE_MEDIA_TYPE,
        "size_bytes": len(payload),
        "availability": "retained",
    }
    return ref, payload


def durable_tool_reason(*, execution: str, reason: str | None, domain: str = "unknown") -> str:
    """Project typed tool reasons onto the reviewed durable-event vocabulary.

    Successful execution has an independent domain axis, so a command that ran and
    observed a failure still has reason ``completed``. The one exception is a clean
    run whose result is unproven for the current tree (domain ``unknown``), which
    keeps its typed reason. Any non-OK execution must carry a known typed reason;
    silently guessing a new mapping would weaken the contract.
    """
    if execution not in _ALLOWED_EXECUTION:
        raise DurableActivityError(f"unknown tool execution status {execution!r}")
    if execution == "ok":
        if reason is None:
            return "completed"
        if reason in _UNPROVEN_OK_REASONS and domain == "unknown":
            return _TOOL_REASON_TO_EVENT_REASON[reason]
        raise DurableActivityError(
            f"successful tool execution cannot carry reason {reason!r} with domain {domain!r}"
        )
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
        receipt.wait(DURABLE_WRITE_TIMEOUT_S)
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
        failure_detail: str | None = None,
        process_started: bool | None = None,
    ) -> None:
        """Commit a typed finish for exactly the currently-open durable call.

        ``failure_detail`` is the tool's own message for a call that did not
        execute cleanly; it is retained with the typed reason as ``result_ref``.
        """
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
        durable_reason = durable_tool_reason(execution=execution, reason=reason, domain=domain)
        result_ref: dict[str, Any] | None = None
        result_bytes: bytes | None = None
        if failure_detail is not None or process_started is not None:
            if execution == "ok":
                raise DurableActivityError("a cleanly executed tool call has no failure detail")
            if failure_detail is not None and not isinstance(failure_detail, str):
                raise ValueError("failure_detail must be a string")
            if process_started is not None and not isinstance(process_started, bool):
                raise ValueError("process_started must be a boolean")
            if reason is not None:
                result_ref, result_bytes = tool_failure_artifact(
                    tool_name=tool_name,
                    tool_reason=reason,
                    failure_detail=failure_detail or "",
                    process_started=process_started,
                )
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
                "result_ref": result_ref,
                "execution_epoch": self.execution_epoch,
            },
            task_id=self.task_id,
            result_bytes=result_bytes,
        )
        receipt.wait(DURABLE_WRITE_TIMEOUT_S)
        self._open = None
