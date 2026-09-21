from __future__ import annotations

from threading import Event, Thread
from uuid import UUID, uuid4

import pytest

from local_agent.watch.fixed_watch_runner import FixedWatchRunner, SourceObservation, WatchExecutionResult
from local_agent.watch.job_store import SQLiteWatchJobStore
from local_agent.watch.jobs import DirtyTreePolicy, FixedWatchJob, SourceSelection
from local_agent.watch.service import FixedWatchService, WatchDisabled, WatchOverlap, WatchTrigger
from local_agent.watch.watch_run_store import SQLiteWatchRunStore


def _job(*, job_id: str | None = None) -> FixedWatchJob:
    return FixedWatchJob(
        job_id=job_id or str(uuid4()),
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


def _service(tmp_path, *, execute=None, on_factory=None):
    job_store = SQLiteWatchJobStore(tmp_path / "jobs.db")
    run_store = SQLiteWatchRunStore(tmp_path / "runs.db")

    def factory(record, run_id):
        UUID(run_id)
        if on_factory is not None:
            on_factory(record, run_id)
        return FixedWatchRunner(
            run_store,
            execution_contract_sha256="b" * 64,
            observe_source=lambda _job: SourceObservation(
                source_head="main",
                source_digest="c" * 64,
            ),
            execute=execute or (
                lambda _job, _source: WatchExecutionResult(
                    result_schema="lca.watch.result/1",
                    complete=True,
                    observations=(("tests", "pass"),),
                )
            ),
        )

    return FixedWatchService(job_store, run_store, runner_factory=factory)


def test_manual_run_works_while_job_is_disabled_and_persists_history(tmp_path):
    service = _service(tmp_path)
    job = _job()
    service.add_job(job, enabled=False)

    completed = service.run_job(job.job_id, trigger=WatchTrigger.MANUAL)

    assert completed.configuration_version == 1
    assert completed.trigger is WatchTrigger.MANUAL
    assert completed.run_id == completed.run.current.run_id
    assert completed.run.current.job_id == job.job_id
    assert service.latest(job.job_id) == completed.run.current
    assert service.history(job.job_id) == [completed.run.current]


def test_run_identity_is_available_to_factory_before_observation_or_execution(tmp_path):
    order: list[tuple[str, str]] = []

    def on_factory(record, run_id):
        order.append(("factory", run_id))
        assert record.job.job_id == job.job_id

    def execute(_job, _source):
        order.append(("execute", ""))
        return WatchExecutionResult(
            result_schema="lca.watch.result/1",
            complete=True,
            observations=(("tests", "pass"),),
        )

    service = _service(tmp_path, execute=execute, on_factory=on_factory)
    job = _job()
    service.add_job(job)

    completed = service.run_job(job.job_id)

    assert order[0] == ("factory", completed.run_id)
    assert order[1][0] == "execute"
    assert completed.run.current.run_id == completed.run_id


def test_scheduled_run_requires_enabled_job_and_durable_schedule(tmp_path):
    service = _service(tmp_path)
    job = _job()
    first, _ = service.add_job(job, enabled=False)

    with pytest.raises(WatchDisabled, match="enabled"):
        service.run_job(job.job_id, trigger=WatchTrigger.SCHEDULED)

    enabled = service.enable(job.job_id, expected_version=first.version)
    with pytest.raises(WatchDisabled, match="schedule"):
        service.run_job(job.job_id, trigger=WatchTrigger.SCHEDULED)

    scheduled = service.set_schedule(job.job_id, "hourly", expected_version=enabled.version)
    completed = service.run_job(job.job_id, trigger=WatchTrigger.SCHEDULED)
    assert scheduled.enabled is True
    assert completed.configuration_version == scheduled.version
    assert completed.trigger is WatchTrigger.SCHEDULED


def test_disable_blocks_future_scheduled_runs_but_not_manual_runs(tmp_path):
    service = _service(tmp_path)
    job = _job()
    current, _ = service.add_job(job, enabled=True, schedule_spec="hourly")
    disabled = service.disable(job.job_id, expected_version=current.version)

    with pytest.raises(WatchDisabled):
        service.run_job(job.job_id, trigger=WatchTrigger.SCHEDULED)
    manual = service.run_job(job.job_id)
    assert manual.configuration_version == disabled.version


def test_same_job_overlap_is_refused_without_creating_a_second_attempt(tmp_path):
    entered = Event()
    release = Event()

    def execute(_job, _source):
        entered.set()
        assert release.wait(2)
        return WatchExecutionResult(
            result_schema="lca.watch.result/1",
            complete=True,
            observations=(("tests", "pass"),),
        )

    service = _service(tmp_path, execute=execute)
    job = _job()
    service.add_job(job)
    errors = []

    def first_run():
        try:
            service.run_job(job.job_id)
        except BaseException as exc:
            errors.append(exc)

    thread = Thread(target=first_run)
    thread.start()
    assert entered.wait(1)
    assert service.active(job.job_id) is True

    with pytest.raises(WatchOverlap, match="active run"):
        service.run_job(job.job_id)

    release.set()
    thread.join(2)
    assert not thread.is_alive()
    assert not errors
    assert service.active(job.job_id) is False
    assert len(service.history(job.job_id)) == 1


def test_different_jobs_may_run_independently(tmp_path):
    service = _service(tmp_path)
    first = _job()
    second = _job()
    service.add_job(first)
    service.add_job(second)

    first_run = service.run_job(first.job_id)
    second_run = service.run_job(second.job_id)

    assert first_run.run.current.job_id == first.job_id
    assert second_run.run.current.job_id == second.job_id


def test_lifecycle_updates_are_exposed_through_one_service(tmp_path):
    service = _service(tmp_path)
    job = _job()
    first, created = service.add_job(job)
    assert created is True
    assert service.jobs() == [first]

    enabled = service.enable(job.job_id, expected_version=1)
    scheduled = service.set_schedule(job.job_id, "hourly", expected_version=enabled.version)
    disabled = service.disable(job.job_id, expected_version=scheduled.version)

    assert service.jobs(enabled_only=True) == []
    assert service.jobs() == [disabled]
