from __future__ import annotations

import hashlib
import json
from uuid import uuid4

import pytest

from local_agent.session.session_event_service import DurableSessionService
from local_agent.session.session_store import SQLiteSessionStore
from local_agent.session.textual_feed import DurableHubFeed, HubFeedError
from local_agent.session.textual_hub import render_activity


def _service(tmp_path) -> DurableSessionService:
    return DurableSessionService(
        SQLiteSessionStore(tmp_path / "session.db"),
        stream_id=str(uuid4()),
        session_id=str(uuid4()),
    )


def _admit(service: DurableSessionService) -> str:
    receipt = service.submit_task(
        request_id="retained-result-fixture",
        payload_sha256="a" * 64,
        admission_payload={
            "origin": {
                "kind": "user_direct",
                "turn_ref": {
                    "conversation_id": "conv-1",
                    "turn_index": 0,
                    "turn_sha256": "b" * 64,
                },
            },
            "request_ref": {
                "artifact_id": str(uuid4()),
                "sha256": "c" * 64,
                "media_type": "application/json",
                "size_bytes": 1,
                "availability": "unavailable",
            },
            "contract_sha256": "d" * 64,
            "repository_id": "repo-1",
            "skill": "inspect",
            "execution_epoch": 0,
            "deadline_utc": "2030-01-01T00:00:00Z",
        },
    )
    receipt.wait(5)
    assert receipt.task_id is not None
    return receipt.task_id


def _finish(
    service: DurableSessionService,
    task_id: str,
    *,
    answer: str,
    result_evidence: list[str] | None = None,
    verdict_evidence: list[str] | None = None,
) -> None:
    result_evidence = ["tool:0"] if result_evidence is None else result_evidence
    verdict_evidence = result_evidence if verdict_evidence is None else verdict_evidence
    payload = json.dumps(
        {
            "schema": "lca.task-result/1",
            "task_id": task_id,
            "outcome": "FAIL",
            "terminal_state": "failed",
            "verdict": "FAILED",
            "verification_ran": True,
            "verified_at_completion": False,
            "evidence_ids": result_evidence,
            "answer": answer,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    ref = {
        "artifact_id": str(uuid4()),
        "sha256": hashlib.sha256(payload).hexdigest(),
        "media_type": "application/vnd.lca.task-result+json",
        "size_bytes": len(payload),
        "availability": "retained",
    }
    receipt = service.finalize_task(
        task_id,
        verdict_payload={
            "completion": {
                "task_id": task_id,
                "status": "failed",
                "verdict_block": {
                    "verdict": "FAILED",
                    "reason_code": "verification_failed",
                    "scope": "fixture",
                    "evidence_ids": verdict_evidence,
                    "tree_sha256": None,
                    "rendered_lines": [
                        "FAILED: verification did not establish success.",
                        "Verification: ran but did not establish success.",
                    ],
                },
                "worker_artifact_ref": None,
                "result_ref": ref,
            }
        },
        closed_payload={"status": "failed", "result_ref": ref, "cleanup": "not_needed"},
        result_bytes=payload,
    )
    receipt.wait(5)


def test_live_hub_exposes_integrity_checked_retained_answer(tmp_path):
    service = _service(tmp_path)
    try:
        task_id = _admit(service)
        answer = "Compiler failed in src/widget.cpp:42 after the requested build."
        _finish(service, task_id, answer=answer)

        state = DurableHubFeed(service).start()

        assert state.tasks[0].result_answer == answer
        assert state.tasks[0].result_verification_ran is True
        assert state.tasks[0].result_verified_at_completion is False
        rendered = render_activity(state)
        assert "Retained result:" in rendered
        assert answer in rendered
        assert "Result verification: ran but did not establish success" in rendered
    finally:
        service.close()


def test_live_hub_refuses_result_that_disagrees_with_durable_verdict(tmp_path):
    service = _service(tmp_path)
    try:
        task_id = _admit(service)
        _finish(
            service,
            task_id,
            answer="historical answer",
            result_evidence=["result:0"],
            verdict_evidence=["different:0"],
        )

        with pytest.raises(HubFeedError, match="cannot be projected truthfully"):
            DurableHubFeed(service).start()
    finally:
        service.close()
