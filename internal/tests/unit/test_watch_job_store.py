from __future__ import annotations

from uuid import uuid4

import pytest

from local_agent.watch.job_store import SQLiteWatchJobStore, WatchJobConflict, WatchJobNotFound
from local_agent.watch.jobs import DirtyTreePolicy, FixedWatchJob, SourceSelection


def _job(*, job_id: str | None = None, timeout_seconds: int = 60, name: str = "fixture health") -> FixedWatchJob:
    return FixedWatchJob(
        job_id=job_id or str(uuid4()),
        display_name=name,
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


def test_create_round_trip_and_exact_replay_is_idempotent(tmp_path):
    store = SQLiteWatchJobStore(tmp_path / "watch.db")
    job = _job()

    first, created = store.create(job, enabled=False, schedule_spec="hourly")
    second, created_again = store.create(job, enabled=False, schedule_spec="hourly")

    assert created is True
    assert created_again is False
    assert first == second == store.require(job.job_id)
    assert first.version == 1
    assert first.revision_sha256 == job.revision_sha256


def test_same_job_id_with_different_initial_configuration_is_refused(tmp_path):
    store = SQLiteWatchJobStore(tmp_path / "watch.db")
    job_id = str(uuid4())
    store.create(_job(job_id=job_id, timeout_seconds=60))

    with pytest.raises(WatchJobConflict, match="different configuration"):
        store.create(_job(job_id=job_id, timeout_seconds=120))


def test_procedure_update_creates_immutable_new_version(tmp_path):
    store = SQLiteWatchJobStore(tmp_path / "watch.db")
    job_id = str(uuid4())
    original = _job(job_id=job_id, timeout_seconds=60)
    current, _ = store.create(original, enabled=True, schedule_spec="hourly")
    changed = _job(job_id=job_id, timeout_seconds=120)

    updated = store.update_job(changed, expected_version=current.version)

    assert updated.version == 2
    assert updated.enabled is True
    assert updated.schedule_spec == "hourly"
    assert updated.revision_sha256 != current.revision_sha256
    assert store.version(job_id, 1) == current
    assert store.version(job_id, 2) == updated


def test_display_name_change_versions_configuration_without_moving_procedure_revision(tmp_path):
    store = SQLiteWatchJobStore(tmp_path / "watch.db")
    job_id = str(uuid4())
    original = _job(job_id=job_id, name="old label")
    first, _ = store.create(original)
    renamed = _job(job_id=job_id, name="new label")

    second = store.update_job(renamed, expected_version=1)

    assert second.version == 2
    assert second.revision_sha256 == first.revision_sha256
    assert second.job.display_name == "new label"


def test_enable_and_schedule_changes_are_versioned_but_not_procedure_identity(tmp_path):
    store = SQLiteWatchJobStore(tmp_path / "watch.db")
    job = _job()
    first, _ = store.create(job)

    enabled = store.set_enabled(job.job_id, True, expected_version=1)
    scheduled = store.set_schedule(job.job_id, "0 * * * *", expected_version=2)

    assert enabled.version == 2 and enabled.enabled is True
    assert scheduled.version == 3 and scheduled.schedule_spec == "0 * * * *"
    assert first.revision_sha256 == enabled.revision_sha256 == scheduled.revision_sha256
    assert store.version(job.job_id, 1) == first
    assert store.version(job.job_id, 2) == enabled
    assert store.version(job.job_id, 3) == scheduled


def test_stale_update_version_fails_closed(tmp_path):
    store = SQLiteWatchJobStore(tmp_path / "watch.db")
    job = _job()
    store.create(job)
    store.set_enabled(job.job_id, True, expected_version=1)

    with pytest.raises(WatchJobConflict, match="version changed"):
        store.set_schedule(job.job_id, "hourly", expected_version=1)


def test_listing_can_select_only_enabled_jobs(tmp_path):
    store = SQLiteWatchJobStore(tmp_path / "watch.db")
    disabled = _job()
    enabled = _job()
    store.create(disabled, enabled=False)
    store.create(enabled, enabled=True)

    assert {row.job.job_id for row in store.list_jobs()} == {disabled.job_id, enabled.job_id}
    assert [row.job.job_id for row in store.list_jobs(enabled_only=True)] == [enabled.job_id]


def test_missing_job_is_explicit(tmp_path):
    store = SQLiteWatchJobStore(tmp_path / "watch.db")
    missing = str(uuid4())
    assert store.get(missing) is None
    with pytest.raises(WatchJobNotFound):
        store.require(missing)


def test_schedule_must_be_nonempty_when_present(tmp_path):
    store = SQLiteWatchJobStore(tmp_path / "watch.db")
    with pytest.raises(ValueError, match="schedule"):
        store.create(_job(), schedule_spec="   ")
