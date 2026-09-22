"""Durable task admission for fixed-watch runs before any watch effect begins.

Watch configuration and run identity are controller-owned facts, not conversation routes.
This adapter turns one preallocated ``(job_id, run_id)`` into the existing schema-declared
``origin={kind: watch, ...}`` task admission. It intentionally stops at admission: a later
composition slice may start the admitted task, but no source observation, model call or
command effect is permitted here.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import json
from uuid import UUID, uuid5

from ..watch.job_store import StoredWatchJob
from .contracts import MAX_MESSAGE_CHARS
from .durable_watch import watch_schedule_revision
from .session_event_service import DurableSessionService
from .task_admission import execution_contract_sha256, repository_id


class DurableWatchAdmissionError(RuntimeError):
    """A watch run cannot be admitted truthfully to this Session Hub."""


@dataclass(frozen=True)
class WatchTaskAdmission:
    task_id: str
    request_id: str
    created: bool


def _canonical_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def _canonical_uuid(name: str, value: str) -> str:
    try:
        return str(UUID(value))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be a UUID") from exc


def _optional_skill(value: str | None) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ValueError("watch admission skill must be nonempty when present")
    if len(value) > 128:
        raise ValueError("watch admission skill exceeds size limit")
    return value


def _deadline_utc(timeout_seconds: int) -> str:
    deadline = datetime.now(timezone.utc) + timedelta(seconds=timeout_seconds)
    return deadline.isoformat(timespec="seconds").replace("+00:00", "Z")


class DurableWatchTaskAdmission:
    """Commit immutable watch-run task identity without starting task effects."""

    def __init__(self, service: DurableSessionService, controller) -> None:
        if not isinstance(service, DurableSessionService):
            raise TypeError("watch task admission requires DurableSessionService")
        for name in ("repo", "allow_execution", "context_budget_tokens"):
            if not hasattr(controller, name):
                raise TypeError(f"watch task admission requires controller.{name}")
        self.service = service
        self.controller = controller

    def admit(
        self,
        record: StoredWatchJob,
        *,
        run_id: str,
        task: str,
        skill: str | None = None,
    ) -> WatchTaskAdmission:
        if not isinstance(record, StoredWatchJob):
            raise TypeError("watch task admission requires StoredWatchJob")
        run_id = _canonical_uuid("watch run id", run_id)
        if not isinstance(task, str) or not task.strip():
            raise ValueError("watch task must be nonempty")
        if len(task) > MAX_MESSAGE_CHARS:
            raise ValueError("watch task exceeds size limit")
        skill = _optional_skill(skill)

        configured_repository = repository_id(self.controller.repo)
        if record.job.repository_id != configured_repository:
            raise DurableWatchAdmissionError(
                "watch job repository identity does not match the admitted controller"
            )

        contract_sha256 = execution_contract_sha256(self.controller)
        request_id = f"watch:{record.job.job_id}:{run_id}"
        request_payload = {
            "job_id": record.job.job_id,
            "run_id": run_id,
            "job_revision": record.job.revision_sha256,
            "configuration_version": record.version,
            "schedule_revision": watch_schedule_revision(record),
            "repository_id": configured_repository,
            "contract_sha256": contract_sha256,
            "skill": skill,
            "task": task,
        }
        request_bytes = _canonical_bytes(request_payload)
        payload_sha256 = hashlib.sha256(request_bytes).hexdigest()
        request_artifact_id = uuid5(
            UUID(self.service.stream_id),
            request_id + ":request",
        )
        receipt = self.service.submit_task(
            request_id=request_id,
            payload_sha256=payload_sha256,
            admission_payload={
                "origin": {
                    "kind": "watch",
                    "job_id": record.job.job_id,
                    "run_id": run_id,
                },
                "request_ref": {
                    "artifact_id": str(request_artifact_id),
                    "sha256": payload_sha256,
                    "media_type": "application/vnd.lca.watch-task-request+json",
                    "size_bytes": len(request_bytes),
                    "availability": "retained",
                },
                "contract_sha256": contract_sha256,
                "repository_id": configured_repository,
                "skill": skill,
                "execution_epoch": 0,
                "deadline_utc": _deadline_utc(record.job.timeout_seconds),
            },
            request_bytes=request_bytes,
        )
        receipt.wait(30)
        if receipt.task_id is None or receipt.created is None:
            raise DurableWatchAdmissionError("durable watch admission returned no committed identity")
        return WatchTaskAdmission(
            task_id=receipt.task_id,
            request_id=request_id,
            created=receipt.created,
        )


__all__ = [
    "DurableWatchAdmissionError",
    "DurableWatchTaskAdmission",
    "WatchTaskAdmission",
]
