from __future__ import annotations

import hashlib
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
from uuid import UUID, uuid4


REPO = Path(__file__).resolve().parents[3]
INTERNAL = REPO / "internal"
SCRIPT = INTERNAL / "scripts" / "session-hub.py"


def _load_hub():
    spec = spec_from_file_location("session_hub_product", SCRIPT)
    assert spec and spec.loader
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


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
                "conversation_id": "product-conversation",
                "turn_index": 0,
                "turn_sha256": "a" * 64,
            },
        },
        "request_ref": _artifact_ref(),
        "contract_sha256": "b" * 64,
        "repository_id": "repo-1",
        "skill": None,
        "execution_epoch": 0,
        "deadline_utc": "2030-01-01T00:00:00Z",
    }


def test_root_session_command_reaches_product_composition_root():
    wrapper = (REPO / "local-code-agent.ps1").read_text(encoding="utf-8")
    assert "(Join-Path $internal 'scripts\\session-hub.py')" in wrapper
    assert "-m local_agent.session.cli @Rest" not in wrapper


def test_product_session_constructs_real_durable_components():
    source = SCRIPT.read_text(encoding="utf-8")
    assert "SQLiteSessionStore" in source
    assert "DurableSessionService" in source
    assert "recover_unknown_tasks()" in source
    assert 'service.append(\n        "session.opened"' in source
    assert "with conversation(runtime_root, conversation_id) as opened:" in source
    assert "conversation=opened" in source


def test_durable_stream_and_session_ids_are_stable_and_distinct():
    hub = _load_hub()
    first = hub._durable_ids("conversation-1")
    second = hub._durable_ids("conversation-1")
    other = hub._durable_ids("conversation-2")
    assert first == second
    assert first != other
    assert first[0] != first[1]
    UUID(first[0])
    UUID(first[1])


def test_product_restart_recovers_its_own_unfinished_stream_without_retry(tmp_path):
    hub = _load_hub()
    first, recovered = hub._open_durable_service(tmp_path, "conversation-1")
    try:
        assert recovered == []
        receipt = first.submit_task(
            request_id="unfinished-product-task",
            payload_sha256="c" * 64,
            admission_payload=_admission_payload(),
        )
        receipt.wait(5)
        task_id = receipt.task_id
        assert task_id is not None
        assert first.store.task_record(task_id)["terminal"] is False
    finally:
        first.close()

    second, recovered = hub._open_durable_service(tmp_path, "conversation-1")
    try:
        assert recovered == [task_id]
        record = second.store.task_record(task_id)
        assert record is not None and record["terminal"] is True
        events = second.replay()
        assert [event["kind"] for event in events] == [
            "task.admitted",
            "task.verdict",
            "task.closed",
        ]
        assert events[1]["payload"]["completion"]["verdict_block"]["verdict"] == "NO_VERDICT"
        assert events[2]["payload"]["cleanup"] == "unknown"
    finally:
        second.close()


def test_different_conversation_does_not_recover_another_stream(tmp_path):
    hub = _load_hub()
    first, _ = hub._open_durable_service(tmp_path, "conversation-1")
    try:
        receipt = first.submit_task(
            request_id="stream-owned-task",
            payload_sha256="c" * 64,
            admission_payload=_admission_payload(),
        )
        receipt.wait(5)
        task_id = receipt.task_id
        assert task_id is not None
    finally:
        first.close()

    other, recovered = hub._open_durable_service(tmp_path, "conversation-2")
    try:
        assert recovered == []
        assert other.store.task_record(task_id)["terminal"] is False
    finally:
        other.close()
