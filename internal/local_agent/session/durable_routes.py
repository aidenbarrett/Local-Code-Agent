"""Durable Session Hub route provenance for trusted work intents.

Routing and admission are separate facts. This adapter records the reviewed
``route.proposed`` / ``route.resolved`` contract without giving model prose any
policy authority. It accepts an already-validated ``TaskIntent`` and emits fixed,
deterministic explanations from provenance rather than persisting free-form model
reasoning as a durable fact.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from uuid import UUID, uuid5

from .contracts import RouteSource
from .intents import TaskIntent


class DurableRouteError(RuntimeError):
    """A route transition cannot be represented truthfully."""


@dataclass(frozen=True)
class DurableRouteRef:
    route_id: str
    revision: int
    turn_ref: dict[str, object]
    skill: str | None
    requires_acceptance: bool

    def __post_init__(self) -> None:
        UUID(self.route_id)
        if not isinstance(self.revision, int) or isinstance(self.revision, bool) or self.revision < 0:
            raise ValueError("route revision must be a non-negative integer")
        object.__setattr__(self, "turn_ref", dict(self.turn_ref))


def _canonical_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def _optional_skill(value: str | None) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ValueError("route skill must be nonempty when present")
    if len(value) > 128:
        raise ValueError("route skill exceeds size limit")
    return value


def _proposal_explanation(intent: TaskIntent) -> str:
    if intent.origin == RouteSource.RULE:
        return f"deterministic rule {intent.rule_id} selected repository work"
    if intent.origin == RouteSource.USER_DIRECT:
        return "explicit user work request selected repository work"
    if intent.origin == RouteSource.MODEL_PROPOSAL:
        return "model proposed repository work; explicit user acceptance required"
    raise DurableRouteError(f"unsupported route source {intent.origin!r}")


class DurableRouteEvents:
    """Commit typed route provenance to one ``DurableSessionService`` stream."""

    def __init__(self, service) -> None:
        self.service = service
        # Route UUIDs are stream-scoped so a persisted route cannot be confused
        # with a proposal from another Session Hub event stream.
        UUID(service.stream_id)

    def propose(self, intent: TaskIntent, *, skill: str | None = None) -> DurableRouteRef:
        if not isinstance(intent, TaskIntent):
            raise TypeError("durable route proposal requires a TaskIntent")
        skill = _optional_skill(skill)
        requires_acceptance = intent.origin == RouteSource.MODEL_PROPOSAL
        identity = {
            "turn_ref": intent.turn_ref,
            "objective_sha256": hashlib.sha256(intent.objective.encode("utf-8")).hexdigest(),
            "source": intent.origin.value,
            "rule_id": intent.rule_id,
            "reference_ids": list(intent.proposed_reference_ids),
            "skill": skill,
        }
        route_id = str(uuid5(UUID(self.service.stream_id), _canonical_bytes(identity).decode("utf-8")))
        payload = {
            "route_id": route_id,
            "revision": 0,
            "turn_ref": dict(intent.turn_ref),
            "source": intent.origin.value,
            "mode": "work",
            "skill": skill,
            "rule_id": intent.rule_id,
            "explanation": _proposal_explanation(intent),
            "requires_acceptance": requires_acceptance,
        }
        receipt = self.service.append("route.proposed", payload)
        receipt.wait(30)
        return DurableRouteRef(
            route_id=route_id,
            revision=0,
            turn_ref=intent.turn_ref,
            skill=skill,
            requires_acceptance=requires_acceptance,
        )

    def resolve(
        self,
        route: DurableRouteRef,
        *,
        resolution: str = "accepted",
        source: str,
        mode: str = "work",
        skill: str | None = None,
    ) -> DurableRouteRef:
        if not isinstance(route, DurableRouteRef):
            raise TypeError("route resolution requires a DurableRouteRef")
        if resolution not in {"accepted", "corrected", "rejected"}:
            raise ValueError("unknown route resolution")
        if source not in {"user", "controller"}:
            raise ValueError("route resolution source must be user or controller")
        if mode not in {"conversation", "work", "clarify"}:
            raise ValueError("unknown resolved route mode")
        skill = route.skill if skill is None and mode == "work" else _optional_skill(skill)
        if mode != "work" and skill is not None:
            raise ValueError("non-work route resolution cannot carry a skill")
        if route.requires_acceptance and resolution == "accepted" and source != "user":
            raise DurableRouteError("model-proposed work requires explicit user acceptance")
        if resolution in {"corrected", "rejected"} and source != "user":
            raise DurableRouteError("route correction/rejection must come from the user")

        revision = route.revision + 1 if resolution == "corrected" else route.revision
        receipt = self.service.append(
            "route.resolved",
            {
                "route_id": route.route_id,
                "revision": revision,
                "resolution": resolution,
                "mode": mode,
                "source": source,
                "skill": skill,
            },
        )
        receipt.wait(30)
        return DurableRouteRef(
            route_id=route.route_id,
            revision=revision,
            turn_ref=route.turn_ref,
            skill=skill,
            requires_acceptance=False if resolution != "rejected" else route.requires_acceptance,
        )