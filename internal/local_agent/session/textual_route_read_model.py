"""Read-only projection of durable route provenance for Session Hub presentation.

Route events are controller-owned facts, not model confidence. This module projects the
latest durable route state without granting acceptance, admission, tool or verdict
authority to the UI.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable


class RouteReadModelError(RuntimeError):
    """Durable route history is internally inconsistent."""


@dataclass(frozen=True)
class RouteSnapshot:
    route_id: str
    revision: int
    source: str
    mode: str
    skill: str | None
    rule_id: str | None
    explanation: str
    requires_acceptance: bool
    resolution: str | None = None
    resolution_source: str | None = None

    @property
    def pending(self) -> bool:
        return self.resolution is None and self.requires_acceptance

    def summary(self) -> str:
        source = {
            "rule": "rule",
            "user_direct": "user",
            "model_proposal": "model proposal",
        }.get(self.source, self.source)
        parts = [source, self.mode]
        if self.skill:
            parts.append(self.skill)
        if self.pending:
            parts.append("awaiting acceptance")
        elif self.resolution:
            parts.append(self.resolution)
        return " · ".join(parts)


def _proposal_snapshot(payload: dict) -> RouteSnapshot:
    return RouteSnapshot(
        route_id=payload["route_id"],
        revision=int(payload["revision"]),
        source=payload["source"],
        mode=payload["mode"],
        skill=payload["skill"],
        rule_id=payload["rule_id"],
        explanation=payload["explanation"],
        requires_acceptance=bool(payload["requires_acceptance"]),
    )


def project_routes(events: Iterable[dict]) -> tuple[RouteSnapshot, ...]:
    """Project route events in durable sequence order and fail on impossible transitions."""
    routes: dict[str, RouteSnapshot] = {}
    order: list[str] = []
    last_sequence = 0

    for event in events:
        sequence = int(event.get("sequence", 0))
        if sequence <= last_sequence:
            raise RouteReadModelError("route projection requires strictly increasing sequence")
        last_sequence = sequence
        kind = event.get("kind")
        if kind == "route.proposed":
            proposed = _proposal_snapshot(event["payload"])
            current = routes.get(proposed.route_id)
            if current is None:
                routes[proposed.route_id] = proposed
                order.append(proposed.route_id)
                continue
            # The producer derives a stable route UUID from the exact typed intent, so an
            # exact repeated proposal is idempotent presentation input. A changed proposal
            # under the same UUID, or a proposal after resolution, is inconsistent history.
            if current.resolution is not None or current != proposed:
                raise RouteReadModelError("route proposal identity was reused inconsistently")
            continue

        if kind != "route.resolved":
            continue
        payload = event["payload"]
        route_id = payload["route_id"]
        current = routes.get(route_id)
        if current is None:
            raise RouteReadModelError("route resolution has no durable proposal")
        revision = int(payload["revision"])
        resolution = payload["resolution"]
        expected_revision = current.revision + (1 if resolution == "corrected" else 0)
        if revision != expected_revision:
            raise RouteReadModelError("route resolution revision is inconsistent")
        if current.resolution is not None:
            raise RouteReadModelError("route was resolved more than once")
        routes[route_id] = RouteSnapshot(
            route_id=current.route_id,
            revision=revision,
            source=current.source,
            mode=payload["mode"],
            skill=payload["skill"],
            rule_id=current.rule_id,
            explanation=current.explanation,
            requires_acceptance=False,
            resolution=resolution,
            resolution_source=payload["source"],
        )

    return tuple(routes[route_id] for route_id in order)


def latest_route_snapshot(events: Iterable[dict]) -> RouteSnapshot | None:
    """Return the latest typed durable route state for presentation decisions."""
    routes = project_routes(events)
    return None if not routes else routes[-1]


def latest_route_summary(events: Iterable[dict]) -> str | None:
    latest = latest_route_snapshot(events)
    return None if latest is None else latest.summary()


__all__ = [
    "RouteReadModelError",
    "RouteSnapshot",
    "latest_route_snapshot",
    "latest_route_summary",
    "project_routes",
]
