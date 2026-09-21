"""Deterministic read model for durable Session Hub watch events.

The watch pane must survive restart from controller-owned facts alone. This projector
consumes the reviewed ``watch.state_changed`` and ``watch.run_recorded`` v1 events and
rejects revision/history contradictions instead of smoothing them over in presentation.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable
from uuid import UUID


class WatchReadModelError(RuntimeError):
    pass


@dataclass(frozen=True)
class WatchRunSummary:
    sequence: int
    run_id: str
    task_id: str
    job_revision: str
    schedule_revision: str
    execution_contract_sha256: str
    status: str
    verdict: str
    comparison: str
    previous_attempt_id: str | None
    comparison_run_id: str | None
    due_utc: str | None
    started_utc: str
    finished_utc: str
    next_due_utc: str | None
    counts: dict | None
    delta_ref: dict | None


@dataclass(frozen=True)
class WatchActivity:
    sequence: int
    occurred_utc: str
    kind: str
    summary: str


@dataclass
class WatchSnapshot:
    job_id: str
    first_sequence: int
    last_sequence: int
    job_revision: str
    schedule_revision: str
    state: str
    reason_code: str
    due_utc: str | None
    next_due_utc: str | None
    runs: list[WatchRunSummary] = field(default_factory=list)
    activity: list[WatchActivity] = field(default_factory=list)

    @property
    def last_run(self) -> WatchRunSummary | None:
        return None if not self.runs else self.runs[-1]


def _require_uuid(name: str, value: str) -> None:
    try:
        UUID(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be a UUID") from exc


def _require_sha(name: str, value: str) -> None:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(ch not in "0123456789abcdef" for ch in value)
    ):
        raise ValueError(f"{name} must be lowercase SHA-256")


def _activity(sequence: int, occurred_utc: str, kind: str, summary: str) -> WatchActivity:
    return WatchActivity(sequence=sequence, occurred_utc=occurred_utc, kind=kind, summary=summary)


def _state_event(event: dict) -> tuple[str, str, str, str, str | None, str | None]:
    payload = event["payload"]
    job_id = str(payload["job_id"])
    job_revision = str(payload["job_revision"])
    schedule_revision = str(payload["schedule_revision"])
    _require_uuid("watch job_id", job_id)
    _require_sha("watch job_revision", job_revision)
    _require_sha("watch schedule_revision", schedule_revision)
    return (
        job_id,
        job_revision,
        schedule_revision,
        str(payload["state"]),
        None if payload["due_utc"] is None else str(payload["due_utc"]),
        None if payload["next_due_utc"] is None else str(payload["next_due_utc"]),
    )


def _apply(snapshot: WatchSnapshot, event: dict) -> None:
    sequence = int(event["sequence"])
    if sequence <= snapshot.last_sequence:
        raise WatchReadModelError("watch events must have strictly increasing durable sequence")
    snapshot.last_sequence = sequence
    kind = str(event["kind"])
    payload = event["payload"]
    occurred_utc = str(event["occurred_utc"])

    if kind == "watch.state_changed":
        job_id, job_revision, schedule_revision, state, due_utc, next_due_utc = _state_event(event)
        if job_id != snapshot.job_id:
            raise WatchReadModelError("watch state event names another job")
        snapshot.job_revision = job_revision
        snapshot.schedule_revision = schedule_revision
        snapshot.state = state
        snapshot.reason_code = str(payload["reason_code"])
        snapshot.due_utc = due_utc
        snapshot.next_due_utc = next_due_utc
        snapshot.activity.append(
            _activity(sequence, occurred_utc, kind, f"watch {state}: {snapshot.reason_code}")
        )
        return

    if kind == "watch.run_recorded":
        job_id = str(payload["job_id"])
        if job_id != snapshot.job_id:
            raise WatchReadModelError("watch run event names another job")
        job_revision = str(payload["job_revision"])
        schedule_revision = str(payload["schedule_revision"])
        if job_revision != snapshot.job_revision:
            raise WatchReadModelError("watch run used a job revision that was not the current durable state")
        if schedule_revision != snapshot.schedule_revision:
            raise WatchReadModelError("watch run used a schedule revision that was not the current durable state")
        execution_contract = str(payload["execution_contract_sha256"])
        _require_sha("execution_contract_sha256", execution_contract)
        run_id = str(payload["run_id"])
        task_id = str(payload["task_id"])
        _require_uuid("watch run_id", run_id)
        _require_uuid("watch task_id", task_id)
        previous_attempt_id = (
            None if payload["previous_attempt_id"] is None else str(payload["previous_attempt_id"])
        )
        comparison_run_id = (
            None if payload["comparison_run_id"] is None else str(payload["comparison_run_id"])
        )
        if previous_attempt_id is not None:
            _require_uuid("previous_attempt_id", previous_attempt_id)
        if comparison_run_id is not None:
            _require_uuid("comparison_run_id", comparison_run_id)

        prior = snapshot.last_run
        if prior is None:
            if previous_attempt_id is not None:
                raise WatchReadModelError("first durable watch run cannot name a previous attempt")
            if str(payload["comparison"]) != "BASELINE":
                raise WatchReadModelError("first durable watch run must be BASELINE")
        else:
            if previous_attempt_id != prior.run_id:
                raise WatchReadModelError("watch previous_attempt_id does not match the prior durable run")
            if str(payload["comparison"]) == "BASELINE":
                raise WatchReadModelError("non-first durable watch run cannot be BASELINE")

        summary = WatchRunSummary(
            sequence=sequence,
            run_id=run_id,
            task_id=task_id,
            job_revision=job_revision,
            schedule_revision=schedule_revision,
            execution_contract_sha256=execution_contract,
            status=str(payload["status"]),
            verdict=str(payload["verdict"]),
            comparison=str(payload["comparison"]),
            previous_attempt_id=previous_attempt_id,
            comparison_run_id=comparison_run_id,
            due_utc=None if payload["due_utc"] is None else str(payload["due_utc"]),
            started_utc=str(payload["started_utc"]),
            finished_utc=str(payload["finished_utc"]),
            next_due_utc=None if payload["next_due_utc"] is None else str(payload["next_due_utc"]),
            counts=None if payload["counts"] is None else dict(payload["counts"]),
            delta_ref=None if payload["delta_ref"] is None else dict(payload["delta_ref"]),
        )
        snapshot.runs.append(summary)
        snapshot.next_due_utc = summary.next_due_utc
        snapshot.activity.append(
            _activity(
                sequence,
                occurred_utc,
                kind,
                f"run {summary.status}/{summary.verdict} {summary.comparison}",
            )
        )
        return

    raise WatchReadModelError(f"unsupported watch event kind {kind!r}")


def project_watch(events: Iterable[dict], job_id: str) -> WatchSnapshot:
    _require_uuid("watch job_id", job_id)
    relevant = [
        event
        for event in events
        if event.get("kind") in {"watch.state_changed", "watch.run_recorded"}
        and str(event.get("payload", {}).get("job_id", "")) == job_id
    ]
    if not relevant:
        raise WatchReadModelError("no durable watch events exist for the requested job")
    relevant.sort(key=lambda event: int(event["sequence"]))
    first = relevant[0]
    if first.get("kind") != "watch.state_changed":
        raise WatchReadModelError("first durable watch event must be watch.state_changed")
    first_job_id, job_revision, schedule_revision, state, due_utc, next_due_utc = _state_event(first)
    if first_job_id != job_id:
        raise WatchReadModelError("first watch event names another job")
    payload = first["payload"]
    snapshot = WatchSnapshot(
        job_id=job_id,
        first_sequence=int(first["sequence"]),
        last_sequence=int(first["sequence"]),
        job_revision=job_revision,
        schedule_revision=schedule_revision,
        state=state,
        reason_code=str(payload["reason_code"]),
        due_utc=due_utc,
        next_due_utc=next_due_utc,
    )
    snapshot.activity.append(
        _activity(
            snapshot.first_sequence,
            str(first["occurred_utc"]),
            "watch.state_changed",
            f"watch {state}: {snapshot.reason_code}",
        )
    )
    for event in relevant[1:]:
        _apply(snapshot, event)
    return snapshot


def project_watches(events: Iterable[dict]) -> list[WatchSnapshot]:
    materialised = list(events)
    first_by_job: dict[str, int] = {}
    for event in materialised:
        if event.get("kind") != "watch.state_changed":
            continue
        payload = event.get("payload", {})
        job_id = payload.get("job_id")
        if not isinstance(job_id, str):
            raise WatchReadModelError("watch.state_changed is missing job_id")
        _require_uuid("watch job_id", job_id)
        sequence = int(event["sequence"])
        first_by_job[job_id] = min(sequence, first_by_job.get(job_id, sequence))
    return [
        project_watch(materialised, job_id)
        for job_id, _sequence in sorted(first_by_job.items(), key=lambda item: item[1])
    ]
