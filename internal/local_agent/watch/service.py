"""Lifecycle service over durable fixed-watch configuration and run history.

This is the controller-facing watch API. It does not schedule itself: an OS scheduler or
CLI may invoke ``run_job(..., trigger=SCHEDULED)``. Scheduled execution is refused unless
the durable job is both enabled and has schedule configuration. Manual runs are allowed
for disabled jobs so operators can test a procedure before enabling it.

Optional lifecycle hooks observe the already-owned run boundary without importing any
Session Hub concepts into the watch package. They can durably fence a preallocated run
before runner construction/effects, mark it running before ``FixedWatchRunner.run``, and
project completion or fail-closed interruption afterwards.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from threading import Lock
from typing import Callable, Protocol
from uuid import uuid4

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
    run_id: str
    run: FixedWatchRun


class WatchRunLifecycle(Protocol):
    """Side-band lifecycle observer around one deterministic fixed-watch run.

    ``admit`` is the execution fence: it runs after the immutable UUID is allocated but
    before runner construction, source observation or procedure execution. ``start`` runs
    after construction but before ``runner.run``. ``failed`` is called only when admission
    succeeded and the deterministic run did not return a ``WatchServiceRun``. Errors from
    ``completed`` are never compensated through ``failed`` because the run already exists.
    """

    def admit(self, record: StoredWatchJob, run_id: str) -> None: ...

    def start(self, record: StoredWatchJob, run_id: str) -> None: ...

    def completed(self, record: StoredWatchJob, run: WatchServiceRun) -> None: ...

    def failed(self, record: StoredWatchJob, run_id: str, error: BaseException) -> None: ...


# The immutable run UUID is supplied before construction so a factory that bridges
# into Session Hub task admission can use origin={kind: watch, job_id, run_id}
# before any source observation or command effect begins.
RunnerFactory = Callable[[StoredWatchJob, str], FixedWatchRunner]


class FixedWatchService:
    """Durable watch configuration, execution and history with overlap fencing."""

    def __init__(
        self,
        job_store: SQLiteWatchJobStore,
        run_store: SQLiteWatchRunStore,
        *,
        runner_factory: RunnerFactory,
        run_lifecycle: WatchRunLifecycle | None = None,
    ) -> None:
        self.job_store = job_store
        self.run_store = run_store
        self.runner_factory = runner_factory
        self.run_lifecycle = run_lifecycle
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
            run_id = str(uuid4())
            lifecycle = self.run_lifecycle
            admitted = False
            if lifecycle is not None:
                # This call is the authority fence. No runner construction, source
                # observation or watch procedure is permitted before it returns.
                lifecycle.admit(record, run_id)
                admitted = True

            try:
                runner = self.runner_factory(record, run_id)
                if not isinstance(runner, FixedWatchRunner):
                    raise TypeError("watch runner factory must return FixedWatchRunner")
                if lifecycle is not None:
                    lifecycle.start(record, run_id)
                fixed_run = runner.run(record.job, run_id=run_id)
                if fixed_run.current.run_id != run_id:
                    raise WatchLifecycleError("watch runner changed the preallocated run identity")
            except BaseException as exc:
                if admitted and lifecycle is not None:
                    try:
                        lifecycle.failed(record, run_id, exc)
                    except BaseException as lifecycle_exc:
                        try:
                            exc.add_note(
                                "watch lifecycle failure handling also failed: "
                                f"{type(lifecycle_exc).__name__}: {lifecycle_exc}"
                            )
                        except AttributeError:  # pragma: no cover - compatibility only
                            pass
                raise

            completed = WatchServiceRun(
                configuration_version=record.version,
                trigger=trigger,
                run_id=run_id,
                run=fixed_run,
            )
            if lifecycle is not None:
                # Once a fixed run exists, completion/projection errors must surface as
                # such. Calling failed() here could double-finalize an already completed
                # durable task or fabricate a second interpretation of the same run.
                lifecycle.completed(record, completed)
            return completed
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
