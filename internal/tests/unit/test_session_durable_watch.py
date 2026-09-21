from __future__ import annotations

from uuid import uuid4

import pytest

from local_agent.session.contracts import TaskOutcome, TaskResult
from local_agent.session.durable_watch import (
    DurableWatchError,
    DurableWatchEvents,
    watch_schedule_revision,
)
from local_agent.session.session_event_service import DurableSessionService
from local_agent.session.session_store import SQLiteSessionStore
from local_agent.session.watch_read_model import project_watch
from local_agent.watch.fixed_watch_runner import FixedWatchRunner, SourceObservation, WatchExecutionResult
from local_agent.watch.job_store import SQLiteWatchJobStore
from local_agent.watch.jobs import DirtyTreePolicy, FixedWatchJob, SourceSelection
from local_agent.watch.service import FixedWatchService
from local_agent.watch.watch_run_store import SQLiteWatchRunStore


def _job() -> FixedWatchJob:
    return FixedWatchJob(
        job_id=str(uuid4()),
        display_name="fixture health",
        repository_id="repo-1",
        repository_host="local",
        source_selection=SourceSelection.LOCAL_REF,
        source_selector="main",
        dirty_tree_policy=DirtyTreePolicy.REFUSE,
        command_profile="fixture-test",
        environment_allowlist=("PATH",),
        verification_contract_sha256="a" * 64,
        scope=("build", "tests"),
        timeout_seconds=60,
    )


def _watch_service(tmp_path, *, complete: bool = True):
    jobs = SQLiteWatchJobStore(tmp_path / "jobs.db")
    runs = SQLiteWatchRunStore(tmp_path / "runs.db")

    def factory(_record, _run_id):
        return FixedWatchRunner(
            runs,
            execution_contract_sha256="b" * 64,
            observe_source=lambda _job: SourceObservation("main", "c" * 64),
            execute=lambda _job, _source: WatchExecutionResult(
                result_schema="lca.watch.result/1",
                complete=complete,
                observations=(("tests", "pass"),),
            ),
        )

    return FixedWatchService(jobs, runs, runner_factory=factory)


def _session_service(tmp_path):
    return DurableSessionService(
        SQLiteSessionStore(tmp_path / "session.db"),
        stream_id=str(uuid4()),
        session_id=str(uuid4()),
    )


def test_state_then_completed_run_round_trips_through_watch_read_model(tmp_path):
    watch = _watch_service(tmp_path)
    job = _job()
    record, _ = watch.add_job(job, enabled=False)
    completed = watch.run_job(job.job_id)
    service = _session_service(tmp_path)
    try:
        durable = DurableWatchEvents(service)
        durable.record_state(record)
        result = TaskResult(str(uuid4()), TaskOutcome.PASS, "green", True)
        durable.record_run(
            record,
            completed,
            result,
            started_utc="2030-01-01T00:00:00Z",
            finished_utc="2030-01-01T00:00:01Z",
        )

        events = service.replay()
        assert [event["sequence"] for event in events] == [1, 2]
        snapshot = project_watch(events, job.job_id)
        assert snapshot.state == "disabled"
        assert snapshot.job_revision == record.job.revision_sha256
        assert snapshot.schedule_revision == watch_schedule_revision(record)
        assert snapshot.last_run is not None
        assert snapshot.last_run.run_id == completed.run_id
        assert snapshot.last_run.task_id == result.task_id
        assert snapshot.last_run.status == "completed"
        assert snapshot.last_run.verdict == "VERIFIED"
        assert snapshot.last_run.comparison == "BASELINE"
        assert snapshot.last_run.counts is None
        assert snapshot.last_run.delta_ref is None
    finally:
        service.close()


def test_second_run_records_explicit_comparable_predecessor(tmp_path):
    watch = _watch_service(tmp_path)
    job = _job()
    record, _ = watch.add_job(job)
    first = watch.run_job(job.job_id)
    second = watch.run_job(job.job_id)
    service = _session_service(tmp_path)
    try:
        durable = DurableWatchEvents(service)
        durable.record_state(record)
        durable.record_run(
            record,
            first,
            TaskResult(str(uuid4()), TaskOutcome.FAIL, "first", False, verification_ran=True),
            started_utc="2030-01-01T00:00:00Z",
            finished_utc="2030-01-01T00:00:01Z",
        )
        second_result = TaskResult(str(uuid4()), TaskOutcome.FAIL, "second", False, verification_ran=True)
        durable.record_run(
            record,
            second,
            second_result,
            started_utc="2030-01-01T00:01:00Z",
            finished_utc="2030-01-01T00:01:01Z",
        )
        last = project_watch(service.replay(), job.job_id).last_run
        assert last is not None
        assert last.comparison == "COMPARABLE"
        assert last.previous_attempt_id == first.run_id
        assert last.comparison_run_id == first.run_id
        assert last.task_id == second_result.task_id
    finally:
        service.close()


def test_run_refuses_to_publish_before_matching_durable_state(tmp_path):
    watch = _watch_service(tmp_path)
    job = _job()
    record, _ = watch.add_job(job)
    completed = watch.run_job(job.job_id)
    service = _session_service(tmp_path)
    try:
        durable = DurableWatchEvents(service)
        with pytest.raises(DurableWatchError, match="state must be recorded"):
            durable.record_run(
                record,
                completed,
                TaskResult(str(uuid4()), TaskOutcome.FAIL, "failed", False),
                started_utc="2030-01-01T00:00:00Z",
                finished_utc="2030-01-01T00:00:01Z",
            )
        assert service.replay() == []
    finally:
        service.close()


def test_incomplete_attempt_cannot_be_rendered_as_verified(tmp_path):
    watch = _watch_service(tmp_path, complete=False)
    job = _job()
    record, _ = watch.add_job(job)
    completed = watch.run_job(job.job_id)
    service = _session_service(tmp_path)
    try:
        durable = DurableWatchEvents(service)
        durable.record_state(record)
        with pytest.raises(DurableWatchError, match="incomplete watch attempt"):
            durable.record_run(
                record,
                completed,
                TaskResult(str(uuid4()), TaskOutcome.PASS, "green", True),
                started_utc="2030-01-01T00:00:00Z",
                finished_utc="2030-01-01T00:00:01Z",
            )
        assert [event["kind"] for event in service.replay()] == ["watch.state_changed"]
    finally:
        service.close()


def test_schedule_revision_changes_when_lifecycle_configuration_changes(tmp_path):
    watch = _watch_service(tmp_path)
    job = _job()
    initial, _ = watch.add_job(job, enabled=False)
    enabled = watch.enable(job.job_id, expected_version=initial.version)
    scheduled = watch.set_schedule(job.job_id, "hourly", expected_version=enabled.version)
    assert watch_schedule_revision(initial) != watch_schedule_revision(enabled)
    assert watch_schedule_revision(enabled) != watch_schedule_revision(scheduled)
