from __future__ import annotations

from uuid import uuid4

import pytest

from local_agent.session.task_read_model import TaskReadModelError, project_task, project_tasks


def _ref():
    return {
        "artifact_id": str(uuid4()),
        "sha256": "a" * 64,
        "media_type": "application/json",
        "size_bytes": 1,
        "availability": "unavailable",
    }


def _event(sequence: int, kind: str, task_id: str, payload: dict):
    return {
        "schema_id": "lca.session.events",
        "schema_version": 1,
        "event_id": str(uuid4()),
        "stream_id": str(uuid4()),
        "stream_kind": "durable",
        "sequence": sequence,
        "producer_epoch": str(uuid4()),
        "session_id": str(uuid4()),
        "task_id": task_id,
        "occurred_utc": f"2030-01-01T00:00:{sequence:02d}Z",
        "kind": kind,
        "payload": payload,
    }


def _admitted(sequence: int, task_id: str, *, epoch: int = 0):
    return _event(
        sequence,
        "task.admitted",
        task_id,
        {
            "origin": {
                "kind": "user_direct",
                "turn_ref": {
                    "conversation_id": "conv-1",
                    "turn_index": 0,
                    "turn_sha256": "b" * 64,
                },
            },
            "request_ref": _ref(),
            "contract_sha256": "c" * 64,
            "repository_id": "repo-1",
            "skill": "build-and-test",
            "execution_epoch": epoch,
            "deadline_utc": "2030-01-01T01:00:00Z",
        },
    )


def _verdict(sequence: int, task_id: str, *, verdict: str = "VERIFIED"):
    result_ref = _ref()
    return _event(
        sequence,
        "task.verdict",
        task_id,
        {
            "completion": {
                "task_id": task_id,
                "status": "completed",
                "verdict_block": {
                    "verdict": verdict,
                    "reason_code": "verification_passed",
                    "scope": "repo",
                    "evidence_ids": ["run_test:0"],
                    "tree_sha256": None,
                    "rendered_lines": ["Verified by current-tree tests."],
                },
                "worker_artifact_ref": None,
                "result_ref": result_ref,
            }
        },
    )


def _closed(sequence: int, task_id: str):
    return _event(
        sequence,
        "task.closed",
        task_id,
        {"status": "completed", "result_ref": _ref(), "cleanup": "confirmed"},
    )


def test_projects_restart_safe_task_state_tool_endpoint_verdict_and_close():
    task_id = str(uuid4())
    call_id = str(uuid4())
    lease_id = str(uuid4())
    events = [
        _admitted(1, task_id),
        _event(2, "task.state_changed", task_id, {
            "previous": "admitted", "current": "running", "reason_code": "none", "execution_epoch": 0,
        }),
        _event(3, "endpoint.state_changed", task_id, {
            "endpoint_id": "ovms:npu:8000", "role": "worker", "state": "acquired",
            "queue_position": None, "wait_ms": 12, "lease_id": lease_id,
        }),
        _event(4, "tool.started", task_id, {
            "call_id": call_id, "tool_name": "run_test", "arguments_ref": None,
            "execution_epoch": 0, "deadline_utc": "2030-01-01T01:00:00Z",
        }),
        _event(5, "tool.finished", task_id, {
            "call_id": call_id, "tool_name": "run_test", "execution": "ok", "domain": "pass",
            "reason_code": "completed", "exit_code": 0, "duration_ms": 17,
            "evidence_ids": ["run_test:0"], "result_ref": None, "execution_epoch": 0,
        }),
        _verdict(6, task_id),
        _closed(7, task_id),
    ]

    snapshot = project_task(reversed(events), task_id)

    assert snapshot.terminal is True
    assert snapshot.state == "completed"
    assert snapshot.execution_epoch == 0
    assert snapshot.origin_kind == "user_direct"
    assert snapshot.repository_id == "repo-1"
    assert snapshot.skill == "build-and-test"
    assert snapshot.endpoint is not None and snapshot.endpoint.lease_id == lease_id
    assert snapshot.open_tools == {}
    assert snapshot.last_tool is not None
    assert snapshot.last_tool.execution == "ok"
    assert snapshot.last_tool.domain == "pass"
    assert snapshot.last_tool.evidence_ids == ("run_test:0",)
    assert snapshot.verdict == "VERIFIED"
    assert snapshot.verdict_reason == "verification_passed"
    assert snapshot.cleanup == "confirmed"
    assert [item.kind for item in snapshot.activity] == [event["kind"] for event in events]


