from __future__ import annotations

import json
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest

from local_agent.session.session_event_service import DurableSessionService
from local_agent.session.session_store import AdmissionConflict, SQLiteSessionStore
from local_agent.session.task_admission import repository_id
from local_agent.session.watch_task_admission import (
    DurableWatchAdmissionError,
    DurableWatchTaskAdmission,
)
from local_agent.watch.job_store import StoredWatchJob
from local_agent.watch.jobs import DirtyTreePolicy, FixedWatchJob, SourceSelection


def _service(tmp_path):
    return DurableSessionService(
        SQLiteSessionStore(tmp_path / "session.db"),
        stream_id=str(uuid4()),
        session_id=str(uuid4()),
    )


def _record(repo, *, version: int = 3) -> StoredWatchJob:
    job = FixedWatchJob(
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
    return StoredWatchJob(job=job, version=version, enabled=True, schedule_spec="hourly")


def _controller(repo):
    return SimpleNamespace(repo=repo, allow_execution=False, context_budget_tokens=12_000)


def test_watch_run_is_durably_admitted_with_schema_declared_origin(tmp_path, loaded):
    _sandbox, repo, _registry, _store, _skills = loaded
    service = _service(tmp_path)
    run_id = str(uuid4())
    record = _record(repo)
    try:
        admission = DurableWatchTaskAdmission(service, _controller(repo)).admit(
            record,
            run_id=run_id,
            task="Run the fixed fixture health procedure",
            skill="watch-fixture-health",
        )
        assert admission.created is True
        assert UUID(admission.task_id)
        assert admission.request_id == f"watch:{record.job.job_id}:{run_id}"

        events = service.replay()
        assert [event["kind"] for event in events] == ["task.admitted"]
        event = events[0]
        assert event["task_id"] == admission.task_id
        payload = event["payload"]
        assert payload["origin"] == {
            "kind": "watch",
            "job_id": record.job.job_id,
            "run_id": run_id,
        }
        assert payload["repository_id"] == repository_id(repo)
        assert payload["skill"] == "watch-fixture-health"
        assert payload["request_ref"]["availability"] == "retained"
        request = service.store.artifact_bytes(payload["request_ref"])
        assert request is not None
        decoded = json.loads(request)
        assert decoded["job_revision"] == record.job.revision_sha256
        assert decoded["configuration_version"] == record.version
        assert decoded["run_id"] == run_id
        assert decoded["task"] == "Run the fixed fixture health procedure"
    finally:
        service.close()


def test_same_watch_run_admission_is_idempotent_and_never_duplicates_event(tmp_path, loaded):
    _sandbox, repo, _registry, _store, _skills = loaded
    service = _service(tmp_path)
    runner = DurableWatchTaskAdmission(service, _controller(repo))
    record = _record(repo)
    run_id = str(uuid4())
    try:
        first = runner.admit(record, run_id=run_id, task="fixture health")
        second = runner.admit(record, run_id=run_id, task="fixture health")
        assert first.created is True
        assert second.created is False
        assert second.task_id == first.task_id
        assert [event["kind"] for event in service.replay()] == ["task.admitted"]
    finally:
        service.close()


def test_same_watch_run_identity_refuses_changed_request_bytes(tmp_path, loaded):
    _sandbox, repo, _registry, _store, _skills = loaded
    service = _service(tmp_path)
    runner = DurableWatchTaskAdmission(service, _controller(repo))
    record = _record(repo)
    run_id = str(uuid4())
    try:
        runner.admit(record, run_id=run_id, task="fixture health")
        with pytest.raises(AdmissionConflict, match="different task bytes"):
            runner.admit(record, run_id=run_id, task="different procedure")
        assert [event["kind"] for event in service.replay()] == ["task.admitted"]
    finally:
        service.close()


def test_watch_admission_refuses_wrong_repository_before_durable_write(tmp_path, loaded):
    _sandbox, repo, _registry, _store, _skills = loaded
    service = _service(tmp_path)
    record = _record(repo)
    wrong_job = FixedWatchJob(
        **{
            **record.job.__dict__,
            "repository_id": "other-repository",
        }
    )
    wrong_record = StoredWatchJob(
        job=wrong_job,
        version=record.version,
        enabled=record.enabled,
        schedule_spec=record.schedule_spec,
    )
    try:
        with pytest.raises(DurableWatchAdmissionError, match="repository identity"):
            DurableWatchTaskAdmission(service, _controller(repo)).admit(
                wrong_record,
                run_id=str(uuid4()),
                task="fixture health",
            )
        assert service.replay() == []
    finally:
        service.close()


def test_watch_admission_requires_preallocated_run_uuid(tmp_path, loaded):
    _sandbox, repo, _registry, _store, _skills = loaded
    service = _service(tmp_path)
    try:
        with pytest.raises(ValueError, match="watch run id must be a UUID"):
            DurableWatchTaskAdmission(service, _controller(repo)).admit(
                _record(repo),
                run_id="not-a-run-id",
                task="fixture health",
            )
        assert service.replay() == []
    finally:
        service.close()
