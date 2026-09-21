from __future__ import annotations

from uuid import uuid4

import pytest

from local_agent.session.cancellation import (
    CancellationSite,
    OwnedProcessHandle,
    ProcessContainment,
    StaleExecutionEpoch,
)
from local_agent.session.cancellation_runtime import (
    CancellationRuntime,
    CancellationRuntimeError,
)


def test_cancel_revokes_dispatch_epoch_and_retry_is_idempotent():
    runtime = CancellationRuntime()
    task_id = str(uuid4())
    runtime.register_task(task_id, execution_epoch=3)
    runtime.require_dispatch_authority(task_id, 3)

    first = runtime.request_cancel(task_id, 3, request_id=str(uuid4()))
    second = runtime.request_cancel(task_id, 3, request_id=str(uuid4()))

    assert second == first
    assert first.revoked_epoch == 3
    assert first.current_epoch == 4
    assert runtime.current_epoch(task_id) == 4
    with pytest.raises(StaleExecutionEpoch):
        runtime.require_dispatch_authority(task_id, 3)


def test_queued_cancel_requires_proof_that_queue_entry_was_removed():
    runtime = CancellationRuntime()
    task_id = str(uuid4())
    runtime.register_task(task_id, site=CancellationSite.QUEUED)
    decision = runtime.request_cancel(task_id, 0)
    assert decision.plan.remove_queued_atomically is True

    unresolved = runtime.reconcile(task_id)
    resolved = runtime.reconcile(task_id, queue_removed=True)
    assert unresolved.complete is False
    assert unresolved.missing_evidence == ("queue_removal_unproven",)
    assert resolved.complete is True


def test_inference_cancel_requires_endpoint_stop_reconciliation():
    runtime = CancellationRuntime()
    task_id = str(uuid4())
    runtime.register_task(task_id, site=CancellationSite.WAITING_INFERENCE)
    runtime.request_cancel(task_id, 0)

    assert runtime.reconcile(task_id, endpoint_stopped=False).complete is False
    assert runtime.reconcile(task_id, endpoint_stopped=True).complete is True


def test_direct_child_exit_cannot_be_misreported_as_process_tree_cleanup():
    runtime = CancellationRuntime()
    task_id = str(uuid4())
    runtime.register_task(task_id)
    runtime.attach_process(
        OwnedProcessHandle(
            task_id=task_id,
            execution_epoch=0,
            pid=123,
            birth_token="pid-123-birth",
            containment=ProcessContainment.DIRECT_CHILD,
        )
    )
    runtime.request_cancel(task_id, 0)

    report = runtime.reconcile(task_id, process_tree_stopped=True)
    assert report.complete is False
    assert report.missing_evidence == ("process_tree_containment_unproven",)


def test_tree_container_plus_stop_proof_can_reconcile_subprocess_cancel():
    runtime = CancellationRuntime()
    task_id = str(uuid4())
    runtime.register_task(task_id)
    runtime.attach_process(
        OwnedProcessHandle(
            task_id=task_id,
            execution_epoch=0,
            pid=456,
            birth_token="pid-456-birth",
            containment=ProcessContainment.POSIX_PROCESS_GROUP,
            containment_id=456,
        )
    )
    runtime.request_cancel(task_id, 0)

    assert runtime.reconcile(task_id).missing_evidence == ("process_tree_stop_unproven",)
    assert runtime.reconcile(task_id, process_tree_stopped=True).complete is True


def test_mutation_and_finalising_sites_require_their_specific_proof():
    mutation = CancellationRuntime()
    mutation_task = str(uuid4())
    mutation.register_task(mutation_task, site=CancellationSite.MUTATION)
    mutation.request_cancel(mutation_task, 0)
    assert mutation.reconcile(mutation_task).missing_evidence == (
        "mutation_reconciliation_unproven",
    )
    assert mutation.reconcile(mutation_task, mutation_reconciled=True).complete is True

    finalising = CancellationRuntime()
    final_task = str(uuid4())
    finalising.register_task(final_task, site=CancellationSite.FINALISING)
    finalising.request_cancel(final_task, 0)
    assert finalising.reconcile(final_task).missing_evidence == (
        "terminal_commit_race_unresolved",
    )
    assert finalising.reconcile(final_task, terminal_commit_resolved=True).complete is True


def test_cancelled_execution_cannot_acquire_new_authority():
    runtime = CancellationRuntime()
    task_id = str(uuid4())
    runtime.register_task(task_id)
    runtime.request_cancel(task_id, 0)

    with pytest.raises(StaleExecutionEpoch):
        runtime.set_site(task_id, 0, CancellationSite.MUTATION)
    with pytest.raises(StaleExecutionEpoch):
        runtime.attach_process(
            OwnedProcessHandle(
                task_id=task_id,
                execution_epoch=0,
                pid=999,
                birth_token="birth",
                containment=ProcessContainment.DIRECT_CHILD,
            )
        )


def test_unregistered_task_cannot_be_cancelled():
    runtime = CancellationRuntime()
    with pytest.raises(CancellationRuntimeError, match="not registered"):
        runtime.request_cancel(str(uuid4()), 0)
