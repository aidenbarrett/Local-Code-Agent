from __future__ import annotations

import hashlib
import json
from uuid import uuid4

import pytest

from local_agent.session.session_event_service import DurableSessionService
from local_agent.session.session_store import SQLiteSessionStore


def _service(tmp_path):
    return DurableSessionService(
        SQLiteSessionStore(tmp_path / "session.db"),
        stream_id=str(uuid4()),
        session_id=str(uuid4()),
    )


def _artifact_ref(data: bytes) -> dict:
    return {
        "artifact_id": str(uuid4()),
        "sha256": hashlib.sha256(data).hexdigest(),
        "media_type": "application/vnd.lca.task-result+json",
        "size_bytes": len(data),
        "availability": "retained",
    }


def _bundle(task_id: str):
    result_bytes = json.dumps(
        {
            "schema": "lca.task-result/1",
            "task_id": task_id,
            "outcome": "FAIL",
            "terminal_state": "failed",
            "verdict": "FAILED",
            "verification_ran": True,
            "verified_at_completion": False,
            "evidence_ids": ["compiler:0"],
            "answer": "verification failed",
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    ref = _artifact_ref(result_bytes)
    verdict_payload = {
        "completion": {
            "task_id": task_id,
            "status": "failed",
            "verdict_block": {
                "verdict": "FAILED",
                "reason_code": "verification_failed",
                "scope": "controller task result at durable completion",
                "evidence_ids": ["compiler:0"],
                "tree_sha256": None,
                "rendered_lines": [
                    "FAILED: FAIL.",
                    "Verification: ran but did not establish success.",
                    "Evidence IDs: compiler:0.",
                ],
            },
            "worker_artifact_ref": None,
            "result_ref": ref,
        }
    }
    closed_payload = {"status": "failed", "result_ref": ref, "cleanup": "not_needed"}
    return result_bytes, verdict_payload, closed_payload


def _admit(service: DurableSessionService) -> str:
    receipt = service.submit_task(
        request_id="request-1",
        payload_sha256="c" * 64,
        admission_payload={
            "origin": {
                "kind": "user_direct",
                "turn_ref": {
                    "conversation_id": "conv-1",
                    "turn_index": 0,
                    "turn_sha256": "a" * 64,
                },
            },
            "request_ref": {
                "artifact_id": str(uuid4()),
                "sha256": "d" * 64,
                "media_type": "application/json",
                "size_bytes": 1,
                "availability": "unavailable",
            },
            "contract_sha256": "b" * 64,
            "repository_id": "repo-1",
            "skill": "inspect",
            "execution_epoch": 0,
            "deadline_utc": "2030-01-01T00:00:00Z",
        },
    )
    receipt.wait(5)
    assert receipt.task_id is not None
    return receipt.task_id


def test_terminal_bundle_rejects_status_contradiction_before_commit(tmp_path):
    service = _service(tmp_path)
    try:
        task_id = _admit(service)
        result_bytes, verdict_payload, closed_payload = _bundle(task_id)
        closed_payload["status"] = "completed"

        with pytest.raises(ValueError, match="terminal status"):
            service.finalize_task(
                task_id,
                verdict_payload=verdict_payload,
                closed_payload=closed_payload,
                result_bytes=result_bytes,
            )

        assert [event["kind"] for event in service.replay()] == ["task.admitted"]
        assert service.store.task_record(task_id)["terminal"] is False
    finally:
        service.close()


def test_terminal_bundle_rejects_rendered_verdict_contradiction(tmp_path):
    service = _service(tmp_path)
    try:
        task_id = _admit(service)
        result_bytes, verdict_payload, closed_payload = _bundle(task_id)
        verdict_payload["completion"]["verdict_block"]["rendered_lines"][0] = "VERIFIED: PASS."

        with pytest.raises(ValueError, match="rendered verdict"):
            service.finalize_task(
                task_id,
                verdict_payload=verdict_payload,
                closed_payload=closed_payload,
                result_bytes=result_bytes,
            )
    finally:
        service.close()


def test_terminal_bundle_rejects_cleanup_and_result_reference_lies(tmp_path):
    service = _service(tmp_path)
    try:
        task_id = _admit(service)
        result_bytes, verdict_payload, closed_payload = _bundle(task_id)
        closed_payload["cleanup"] = "unknown"
        with pytest.raises(ValueError, match="cleanup"):
            service.finalize_task(
                task_id,
                verdict_payload=verdict_payload,
                closed_payload=closed_payload,
                result_bytes=result_bytes,
            )

        _, verdict_payload, closed_payload = _bundle(task_id)
        closed_payload["result_ref"] = dict(closed_payload["result_ref"], artifact_id=str(uuid4()))
        with pytest.raises(ValueError, match="same retained result"):
            service.finalize_task(
                task_id,
                verdict_payload=verdict_payload,
                closed_payload=closed_payload,
                result_bytes=result_bytes,
            )
    finally:
        service.close()


def test_terminal_bundle_accepts_one_coherent_fact(tmp_path):
    service = _service(tmp_path)
    try:
        task_id = _admit(service)
        result_bytes, verdict_payload, closed_payload = _bundle(task_id)
        receipt = service.finalize_task(
            task_id,
            verdict_payload=verdict_payload,
            closed_payload=closed_payload,
            result_bytes=result_bytes,
        )
        receipt.wait(5)
        assert [event["kind"] for event in service.replay()] == [
            "task.admitted",
            "task.verdict",
            "task.closed",
        ]
        assert service.store.task_record(task_id)["terminal"] is True
    finally:
        service.close()