def test_other_task_events_are_ignored():
    task_id = str(uuid4())
    other = str(uuid4())
    snapshot = project_task([
        _admitted(1, task_id),
        _admitted(2, other),
        _event(3, "task.state_changed", other, {
            "previous": "admitted", "current": "running", "reason_code": "none", "execution_epoch": 0,
        }),
    ], task_id)
    assert snapshot.state == "admitted"
    assert snapshot.last_sequence == 1


def test_state_previous_value_must_match_projected_authority():
    task_id = str(uuid4())
    with pytest.raises(TaskReadModelError, match="expected previous"):
        project_task([
            _admitted(1, task_id),
            _event(2, "task.state_changed", task_id, {
                "previous": "queued", "current": "running", "reason_code": "none", "execution_epoch": 0,
            }),
        ], task_id)


def test_tool_events_are_epoch_fenced_and_finish_must_match_open_call():
    task_id = str(uuid4())
    call_id = str(uuid4())
    with pytest.raises(TaskReadModelError, match="non-current execution epoch"):
        project_task([
            _admitted(1, task_id, epoch=1),
            _event(2, "tool.started", task_id, {
                "call_id": call_id, "tool_name": "git_status", "arguments_ref": None,
                "execution_epoch": 0, "deadline_utc": "2030-01-01T01:00:00Z",
            }),
        ], task_id)

    with pytest.raises(TaskReadModelError, match="no matching open tool"):
        project_task([
            _admitted(1, task_id),
            _event(2, "tool.finished", task_id, {
                "call_id": call_id, "tool_name": "git_status", "execution": "ok", "domain": "pass",
                "reason_code": "completed", "exit_code": 0, "duration_ms": 1,
                "evidence_ids": [], "result_ref": None, "execution_epoch": 0,
            }),
        ], task_id)


def test_duplicate_tool_start_is_refused():
    task_id = str(uuid4())
    call_id = str(uuid4())
    start = {
        "call_id": call_id, "tool_name": "git_status", "arguments_ref": None,
        "execution_epoch": 0, "deadline_utc": "2030-01-01T01:00:00Z",
    }
    with pytest.raises(TaskReadModelError, match="duplicate"):
        project_task([_admitted(1, task_id), _event(2, "tool.started", task_id, start), _event(3, "tool.started", task_id, start)], task_id)


def test_close_requires_verdict_and_lifecycle_cannot_continue_after_close():
    task_id = str(uuid4())
    with pytest.raises(TaskReadModelError, match="before task.verdict"):
        project_task([_admitted(1, task_id), _closed(2, task_id)], task_id)

    with pytest.raises(TaskReadModelError, match="after durable task.closed"):
        project_task([
            _admitted(1, task_id),
            _verdict(2, task_id),
            _closed(3, task_id),
            _event(4, "task.cancel_requested", task_id, {
                "request_id": str(uuid4()), "source": "user", "reason_code": "requested", "execution_epoch": 0,
            }),
        ], task_id)


def test_verdict_completion_cannot_name_another_task():
    task_id = str(uuid4())
    verdict = _verdict(2, task_id)
    verdict["payload"]["completion"]["task_id"] = str(uuid4())
    with pytest.raises(TaskReadModelError, match="different task"):
        project_task([_admitted(1, task_id), verdict], task_id)


def test_project_tasks_orders_by_durable_admission_sequence_and_rejects_duplicate_admission():
    first = str(uuid4())
    second = str(uuid4())
    snapshots = project_tasks([_admitted(5, second), _admitted(2, first)])
    assert [snapshot.task_id for snapshot in snapshots] == [first, second]

    with pytest.raises(TaskReadModelError, match="more than one"):
        project_tasks([_admitted(1, first), _admitted(2, first)])
