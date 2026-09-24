from __future__ import annotations

import hashlib
from uuid import uuid4

from local_agent.session.contracts import TaskOutcome, TaskResult
from local_agent.session.durable_task_controller import AdmittedDurableTaskController
from local_agent.session.endpoint_call import EndpointCallAdapter
from local_agent.session.endpoint_client import ManagedWorkerClientFactory
from local_agent.session.endpoint_lease import EndpointArbiter
from local_agent.session.endpoint_runtime import EndpointRuntime
from local_agent.session.session_event_service import DurableSessionService
from local_agent.session.session_store import SQLiteSessionStore


class _RawClient:
    def chat(self, messages, tools=None, max_tokens=None):
        return "owned"


def _payload(request: bytes, epoch: int) -> dict:
    return {
        "origin": {"kind": "user_direct", "turn_ref": {
            "conversation_id": "conv", "turn_index": 0, "turn_sha256": "a" * 64,
        }},
        "request_ref": {
            "artifact_id": str(uuid4()), "sha256": hashlib.sha256(request).hexdigest(),
            "media_type": "application/json", "size_bytes": len(request),
            "availability": "unavailable",
        },
        "contract_sha256": "b" * 64,
        "repository_id": "repo-1",
        "skill": "inspect",
        "execution_epoch": epoch,
        "deadline_utc": "2030-01-01T00:00:00Z",
    }


def test_durable_bridge_binds_worker_factory_to_exact_admitted_epoch(tmp_path):
    service = DurableSessionService(
        SQLiteSessionStore(tmp_path / "session.db"),
        stream_id=str(uuid4()), session_id=str(uuid4()),
    )
    endpoint = EndpointRuntime(EndpointArbiter("http://127.0.0.1:8000/v1"))
    factory = ManagedWorkerClientFactory(
        _RawClient, EndpointCallAdapter(endpoint), session_id=service.session_id,
    )
    request = b"request"
    receipt = service.submit_task(
        request_id="request-9", payload_sha256="c" * 64,
        admission_payload=_payload(request, 9),
    )
    receipt.wait(5)
    task_id = receipt.task_id
    assert task_id is not None
    service.append(
        "task.state_changed",
        {"previous": "admitted", "current": "running", "reason_code": "requested", "execution_epoch": 9},
        task_id=task_id,
    ).wait(5)

    class Controller:
        repo = object()
        allow_execution = False
        context_budget_tokens = 4096
        worker_factory = factory

        def resolve_skill(self, name):
            return name

        def run(self, task, **kwargs):
            assert factory().chat([]) == "owned"
            active = endpoint.arbiter.active_lease
            assert active is None
            return TaskResult(kwargs["task_id"], TaskOutcome.FAIL, "done", False)

    try:
        result = AdmittedDurableTaskController(service, Controller()).run(
            "inspect", task_id=task_id, route_source="user_direct"
        )
        assert result.task_id == task_id
        try:
            factory()
        except RuntimeError as exc:
            assert "admitted task authority" in str(exc)
        else:
            raise AssertionError("worker authority leaked beyond durable execution scope")
    finally:
        service.close()
