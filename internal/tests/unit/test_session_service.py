from __future__ import annotations

import hashlib
import time
from uuid import uuid4

import pytest

from local_agent.session.contracts import ProductOutcome, TaskResult
from local_agent.session.session_event_service import DurableSessionService, DurableTaskExecutor, SubscriptionGap
from local_agent.session.session_store import SQLiteSessionStore


def _artifact_ref(data: bytes = b"request") -> dict:
    return {
        "artifact_id": str(uuid4()),
        "sha256": hashlib.sha256(data).hexdigest(),
        "media_type": "application/json",
        "size_bytes": len(data),
        "availability": "retained",
    }


def _admission_payload() -> dict:
    return {
        "origin": {
            "kind": "user_direct",
            "turn_ref": {
                "conversation_id": "conv-1",
                "turn_index": 0,
                "turn_sha256": "a" * 64,
            },
        },
        "request_ref": _artifact_ref(),
        "contract_sha256": "b" * 64,
        "repository_id": "repo-1",
        "skill": "inspect",
        "execution_epoch": 0,
        "deadline_utc": "2030-01-01T00:00:00Z",
    }


def _service(tmp_path, *, subscriber_capacity=8):
    stream_id = str(uuid4())
    session_id = str(uuid4())
    service = DurableSessionService(
        SQLiteSessionStore(tmp_path / "session.db"),
        stream_id=stream_id,
        session_id=session_id,
    )
    return service, service.subscribe(capacity=subscriber_capacity)


def test_submit_returns_task_id_before_durable_commit_and_publish_follows_commit(tmp_path):
    service, sub = _service(tmp_path)
    try:
        started = time.monotonic()
        receipt = service.submit_task(
            request_id="request-1",
            payload_sha256="c" * 64,
            admission_payload=_admission_payload(),
        )
        assert receipt.task_id is not None
        assert time.monotonic() - started < 0.25
        receipt.wait(5)
        assert receipt.created is True

        durable = service.replay()
        delivered = sub.drain()
        assert durable == delivered
        assert durable[0]["kind"] == "task.admitted"
        assert durable[0]["task_id"] == receipt.task_id
    finally:
        service.close()


def test_same_request_id_returns_same_task_and_does_not_duplicate_admission(tmp_path):
    service, _ = _service(tmp_path)
    try:
        first = service.submit_task(
            request_id="same-request", payload_sha256="c" * 64,
            admission_payload=_admission_payload(),
        )
        first.wait(5)
        second = service.submit_task(
            request_id="same-request", payload_sha256="c" * 64,
            admission_payload=_admission_payload(),
        )
        second.wait(5)
        assert first.task_id == second.task_id
        assert first.created is True
        assert second.created is False
        assert [event["kind"] for event in service.replay()] == ["task.admitted"]
    finally:
        service.close()


def test_subscription_overflow_is_explicit_gap_not_silent_loss(tmp_path):
    service, sub = _service(tmp_path, subscriber_capacity=1)
    try:
        one = service.append("session.opened", {
            "conversation_id": "conv-1",
            "repository_id": "repo-1",
            "controller_commit": "deadbeef",
            "capabilities": ["inspect"],
            "recovered": False,
        })
        one.wait(5)
        two = service.append("session.opened", {
            "conversation_id": "conv-1",
            "repository_id": "repo-1",
            "controller_commit": "deadbeef",
            "capabilities": ["inspect"],
            "recovered": True,
        })
        two.wait(5)
        with pytest.raises(SubscriptionGap):
            sub.drain()
        assert len(service.replay()) == 2
    finally:
        service.close()


def test_crash_recovery_closes_unknown_task_without_reexecution(tmp_path):
    service, _ = _service(tmp_path)
    try:
        admitted = service.submit_task(
            request_id="request-1", payload_sha256="c" * 64,
            admission_payload=_admission_payload(),
        )
        admitted.wait(5)
        task_id = admitted.task_id
        assert task_id is not None

        recovered = service.recover_unknown_tasks()
        assert recovered == [task_id]
        events = service.replay()
        assert [event["kind"] for event in events] == [
            "task.admitted", "task.verdict", "task.closed"
        ]
        completion = events[1]["payload"]["completion"]
        assert completion["status"] == "unknown"
        assert completion["verdict_block"]["verdict"] == "NO_VERDICT"
        assert completion["verdict_block"]["reason_code"] == "controller_crash"
        assert events[2]["payload"]["status"] == "unknown"
        assert events[2]["payload"]["cleanup"] == "unknown"
        assert service.store.unterminated_tasks() == []
    finally:
        service.close()


def test_durable_executor_waits_for_admission_and_never_reexecutes_same_request(tmp_path):
    service, _ = _service(tmp_path)
    calls: list[str] = []

    class Controller:
        def run(self, task, *, self_check=False, route_source=None, task_id=None):
            assert task_id is not None
            assert [row["task_id"] for row in service.store.unterminated_tasks()] == [task_id]
            calls.append(task_id)
            return TaskResult(
                task_id,
                ProductOutcome.FAIL,
                "verification failed",
                False,
                verification_ran=True,
            )

    try:
        executor = DurableTaskExecutor(service, Controller())
        payload = _admission_payload()
        first = executor.submit(
            task="inspect",
            request_id="same",
            payload_sha256="c" * 64,
            admission_payload=payload,
            route_source="user_direct",
        )
        result = first.wait(10)
        assert result is not None and result.outcome == ProductOutcome.FAIL
        assert calls == [first.task_id]
        assert [event["kind"] for event in service.replay()] == [
            "task.admitted", "task.state_changed", "task.verdict", "task.closed"
        ]

        retry = executor.submit(
            task="inspect",
            request_id="same",
            payload_sha256="c" * 64,
            admission_payload=payload,
            route_source="user_direct",
        )
        assert retry.wait(10) is None
        assert retry.task_id == first.task_id
        assert calls == [first.task_id]
        assert len(service.replay()) == 4
    finally:
        service.close()