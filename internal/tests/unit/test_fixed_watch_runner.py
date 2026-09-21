from __future__ import annotations

from uuid import uuid4

import pytest

from local_agent.watch.delta import ComparisonStatus
from local_agent.watch.fixed_watch_runner import (
    FixedWatchRunner,
    SourceObservation,
    WatchExecutionResult,
)
from local_agent.watch.jobs import DirtyTreePolicy, FixedWatchJob, SourceSelection
from local_agent.watch.watch_run_store import SQLiteWatchRunStore


def _job(*, job_id: str | None = None, timeout_seconds: int = 60) -> FixedWatchJob:
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
        timeout_seconds=timeout_seconds,
    )


def test_first_run_is_baseline_and_second_run_compares_durable_attempts(tmp_path):
    store = SQLiteWatchRunStore(tmp_path / "watch.db")
    source_number = {"value": 0}

    def observe_source(_job):
        source_number["value"] += 1
        value = str(source_number["value"])
        return SourceObservation(source_head=f"head-{value}", source_digest=value * 64)

    def execute(_job, source):
        return WatchExecutionResult(
            result_schema="lca.watch.result/1",
            complete=True,
            observations=(("build", "pass"), ("head", source.source_head)),
        )

    runner = FixedWatchRunner(
        store,
        execution_contract_sha256="b" * 64,
        observe_source=observe_source,
        execute=execute,
    )
    job = _job()

    first = runner.run(job)
    second = runner.run(job)

    assert first.baseline is True
    assert first.previous is None and first.delta is None
    assert second.baseline is False
    assert second.previous == first.current
    assert second.delta is not None
    assert second.delta.status is ComparisonStatus.COMPARABLE
    assert [change.key for change in second.delta.changed] == ["head"]
    assert second.delta.unchanged_count == 1
    assert store.history(job.job_id) == [second.current, first.current]


def test_job_revision_change_makes_next_delta_incomparable(tmp_path):
    store = SQLiteWatchRunStore(tmp_path / "watch.db")

    def observe_source(_job):
        return SourceObservation(source_head="main", source_digest="c" * 64)

    def execute(_job, _source):
        return WatchExecutionResult(
            result_schema="lca.watch.result/1",
            complete=True,
            observations=(("tests", "pass"),),
        )

    runner = FixedWatchRunner(
        store,
        execution_contract_sha256="b" * 64,
        observe_source=observe_source,
        execute=execute,
    )
    job_id = str(uuid4())
    runner.run(_job(job_id=job_id, timeout_seconds=60))
    changed = runner.run(_job(job_id=job_id, timeout_seconds=120))

    assert changed.delta is not None
    assert changed.delta.status is ComparisonStatus.INCOMPARABLE
    assert "job_revision_changed" in changed.delta.reasons


def test_incomplete_attempt_is_persisted_and_cannot_form_a_comparable_delta(tmp_path):
    store = SQLiteWatchRunStore(tmp_path / "watch.db")
    calls = {"count": 0}

    def observe_source(_job):
        return SourceObservation(source_head="main", source_digest="d" * 64)

    def execute(_job, _source):
        calls["count"] += 1
        return WatchExecutionResult(
            result_schema="lca.watch.result/1",
            complete=calls["count"] == 1,
            observations=(("tests", "pass" if calls["count"] == 1 else "unknown"),),
        )

    runner = FixedWatchRunner(
        store,
        execution_contract_sha256="e" * 64,
        observe_source=observe_source,
        execute=execute,
    )
    job = _job()
    runner.run(job)
    second = runner.run(job)

    assert second.current.complete is False
    assert second.delta is not None
    assert second.delta.status is ComparisonStatus.INCOMPARABLE
    assert "attempt_incomplete" in second.delta.reasons
    assert store.latest_attempt(job.job_id) == second.current


def test_source_or_executor_exception_does_not_invent_a_durable_attempt(tmp_path):
    store = SQLiteWatchRunStore(tmp_path / "watch.db")
    job = _job()

    def broken_source(_job):
        raise RuntimeError("source unavailable")

    runner = FixedWatchRunner(
        store,
        execution_contract_sha256="f" * 64,
        observe_source=broken_source,
        execute=lambda _job, _source: WatchExecutionResult(
            result_schema="lca.watch.result/1", complete=True, observations=()
        ),
    )
    with pytest.raises(RuntimeError, match="source unavailable"):
        runner.run(job)
    assert store.history(job.job_id) == []


def test_executor_must_return_typed_result(tmp_path):
    store = SQLiteWatchRunStore(tmp_path / "watch.db")
    runner = FixedWatchRunner(
        store,
        execution_contract_sha256="1" * 64,
        observe_source=lambda _job: SourceObservation(source_head="main", source_digest="2" * 64),
        execute=lambda _job, _source: {"complete": True},
    )
    with pytest.raises(TypeError, match="WatchExecutionResult"):
        runner.run(_job())
