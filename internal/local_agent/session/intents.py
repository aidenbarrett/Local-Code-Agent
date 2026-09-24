"""Deterministic-first Session Hub routing primitives.

This module is deliberately pure policy. It does not execute work, inspect the
repository, call a model, or admit a task. It resolves only the route information
that deterministic code can know before the model-fallback boundary.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import re
from typing import Sequence
from uuid import UUID

from .contracts import MAX_MESSAGE_CHARS, RouteSource


RULE_GIT_REVIEW = "git-review/v1"
RULE_REPO_NAVIGATION = "repo-navigation/v1"
RULE_BUILD_AND_TEST = "build-and-test/v1"
RULE_TASK_DIAGNOSTIC = "task-diagnostic/v1"
RULE_MUTATION_UNAVAILABLE = "mutation-unavailable/v1"
RULE_SELF_CHECK = "self-check/v1"


class RouteAction(str, Enum):
    CONTROL = "control"
    CHAT = "chat"
    WORK = "work"
    MODEL_FALLBACK = "model_fallback"
    CLARIFY = "clarify"
    REFUSE = "refuse"


class ExplicitMode(str, Enum):
    CHAT = "chat"
    WORK = "work"


class CorrectionStatus(str, Enum):
    NOT_A_CORRECTION = "not_a_correction"
    APPLIED = "applied"
    CLARIFY = "clarify"


def _validate_reference_ids(reference_ids: Sequence[str]) -> tuple[str, ...]:
    values = tuple(reference_ids)
    if any(not isinstance(value, str) or not value.strip() for value in values):
        raise ValueError("task reference ids must be nonempty strings")
    if len(set(values)) != len(values):
        raise ValueError("task reference ids must be unique")
    return values


@dataclass(frozen=True)
class RouteDecision:
    action: RouteAction
    objective: str | None = None
    source: RouteSource | None = None
    rule_id: str | None = None
    skill: str | None = None
    reference_ids: tuple[str, ...] = ()
    reason_code: str | None = None
    control_id: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "action", RouteAction(self.action))
        if self.source is not None:
            object.__setattr__(self, "source", RouteSource(self.source))
        object.__setattr__(self, "reference_ids", _validate_reference_ids(self.reference_ids))

        if self.action == RouteAction.WORK:
            if self.source is None:
                raise ValueError("work route requires source provenance")
            if not isinstance(self.objective, str) or not self.objective.strip():
                raise ValueError("work route requires a nonempty objective")
            if self.source == RouteSource.RULE and not self.rule_id:
                raise ValueError("rule work route requires rule identity")
        elif self.source is not None:
            raise ValueError("only work routes carry task route provenance")

        if self.action == RouteAction.CONTROL:
            if not self.control_id:
                raise ValueError("control route requires a control id")
        elif self.control_id is not None:
            raise ValueError("non-control route cannot carry a control id")

        if self.action in (RouteAction.CLARIFY, RouteAction.REFUSE):
            if not self.reason_code:
                raise ValueError("clarification/refusal requires a reason code")
        elif self.reason_code is not None:
            raise ValueError("only clarification/refusal carries a reason code")

        if self.reference_ids and self.action != RouteAction.WORK:
            raise ValueError("only work routes may carry task references")


@dataclass(frozen=True)
class TaskIntent:
    """Untrusted work intent; never an admitted task or permission object."""

    turn_ref: dict[str, object]
    objective: str
    proposed_reference_ids: tuple[str, ...]
    origin: RouteSource
    rule_id: str | None = None

    def __post_init__(self) -> None:
        copied_ref = dict(self.turn_ref)
        _validate_turn_ref(copied_ref)
        object.__setattr__(self, "turn_ref", copied_ref)
        object.__setattr__(self, "origin", RouteSource(self.origin))
        object.__setattr__(
            self,
            "proposed_reference_ids",
            _validate_reference_ids(self.proposed_reference_ids),
        )
        if not isinstance(self.objective, str) or not self.objective.strip():
            raise ValueError("task intent objective must be nonempty")
        if len(self.objective) > MAX_MESSAGE_CHARS:
            raise ValueError("task intent objective exceeds size limit")
        if self.origin == RouteSource.RULE and not self.rule_id:
            raise ValueError("rule-origin task intent requires rule identity")
        if self.origin != RouteSource.RULE and self.rule_id is not None:
            raise ValueError("non-rule task intent cannot carry rule identity")


@dataclass(frozen=True)
class PendingRouteRef:
    route_id: str
    revision: int

    def __post_init__(self) -> None:
        if not isinstance(self.route_id, str) or not self.route_id.strip():
            raise ValueError("pending route id must be nonempty")
        if not isinstance(self.revision, int) or isinstance(self.revision, bool) or self.revision < 0:
            raise ValueError("pending route revision must be a nonnegative integer")


@dataclass(frozen=True)
class RouteCorrection:
    status: CorrectionStatus
    route_id: str | None = None
    revision: int | None = None
    mode: ExplicitMode | None = None
    reason_code: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "status", CorrectionStatus(self.status))
        if self.mode is not None:
            object.__setattr__(self, "mode", ExplicitMode(self.mode))
        if self.status == CorrectionStatus.APPLIED:
            if self.route_id is None or self.revision is None or self.mode is None:
                raise ValueError("applied correction requires route, revision and mode")
            if self.reason_code is not None:
                raise ValueError("applied correction cannot carry a reason")
        elif self.status == CorrectionStatus.CLARIFY:
            if not self.reason_code:
                raise ValueError("clarification requires a reason")
            if self.route_id is not None or self.revision is not None or self.mode is not None:
                raise ValueError("clarification cannot pretend a correction was applied")
        else:
            if any(value is not None for value in (self.route_id, self.revision, self.mode, self.reason_code)):
                raise ValueError("non-correction cannot carry correction state")


_GIT_REVIEW = re.compile(r"^what changed on my branch\??$", re.IGNORECASE)
_REPO_INSPECT = re.compile(r"^inspect (?:this|the) (?:repo|repository)[.!]?$", re.IGNORECASE)
_SYMBOL_LOOKUP = re.compile(
    r"^where is (?P<symbol>[A-Za-z_~][A-Za-z0-9_:.<>~]*) defined\??$",
    re.IGNORECASE,
)
_BUILD = re.compile(r"^build (?:it|this|the repo|the repository)[.!]?$", re.IGNORECASE)
_DIAGNOSTIC = re.compile(r"^why did that fail\??$", re.IGNORECASE)
_EXPLICIT_DIAGNOSTIC = re.compile(r"^why did task (?P<task_id>\S+) fail\??$", re.IGNORECASE)
_FIX = re.compile(r"^fix it[.!]?$", re.IGNORECASE)
_SELF_CHECK = re.compile(r"^/check$", re.IGNORECASE)
_CONTROL = {"/quit": "quit", "/exit": "quit"}


def _validate_turn_ref(value: dict[str, object]) -> None:
    required = {"conversation_id", "turn_index", "turn_sha256"}
    if not isinstance(value, dict) or set(value) != required:
        raise ValueError("task intent requires an exact TurnRef")
    conversation_id = value["conversation_id"]
    turn_index = value["turn_index"]
    digest = value["turn_sha256"]
    if not isinstance(conversation_id, str) or not conversation_id:
        raise ValueError("TurnRef conversation id must be nonempty")
    if not isinstance(turn_index, int) or isinstance(turn_index, bool) or turn_index < 0:
        raise ValueError("TurnRef turn index must be a nonnegative integer")
    if (
        not isinstance(digest, str)
        or len(digest) != 64
        or any(ch not in "0123456789abcdef" for ch in digest)
    ):
        raise ValueError("TurnRef hash must be lowercase SHA-256")


def _unique_referent(eligible_task_ids: Sequence[str]) -> tuple[str | None, str | None]:
    values = _validate_reference_ids(eligible_task_ids)
    if not values:
        return None, "no_eligible_task_reference"
    if len(values) > 1:
        return None, "ambiguous_task_reference"
    return values[0], None


def _explicit_referent(
    raw_task_id: str,
    eligible_task_ids: Sequence[str],
) -> tuple[str | None, str | None]:
    """Resolve only a canonical durable UUID present in the eligible candidate set."""
    try:
        task_id = str(UUID(raw_task_id))
    except (ValueError, AttributeError):
        return None, "invalid_task_reference"
    # UUID() accepts braces and compact forms. The conversation contract deliberately
    # requires the visible canonical UUID so referent authority is explicit and auditable.
    if raw_task_id.lower() != task_id:
        return None, "invalid_task_reference"
    values = _validate_reference_ids(eligible_task_ids)
    if task_id not in values:
        return None, "ineligible_task_reference"
    return task_id, None


def _repository_target_reason(active_repo_count: int | None) -> str | None:
    if active_repo_count is None:
        return "active_repository_unknown"
    if active_repo_count == 0:
        return "no_active_repository"
    if active_repo_count > 1:
        return "ambiguous_active_repository"
    return None


def decide_route(
    text: str,
    *,
    explicit_mode: ExplicitMode | str | None = None,
    active_repo_count: int | None = None,
    eligible_task_ids: Sequence[str] = (),
) -> RouteDecision:
    """Resolve deterministic routing before the model-fallback boundary.

    Matching is intentionally anchored. Quoted examples, negations and explanatory
    sentences do not inherit authority merely because they contain words such as
    ``build`` or ``fix``.
    """
    if not isinstance(text, str):
        raise TypeError("route input must be text")
    if len(text) > MAX_MESSAGE_CHARS:
        raise ValueError("route input exceeds size limit")
    if active_repo_count is not None and (
        not isinstance(active_repo_count, int)
        or isinstance(active_repo_count, bool)
        or active_repo_count < 0
    ):
        raise ValueError("active_repo_count must be a nonnegative integer or None")

    stripped = text.strip()
    lowered = stripped.lower()
    if lowered in _CONTROL:
        return RouteDecision(RouteAction.CONTROL, control_id=_CONTROL[lowered])
    if not stripped:
        return RouteDecision(RouteAction.CLARIFY, reason_code="empty_input")

    mode = None if explicit_mode is None else ExplicitMode(explicit_mode)
    if mode == ExplicitMode.CHAT:
        return RouteDecision(RouteAction.CHAT, objective=text)
    if mode == ExplicitMode.WORK:
        return RouteDecision(RouteAction.WORK, objective=text, source=RouteSource.USER_DIRECT)

    if _SELF_CHECK.fullmatch(stripped):
        return RouteDecision(
            RouteAction.WORK,
            objective=text,
            source=RouteSource.RULE,
            rule_id=RULE_SELF_CHECK,
            skill="self-check",
        )

    if _REPO_INSPECT.fullmatch(stripped) or _SYMBOL_LOOKUP.fullmatch(stripped):
        reason = _repository_target_reason(active_repo_count)
        if reason is not None:
            return RouteDecision(RouteAction.CLARIFY, reason_code=reason)
        return RouteDecision(
            RouteAction.WORK,
            objective=text,
            source=RouteSource.RULE,
            rule_id=RULE_REPO_NAVIGATION,
            skill="repo-navigation",
        )

    if _GIT_REVIEW.fullmatch(stripped):
        reason = _repository_target_reason(active_repo_count)
        if reason is not None:
            return RouteDecision(RouteAction.CLARIFY, reason_code=reason)
        return RouteDecision(
            RouteAction.WORK,
            objective=text,
            source=RouteSource.RULE,
            rule_id=RULE_GIT_REVIEW,
            skill="git-review",
        )

    if _BUILD.fullmatch(stripped):
        reason = _repository_target_reason(active_repo_count)
        if reason is not None:
            return RouteDecision(RouteAction.CLARIFY, reason_code=reason)
        return RouteDecision(
            RouteAction.WORK,
            objective=text,
            source=RouteSource.RULE,
            rule_id=RULE_BUILD_AND_TEST,
            skill="build-and-test",
        )

    explicit_diagnostic = _EXPLICIT_DIAGNOSTIC.fullmatch(stripped)
    if explicit_diagnostic is not None:
        task_id, reason = _explicit_referent(
            explicit_diagnostic.group("task_id"),
            eligible_task_ids,
        )
        if task_id is None:
            return RouteDecision(RouteAction.CLARIFY, reason_code=reason)
        return RouteDecision(
            RouteAction.WORK,
            objective=text,
            source=RouteSource.RULE,
            rule_id=RULE_TASK_DIAGNOSTIC,
            skill="task-diagnostic",
            reference_ids=(task_id,),
        )

    if _DIAGNOSTIC.fullmatch(stripped):
        task_id, reason = _unique_referent(eligible_task_ids)
        if task_id is None:
            return RouteDecision(RouteAction.CLARIFY, reason_code=reason)
        return RouteDecision(
            RouteAction.WORK,
            objective=text,
            source=RouteSource.RULE,
            rule_id=RULE_TASK_DIAGNOSTIC,
            skill="task-diagnostic",
            reference_ids=(task_id,),
        )

    if _FIX.fullmatch(stripped):
        return RouteDecision(
            RouteAction.REFUSE,
            objective=text,
            rule_id=RULE_MUTATION_UNAVAILABLE,
            reason_code="mutation_workflow_unavailable",
        )

    return RouteDecision(RouteAction.MODEL_FALLBACK, objective=text)


def task_intent_from_decision(decision: RouteDecision, turn_ref: dict[str, object]) -> TaskIntent:
    if decision.action != RouteAction.WORK or decision.source is None or decision.objective is None:
        raise ValueError("only a work route can become a task intent")
    return TaskIntent(
        turn_ref=turn_ref,
        objective=decision.objective,
        proposed_reference_ids=decision.reference_ids,
        origin=decision.source,
        rule_id=decision.rule_id,
    )


def correct_pending_route(text: str, pending: Sequence[PendingRouteRef]) -> RouteCorrection:
    """Target one pending revision with an explicit one-word work/chat decision."""
    if not isinstance(text, str):
        raise TypeError("route correction must be text")
    normalized = text.strip().lower()
    if normalized not in {ExplicitMode.WORK.value, ExplicitMode.CHAT.value}:
        return RouteCorrection(CorrectionStatus.NOT_A_CORRECTION)
    candidates = tuple(pending)
    if any(not isinstance(candidate, PendingRouteRef) for candidate in candidates):
        raise ValueError("pending route candidates must be PendingRouteRef values")
    if not candidates:
        return RouteCorrection(CorrectionStatus.CLARIFY, reason_code="no_pending_route")
    if len(candidates) > 1:
        return RouteCorrection(CorrectionStatus.CLARIFY, reason_code="ambiguous_pending_route")
    current = candidates[0]
    return RouteCorrection(
        CorrectionStatus.APPLIED,
        route_id=current.route_id,
        revision=current.revision,
        mode=ExplicitMode(normalized),
    )
