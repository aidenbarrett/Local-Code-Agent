"""Durable Session Hub projection for fixed-watch configuration and completed runs.

The fixed-watch stores remain the authority for job configuration and attempt data. This
adapter emits the reviewed Session Hub event vocabulary so the Textual watch pane can be
rebuilt after restart. It never converts an incomplete attempt into success and it refuses
to record a run until matching durable watch state already exists in the session stream.
"""
from __future__ import annotations

import hashlib
import json

from ..watch.delta import ComparisonStatus
from ..watch.job_store import StoredWatchJob
from ..watch.service import WatchServiceRun
from .contracts import TaskResult
from .watch_read_model import WatchReadModelError, project_watch


class DurableWatchError(RuntimeError):
    """Watch state cannot be projected truthfully into the durable Session Hub."""


def _canonical_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def watch_schedule_revision(record: StoredWatchJob) -> str:
    """Hash only lifecycle/schedule configuration, separate from procedure revision."""
    if not isinstance(record, StoredWatchJob):
        raise TypeError("watch schedule revision requires StoredWatchJob")
    value = {
        "version": record.version,
        "enabled": record.enabled,
        "schedule_spec": record.schedule_spec,
    }
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


class DurableWatchEvents:
    """Publish validated watch state/run facts through one DurableSessionService."""

    def __init__(self, service) -> None:
        self.service = service

    def record_state(
        self,
        record: StoredWatchJob,
        *,
        state: str | None = None,
        reason_code: str = "requested",
        due_utc: str | None = None,
        next_due_utc: str | None = None,
    ) -> None:
        if not isinstance(record, StoredWatchJob):
            raise TypeError("durable watch state requires StoredWatchJob")
        resolved_state = state or ("enabled" if record.enabled else "disabled")
        if resolved_state not in {"enabled", "paused", "disabled", "skipped"}:
            raise ValueError("unsupported durable watch state")
        receipt = self.service.append(
            "watch.state_changed",
            {
                "job_id": record.job.job_id,
                "job_revision": record.job.revision_sha256,
                "schedule_revision": watch_schedule_revision(record),
                "state": resolved_state,
                "reason_code": reason_code,
                "due_utc": due_utc,
                "next_due_utc": next_due_utc,
            },
        )
        receipt.wait(30)

    def record_run(
        self,
        record: StoredWatchJob,
        completed: WatchServiceRun,
        task_result: TaskResult,
        *,
        started_utc: str,
        finished_utc: str,
        due_utc: str | None = None,
        next_due_utc: str | None = None,
    ) -> None:
        if not isinstance(record, StoredWatchJob):
            raise TypeError("durable watch run requires StoredWatchJob")
        if not isinstance(completed, WatchServiceRun):
            raise TypeError("durable watch run requires WatchServiceRun")
        if not isinstance(task_result, TaskResult):
            raise TypeError("durable watch run requires typed TaskResult")
        current = completed.run.current
        if current.job_id != record.job.job_id:
            raise DurableWatchError("watch run belongs to another job")
        if current.job_revision != record.job.revision_sha256:
            raise DurableWatchError("watch run used another procedure revision")
        if current.run_id != completed.run_id:
            raise DurableWatchError("watch service run identity disagrees with persisted attempt")
        if task_result.verified_at_completion and not current.complete:
            raise DurableWatchError("incomplete watch attempt cannot carry a verified task result")

        expected_schedule = watch_schedule_revision(record)
        try:
            snapshot = project_watch(self.service.replay(), record.job.job_id)
        except WatchReadModelError as exc:
            raise DurableWatchError(
                "durable watch state must be recorded before a watch run"
            ) from exc
        if snapshot.job_revision != record.job.revision_sha256:
            raise DurableWatchError("durable watch state has a stale procedure revision")
        if snapshot.schedule_revision != expected_schedule:
            raise DurableWatchError("durable watch state has a stale schedule revision")

        previous = completed.run.previous
        if previous is None:
            comparison = "BASELINE"
            previous_attempt_id = None
            comparison_run_id = None
        else:
            if completed.run.delta is None:
                raise DurableWatchError("non-baseline watch run is missing comparison data")
            comparison = (
                "COMPARABLE"
                if completed.run.delta.status == ComparisonStatus.COMPARABLE
                else "INCOMPARABLE"
            )
            previous_attempt_id = previous.run_id
            comparison_run_id = previous.run_id

        projection = task_result.projection
        receipt = self.service.append(
            "watch.run_recorded",
            {
                "job_id": record.job.job_id,
                "job_revision": record.job.revision_sha256,
                "schedule_revision": expected_schedule,
                "execution_contract_sha256": current.execution_contract_sha256,
                "run_id": completed.run_id,
                "task_id": task_result.task_id,
                "due_utc": due_utc,
                "started_utc": started_utc,
                "finished_utc": finished_utc,
                "status": projection.terminal_state.value,
                "verdict": projection.verdict.value,
                "previous_attempt_id": previous_attempt_id,
                "comparison_run_id": comparison_run_id,
                "comparison": comparison,
                # Generic fixed-watch observations do not imply the domain-specific
                # failure/test/path counters in the v1 schema. Leave them unknown
                # until a procedure supplies a typed delta artifact.
                "counts": None,
                "delta_ref": None,
                "next_due_utc": next_due_utc,
            },
            task_id=task_result.task_id,
        )
        receipt.wait(30)


__all__ = ["DurableWatchError", "DurableWatchEvents", "watch_schedule_revision"]
