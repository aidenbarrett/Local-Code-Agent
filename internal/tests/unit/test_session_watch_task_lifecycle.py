from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest

from local_agent.session.contracts import TaskOutcome, TaskResult
from local_agent.session.durable_watch import DurableWatchEvents
from local_agent.session.session_event_service import DurableSessionService
from local_agent.session.session_store import SQLiteSessionStore
from local_agent.session.task_admission import execution_contract_sha256, repository_id
from local_agent.session.watch_task_lifecycle import (
    DurableWatchLifecycleError,
    DurableWatchTaskLifecycle,
)
from local_agent.watch.fixed_watch_runner import (
    FixedWatchRunner,
    SourceObservation,
    WatchExecutionResult,
)
from local_agent.watch.job_store import SQLiteWatchJobStore
from local_agent.watch.jobs import DirtyTreePolicy, FixedWatchJob, SourceSelection
from local_agent.watch.service import FixedWatchService
from local_agent.watch.watch_run_store import SQLiteWatchRunStore


def _session(tmp_path):
    return DurableSessionService(
        SQLiteSessionStore(tmp_path / "session.db"),
        stream_id=str(uuid4()),
        session_id=str(uuid4()),
    )


def _controller(repo):
    return SimpleNamespace(repo=repo, allow_execution=False, context_budget_tokens=12_000)


