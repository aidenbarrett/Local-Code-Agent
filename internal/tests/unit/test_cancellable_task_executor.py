from __future__ import annotations

import hashlib
import threading
from uuid import uuid4

import pytest

from local_agent.session.cancellation_runtime import CancellationRuntimeError
from local_agent.session.cancellable_task_executor import CancellableDurableTaskExecutor
from local_agent.session.contracts import TaskOutcome, TaskResult
from local_agent.session.session_event_service import DurableSessionService
from local_agent.session.session_store import SQLiteSessionStore
from local_agent.session.task_history import DurableTaskHistory
from local_agent.session.task_read_model import project_task
from local_agent.session.terminal_truth import CancelUnreconciled


def _artifact_ref(data: bytes = b"request") -> dict:
    return {
        "artifact_id": str(uuid4()),
        "sha256": hashlib.sha256(data).hexdigest(),
        "media_type": "application/json",
        "size_bytes": len(data),
        "availability": "unavailable",
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


def _service(tmp_path):
    return DurableSessionService(
        SQLiteSessionStore(tmp_path / "session.db"),
        stream_id=str(uuid4()),
        session_id=str(uuid4()),
    )


def test_cancelled_epoch_cannot_commit_late_result_but_terminalizes_no_verdict(tmp_path):
    service = _service(tmp_path)
    entered = threading.Event()
    release = threading.Event()

    class Controller:
        def run(self, task, *, self_check=False, route_source=None, task_id=None, skill_name=None):
            assert task_id is not None
            entered.set()
            assert release.wait(5)
            return TaskResult(
                task_id,
                TaskOutcome.FAIL,
                "late result that must not regain authority",
                False,
                verification_ran=True,
            )

    try:
        executor = CancellableDurableTaskExecutor(service, Controller())
        handle = executor.submit(
            task="inspect",
            request_id="cancel-race",
            payload_sha256="c" * 64,
            admission_payload=_admission_payload(),
            route_source="user_direct",
        )
        assert entered.wait(5)

        decision = executor.request_cancel(handle.task_id, execution_epoch=0)
        assert decision.revoked_epoch == 0
        assert decision.current_epoch == 1
        assert executor.cancellation.current_epoch(handle.task_id) == 1

        release.set()
        with pytest.raises(CancelUnreconciled):
            handle.wait(5)

        events = service.replay()
        assert [event["kind"] for event in events] == [
            "task.admitted",
            "task.state_changed",
            "task.cancel_requested",
            "task.verdict",
            "task.closed",
        ]
        cancel = events[2]["payload"]
        assert cancel["request_id"] == decision.request.request_id
        assert cancel["execution_epoch"] == 0

        task = project_task(events, handle.task_id)
        assert task.terminal is True
        assert task.state == "unknown"
        assert task.verdict == "NO_VERDICT"
        assert task.verdict_reason == "cancel_unreconciled"
        assert task.cleanup == "not_needed"
        assert "cancelled" not in task.verdict_lines

        retained = DurableTaskHistory(
            service.store,
            stream_id=service.stream_id,
        ).result_for_task(handle.task_id)
        assert retained is not None
        assert retained.status == "unknown"
        assert retained.verdict == "NO_VERDICT"
        assert "late result that must not regain authority" not in retained.answer
        assert "Cleanup has not been fully reconciled" in retained.answer

        record = service.store.task_record(handle.task_id)
        assert record is not None
        assert record["terminal"] is True

        with pytest.raises(CancellationRuntimeError, match="not registered"):
            executor.request_cancel(handle.task_id, execution_epoch=1)
    finally:
        release.set()
        service.close()


def test_normal_terminal_commit_releases_cancellation_authority(tmp_path):
    service = _service(tmp_path)

    class Controller:
        def run(self, task, *, self_check=False, route_source=None, task_id=None, skill_name=None):
            return TaskResult(
                task_id,
                TaskOutcome.FAIL,
                "observed verification failure",
                False,
                verification_ran=True,
            )

    try:
        executor = CancellableDurableTaskExecutor(service, Controller())
        handle = executor.submit(
            task="inspect",
            request_id="terminal-wins",
            payload_sha256="c" * 64,
            admission_payload=_admission_payload(),
            route_source="user_direct",
        )
        result = handle.wait(5)
        assert result is not None
        assert service.store.task_record(handle.task_id)["terminal"] is True

        with pytest.raises(CancellationRuntimeError, match="not registered"):
            executor.request_cancel(handle.task_id, execution_epoch=0)
    finally:
        service.close()
