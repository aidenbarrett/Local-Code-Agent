from __future__ import annotations

import hashlib
from uuid import uuid4

import pytest

from local_agent.session.cancellable_task_executor import CancellableDurableTaskExecutor
from local_agent.session.contracts import TaskOutcome, TaskResult
from local_agent.session.durable_activity import DurableToolActivity
from local_agent.session.session_event_service import DurableSessionService
from local_agent.session.session_store import SQLiteSessionStore


def _service(tmp_path) -> DurableSessionService:
    return DurableSessionService(
        SQLiteSessionStore(tmp_path / "session.db"),
        stream_id=str(uuid4()),
        session_id=str(uuid4()),
    )


def _admission_payload() -> dict:
    request = b"request"
    return {
        "origin": {
            "kind": "user_direct",
            "turn_ref": {
                "conversation_id": "conv-terminal-truth",
                "turn_index": 0,
                "turn_sha256": "a" * 64,
            },
        },
        "request_ref": {
            "artifact_id": str(uuid4()),
            "sha256": hashlib.sha256(request).hexdigest(),
            "media_type": "application/json",
            "size_bytes": len(request),
            "availability": "unavailable",
        },
        "contract_sha256": "b" * 64,
        "repository_id": "repo-1",
        "skill": "build-and-test",
        "execution_epoch": 0,
        "deadline_utc": "2030-01-01T00:00:00Z",
    }


def _submit(executor, *, request_id: str):
    return executor.submit(
        task="build it",
        request_id=request_id,
        payload_sha256="c" * 64,
        admission_payload=_admission_payload(),
        route_source="user_direct",
    )


def test_timed_out_build_cannot_claim_cleanup_not_needed(tmp_path):
    service = _service(tmp_path)

    class Controller:
        def process_spawning_tools(self):
            return {"build_target", "run_test"}

        def run(self, task, *, self_check=False, route_source=None, task_id=None, skill_name=None):
            activity = DurableToolActivity.from_task(service, task_id)
            opened = activity.start_tool("build_target")
            activity.finish_tool(
                call_id=opened.call_id,
                tool_name=opened.tool_name,
                execution="error",
                domain="unknown",
                reason="orchestrator_timeout",
                exit_code=None,
                duration_ms=1000,
            )
            return TaskResult(
                task_id,
                TaskOutcome.FAIL,
                "build timed out",
                False,
                verification_ran=False,
                reason_code="missing_evidence",
            )

    try:
        handle = _submit(CancellableDurableTaskExecutor(service, Controller()), request_id="timeout")
        assert handle.wait(5) is not None
        closed = next(event for event in service.replay() if event["kind"] == "task.closed")
        assert closed["payload"]["cleanup"] == "attempted"
        assert closed["payload"]["cleanup"] != "not_needed"
    finally:
        service.close()


def test_controller_attribute_error_is_durable_fault_and_still_raises(tmp_path):
    service = _service(tmp_path)

    class Controller:
        def process_spawning_tools(self):
            return set()

        def run(self, task, *, self_check=False, route_source=None, task_id=None, skill_name=None):
            raise AttributeError("injected controller bug")

    try:
        handle = _submit(CancellableDurableTaskExecutor(service, Controller()), request_id="fault")
        with pytest.raises(AttributeError, match="injected controller bug"):
            handle.wait(5)

        events = service.replay()
        verdict = next(event for event in events if event["kind"] == "task.verdict")
        completion = verdict["payload"]["completion"]
        assert completion["verdict_block"]["reason_code"] == "controller_fault"
        assert completion["worker_artifact_ref"] == completion["result_ref"]
        retained = service.store.artifact_bytes(completion["worker_artifact_ref"])
        assert retained is not None
        assert b"AttributeError" in retained
        assert b"injected controller bug" in retained
        assert service.store.task_record(handle.task_id)["terminal"] is True
    finally:
        service.close()


def test_open_tool_activity_forbids_completed_terminal_state(tmp_path):
    service = _service(tmp_path)

    class Controller:
        def process_spawning_tools(self):
            return set()

        def run(self, task, *, self_check=False, route_source=None, task_id=None, skill_name=None):
            DurableToolActivity.from_task(service, task_id).start_tool("read_file")
            return TaskResult(
                task_id,
                TaskOutcome.PASS,
                "claimed complete while tool lifecycle is open",
                True,
                verification_ran=True,
                reason_code="verification_passed",
            )

    try:
        handle = _submit(CancellableDurableTaskExecutor(service, Controller()), request_id="open-tool")
        with pytest.raises(RuntimeError, match="durable tool activity remained open"):
            handle.wait(5)
        verdict = next(event for event in service.replay() if event["kind"] == "task.verdict")
        assert verdict["payload"]["completion"]["verdict_block"]["reason_code"] == "controller_fault"
        assert verdict["payload"]["completion"]["status"] == "unknown"
    finally:
        service.close()


def test_restart_recovery_records_controller_restarted(tmp_path):
    service = _service(tmp_path)
    try:
        admission = service.submit_task(
            request_id="restart",
            payload_sha256="c" * 64,
            admission_payload=_admission_payload(),
        )
        admission.wait(5)
        assert admission.task_id is not None

        assert service.recover_unknown_tasks() == [admission.task_id]

        verdict = next(event for event in service.replay() if event["kind"] == "task.verdict")
        closed = next(event for event in service.replay() if event["kind"] == "task.closed")
        assert verdict["payload"]["completion"]["verdict_block"]["reason_code"] == "controller_restarted"
        assert closed["payload"]["cleanup"] == "unknown"
    finally:
        service.close()