def _job(repo) -> FixedWatchJob:
    return FixedWatchJob(
        job_id=str(uuid4()),
        display_name="fixture health",
        repository_id=repository_id(repo),
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


def _watch(
    tmp_path,
    *,
    lifecycle,
    contract_sha256: str,
    execute=None,
    on_factory=None,
):
    jobs = SQLiteWatchJobStore(tmp_path / "jobs.db")
    runs = SQLiteWatchRunStore(tmp_path / "runs.db")

    def factory(record, run_id):
        if on_factory is not None:
            on_factory(record, run_id)
        return FixedWatchRunner(
            runs,
            execution_contract_sha256=contract_sha256,
            observe_source=lambda _job: SourceObservation("main", "c" * 64),
            execute=execute
            or (
                lambda _job, _source: WatchExecutionResult(
                    result_schema="lca.watch.result/1",
                    complete=True,
                    observations=(("tests", "pass"),),
                )
            ),
        )

    return FixedWatchService(jobs, runs, runner_factory=factory, run_lifecycle=lifecycle)


def test_watch_effect_runs_only_after_matching_state_admission_and_running(tmp_path, loaded):
    _sandbox, repo, _registry, _store, _skills = loaded
    session = _session(tmp_path)
    controller = _controller(repo)
    observed = {}

    def verifier(_record, completed, task_id):
        observed["verified_run_id"] = completed.run_id
        return TaskResult(
            task_id,
            TaskOutcome.PASS,
            "fixed watch verification passed",
            True,
            verification_ran=True,
        )

    lifecycle = DurableWatchTaskLifecycle(session, controller, verifier=verifier)

    def on_factory(_record, run_id):
        # Admission is the fence before runner construction. Running is intentionally
        # later, immediately before FixedWatchRunner.run().
        events = session.replay()
        assert [event["kind"] for event in events] == [
            "watch.state_changed",
            "task.admitted",
        ]
        assert events[-1]["payload"]["origin"]["run_id"] == run_id

    def execute(_job, _source):
        events = session.replay()
        assert [event["kind"] for event in events] == [
            "watch.state_changed",
            "task.admitted",
            "task.state_changed",
        ]
        assert events[-1]["payload"]["current"] == "running"
        return WatchExecutionResult(
            result_schema="lca.watch.result/1",
            complete=True,
            observations=(("tests", "pass"),),
        )

    watch = _watch(
        tmp_path,
        lifecycle=lifecycle,
        contract_sha256=execution_contract_sha256(controller),
        execute=execute,
        on_factory=on_factory,
    )
    job = _job(repo)
    record, _ = watch.add_job(job, enabled=True, schedule_spec="hourly")
    DurableWatchEvents(session).record_state(record)

    try:
        completed = watch.run_job(job.job_id)
        events = session.replay()
        assert [event["kind"] for event in events] == [
            "watch.state_changed",
            "task.admitted",
            "task.state_changed",
            "task.verdict",
            "task.closed",
            "watch.run_recorded",
        ]
        admitted = events[1]
        recorded = events[-1]
        assert admitted["payload"]["origin"] == {
            "kind": "watch",
            "job_id": job.job_id,
            "run_id": completed.run_id,
        }
        assert recorded["payload"]["run_id"] == completed.run_id
        assert recorded["payload"]["task_id"] == admitted["task_id"]
        assert recorded["payload"]["verdict"] == "VERIFIED"
        assert observed["verified_run_id"] == completed.run_id
        task = session.store.task_record(admitted["task_id"])
        assert task is not None
        assert task["terminal"] is True
        assert task["state"] == "completed"
    finally:
        session.close()


def test_missing_durable_watch_state_refuses_before_runner_construction(tmp_path, loaded):
    _sandbox, repo, _registry, _store, _skills = loaded
    session = _session(tmp_path)
    controller = _controller(repo)
    factory_calls = []
    lifecycle = DurableWatchTaskLifecycle(
        session,
        controller,
        verifier=lambda *_args: (_ for _ in ()).throw(AssertionError("must not verify")),
    )
    watch = _watch(
        tmp_path,
        lifecycle=lifecycle,
        contract_sha256=execution_contract_sha256(controller),
        on_factory=lambda *_args: factory_calls.append(True),
    )
    job = _job(repo)
    watch.add_job(job, enabled=True, schedule_spec="hourly")

    try:
        with pytest.raises(DurableWatchLifecycleError, match="durable watch state"):
            watch.run_job(job.job_id)
        assert factory_calls == []
        assert session.replay() == []
        assert watch.history(job.job_id) == []
    finally:
        session.close()


def test_execution_exception_closes_task_unknown_without_fabricating_watch_run(tmp_path, loaded):
    _sandbox, repo, _registry, _store, _skills = loaded
    session = _session(tmp_path)
    controller = _controller(repo)
    lifecycle = DurableWatchTaskLifecycle(
        session,
        controller,
        verifier=lambda *_args: (_ for _ in ()).throw(AssertionError("must not verify")),
    )

    def execute(_job, _source):
        raise RuntimeError("fixture exploded")

    watch = _watch(
        tmp_path,
        lifecycle=lifecycle,
        contract_sha256=execution_contract_sha256(controller),
        execute=execute,
    )
    job = _job(repo)
    record, _ = watch.add_job(job, enabled=True, schedule_spec="hourly")
    DurableWatchEvents(session).record_state(record)

    try:
        with pytest.raises(RuntimeError, match="fixture exploded"):
            watch.run_job(job.job_id)
        events = session.replay()
        assert [event["kind"] for event in events] == [
            "watch.state_changed",
            "task.admitted",
            "task.state_changed",
            "task.verdict",
            "task.closed",
        ]
        assert events[-2]["payload"]["completion"]["status"] == "unknown"
        assert events[-2]["payload"]["completion"]["verdict_block"]["verdict"] == "NO_VERDICT"
        assert watch.history(job.job_id) == []
    finally:
        session.close()


def test_verifier_failure_preserves_attempt_but_closes_task_no_verdict(tmp_path, loaded):
    _sandbox, repo, _registry, _store, _skills = loaded
    session = _session(tmp_path)
    controller = _controller(repo)

    def verifier(_record, _completed, _task_id):
        raise ValueError("verification contract failed")

    lifecycle = DurableWatchTaskLifecycle(session, controller, verifier=verifier)
    watch = _watch(
        tmp_path,
        lifecycle=lifecycle,
        contract_sha256=execution_contract_sha256(controller),
    )
    job = _job(repo)
    record, _ = watch.add_job(job, enabled=True, schedule_spec="hourly")
    DurableWatchEvents(session).record_state(record)

    try:
        with pytest.raises(DurableWatchLifecycleError, match="closed as NO_VERDICT"):
            watch.run_job(job.job_id)
        events = session.replay()
        assert [event["kind"] for event in events] == [
            "watch.state_changed",
            "task.admitted",
            "task.state_changed",
            "task.verdict",
            "task.closed",
            "watch.run_recorded",
        ]
        assert events[-1]["payload"]["verdict"] == "NO_VERDICT"
        assert events[-2]["payload"]["status"] == "unknown"
        assert len(watch.history(job.job_id)) == 1
    finally:
        session.close()


def test_execution_contract_drift_cannot_be_verified_green(tmp_path, loaded):
    _sandbox, repo, _registry, _store, _skills = loaded
    session = _session(tmp_path)
    controller = _controller(repo)
    verifier_calls = []

    def verifier(_record, _completed, task_id):
        verifier_calls.append(task_id)
        return TaskResult(task_id, TaskOutcome.PASS, "green", True, verification_ran=True)

    lifecycle = DurableWatchTaskLifecycle(session, controller, verifier=verifier)
    watch = _watch(
        tmp_path,
        lifecycle=lifecycle,
        contract_sha256="f" * 64,
    )
    job = _job(repo)
    record, _ = watch.add_job(job, enabled=True, schedule_spec="hourly")
    DurableWatchEvents(session).record_state(record)

    try:
        with pytest.raises(DurableWatchLifecycleError, match="closed as NO_VERDICT"):
            watch.run_job(job.job_id)
        assert verifier_calls == []
        events = session.replay()
        assert events[-1]["kind"] == "watch.run_recorded"
        assert events[-1]["payload"]["verdict"] == "NO_VERDICT"
        assert events[-3]["kind"] == "task.verdict"
        assert events[-3]["payload"]["completion"]["verdict_block"]["verdict"] == "NO_VERDICT"
    finally:
        session.close()
