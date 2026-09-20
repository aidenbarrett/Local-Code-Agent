from __future__ import annotations

from uuid import uuid4

import pytest

from local_agent.session.endpoint_lease import (
    EndpointArbiter,
    EndpointLeaseConflict,
    EndpointQueueFull,
    EndpointRequest,
    EndpointRole,
    EndpointUnavailable,
    QueueClass,
)


ENDPOINT = "ovms://ptl-npu"


def _chat(session_id: str, *, request_id: str | None = None) -> EndpointRequest:
    return EndpointRequest(
        request_id=request_id or str(uuid4()),
        endpoint_id=ENDPOINT,
        role=EndpointRole.CONVERSATION,
        session_id=session_id,
    )


def _work(
    *,
    role: EndpointRole = EndpointRole.WORKER,
    request_id: str | None = None,
    task_id: str | None = None,
    epoch: int = 0,
) -> EndpointRequest:
    return EndpointRequest(
        request_id=request_id or str(uuid4()),
        endpoint_id=ENDPOINT,
        role=role,
        task_id=task_id or str(uuid4()),
        execution_epoch=epoch,
    )


def test_one_physical_endpoint_has_at_most_one_active_lease_across_roles():
    arbiter = EndpointArbiter(ENDPOINT)
    chat = _chat("conversation-1")
    worker = _work()
    arbiter.enqueue(chat)
    arbiter.enqueue(worker)

    first = arbiter.acquire_next()
    assert first is not None
    assert first.request == chat
    assert arbiter.acquire_next() is None
    arbiter.release(first.lease_id)

    second = arbiter.acquire_next()
    assert second is not None
    assert second.request == worker


def test_scheduler_alternates_chat_and_work_classes_while_preserving_fifo():
    arbiter = EndpointArbiter(ENDPOINT)
    chat1, chat2 = _chat("conversation-1"), _chat("conversation-2")
    work1, work2 = _work(), _work(role=EndpointRole.STRONG)
    for request in (chat1, chat2, work1, work2):
        arbiter.enqueue(request)

    observed = []
    for _ in range(4):
        lease = arbiter.acquire_next()
        assert lease is not None
        observed.append(lease.request.request_id)
        arbiter.release(lease.lease_id)

    assert observed == [
        chat1.request_id,
        work1.request_id,
        chat2.request_id,
        work2.request_id,
    ]


def test_waiting_worker_gets_next_slot_after_chat_even_if_chat_queue_stays_busy():
    arbiter = EndpointArbiter(ENDPOINT)
    first_chat = _chat("conversation-1")
    second_chat = _chat("conversation-1")
    worker = _work()
    arbiter.enqueue(first_chat)
    arbiter.enqueue(second_chat)

    lease = arbiter.acquire_next()
    assert lease is not None and lease.request == first_chat
    arbiter.enqueue(worker)
    arbiter.release(lease.lease_id)

    next_lease = arbiter.acquire_next()
    assert next_lease is not None
    assert next_lease.request == worker


def test_when_preferred_class_is_empty_other_class_can_progress_without_losing_fairness():
    arbiter = EndpointArbiter(ENDPOINT)
    chat1, chat2 = _chat("conversation-1"), _chat("conversation-1")
    arbiter.enqueue(chat1)
    arbiter.enqueue(chat2)

    first = arbiter.acquire_next()
    assert first is not None and first.request == chat1
    arbiter.release(first.lease_id)

    # Work is preferred now but absent, so chat may use the slot.
    second = arbiter.acquire_next()
    assert second is not None and second.request == chat2
    worker = _work()
    arbiter.enqueue(worker)
    arbiter.release(second.lease_id)

    # Preference remains work; the newly waiting worker is not starved.
    third = arbiter.acquire_next()
    assert third is not None and third.request == worker


def test_chat_queue_limit_is_per_session_but_position_is_real_chat_fifo():
    arbiter = EndpointArbiter(ENDPOINT, chat_pending_limit_per_session=4)
    a = [_chat("conversation-a") for _ in range(4)]
    b = _chat("conversation-b")
    for index, request in enumerate(a):
        position = arbiter.enqueue(request)
        assert position.queue_class == QueueClass.CHAT
        assert position.class_position == index

    b_position = arbiter.enqueue(b)
    assert b_position.class_position == 4
    assert arbiter.pending_position(b.request_id).class_position == 4

    with pytest.raises(EndpointQueueFull, match="this session"):
        arbiter.enqueue(_chat("conversation-a"))


def test_active_chat_does_not_count_against_pending_chat_limit():
    arbiter = EndpointArbiter(ENDPOINT, chat_pending_limit_per_session=2)
    active_request = _chat("conversation-a")
    arbiter.enqueue(active_request)
    lease = arbiter.acquire_next()
    assert lease is not None

    arbiter.enqueue(_chat("conversation-a"))
    arbiter.enqueue(_chat("conversation-a"))
    with pytest.raises(EndpointQueueFull):
        arbiter.enqueue(_chat("conversation-a"))


