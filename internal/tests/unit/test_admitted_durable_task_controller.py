from __future__ import annotations

import hashlib
from pathlib import Path
from uuid import uuid4

import pytest

from local_agent.session.contracts import TaskOutcome, TaskResult
from local_agent.session.durable_task_controller import AdmittedDurableTaskController
from local_agent.session.session_event_service import DurableSessionService
from local_agent.session.session_store import SQLiteSessionStore


REPO = Path(__file__).resolve().parents[3]


def _payload(*, execution_epoch: int = 0) -> dict:
    request = b"request"
    return {
        "origin": {
            "kind": "user_direct",
            "turn_ref": {
                "conversation_id": "conv-durable-controller",
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
        "skill": "inspect",
        "execution_epoch": execution_epoch,
        "deadline_utc": "2030-01-01T00:00:00Z",
    }


def _admit(service: DurableSessionService, *, execution_epoch: int = 0) -> str:
    receipt = service.submit_task(
        request_id=f"request-{execution_epoch}",
        payload_sha256="c" * 64,
        admission_payload=_payload(execution_epoch=execution_epoch),
    )
    receipt.wait(5)
    assert receipt.task_id is not None
    running = service.append(
        "task.state_changed",
        {
            "previous": "admitted",
            "current": "running",
            "reason_code": "requested",
            "execution_epoch": execution_epoch,
        },
        task_id=receipt.task_id,
    )
    running.wait(5)
    return receipt.task_id


class _ControllerContract:
    repo = object()
    allow_execution = False
    context_budget_tokens = 4096

    def resolve_skill(self, skill_name: str) -> str:
        return skill_name


def test_bridge_recovers_exact_admitted_epoch_and_deadline_before_controller_call(tmp_path):
    service = DurableSessionService(
        SQLiteSessionStore(tmp_path / "session.db"),
        stream_id=str(uuid4()),
        session_id=str(uuid4()),
    )
    captured = {}

    class Controller(_ControllerContract):
        def run(self, task, **kwargs):
            captured.update(kwargs)
            activity = kwargs["durable_activity"]
            assert [event["kind"] for event in service.replay()] == [
                "task.admitted",
                "task.state_changed",
            ]
            assert activity.execution_epoch == 7
            assert activity.deadline_utc == "2030-01-01T00:00:00Z"
            return TaskResult(kwargs["task_id"], TaskOutcome.FAIL, "failed", False)

    try:
        task_id = _admit(service, execution_epoch=7)
        base = Controller()
        bridge = AdmittedDurableTaskController(service, base)
        assert bridge.repo is base.repo
        assert bridge.allow_execution is False
        assert bridge.context_budget_tokens == 4096
        result = bridge.run(
            "inspect",
            self_check=False,
            route_source="user_direct",
            task_id=task_id,
        )
        assert result.task_id == task_id
        assert captured["task_id"] == task_id
        assert captured["route_source"] == "user_direct"
        assert captured["skill_name"] is None
    finally:
        service.close()


def test_bridge_requires_admitted_task_identity_before_controller_effect(tmp_path):
    service = DurableSessionService(
        SQLiteSessionStore(tmp_path / "session.db"),
        stream_id=str(uuid4()),
        session_id=str(uuid4()),
    )
    calls = []

    class Controller(_ControllerContract):
        def run(self, *args, **kwargs):
            calls.append((args, kwargs))
            raise AssertionError("controller must not run")

    try:
        bridge = AdmittedDurableTaskController(service, Controller())
        with pytest.raises(ValueError, match="admitted task id"):
            bridge.run("inspect")
        assert calls == []
    finally:
        service.close()


def test_public_session_composes_admitted_controller_before_durable_executor():
    source = (REPO / "internal" / "scripts" / "session-hub.py").read_text(encoding="utf-8")
    assert "AdmittedDurableTaskController(service, controller)" in source
    assert "DurableTaskExecutor(service, admitted_controller)" in source
    assert '"durable_tool_activity"' in source


def test_task_controller_wraps_registry_only_when_durable_activity_is_supplied():
    source = (REPO / "internal" / "local_agent" / "session" / "task_controller.py").read_text(
        encoding="utf-8"
    )
    assert "if durable_activity is not None:" in source
    assert "wrap_registry_with_durable_activity(registry, durable_activity)" in source
