"""Comparable fixed-watch attempt summaries and deterministic deltas."""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from uuid import UUID


class ComparisonStatus(str, Enum):
    COMPARABLE = "comparable"
    INCOMPARABLE = "incomparable"


@dataclass(frozen=True)
class WatchAttempt:
    run_id: str
    job_id: str
    job_revision: str
    execution_contract_sha256: str
    result_schema: str
    source_head: str
    source_digest: str
    complete: bool
    observations: tuple[tuple[str, str], ...]

    def __post_init__(self) -> None:
        for name, value in (("run_id", self.run_id), ("job_id", self.job_id)):
            try:
                UUID(value)
            except (TypeError, ValueError) as exc:
                raise ValueError(f"{name} must be a UUID") from exc
        for name, value in (
            ("job_revision", self.job_revision),
            ("execution_contract_sha256", self.execution_contract_sha256),
            ("source_digest", self.source_digest),
        ):
            if (
                not isinstance(value, str)
                or len(value) != 64
                or any(ch not in "0123456789abcdef" for ch in value)
            ):
                raise ValueError(f"{name} must be lowercase SHA-256")
        for name, value in (("result_schema", self.result_schema), ("source_head", self.source_head)):
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{name} must be nonempty")
        if not isinstance(self.complete, bool):
            raise ValueError("complete must be boolean")
        values = tuple(self.observations)
        keys = [key for key, _value in values]
        if any(not isinstance(key, str) or not key.strip() for key in keys):
            raise ValueError("observation keys must be nonempty strings")
        if any(not isinstance(value, str) for _key, value in values):
            raise ValueError("observation values must be strings")
        if len(set(keys)) != len(keys):
            raise ValueError("observation keys must be unique")
        object.__setattr__(self, "observations", tuple(sorted(values)))


@dataclass(frozen=True)
class ObservationChange:
    key: str
    before: str
    after: str


@dataclass(frozen=True)
class WatchDelta:
    status: ComparisonStatus
    reasons: tuple[str, ...] = ()
    added: tuple[str, ...] = ()
    removed: tuple[str, ...] = ()
    changed: tuple[ObservationChange, ...] = ()
    unchanged_count: int = 0

    def __post_init__(self) -> None:
        if self.status == ComparisonStatus.INCOMPARABLE:
            if not self.reasons:
                raise ValueError("incomparable delta requires a reason")
            if self.added or self.removed or self.changed or self.unchanged_count:
                raise ValueError("incomparable delta cannot contain synthetic changes")
        elif self.reasons:
            raise ValueError("comparable delta cannot carry incomparable reasons")
        if self.unchanged_count < 0:
            raise ValueError("unchanged_count cannot be negative")


def compare_attempts(previous: WatchAttempt, current: WatchAttempt) -> WatchDelta:
    """Compare only runs with the same procedure/execution/result contract.

    Observed source HEAD/content are intentionally not comparability keys. They are
    the thing the watch is allowed to observe changing.
    """
    reasons: list[str] = []
    if previous.run_id == current.run_id:
        reasons.append("same_run")
    if previous.job_id != current.job_id:
        reasons.append("job_id_changed")
    if previous.job_revision != current.job_revision:
        reasons.append("job_revision_changed")
    if previous.execution_contract_sha256 != current.execution_contract_sha256:
        reasons.append("execution_contract_changed")
    if previous.result_schema != current.result_schema:
        reasons.append("result_schema_changed")
    if not previous.complete or not current.complete:
        reasons.append("attempt_incomplete")
    if reasons:
        return WatchDelta(ComparisonStatus.INCOMPARABLE, reasons=tuple(reasons))

    before = dict(previous.observations)
    after = dict(current.observations)
    before_keys = set(before)
    after_keys = set(after)
    added = tuple(sorted(after_keys - before_keys))
    removed = tuple(sorted(before_keys - after_keys))
    shared = sorted(before_keys & after_keys)
    changed = tuple(
        ObservationChange(key, before[key], after[key])
        for key in shared
        if before[key] != after[key]
    )
    unchanged = sum(1 for key in shared if before[key] == after[key])
    return WatchDelta(
        ComparisonStatus.COMPARABLE,
        added=added,
        removed=removed,
        changed=changed,
        unchanged_count=unchanged,
    )
