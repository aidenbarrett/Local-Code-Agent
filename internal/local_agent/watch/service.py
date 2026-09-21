"""Lifecycle service over durable fixed-watch configuration and run history.

This is the controller-facing watch API. It does not schedule itself: an OS scheduler or
CLI may invoke ``run_job(..., trigger=SCHEDULED)``. Scheduled execution is refused unless
the durable job is both enabled and has schedule configuration. Manual runs are allowed
for disabled jobs so operators can test a procedure before enabling it.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from threading import Lock
from typing import Callable

from .fixed_watch_runner import FixedWatchRun, FixedWatchRunner
from .job_store import SQLiteWatchJobStore, StoredWatchJob
from .jobs import FixedWatchJob
from .watch_run_store import SQLiteWatchRunStore


class WatchTrigger(str, Enum):
    MANUAL = "manual"
    SCHEDULED = "scheduled"


class WatchLifecycleError(RuntimeError):
    pass


class WatchDisabled(WatchLifecycleError):
    pass


class WatchOverlap(WatchLifecycleError):
    pass


@dataclass(frozen=True)
class WatchServiceRun:
    configuration_version: int
    trigger: WatchTrigger
    run: FixedWatchRun


RunnerFactory = Callable[[StoredWatchJob], FixedWatchRunner]


class FixedWatchService:
    """Durable watch configuration, execution and history with overlap fencing."""

    def __init__(
        self,
        job_store: SQLiteWatchJobStore,
        run_store: SQLiteWatchRunStore,
        *,
        runner_factory: RunnerFactory,
    ) -> None:
        self.job_store = job_store
        self.run_store = run_store
        self.runner_factory = runner_factory
        self._lock = Lock()
        self._active_job_ids: set[str] = set()

    def add_job(
        self,
        job: FixedWatchJob,
        *,
        enabled: bool = False,
        schedule_spec: str | None = None,
    ) -> tuple[StoredWatchJob, bool]:
        return self.job_store.create(job, enabled=enabled, schedule_spec=schedule_spec)

    def jobs(self, *, enabled_only: bool = False) -> list[StoredWatchJob]:
        return self.job_store.list_jobs(enabled_only=enabled_only)

    def enable(self, job_id: str, *, expected_version: int) -> StoredWatchJob:
        return self.job_store.set_enabled(job_id, True, expected_version=expected_version)

    def disable(self, job_id: str, *, expected_version: int) -> StoredWatchJob:
        return self.job_store.set_enabled(job_id, False, expected_version=expected_version)

    def set_schedule(
        self,
        job_id: str,
        schedule_spec: str | None,
        *,
        expected_version: int,
    ) -> StoredWatchJob:
        return self.job_store.set_schedule(
            job_id,
            schedule_spec,
            expected_version=expected_version,
        )

    def update_job(self, job: FixedWatchJob, *, expected_version: int) -> StoredWatchJob:
        return self.job_store.update_job(job, expected_version=expected_version)

    def _claim(self, job_id: str) -> None:
        with self._lock:
            if job_id in self._active_job_ids:
                raise WatchOverlap(f"watch job {job_id} already has an active run")
            self._active_job_ids.add(job_id)

    def _release(self, job_id: str) -> None:
        with self._lock:
            if job_id not in self._active_job_ids:
                raise WatchLifecycleError("watch run ownership was lost before release")
            self._active_job_ids.remove(job_id)

    def run_job(
        self,
        job_id: str,
        *,
        trigger: WatchTrigger | str = WatchTrigger.MANUAL,
    ) -> WatchServiceRun:
        trigger = WatchTrigger(trigger)
        record = self.job_store.require(job_id)
        if trigger is WatchTrigger.SCHEDULED:
            if not record.enabled:
                raise WatchDisabled("scheduled watch execution requires an enabled job")
            if record.schedule_spec is None:
                raise WatchDisabled("scheduled watch execution requires durable schedule configuration")

        self._claim(job_id)
        try:
            runner = self.runner_factory(record)
            if not isinstance(runner, FixedWatchRunner):
                raise TypeError("watch runner factory must return FixedWatchRunner")
            completed = runner.run(record.job)
            return WatchServiceRun(
                configuration_version=record.version,
                trigger=trigger,
                run=completed,
            )
        finally:
            self._release(job_id)

    def history(self, job_id: str, *, limit: int = 100):
        self.job_store.require(job_id)
        return self.run_store.history(job_id, limit=limit)

    def latest(self, job_id: str):
        self.job_store.require(job_id)
        return self.run_store.latest_attempt(job_id)

    def active(self, job_id: str) -> bool:
        with self._lock:
            return job_id in self._active_job_ids
