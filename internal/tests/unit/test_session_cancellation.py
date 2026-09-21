from __future__ import annotations

from dataclasses import fields
from uuid import uuid4

import pytest

from local_agent.session.cancellation import (
    CancellationSite,
    CancellationSource,
    CancellationToken,
    EpochFence,
    OwnedProcessHandle,
    ProcessContainment,
    StaleExecutionEpoch,
    cancellation_plan,
)
from local_agent.session.contracts import TaskVerdict, TerminalState


def _task_id() -> str:
    return str(uuid4())


def test_advancing_epoch_revokes_stale_dispatch_authority():
    fence = EpochFence(3)
    assert fence.accepts(3)
    assert fence.advance() == 4
    assert not fence.accepts(3)
    assert fence.accepts(4)
    with pytest.raises(StaleExecutionEpoch, match="stale execution epoch 3"):
        fence.require_current(3)
    fence.require_current(4)


def test_epoch_inputs_fail_closed():
    with pytest.raises(ValueError, match="nonnegative"):
        EpochFence(-1)
    fence = EpochFence()
    with pytest.raises(ValueError, match="nonnegative"):
        fence.accepts(True)


def test_cancel_request_is_idempotent_and_preserves_first_provenance():
    token = CancellationToken(_task_id(), 7)
    first_id = str(uuid4())
    first = token.request(
        request_id=first_id,
        source=CancellationSource.USER,
        reason_code="requested",
    )
    second = token.request(
        request_id=str(uuid4()),
        source=CancellationSource.WATCHDOG,
        reason_code="task_timeout",
    )
    assert token.requested
    assert first is second
    assert token.first_request is first
    assert first.request_id == first_id
    assert first.source == CancellationSource.USER
    assert first.execution_epoch == 7


def test_cancellation_token_does_not_expose_false_cleanup_or_cancelled_claims():
    names = set(vars(CancellationToken(_task_id(), 0)))
    assert "cancelled" not in names
    assert "cleanup_complete" not in names
    assert "clean" not in names
    assert {field.name for field in fields(type(CancellationToken(_task_id(), 0).request()))} == {
        "request_id",
        "task_id",
        "execution_epoch",
        "source",
        "reason_code",
    }


def test_owned_process_identity_includes_birth_token_to_detect_pid_reuse():
    handle = OwnedProcessHandle(
        task_id=_task_id(),
        execution_epoch=2,
        pid=1234,
        birth_token="create-time:123456.25",
        containment=ProcessContainment.DIRECT_CHILD,
    )
    assert handle.pid == 1234
    assert handle.birth_token == "create-time:123456.25"
    assert not handle.has_tree_container


def test_posix_process_group_is_signalling_scope_and_windows_job_is_tree_container():
    posix = OwnedProcessHandle(
        task_id=_task_id(),
        execution_epoch=0,
        pid=100,
        birth_token="birth-posix",
        containment=ProcessContainment.POSIX_PROCESS_GROUP,
        containment_id=100,
    )
    windows = OwnedProcessHandle(
        task_id=_task_id(),
        execution_epoch=0,
        pid=200,
        birth_token="birth-windows",
        containment=ProcessContainment.WINDOWS_JOB,
        containment_id="job-7",
    )
    assert not posix.has_tree_container
    assert posix.cleanup_proof_scope == "process_group"
    assert windows.has_tree_container
    assert windows.cleanup_proof_scope == "whole_tree"


def test_process_containment_contract_rejects_unsupported_claims():
    with pytest.raises(ValueError, match="process-group"):
        OwnedProcessHandle(
            task_id=_task_id(),
            execution_epoch=0,
            pid=100,
            birth_token="birth",
            containment=ProcessContainment.POSIX_PROCESS_GROUP,
        )
    with pytest.raises(ValueError, match="Windows job"):
        OwnedProcessHandle(
            task_id=_task_id(),
            execution_epoch=0,
            pid=100,
            birth_token="birth",
            containment=ProcessContainment.WINDOWS_JOB,
        )
    with pytest.raises(ValueError, match="cannot carry"):
        OwnedProcessHandle(
            task_id=_task_id(),
            execution_epoch=0,
            pid=100,
            birth_token="birth",
            containment=ProcessContainment.DIRECT_CHILD,
            containment_id=100,
        )


def test_process_handle_from_old_epoch_cannot_regain_authority():
    fence = EpochFence(5)
    handle = OwnedProcessHandle(
        task_id=_task_id(),
        execution_epoch=5,
        pid=1234,
        birth_token="birth",
        containment=ProcessContainment.UNKNOWN,
    )
    fence.require_current(handle.execution_epoch)
    fence.advance()
    with pytest.raises(StaleExecutionEpoch):
        fence.require_current(handle.execution_epoch)


def test_queued_cancellation_is_only_immediate_terminal_plan():
    plan = cancellation_plan(CancellationSite.QUEUED)
    assert plan.revoke_future_dispatch
    assert plan.remove_queued_atomically
    assert not plan.requires_reconciliation
    assert plan.terminal_state_if_immediate == TerminalState.CANCELLED
    assert plan.verdict_if_immediate == TaskVerdict.NO_VERDICT


def test_waiting_inference_requires_endpoint_quarantine_and_reconciliation():
    plan = cancellation_plan(CancellationSite.WAITING_INFERENCE)
    assert plan.revoke_future_dispatch
    assert plan.quarantine_endpoint
    assert plan.requires_reconciliation
    assert plan.terminal_state_if_immediate is None
    assert plan.verdict_if_immediate is None


def test_subprocess_cancellation_requires_owned_tree_termination_and_reconciliation():
    plan = cancellation_plan(CancellationSite.SUBPROCESS)
    assert plan.terminate_owned_process_tree
    assert plan.requires_reconciliation
    assert not plan.quarantine_endpoint
    assert plan.terminal_state_if_immediate is None


def test_mutation_cancellation_requires_journal_reconciliation_before_terminal_claim():
    plan = cancellation_plan(CancellationSite.MUTATION)
    assert plan.reconcile_mutation
    assert plan.requires_reconciliation
    assert plan.terminal_state_if_immediate is None


def test_finalisation_cancellation_is_a_single_writer_terminal_race():
    plan = cancellation_plan(CancellationSite.FINALISING)
    assert plan.terminal_commit_race
    assert plan.requires_reconciliation
    assert plan.terminal_state_if_immediate is None


def test_every_cancellation_site_revokes_future_dispatch():
    for site in CancellationSite:
        assert cancellation_plan(site).revoke_future_dispatch


def test_unknown_cancellation_site_is_rejected():
    with pytest.raises(ValueError):
        cancellation_plan("somewhere_else")
