from __future__ import annotations

from uuid import uuid4

import pytest

from local_agent.session.watch_read_model import WatchReadModelError, project_watch, project_watches


def _event(sequence: int, kind: str, payload: dict):
    return {
        "schema_id": "lca.session.events",
        "schema_version": 1,
        "event_id": str(uuid4()),
        "stream_id": str(uuid4()),
        "stream_kind": "durable",
        "sequence": sequence,
        "producer_epoch": str(uuid4()),
        "session_id": str(uuid4()),
        "task_id": None,
        "occurred_utc": f"2030-01-01T00:00:{sequence:02d}Z",
        "kind": kind,
        "payload": payload,
    }


def _state(sequence: int, job_id: str, *, job_revision: str = "a" * 64,
           schedule_revision: str = "b" * 64, state: str = "enabled"):
    return _event(sequence, "watch.state_changed", {
        "job_id": job_id,
        "job_revision": job_revision,
        "schedule_revision": schedule_revision,
        "state": state,
        "reason_code": "none",
        "due_utc": None,
        "next_due_utc": "2030-01-01T01:00:00Z",
    })


def _run(sequence: int, job_id: str, *, run_id: str | None = None,
         previous_attempt_id: str | None = None, comparison: str = "BASELINE",
         job_revision: str = "a" * 64, schedule_revision: str = "b" * 64):
    return _event(sequence, "watch.run_recorded", {
        "job_id": job_id,
        "job_revision": job_revision,
        "schedule_revision": schedule_revision,
        "execution_contract_sha256": "c" * 64,
        "run_id": run_id or str(uuid4()),
        "task_id": str(uuid4()),
        "due_utc": None,
        "started_utc": "2030-01-01T00:00:00Z",
        "finished_utc": "2030-01-01T00:00:10Z",
        "status": "completed",
        "verdict": "VERIFIED",
        "previous_attempt_id": previous_attempt_id,
        "comparison_run_id": previous_attempt_id,
        "comparison": comparison,
        "counts": None if comparison == "BASELINE" else {
            "new_failures": 0, "recovered": 0, "still_failing": 0,
            "added_tests": 0, "removed_tests": 0, "changed_paths": 0,
        },
        "delta_ref": None,
        "next_due_utc": "2030-01-01T01:00:00Z",
    })


def test_projects_watch_state_and_chained_run_history_after_restart():
    job_id = str(uuid4())
    first_run_id = str(uuid4())
    first = _run(2, job_id, run_id=first_run_id)
    second = _run(
        3,
        job_id,
        previous_attempt_id=first_run_id,
        comparison="COMPARABLE",
    )
    snapshot = project_watch([second, _state(1, job_id), first], job_id)

    assert snapshot.state == "enabled"
    assert snapshot.job_revision == "a" * 64
    assert snapshot.schedule_revision == "b" * 64
    assert [run.run_id for run in snapshot.runs] == [first_run_id, second["payload"]["run_id"]]
    assert snapshot.last_run is not None
    assert snapshot.last_run.comparison == "COMPARABLE"
    assert snapshot.last_run.previous_attempt_id == first_run_id
    assert [item.kind for item in snapshot.activity] == [
        "watch.state_changed", "watch.run_recorded", "watch.run_recorded"
    ]


def test_state_change_can_move_to_new_job_and_schedule_revision_before_next_run():
    job_id = str(uuid4())
    first_run_id = str(uuid4())
    events = [
        _state(1, job_id),
        _run(2, job_id, run_id=first_run_id),
        _state(3, job_id, job_revision="d" * 64, schedule_revision="e" * 64, state="paused"),
        _run(
            4,
            job_id,
            previous_attempt_id=first_run_id,
            comparison="INCOMPARABLE",
            job_revision="d" * 64,
            schedule_revision="e" * 64,
        ),
    ]
    snapshot = project_watch(events, job_id)
    assert snapshot.state == "paused"
    assert snapshot.job_revision == "d" * 64
    assert snapshot.schedule_revision == "e" * 64
    assert snapshot.last_run is not None and snapshot.last_run.comparison == "INCOMPARABLE"


def test_orphan_run_and_revision_mismatch_fail_closed():
    job_id = str(uuid4())
    with pytest.raises(WatchReadModelError, match="first durable watch event"):
        project_watch([_run(1, job_id)], job_id)

    with pytest.raises(WatchReadModelError, match="job revision"):
        project_watch([
            _state(1, job_id),
            _run(2, job_id, job_revision="d" * 64),
        ], job_id)

    with pytest.raises(WatchReadModelError, match="schedule revision"):
        project_watch([
            _state(1, job_id),
            _run(2, job_id, schedule_revision="e" * 64),
        ], job_id)


def test_first_run_must_be_baseline_without_previous_attempt():
    job_id = str(uuid4())
    with pytest.raises(WatchReadModelError, match="cannot name a previous"):
        project_watch([
            _state(1, job_id),
            _run(2, job_id, previous_attempt_id=str(uuid4()), comparison="COMPARABLE"),
        ], job_id)

    with pytest.raises(WatchReadModelError, match="must be BASELINE"):
        project_watch([
            _state(1, job_id),
            _run(2, job_id, comparison="NO_CURRENT_RESULT"),
        ], job_id)


def test_non_first_run_must_chain_to_prior_durable_run_and_cannot_reset_baseline():
    job_id = str(uuid4())
    first_run_id = str(uuid4())
    with pytest.raises(WatchReadModelError, match="does not match"):
        project_watch([
            _state(1, job_id),
            _run(2, job_id, run_id=first_run_id),
            _run(3, job_id, previous_attempt_id=str(uuid4()), comparison="COMPARABLE"),
        ], job_id)

    with pytest.raises(WatchReadModelError, match="cannot be BASELINE"):
        project_watch([
            _state(1, job_id),
            _run(2, job_id, run_id=first_run_id),
            _run(3, job_id, previous_attempt_id=first_run_id, comparison="BASELINE"),
        ], job_id)


def test_project_watches_is_deterministic_by_first_state_sequence():
    first = str(uuid4())
    second = str(uuid4())
    snapshots = project_watches([_state(10, second), _state(3, first)])
    assert [snapshot.job_id for snapshot in snapshots] == [first, second]


def test_events_for_other_jobs_do_not_contaminate_projection():
    job_id = str(uuid4())
    other = str(uuid4())
    snapshot = project_watch([_state(1, job_id), _state(2, other)], job_id)
    assert snapshot.job_id == job_id
    assert snapshot.last_sequence == 1
