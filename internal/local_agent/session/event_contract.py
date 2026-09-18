"""Executable validation/building for the durable Session Hub event contract."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from jsonschema import Draft202012Validator, FormatChecker


_SCHEMA_PATH = Path(__file__).resolve().parents[2] / "docs" / "session-contract" / "v1" / "events.schema.json"


class EventContractError(ValueError):
    """Raised when a durable event does not satisfy the reviewed v1 contract."""


def _load_schema() -> dict[str, Any]:
    return json.loads(_SCHEMA_PATH.read_text(encoding="utf-8"))


_SCHEMA = _load_schema()
_VALIDATOR = Draft202012Validator(_SCHEMA, format_checker=FormatChecker())


def validate_event(envelope: dict[str, Any]) -> None:
    """Validate one complete durable envelope before it can be persisted/published."""
    errors = sorted(_VALIDATOR.iter_errors(envelope), key=lambda e: tuple(str(p) for p in e.absolute_path))
    if not errors:
        return
    first = errors[0]
    path = ".".join(str(part) for part in first.absolute_path) or "<root>"
    raise EventContractError(f"invalid session event at {path}: {first.message}")


def build_event(
    *,
    stream_id: str,
    sequence: int,
    producer_epoch: str,
    kind: str,
    payload: dict[str, Any],
    session_id: str | None = None,
    task_id: str | None = None,
    event_id: str | None = None,
    occurred_utc: str | None = None,
) -> dict[str, Any]:
    """Build and validate a durable v1 event envelope.

    IDs are strings at the boundary because the JSON contract owns their wire form.
    UUID parsing here makes malformed caller-supplied IDs fail before schema traversal.
    """
    for name, value in (("stream_id", stream_id), ("producer_epoch", producer_epoch)):
        try:
            UUID(value)
        except (TypeError, ValueError, AttributeError) as exc:
            raise EventContractError(f"{name} must be a UUID") from exc
    for name, value in (("session_id", session_id), ("task_id", task_id), ("event_id", event_id)):
        if value is None:
            continue
        try:
            UUID(value)
        except (TypeError, ValueError, AttributeError) as exc:
            raise EventContractError(f"{name} must be a UUID") from exc
    if sequence < 1:
        raise EventContractError("sequence must be positive")

    envelope: dict[str, Any] = {
        "schema_id": "lca.session.events",
        "schema_version": 1,
        "event_id": event_id or str(uuid4()),
        "stream_id": stream_id,
        "stream_kind": "durable",
        "sequence": sequence,
        "producer_epoch": producer_epoch,
        "session_id": session_id,
        "task_id": task_id,
        "occurred_utc": occurred_utc or datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "kind": kind,
        "payload": payload,
    }
    validate_event(envelope)
    return envelope
