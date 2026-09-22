"""Durable Session Hub lifecycle around deterministic fixed-watch execution.

This adapter is intentionally not a worker or scheduler. ``FixedWatchService`` owns
lifecycle policy, overlap fencing and the immutable run UUID; ``FixedWatchRunner`` owns
the deterministic procedure. This module only binds that run to the existing durable task
lifecycle before effects, converts an explicit deterministic verifier result into the
canonical task terminal record, and projects the completed run into the watch read model.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from threading import Lock
from typing import Callable

from ..watch.job_store import StoredWatchJob
from ..watch.service import WatchServiceRun
from .contracts import TaskOutcome, TaskResult
from .durable_watch import DurableWatchEvents, watch_schedule_revision
from .results import verdict_block_from_task_result
from .session_event_service import DurableSessionService, DurableTaskExecutor
from .task_admission import execution_contract_sha256
from .watch_read_model import WatchReadModelError, project_watch
from .watch_task_admission import DurableWatchTaskAdmission


class DurableWatchLifecycleError(RuntimeError):
    """A fixed-watch run cannot be represented truthfully in the durable task lifecycle."""


WatchVerifier = Callable[[StoredWatchJob, WatchServiceRun, str], TaskResult]


@dataclass
class _RunState:
    task_id: str
    started_utc: str | None = None


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _finalize_result(
    service: DurableSessionService,
    result: TaskResult,
    *,
    scope: str,
) -> None:
    """Use the same result bytes/verdict projection as the conversation task executor."""
    result_ref, result_bytes = DurableTaskExecutor._result_artifact(result)
    status = result.projection.terminal_state.value
    verdict = verdict_block_from_task_result(result, scope=scope)
    receipt = service.finalize_task(
        result.task_id,
        verdict_payload={
            "completion": {
                "task_id": result.task_id,
                "status": status,
                "verdict_block": verdict.as_payload(),
                "worker_artifact_ref": None,
                "result_ref": result_ref,
            }
        },
        closed_payload={
            "status": status,
            "result_ref": result_ref,
            "cleanup": "unknown" if status == "unknown" else "not_needed",
        },
        result_bytes=result_bytes,
    )
    receipt.wait(30)


class DurableWatchTaskLifecycle:
    """Bind one fixed-watch run to one already-schema-defined durable task lifecycle."""

    def __init__(
        self,
        service: DurableSessionService,
        controller,
        *,
        verifier: WatchVerifier,
    ) -> None:
        if not isinstance(service, DurableSessionService):
            raise TypeError("durable watch lifecycle requires DurableSessionService")
        if not callable(verifier):
            raise TypeError("durable watch lifecycle requires a deterministic verifier")
        self.service = service
        self.admission = DurableWatchTaskAdmission(service, controller)
        self.execution_contract_sha256 = execution_contract_sha256(controller)
        self.verifier = verifier
        self.events = DurableWatchEvents(service)
        self._lock = Lock()
        self._runs: dict[str, _RunState] = {}

    def _require_matching_watch_state(self, record: StoredWatchJob) -> None:
        try:
            snapshot = project_watch(self.service.replay(), record.job.job_id)
        except WatchReadModelError as exc:
            raise DurableWatchLifecycleError(
                "matching durable watch state must exist before task admission"
            ) from exc
        if snapshot.job_revision != record.job.revision_sha256:
            raise DurableWatchLifecycleError("durable watch state has a stale procedure revision")
        if snapshot.schedule_revision != watch_schedule_revision(record):
            raise DurableWatchLifecycleError("durable watch state has a stale schedule revision")

    def _state(self, run_id: str) -> _RunState:
        with self._lock:
            state = self._runs.get(run_id)
        if state is None:
            raise DurableWatchLifecycleError("watch run has no admitted durable task identity")
        return state

    def admit(self, record: StoredWatchJob, run_id: str) -> None:
        self._require_matching_watch_state(record)
        # Check local ownership before the durable write so that, once task.admitted is
        # committed, the remaining path is just an in-memory assignment and cannot leave
        # a task orphaned because of a later duplicate-state validation error.
        with self._lock:
            if run_id in self._runs:
                raise DurableWatchLifecycleError("watch lifecycle reused an active run identity")
        admitted = self.admission.admit(
            record,
            run_id=run_id,
            task=f"Run fixed watch procedure: {record.job.display_name}",
        )
        if not admitted.created:
            raise DurableWatchLifecycleError(
                "watch run was already admitted; refusing to replay deterministic effects"
            )
        with self._lock:
            self._runs[run_id] = _RunState(task_id=admitted.task_id)

    def start(self, record: StoredWatchJob, run_id: str) -> None:
        state = self._state(run_id)
        receipt = self.service.append(
            "task.state_changed",
            {
                "previous": "admitted",
                "current": "running",
                "reason_code": "requested",
                "execution_epoch": 0,
            },
            task_id=state.task_id,
        )
        receipt.wait(30)
        started = _utc_now()
        with self._lock:
            current = self._runs.get(run_id)
            if current is None or current.task_id != state.task_id:
                raise DurableWatchLifecycleError("watch task identity changed while starting")
            if current.started_utc is not None:
                raise DurableWatchLifecycleError("watch task was started more than once")
            current.started_utc = started

    def failed(self, record: StoredWatchJob, run_id: str, error: BaseException) -> None:
        """Terminalize an admitted run that never produced a durable fixed-watch attempt."""
        state = self._state(run_id)
        result = TaskResult(
            state.task_id,
            TaskOutcome.NO_VERDICT,
            f"Fixed watch stopped before a durable run result: {type(error).__name__}.",
            False,
            verification_ran=False,
        )
        try:
            _finalize_result(
                self.service,
                result,
                scope="fixed-watch execution stopped before a durable run result",
            )
        finally:
            with self._lock:
                self._runs.pop(run_id, None)

    def completed(self, record: StoredWatchJob, run: WatchServiceRun) -> None:
        state = self._state(run.run_id)
        if state.started_utc is None:
            raise DurableWatchLifecycleError("completed watch task was never durably started")
        if run.run.current.run_id != run.run_id:
            raise DurableWatchLifecycleError("completed watch run identity changed")

        verification_error: BaseException | None = None
        try:
            try:
                if run.run.current.execution_contract_sha256 != self.execution_contract_sha256:
                    raise DurableWatchLifecycleError(
                        "fixed-watch attempt execution contract differs from durable admission"
                    )
                result = self.verifier(record, run, state.task_id)
                if not isinstance(result, TaskResult):
                    raise TypeError("watch verifier must return TaskResult")
                if result.task_id != state.task_id:
                    raise DurableWatchLifecycleError(
                        "watch verifier returned a result for another durable task"
                    )
                if result.verified_at_completion and not run.run.current.complete:
                    raise DurableWatchLifecycleError(
                        "incomplete fixed-watch attempt cannot verify a durable task"
                    )
            except BaseException as exc:
                verification_error = exc
                result = TaskResult(
                    state.task_id,
                    TaskOutcome.NO_VERDICT,
                    f"Fixed watch verification did not establish a verdict: {type(exc).__name__}.",
                    False,
                    verification_ran=True,
                )

            finished_utc = _utc_now()
            _finalize_result(
                self.service,
                result,
                scope="deterministic fixed-watch procedure and verification",
            )
            self.events.record_run(
                record,
                run,
                result,
                started_utc=state.started_utc,
                finished_utc=finished_utc,
            )
        finally:
            with self._lock:
                self._runs.pop(run.run_id, None)

        if verification_error is not None:
            raise DurableWatchLifecycleError(
                "fixed-watch verification failed; durable task closed as NO_VERDICT"
            ) from verification_error


__all__ = [
    "DurableWatchLifecycleError",
    "DurableWatchTaskLifecycle",
    "WatchVerifier",
]