def test_worker_queue_is_bounded_and_strong_requests_share_the_same_work_class():
    arbiter = EndpointArbiter(ENDPOINT, work_pending_limit=2)
    first = _work(role=EndpointRole.WORKER)
    second = _work(role=EndpointRole.STRONG)
    assert arbiter.enqueue(first).class_position == 0
    assert arbiter.enqueue(second).class_position == 1
    with pytest.raises(EndpointQueueFull, match="worker endpoint queue"):
        arbiter.enqueue(_work())


def test_duplicate_request_identity_is_rejected_even_when_first_request_is_active():
    arbiter = EndpointArbiter(ENDPOINT)
    request_id = str(uuid4())
    first = _chat("conversation-1", request_id=request_id)
    arbiter.enqueue(first)
    lease = arbiter.acquire_next()
    assert lease is not None
    with pytest.raises(EndpointLeaseConflict, match="already active or queued"):
        arbiter.enqueue(_chat("conversation-2", request_id=request_id))


def test_quarantine_blocks_new_work_and_ordinary_release_until_explicit_reconciliation():
    arbiter = EndpointArbiter(ENDPOINT)
    first = _work()
    waiting = _chat("conversation-1")
    arbiter.enqueue(first)
    arbiter.enqueue(waiting)
    lease = arbiter.acquire_next()
    assert lease is not None and lease.request == waiting  # chat gets initial slot
    arbiter.quarantine("inference_timeout", lease_id=lease.lease_id)

    assert arbiter.quarantined
    with pytest.raises(EndpointUnavailable):
        arbiter.acquire_next()
    with pytest.raises(EndpointUnavailable):
        arbiter.enqueue(_chat("conversation-2"))
    with pytest.raises(EndpointLeaseConflict, match="explicit reconciliation"):
        arbiter.release(lease.lease_id)

    assert arbiter.reconcile_quarantine(known_stopped=False, lease_id=lease.lease_id) is False
    assert arbiter.quarantined
    assert arbiter.reconcile_quarantine(known_stopped=True, lease_id=lease.lease_id) is True
    assert not arbiter.quarantined

    next_lease = arbiter.acquire_next()
    assert next_lease is not None and next_lease.request == first


def test_quarantine_reason_cannot_be_silently_rewritten():
    arbiter = EndpointArbiter(ENDPOINT)
    arbiter.quarantine("endpoint_uncertain")
    arbiter.quarantine("endpoint_uncertain")
    with pytest.raises(EndpointLeaseConflict, match="different reason"):
        arbiter.quarantine("other_reason")
    assert arbiter.reconcile_quarantine(known_stopped=True)


def test_queued_request_can_be_removed_atomically_but_active_lease_cannot():
    arbiter = EndpointArbiter(ENDPOINT)
    chat = _chat("conversation-1")
    worker = _work()
    arbiter.enqueue(chat)
    arbiter.enqueue(worker)
    lease = arbiter.acquire_next()
    assert lease is not None and lease.request == chat

    assert arbiter.remove_queued(chat.request_id) is None
    assert arbiter.remove_queued(worker.request_id) == worker
    assert arbiter.pending_position(worker.request_id) is None


def test_worker_and_conversation_requests_carry_separate_authority_shapes():
    chat = _chat("conversation-1")
    assert chat.task_id is None
    assert chat.execution_epoch is None

    work = _work(epoch=7)
    assert work.task_id is not None
    assert work.execution_epoch == 7

    with pytest.raises(ValueError, match="cannot carry task execution authority"):
        EndpointRequest(
            request_id=str(uuid4()),
            endpoint_id=ENDPOINT,
            role=EndpointRole.CONVERSATION,
            session_id="conversation-1",
            task_id=str(uuid4()),
            execution_epoch=0,
        )
    with pytest.raises(ValueError, match="requires a task id"):
        EndpointRequest(
            request_id=str(uuid4()),
            endpoint_id=ENDPOINT,
            role=EndpointRole.WORKER,
            execution_epoch=0,
        )


def test_request_for_other_physical_endpoint_is_refused():
    arbiter = EndpointArbiter(ENDPOINT)
    other = EndpointRequest(
        request_id=str(uuid4()),
        endpoint_id="ovms://other",
        role=EndpointRole.CONVERSATION,
        session_id="conversation-1",
    )
    with pytest.raises(ValueError, match="different physical endpoint"):
        arbiter.enqueue(other)


def test_release_and_reconciliation_require_the_actual_lease_identity():
    arbiter = EndpointArbiter(ENDPOINT)
    arbiter.enqueue(_chat("conversation-1"))
    lease = arbiter.acquire_next()
    assert lease is not None
    with pytest.raises(EndpointLeaseConflict):
        arbiter.release(str(uuid4()))

    arbiter.quarantine("uncertain", lease_id=lease.lease_id)
    with pytest.raises(EndpointLeaseConflict):
        arbiter.reconcile_quarantine(known_stopped=True, lease_id=str(uuid4()))


def test_no_pending_position_is_reported_for_active_or_unknown_request():
    arbiter = EndpointArbiter(ENDPOINT)
    request = _chat("conversation-1")
    arbiter.enqueue(request)
    lease = arbiter.acquire_next()
    assert lease is not None
    assert arbiter.pending_position(request.request_id) is None
    assert arbiter.pending_position(str(uuid4())) is None
