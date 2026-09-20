from __future__ import annotations

from dataclasses import fields, replace
from uuid import uuid4

import pytest

from local_agent.watch.delta import (
    ComparisonStatus,
    ObservationChange,
    WatchAttempt,
    compare_attempts,
)
from local_agent.watch.jobs import (
    DELTA_SCHEMA_V1,
    DirtyTreePolicy,
    FixedWatchJob,
    SourceSelection,
)


SHA_A = "a" * 64
SHA_B = "b" * 64
SHA_C = "c" * 64


def _job(**overrides) -> FixedWatchJob:
    values = dict(
        job_id=str(uuid4()),
        display_name="LCA build/test",
        repository_id="repo-1",
        repository_host="linux-vm-1",
        source_selection=SourceSelection.LOCAL_REF,
        source_selector="HEAD",
        dirty_tree_policy=DirtyTreePolicy.REFUSE,
        command_profile="default",
        environment_allowlist=("PATH", "CC", "CXX"),
        verification_contract_sha256=SHA_A,
        scope=("build", "test"),
        timeout_seconds=900,
        skill_contract_sha256=None,
        endpoint_policy=None,
        delta_schema_version=DELTA_SCHEMA_V1,
    )
    values.update(overrides)
    return FixedWatchJob(**values)


def _attempt(job: FixedWatchJob, **overrides) -> WatchAttempt:
    values = dict(
        run_id=str(uuid4()),
        job_id=job.job_id,
        job_revision=job.revision_sha256,
        execution_contract_sha256=SHA_B,
        result_schema=job.delta_schema_version,
        source_head="abc123",
        source_digest=SHA_C,
        complete=True,
        observations=(("build", "pass"), ("test:unit", "pass")),
    )
    values.update(overrides)
    return WatchAttempt(**values)


def test_display_rename_does_not_change_job_revision():
    job = _job()
    renamed = replace(job, display_name="Nightly verification")
    assert renamed.job_id == job.job_id
    assert renamed.revision_sha256 == job.revision_sha256


def test_job_id_is_separate_from_procedure_revision():
    first = _job()
    second = replace(first, job_id=str(uuid4()))
    assert second.revision_sha256 == first.revision_sha256
    assert second.job_id != first.job_id


def test_environment_and_scope_order_do_not_create_fake_revision_changes():
    first = _job(environment_allowlist=("PATH", "CC", "CXX"), scope=("test", "build"))
    second = replace(first, environment_allowlist=("CXX", "PATH", "CC"), scope=("build", "test"))
    assert first.environment_allowlist == ("CC", "CXX", "PATH")
    assert first.scope == ("build", "test")
    assert second.revision_sha256 == first.revision_sha256


@pytest.mark.parametrize(
    "field,value",
    [
        ("repository_id", "repo-2"),
        ("repository_host", "linux-vm-2"),
        ("source_selection", SourceSelection.FIXED_ROOT),
        ("source_selector", "/trusted/repo"),
        ("dirty_tree_policy", DirtyTreePolicy.ALLOW_READONLY),
        ("command_profile", "release"),
        ("environment_allowlist", ("PATH",)),
        ("verification_contract_sha256", SHA_B),
        ("scope", ("test",)),
        ("timeout_seconds", 901),
        ("skill_contract_sha256", SHA_B),
        ("endpoint_policy", "shared-npu"),
        ("delta_schema_version", "lca.watch.delta/2"),
    ],
)
def test_procedure_fields_move_job_revision(field, value):
    job = _job()
    assert replace(job, **{field: value}).revision_sha256 != job.revision_sha256


def test_job_contract_has_no_secret_values_or_observed_head_fields():
    names = {field.name for field in fields(FixedWatchJob)}
    assert "environment_allowlist" in names
    assert "environment" not in names
    assert "environment_values" not in names
    assert "secrets" not in names
    assert "source_head" not in names
    assert "source_digest" not in names


def test_revision_payload_excludes_label_and_stable_job_identity():
    payload = _job().revision_payload()
    assert "job_id" not in payload
    assert "display_name" not in payload
    assert "source_head" not in payload
    assert "source_digest" not in payload


def test_invalid_job_contract_fails_closed():
    with pytest.raises(ValueError, match="UUID"):
        _job(job_id="not-a-uuid")
    with pytest.raises(ValueError, match="positive"):
        _job(timeout_seconds=0)
    with pytest.raises(ValueError, match="variable name"):
        _job(environment_allowlist=("TOKEN=value",))
    with pytest.raises(ValueError, match="unique"):
        _job(scope=("test", "test"))
    with pytest.raises(ValueError, match="lowercase SHA-256"):
        _job(verification_contract_sha256="A" * 64)


def test_source_head_and_content_may_change_between_comparable_runs():
    job = _job()
    previous = _attempt(job, source_head="old", source_digest=SHA_A)
    current = _attempt(job, source_head="new", source_digest=SHA_C)
    delta = compare_attempts(previous, current)
    assert delta.status == ComparisonStatus.COMPARABLE
    assert delta.reasons == ()
    assert delta.changed == ()
    assert delta.unchanged_count == 2


def test_comparable_delta_reports_stable_key_changes():
    job = _job()
    previous = _attempt(
        job,
        observations=(("build", "pass"), ("test:unit", "pass"), ("test:old", "fail")),
    )
    current = _attempt(
        job,
        observations=(("build", "pass"), ("test:unit", "fail"), ("test:new", "pass")),
    )
    delta = compare_attempts(previous, current)
    assert delta.status == ComparisonStatus.COMPARABLE
    assert delta.added == ("test:new",)
    assert delta.removed == ("test:old",)
    assert delta.changed == (ObservationChange("test:unit", "pass", "fail"),)
    assert delta.unchanged_count == 1


@pytest.mark.parametrize(
    "overrides,reason",
    [
        ({"job_id": str(uuid4())}, "job_id_changed"),
        ({"job_revision": SHA_C}, "job_revision_changed"),
        ({"execution_contract_sha256": SHA_C}, "execution_contract_changed"),
        ({"result_schema": "lca.watch.delta/2"}, "result_schema_changed"),
        ({"complete": False}, "attempt_incomplete"),
    ],
)
def test_incomparable_runs_explain_why(overrides, reason):
    job = _job()
    previous = _attempt(job)
    current = _attempt(job, **overrides)
    delta = compare_attempts(previous, current)
    assert delta.status == ComparisonStatus.INCOMPARABLE
    assert reason in delta.reasons
    assert delta.added == ()
    assert delta.removed == ()
    assert delta.changed == ()
    assert delta.unchanged_count == 0


def test_same_attempt_is_not_a_fake_delta():
    job = _job()
    attempt = _attempt(job)
    delta = compare_attempts(attempt, attempt)
    assert delta.status == ComparisonStatus.INCOMPARABLE
    assert delta.reasons == ("same_run",)


def test_attempt_observations_are_sorted_and_duplicate_keys_fail_closed():
    job = _job()
    attempt = _attempt(job, observations=(("z", "1"), ("a", "2")))
    assert attempt.observations == (("a", "2"), ("z", "1"))
    with pytest.raises(ValueError, match="unique"):
        _attempt(job, observations=(("x", "1"), ("x", "2")))
