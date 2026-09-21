from __future__ import annotations

from uuid import uuid4

import pytest

from local_agent.watch.delta import WatchAttempt
from local_agent.watch.watch_run_store import SQLiteWatchRunStore, WatchRunConflict


def _attempt(
    *,
    run_id: str | None = None,
    job_id: str | None = None,
    head: str = "main",
    digest: str = "d" * 64,
    observations=("build", "pass"),
    complete: bool = True,
) -> WatchAttempt:
    return WatchAttempt(
        run_id=run_id or str(uuid4()),
        job_id=job_id or str(uuid4()),
        job_revision="a" * 64,
        execution_contract_sha256="b" * 64,
        result_schema="lca.watch.result/1",
        source_head=head,
        source_digest=digest,
        complete=complete,
        observations=(observations,),
    )


def test_record_round_trip_is_append_only_and_exact_replay_is_idempotent(tmp_path):
    store = SQLiteWatchRunStore(tmp_path / "watch.db")
    attempt = _attempt()

    first_sequence, first_created = store.record_attempt(attempt)
    second_sequence, second_created = store.record_attempt(attempt)

    assert first_created is True
    assert second_created is False
    assert second_sequence == first_sequence
    assert store.attempt(attempt.run_id) == attempt


def test_same_run_id_with_different_attempt_data_is_a_conflict(tmp_path):
    store = SQLiteWatchRunStore(tmp_path / "watch.db")
    run_id = str(uuid4())
    job_id = str(uuid4())
    original = _attempt(run_id=run_id, job_id=job_id, digest="1" * 64)
    changed = _attempt(run_id=run_id, job_id=job_id, digest="2" * 64)
    store.record_attempt(original)

    with pytest.raises(WatchRunConflict, match="different attempt data"):
        store.record_attempt(changed)
    assert store.attempt(run_id) == original


def test_latest_and_history_are_scoped_to_one_job_and_sequence(tmp_path):
    store = SQLiteWatchRunStore(tmp_path / "watch.db")
    job_id = str(uuid4())
    other_job = str(uuid4())
    first = _attempt(job_id=job_id, head="one", digest="1" * 64)
    second = _attempt(job_id=job_id, head="two", digest="2" * 64)
    foreign = _attempt(job_id=other_job, head="other", digest="3" * 64)

    first_sequence, _ = store.record_attempt(first)
    second_sequence, _ = store.record_attempt(second)
    store.record_attempt(foreign)

    assert store.latest_attempt(job_id) == second
    assert store.latest_attempt(job_id, before_sequence=second_sequence) == first
    assert store.latest_attempt(job_id, before_sequence=first_sequence) is None
    assert store.history(job_id) == [second, first]
    assert store.history(other_job) == [foreign]


def test_incomplete_attempt_is_preserved_not_promoted_to_complete(tmp_path):
    store = SQLiteWatchRunStore(tmp_path / "watch.db")
    attempt = _attempt(complete=False)
    store.record_attempt(attempt)
    assert store.attempt(attempt.run_id) is not None
    assert store.attempt(attempt.run_id).complete is False


def test_history_limit_is_bounded(tmp_path):
    store = SQLiteWatchRunStore(tmp_path / "watch.db")
    job_id = str(uuid4())
    for index in range(3):
        store.record_attempt(_attempt(job_id=job_id, head=str(index), digest=str(index) * 64))
    assert len(store.history(job_id, limit=2)) == 2
    with pytest.raises(ValueError, match="between 1 and 1000"):
        store.history(job_id, limit=0)
