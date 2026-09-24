"""Deterministic Session Hub verdict rendering.

Controller results are authority; model prose is not. This module converts the
existing typed ``TaskResult`` into the v1 ``VerdictBlock`` shape without adding a
second outcome vocabulary or allowing a model to narrate completion.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Iterable

from .contracts import TaskResult, TaskVerdict
from .proof_binding import proof_binding_from_result


MAX_SCOPE_CHARS = 512
MAX_EVIDENCE_IDS = 128
MAX_EVIDENCE_ID_CHARS = 256
MAX_RENDERED_LINES = 12
MAX_RENDERED_LINE_CHARS = 2048


class VerdictReason(str, Enum):
    NONE = "none"
    REQUESTED = "requested"
    COMPLETED = "completed"
    POLICY_DENIED = "policy_denied"
    USER_DENIED = "user_denied"
    MISSING_DEPENDENCY = "missing_dependency"
    INVALID_INPUT = "invalid_input"
    UNAVAILABLE_CAPABILITY = "unavailable_capability"
    ROUTING_UNCERTAIN = "routing_uncertain"
    ENDPOINT_UNAVAILABLE = "endpoint_unavailable"
    INFERENCE_TIMEOUT = "inference_timeout"
    TOOL_TIMEOUT = "tool_timeout"
    TASK_TIMEOUT = "task_timeout"
    CANCELLED = "cancelled"
    CLEANUP_UNKNOWN = "cleanup_unknown"
    CONTROLLER_RESTARTED = "controller_restarted"
    CONTROLLER_FAULT = "controller_fault"
    DURABLE_WRITE_FAILED = "durable_write_failed"
    CANCEL_UNRECONCILED = "cancel_unreconciled"
    CONTROLLER_CRASH = "controller_crash"  # legacy v1 replay only; no current producer
    VERIFICATION_FAILED = "verification_failed"
    VERIFICATION_PASSED = "verification_passed"
    VERIFICATION_NOT_REQUIRED = "verification_not_required"
    MISSING_EVIDENCE = "missing_evidence"
    STALE_EVIDENCE = "stale_evidence"
    STORAGE_ERROR = "storage_error"
    PROTOCOL_MISMATCH = "protocol_mismatch"
    SCOPE_CHANGED = "scope_changed"
    QUEUE_FULL = "queue_full"
    WATCH_OVERLAP = "watch_overlap"
    MISSED_SCHEDULE = "missed_schedule"
    NO_PREVIOUS_RUN = "no_previous_run"
    CONTRACT_CHANGED = "contract_changed"


_CURRENT_REASON_BY_VERDICT: dict[TaskVerdict, VerdictReason] = {
    TaskVerdict.VERIFIED: VerdictReason.VERIFICATION_PASSED,
    TaskVerdict.FAILED: VerdictReason.VERIFICATION_FAILED,
    TaskVerdict.REFUSED: VerdictReason.POLICY_DENIED,
    TaskVerdict.NO_VERDICT: VerdictReason.CLEANUP_UNKNOWN,
}

_ALLOWED_REASONS_BY_VERDICT: dict[TaskVerdict, frozenset[VerdictReason]] = {
    TaskVerdict.VERIFIED: frozenset({VerdictReason.VERIFICATION_PASSED}),
    TaskVerdict.FAILED: frozenset({
        VerdictReason.VERIFICATION_FAILED,
        VerdictReason.MISSING_EVIDENCE,
        VerdictReason.STALE_EVIDENCE,
        VerdictReason.PROTOCOL_MISMATCH,
        VerdictReason.SCOPE_CHANGED,
    }),
    TaskVerdict.REFUSED: frozenset({
        VerdictReason.POLICY_DENIED,
        VerdictReason.USER_DENIED,
        VerdictReason.MISSING_DEPENDENCY,
        VerdictReason.INVALID_INPUT,
        VerdictReason.UNAVAILABLE_CAPABILITY,
        VerdictReason.ROUTING_UNCERTAIN,
        VerdictReason.TOOL_TIMEOUT,
        VerdictReason.TASK_TIMEOUT,
        VerdictReason.CANCELLED,
        VerdictReason.QUEUE_FULL,
    }),
    TaskVerdict.NO_VERDICT: frozenset({
        VerdictReason.CLEANUP_UNKNOWN,
        VerdictReason.CONTROLLER_RESTARTED,
        VerdictReason.CONTROLLER_FAULT,
        VerdictReason.DURABLE_WRITE_FAILED,
        VerdictReason.CANCEL_UNRECONCILED,
        VerdictReason.CONTROLLER_CRASH,
        VerdictReason.ENDPOINT_UNAVAILABLE,
        VerdictReason.INFERENCE_TIMEOUT,
        VerdictReason.TOOL_TIMEOUT,
        VerdictReason.TASK_TIMEOUT,
        VerdictReason.MISSING_EVIDENCE,
        VerdictReason.PROTOCOL_MISMATCH,
        VerdictReason.STORAGE_ERROR,
        VerdictReason.CONTRACT_CHANGED,
    }),
    TaskVerdict.NOT_REQUIRED: frozenset({VerdictReason.VERIFICATION_NOT_REQUIRED}),
}


def _tree_sha256(value: object) -> str | None:
    if value is None:
        return None
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(ch not in "0123456789abcdef" for ch in value)
    ):
        raise ValueError("tree_sha256 must be lowercase SHA-256 or None")
    return value


def _evidence_ids(values: Iterable[str]) -> tuple[str, ...]:
    ids = tuple(values)
    if len(ids) > MAX_EVIDENCE_IDS:
        raise ValueError("too many evidence ids for v1 verdict block")
    for value in ids:
        if not isinstance(value, str) or not value:
            raise ValueError("evidence ids must be nonempty strings")
        if len(value) > MAX_EVIDENCE_ID_CHARS:
            raise ValueError("evidence id exceeds v1 size limit")
    return ids


def _evidence_lines(evidence_ids: tuple[str, ...], *, available_lines: int) -> tuple[str, ...]:
    if available_lines < 1:
        raise ValueError("verdict block has no room to render evidence")
    if not evidence_ids:
        return ("Evidence: none.",)

    lines: list[str] = []
    current = "Evidence: "
    continuation = "Evidence (cont.): "
    for evidence_id in evidence_ids:
        separator = "" if current.endswith(": ") else ", "
        candidate = current + separator + evidence_id
        if len(candidate) <= MAX_RENDERED_LINE_CHARS:
            current = candidate
            continue
        if current.endswith(": "):
            raise ValueError("evidence id cannot be rendered within v1 line limit")
        lines.append(current)
        if len(lines) >= available_lines:
            raise ValueError("evidence ids cannot be rendered within v1 line-count limit")
        current = continuation + evidence_id
        if len(current) > MAX_RENDERED_LINE_CHARS:
            raise ValueError("evidence id cannot be rendered within v1 line limit")
    lines.append(current)
    if len(lines) > available_lines:
        raise ValueError("evidence ids cannot be rendered within v1 line-count limit")
    return tuple(lines)


@dataclass(frozen=True)
class VerdictBlock:
    verdict: TaskVerdict | str
    reason_code: VerdictReason | str
    scope: str
    evidence_ids: tuple[str, ...] = ()
    tree_sha256: str | None = None
    rendered_lines: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "verdict", TaskVerdict(self.verdict))
        object.__setattr__(self, "reason_code", VerdictReason(self.reason_code))
        if not isinstance(self.scope, str) or not self.scope:
            raise ValueError("verdict scope must be nonempty")
        if len(self.scope) > MAX_SCOPE_CHARS:
            raise ValueError("verdict scope exceeds v1 size limit")
        object.__setattr__(self, "evidence_ids", _evidence_ids(self.evidence_ids))
        object.__setattr__(self, "tree_sha256", _tree_sha256(self.tree_sha256))
        lines = tuple(self.rendered_lines)
        if not lines or len(lines) > MAX_RENDERED_LINES:
            raise ValueError("verdict rendered_lines must contain 1..12 lines")
        for line in lines:
            if not isinstance(line, str) or not line:
                raise ValueError("verdict rendered lines must be nonempty strings")
            if len(line) > MAX_RENDERED_LINE_CHARS:
                raise ValueError("verdict rendered line exceeds v1 size limit")
            if "\n" in line or "\r" in line:
                raise ValueError("verdict rendered lines must be single physical lines")
        object.__setattr__(self, "rendered_lines", lines)

    def as_payload(self) -> dict[str, Any]:
        return {
            "verdict": self.verdict.value,
            "reason_code": self.reason_code.value,
            "scope": self.scope,
            "evidence_ids": list(self.evidence_ids),
            "tree_sha256": self.tree_sha256,
            "rendered_lines": list(self.rendered_lines),
        }


def verdict_block_from_task_result(
    result: TaskResult,
    *,
    scope: str = "controller task result at durable completion",
) -> VerdictBlock:
    """Render one typed controller result without erasing its stop reason."""
    if not isinstance(result, TaskResult):
        raise TypeError("verdict rendering requires TaskResult")
    verdict = result.projection.verdict
    if result.reason_code is None:
        try:
            reason = _CURRENT_REASON_BY_VERDICT[verdict]
        except KeyError as exc:
            raise ValueError(f"TaskResult verdict is not renderable yet: {verdict.value}") from exc
    else:
        try:
            reason = VerdictReason(result.reason_code)
        except ValueError as exc:
            raise ValueError(f"unknown TaskResult reason_code: {result.reason_code!r}") from exc
        if reason not in _ALLOWED_REASONS_BY_VERDICT[verdict]:
            raise ValueError(
                f"reason {reason.value!r} is incompatible with verdict {verdict.value!r}"
            )

    evidence_ids = _evidence_ids(result.evidence_ids)
    binding = proof_binding_from_result(result)
    if binding is not None:
        tree = binding.tree_sha256
        scope = f"{binding.scope.value}; request_sha256={binding.request_sha256}"
    else:
        # Compatibility for self-check and historical/current producers that predate
        # typed worker proof binding. Absence remains visible as generic scope.
        tree = _tree_sha256(result.metrics.get("tree_sha256"))
    if result.verified_at_completion:
        verification = "Verification: passed at task completion."
    elif result.verification_ran:
        verification = "Verification: ran but did not establish success."
    else:
        verification = "Verification: not established."

    lines = [
        f"{verdict.value}: {result.outcome.value}.",
        f"Reason: {reason.value}.",
        verification,
        f"Tree SHA-256: {tree if tree is not None else 'none'}.",
    ]
    if binding is not None:
        lines.append(f"Proof scope: {binding.scope.value}; request: {binding.request_sha256}.")
    lines.extend(_evidence_lines(evidence_ids, available_lines=MAX_RENDERED_LINES - len(lines)))
    return VerdictBlock(
        verdict=verdict,
        reason_code=reason,
        scope=scope,
        evidence_ids=evidence_ids,
        tree_sha256=tree,
        rendered_lines=tuple(lines),
    )
