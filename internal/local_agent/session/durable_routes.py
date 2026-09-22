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
    """Commit and recover typed route provenance for one durable event stream."""

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

    def pending_acceptance(
        self,
        *,
        replay_page: int = 1000,
        max_events: int = 20_000,
    ) -> tuple[DurableRouteRef, ...]:
        """Recover unresolved routes that still require explicit user acceptance.

        This is producer-side recovery state, not a presentation projection. It rebuilds
        only from committed events so a future gateway acceptance turn can survive a
        process restart without trusting UI memory. Impossible route history fails closed.
        """
        if not isinstance(replay_page, int) or isinstance(replay_page, bool) or replay_page < 1:
            raise ValueError("route replay page must be a positive integer")
        if not isinstance(max_events, int) or isinstance(max_events, bool) or max_events < 1:
            raise ValueError("route replay event limit must be a positive integer")

        proposals: dict[str, tuple[DurableRouteRef, bytes]] = {}
        order: list[str] = []
        resolved: set[str] = set()
        cursor = 0
        seen = 0

        while True:
            batch = self.service.replay(after=cursor, limit=replay_page)
            if not batch:
                break
            for event in batch:
                seen += 1
                if seen > max_events:
                    raise DurableRouteError(
                        "durable route recovery exceeded its bounded event history"
                    )
                try:
                    sequence = int(event["sequence"])
                except (KeyError, TypeError, ValueError) as exc:
                    raise DurableRouteError("durable route history has an invalid sequence") from exc
                if sequence <= cursor:
                    raise DurableRouteError("durable route replay is not strictly increasing")
                cursor = sequence

                kind = event.get("kind")
                if kind == "route.proposed":
                    payload = event.get("payload")
                    if not isinstance(payload, dict):
                        raise DurableRouteError("durable route proposal payload is malformed")
                    if not isinstance(payload.get("requires_acceptance"), bool):
                        raise DurableRouteError(
                            "durable route proposal acceptance flag is malformed"
                        )
                    try:
                        revision = payload["revision"]
                        if (
                            not isinstance(revision, int)
                            or isinstance(revision, bool)
                            or revision != 0
                        ):
                            raise ValueError("proposal revision must be zero")
                        ref = DurableRouteRef(
                            route_id=payload["route_id"],
                            revision=revision,
                            turn_ref=payload["turn_ref"],
                            skill=_optional_skill(payload.get("skill")),
                            requires_acceptance=payload["requires_acceptance"],
                        )
                        fingerprint = _canonical_bytes(payload)
                    except (KeyError, TypeError, ValueError) as exc:
                        raise DurableRouteError("durable route proposal is malformed") from exc

                    current = proposals.get(ref.route_id)
                    if current is not None:
                        if ref.route_id in resolved or current[1] != fingerprint:
                            raise DurableRouteError(
                                "durable route proposal identity was reused inconsistently"
                            )
                        # Exact replay of the same deterministic proposal is idempotent.
                        continue
                    proposals[ref.route_id] = (ref, fingerprint)
                    order.append(ref.route_id)
                    continue

                if kind != "route.resolved":
                    continue
                payload = event.get("payload")
                if not isinstance(payload, dict):
                    raise DurableRouteError("durable route resolution payload is malformed")
                route_id = payload.get("route_id")
                current = proposals.get(route_id)
                if current is None:
                    raise DurableRouteError("durable route resolution has no proposal")
                if route_id in resolved:
                    raise DurableRouteError("durable route was resolved more than once")
                resolution = payload.get("resolution")
                if resolution not in {"accepted", "corrected", "rejected"}:
                    raise DurableRouteError("durable route resolution is unknown")
                revision = payload.get("revision")
                if not isinstance(revision, int) or isinstance(revision, bool):
                    raise DurableRouteError("durable route resolution revision is malformed")
                expected = current[0].revision + (1 if resolution == "corrected" else 0)
                if revision != expected:
                    raise DurableRouteError("durable route resolution revision is inconsistent")
                resolved.add(route_id)

        return tuple(
            proposals[route_id][0]
            for route_id in order
            if route_id not in resolved and proposals[route_id][0].requires_acceptance
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