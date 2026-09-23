"""Small, strict conversation/controller boundary. Model text is never authority."""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

MAX_MESSAGE_CHARS = 8_000


class RouteSource(str, Enum):
    """Who selected a repository task route.

    This is provenance, not permission. The current prototype is mostly
    ``model_proposal``; explicit/direct and rule routes become visible as they
    are implemented instead of silently changing behaviour.
    """

    RULE = "rule"
    MODEL_PROPOSAL = "model_proposal"
    USER_DIRECT = "user_direct"


class TaskOutcome(str, Enum):
    """Closed product-side task outcome vocabulary.

    This is deliberately separate from the frozen evaluation contract. Session
    Hub may represent uncertainty without redefining generation-2 success.
    """

    PASS = "pass"
    ESCALATED_PASS = "escalated_pass"
    ESCALATED_FAIL = "escalated_fail"
    FAIL = "fail"
    BLOCKED = "blocked"
    NO_VERDICT = "no_verdict"

    @property
    def succeeded(self) -> bool:
        return self in (TaskOutcome.PASS, TaskOutcome.ESCALATED_PASS)


class TerminalState(str, Enum):
    COMPLETED = "completed"
    FAILED = "failed"
    BLOCKED = "blocked"
    CANCELLED = "cancelled"
    TIMED_OUT = "timed_out"
    INTERRUPTED = "interrupted"
    UNKNOWN = "unknown"


class TaskVerdict(str, Enum):
    """Deterministic verification verdict for a task completion."""

    VERIFIED = "VERIFIED"
    FAILED = "FAILED"
    REFUSED = "REFUSED"
    NO_VERDICT = "NO_VERDICT"
    NOT_REQUIRED = "NOT_REQUIRED"


@dataclass(frozen=True)
class TaskOutcomeProjection:
    terminal_state: TerminalState
    verdict: TaskVerdict


# One task-outcome vocabulary, one explicit projection into the v1 lifecycle.
# CANCELLED/TIMED_OUT/INTERRUPTED remain schema-declared but are intentionally
# unreachable from a completed TaskResult until process ownership makes those
# lifecycle claims truthful. UNKNOWN is the honest result projection when a
# reliable final verdict cannot be established.
TASK_OUTCOME_PROJECTIONS: dict[TaskOutcome, TaskOutcomeProjection] = {
    TaskOutcome.PASS: TaskOutcomeProjection(TerminalState.COMPLETED, TaskVerdict.VERIFIED),
    TaskOutcome.ESCALATED_PASS: TaskOutcomeProjection(TerminalState.COMPLETED, TaskVerdict.VERIFIED),
    TaskOutcome.ESCALATED_FAIL: TaskOutcomeProjection(TerminalState.FAILED, TaskVerdict.FAILED),
    TaskOutcome.FAIL: TaskOutcomeProjection(TerminalState.FAILED, TaskVerdict.FAILED),
    TaskOutcome.BLOCKED: TaskOutcomeProjection(TerminalState.BLOCKED, TaskVerdict.REFUSED),
    TaskOutcome.NO_VERDICT: TaskOutcomeProjection(TerminalState.UNKNOWN, TaskVerdict.NO_VERDICT),
}
UNREACHABLE_TERMINAL_STATES = frozenset({
    TerminalState.CANCELLED,
    TerminalState.TIMED_OUT,
    TerminalState.INTERRUPTED,
})


def task_exit_code(outcome: TaskOutcome | str) -> int:
    """Stable CLI projection: success=0, observed failure=1, blocked/unknown=2."""
    value = outcome if isinstance(outcome, TaskOutcome) else TaskOutcome(outcome)
    if value.succeeded:
        return 0
    if value in (TaskOutcome.FAIL, TaskOutcome.ESCALATED_FAIL):
        return 1
    if value in (TaskOutcome.BLOCKED, TaskOutcome.NO_VERDICT):
        return 2
    raise AssertionError(f"unmapped task outcome: {value}")


@dataclass(frozen=True)
class Proposal:
    kind: str
    text: str

    @classmethod
    def parse(cls, raw: str) -> "Proposal":
        def unique(pairs):
            result = {}
            for key, value in pairs:
                if key in result:
                    raise ValueError("duplicate proposal field")
                result[key] = value
            return result

        if len(raw) > MAX_MESSAGE_CHARS + 100:
            raise ValueError("proposal exceeds size limit")
        value = json.loads(raw, object_pairs_hook=unique)
        if not isinstance(value, dict) or set(value) != {"kind", "text"}:
            raise ValueError("expected exactly kind and text")
        if value["kind"] not in ("reply", "repository", "self_check"):
            raise ValueError("unknown proposal kind")
        if not isinstance(value["text"], str) or not value["text"].strip():
            raise ValueError("proposal text must be a nonempty string")
        if len(value["text"]) > MAX_MESSAGE_CHARS:
            raise ValueError("proposal text exceeds size limit")
        return cls(value["kind"], value["text"].strip())


@dataclass(frozen=True)
class EvidenceRef:
    task_id: str
    evidence_id: str


@dataclass(frozen=True)
class TaskResult:
    task_id: str
    outcome: TaskOutcome | str
    answer: str
    verified_at_completion: bool = False
    evidence_ids: tuple[str, ...] = ()
    metrics: dict[str, Any] = field(default_factory=dict)
    verification_ran: bool | None = None
    reason_code: str | None = None

    def __post_init__(self) -> None:
        try:
            outcome = self.outcome if isinstance(self.outcome, TaskOutcome) else TaskOutcome(self.outcome)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"undeclared task outcome: {self.outcome!r}") from exc
        object.__setattr__(self, "outcome", outcome)
        ran = bool(self.verified_at_completion) if self.verification_ran is None else bool(self.verification_ran)
        object.__setattr__(self, "verification_ran", ran)
        if self.reason_code is not None:
            if not isinstance(self.reason_code, str) or not self.reason_code.strip():
                raise ValueError("task result reason_code must be a nonempty string or None")
            object.__setattr__(self, "reason_code", self.reason_code.strip())
        if self.verified_at_completion and not ran:
            raise ValueError("verified_at_completion requires verification_ran")
        if outcome.succeeded != bool(self.verified_at_completion):
            raise ValueError(
                "successful task outcome and verified_at_completion must agree; "
                "use no_verdict when success proof is not established"
            )

    @property
    def evidence_refs(self) -> tuple[EvidenceRef, ...]:
        return tuple(EvidenceRef(self.task_id, local_id) for local_id in self.evidence_ids)

    @property
    def projection(self) -> TaskOutcomeProjection:
        return TASK_OUTCOME_PROJECTIONS[self.outcome]

    def render(self) -> str:
        if self.verified_at_completion:
            proof = "passed at task completion"
        elif self.verification_ran:
            proof = "ran, did not establish success"
        else:
            proof = "not established"
        reason = f"; reason: {self.reason_code}" if self.reason_code else ""
        return (f"{self.answer}\n\n[Controller: {self.outcome.value}; verification: {proof}{reason}; "
                f"evidence: {len(self.evidence_ids)} item(s); task: {self.task_id}]\n"
                f"Evidence IDs: {', '.join(self.evidence_ids) or 'none'}")
