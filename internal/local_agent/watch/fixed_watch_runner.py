"""Deterministic execution core for fixed watch jobs.

Scheduling is intentionally outside this module. One call performs one fixed procedure:
observe source identity, execute a caller-supplied deterministic procedure, persist the
typed attempt, then compare it with the previous durable attempt. No model participates
and no exception is converted into a successful/incomplete observation implicitly.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable
from uuid import uuid4

from .delta import WatchAttempt, WatchDelta, compare_attempts
from .jobs import FixedWatchJob
from .watch_run_store import SQLiteWatchRunStore


def _require_sha256(name: str, value: str) -> None:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(ch not in "0123456789abcdef" for ch in value)
    ):
        raise ValueError(f"{name} must be lowercase SHA-256")


@dataclass(frozen=True)
class SourceObservation:
    source_head: str
    source_digest: str

    def __post_init__(self) -> None:
        if not isinstance(self.source_head, str) or not self.source_head.strip():
            raise ValueError("source_head must be nonempty")
        _require_sha256("source_digest", self.source_digest)


@dataclass(frozen=True)
class WatchExecutionResult:
    result_schema: str
    complete: bool
    observations: tuple[tuple[str, str], ...]

    def __post_init__(self) -> None:
        if not isinstance(self.result_schema, str) or not self.result_schema.strip():
            raise ValueError("result_schema must be nonempty")
        if not isinstance(self.complete, bool):
            raise ValueError("complete must be boolean")
        values = tuple(self.observations)
        keys = [key for key, _value in values]
        if any(not isinstance(key, str) or not key.strip() for key in keys):
            raise ValueError("watch observation keys must be nonempty strings")
        if any(not isinstance(value, str) for _key, value in values):
            raise ValueError("watch observation values must be strings")
        if len(set(keys)) != len(keys):
            raise ValueError("watch observation keys must be unique")
        object.__setattr__(self, "observations", tuple(sorted(values)))


@dataclass(frozen=True)
class FixedWatchRun:
    sequence: int
    current: WatchAttempt
    previous: WatchAttempt | None
    delta: WatchDelta | None

    @property
    def baseline(self) -> bool:
        return self.previous is None


SourceObserver = Callable[[FixedWatchJob], SourceObservation]
WatchExecutor = Callable[[FixedWatchJob, SourceObservation], WatchExecutionResult]


class FixedWatchRunner:
    """Execute one configured watch procedure and persist exactly one attempt."""

    def __init__(
        self,
        store: SQLiteWatchRunStore,
        *,
        execution_contract_sha256: str,
        observe_source: SourceObserver,
        execute: WatchExecutor,
    ) -> None:
        _require_sha256("execution_contract_sha256", execution_contract_sha256)
        self.store = store
        self.execution_contract_sha256 = execution_contract_sha256
        self.observe_source = observe_source
        self.execute = execute

    def run(self, job: FixedWatchJob) -> FixedWatchRun:
        """Run once; expected failure must be returned as ``complete=False``.

        If source observation or the executor itself raises, no attempt is invented or
        persisted. The caller owns that infrastructure fault and may emit a typed watch
        skip/fault event at the lifecycle layer.
        """
        source = self.observe_source(job)
        if not isinstance(source, SourceObservation):
            raise TypeError("watch source observer must return SourceObservation")
        result = self.execute(job, source)
        if not isinstance(result, WatchExecutionResult):
            raise TypeError("watch executor must return WatchExecutionResult")

        attempt = WatchAttempt(
            run_id=str(uuid4()),
            job_id=job.job_id,
            job_revision=job.revision_sha256,
            execution_contract_sha256=self.execution_contract_sha256,
            result_schema=result.result_schema,
            source_head=source.source_head,
            source_digest=source.source_digest,
            complete=result.complete,
            observations=result.observations,
        )
        previous = self.store.latest_attempt(job.job_id)
        sequence, created = self.store.record_attempt(attempt)
        if not created:  # uuid4 collision or broken store semantics; never continue quietly.
            raise RuntimeError("new watch run unexpectedly reused an existing run identity")
        delta = None if previous is None else compare_attempts(previous, attempt)
        return FixedWatchRun(
            sequence=sequence,
            current=attempt,
            previous=previous,
            delta=delta,
        )
