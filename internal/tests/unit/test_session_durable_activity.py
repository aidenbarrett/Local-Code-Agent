from __future__ import annotations

import hashlib
from uuid import uuid4

import pytest

from local_agent.session.durable_activity import (
    DurableActivityError,
    DurableToolActivity,
    durable_tool_reason,
)
from local_agent.session.session_event_service import DurableSessionService
from local_agent.session.session_store import SQLiteSessionStore, TaskStateConflict


def _admission_payload(*, execution_epoch: int = 0) -> dict:
    payload = b"request"
    return {
        "origin": {
            "kind": "user_direct",
            "turn_ref": {
                "conversation_id": "conv-activity",
                "turn_index": 0,
                "turn_sha256": "a" * 64,
            },
        },
        "request_ref": {
            "artifact_id": str(uuid4()),
            "sha256": hashlib.sha256(payload).hexdigest(),
            "media_type": "application/json",
            "size_bytes": len(payload),
            "availability": "unavailable",
        },
        "contract_sha256": "b" * 64,
        "repository_id": "repo-1",
        "skill": "inspect",
        "execution_epoch": execution_epoch,
        "deadline_utc": "2030-01-01T00:00:00Z",
    }


def _admitted_service(tmp_path, *, execution_epoch: int = 0):
    service = DurableSessionService(
        SQLiteSessionStore(tmp_path / "session.db"),
        stream_id=str(uuid4()),
        session_id=str(uuid4()),
    )
    receipt = service.submit_task(
        request_id="request-1",
        payload_sha256="c" * 64,
        admission_payload=_admission_payload(execution_epoch=execution_epoch),
    )
    receipt.wait(5)
    assert receipt.task_id is not None
    return service, receipt.task_id


def test_tool_start_and_finish_commit_reviewed_durable_events(tmp_path):
    service, task_id = _admitted_service(tmp_path)
    try:
        activity = DurableToolActivity.from_task(service, task_id)
        opened = activity.start_tool("run_test")
        activity.finish_tool(
            call_id=opened.call_id,
            tool_name="run_test",
            execution="ok",
            domain="fail",
            reason=None,
            exit_code=1,
            duration_ms=17,
            evidence_ids=("run_test:0",),
        )

        events = service.replay()
        assert [event["kind"] for event in events] == [
            "task.admitted",
            "tool.started",
            "tool.finished",
        ]
        started = events[1]["payload"]
        finished = events[2]["payload"]
        assert started["call_id"] == finished["call_id"] == opened.call_id
        assert started["execution_epoch"] == finished["execution_epoch"] == 0
        assert started["deadline_utc"] == "2030-01-01T00:00:00Z"
        assert started["arguments_ref"] is None
        assert finished["execution"] == "ok"
        assert finished["domain"] == "fail"
        assert finished["reason_code"] == "completed"
        assert finished["evidence_ids"] == ["run_test:0"]
        assert activity.open_call is None
    finally:
        service.close()


def test_stale_execution_epoch_fails_before_a_tool_start_is_committed(tmp_path):
    service, task_id = _admitted_service(tmp_path, execution_epoch=0)
    try:
        activity = DurableToolActivity(
            service,
            task_id=task_id,
            execution_epoch=1,
            deadline_utc="2030-01-01T00:00:00Z",
        )
        with pytest.raises(TaskStateConflict, match="execution_epoch"):
            activity.start_tool("repo_info")
        assert [event["kind"] for event in service.replay()] == ["task.admitted"]
        assert activity.open_call is None
    finally:
        service.close()


def test_finish_requires_the_exact_open_call(tmp_path):
    service, task_id = _admitted_service(tmp_path)
    try:
        activity = DurableToolActivity.from_task(service, task_id)
        opened = activity.start_tool("repo_info")
        with pytest.raises(DurableActivityError, match="does not match"):
            activity.finish_tool(
                call_id=str(uuid4()),
                tool_name="repo_info",
                execution="ok",
                domain="pass",
                reason=None,
                exit_code=0,
                duration_ms=1,
            )
        assert activity.open_call == opened
        assert [event["kind"] for event in service.replay()] == [
            "task.admitted",
            "tool.started",
        ]
    finally:
        service.close()


def test_new_tool_reason_cannot_be_silently_projected_into_durable_history():
    with pytest.raises(DurableActivityError, match="unmapped"):
        durable_tool_reason(execution="error", reason="brand_new_reason")


def test_known_tool_reason_projection_is_explicit():
    assert durable_tool_reason(execution="blocked", reason="policy_denied") == "policy_denied"
    assert durable_tool_reason(execution="error", reason="orchestrator_timeout") == "tool_timeout"
    assert durable_tool_reason(execution="error", reason="stale_binary") == "stale_evidence"
