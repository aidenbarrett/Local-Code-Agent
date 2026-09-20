from __future__ import annotations

import hashlib
from uuid import uuid4

from local_agent.session.conversation_store import append_turn, new_session, turn_ref
from local_agent.session.durable_history import DurableTaskHistory, TurnTaskSessionStore
from local_agent.session.event_contract import build_event


def _artifact(data: bytes) -> dict:
    return {
        "artifact_id": str(uuid4()),
        "sha256": hashlib.sha256(data).hexdigest(),
        "media_type": "application/json",
        "size_bytes": len(data),
        "availability": "unavailable",
    }


def test_staged_result_is_ignored_when_restart_terminal_reason_differs(tmp_path):
    session = new_session("test", "model", "cpu")
    append_turn(session, "user", "inspect repository", 0)
    ref = turn_ref(session, 0)
    stream_id = str(uuid4())
    session_id = str(uuid4())
    task_id = str(uuid4())
    epoch = str(uuid4())
    store = TurnTaskSessionStore(tmp_path / "session.db")

    admitted = build_event(
        stream_id=stream_id,
        sequence=1,
        producer_epoch=epoch,
        session_id=session_id,
        task_id=task_id,
        kind="task.admitted",
        payload={
            "origin": {"kind": "user_direct", "turn_ref": ref},
            "request_ref": _artifact(b"request"),
            "contract_sha256": "b" * 64,
            "repository_id": "repo-1",
            "skill": None,
            "execution_epoch": 0,
            "deadline_utc": "2030-01-01T00:00:00Z",
        },
    )
    store.admit(
        request_id="request-1",
        payload_sha256="c" * 64,
        envelope=admitted,
        expected_sequence=1,
    )

    # This is the dangerous crash window: the controller returned NO_VERDICT and
    # staged useful text, but the process died before its normal terminal commit.
    store.stage_task_summary(
        task_id,
        expected_status="unknown",
        expected_verdict="NO_VERDICT",
        expected_reason="cleanup_unknown",
        answer="STALE CONTROLLER RESULT THAT MUST NOT SURVIVE AS FOLLOW-UP AUTHORITY",
    )

    result_ref = _artifact(b"recovered")
    verdict = build_event(
        stream_id=stream_id,
        sequence=2,
        producer_epoch=str(uuid4()),
        session_id=session_id,
        task_id=task_id,
        kind="task.verdict",
        payload={
            "completion": {
                "task_id": task_id,
                "status": "unknown",
                "verdict_block": {
                    "verdict": "NO_VERDICT",
                    "reason_code": "controller_crash",
                    "scope": "effects and cleanup after controller restart",
                    "evidence_ids": [],
                    "tree_sha256": None,
                    "rendered_lines": [
                        "NO_VERDICT: controller restarted before a durable terminal result.",
                        "Cleanup unknown; task was not retried.",
                    ],
                },
                "worker_artifact_ref": None,
                "result_ref": result_ref,
            }
        },
    )
    closed = build_event(
        stream_id=stream_id,
        sequence=3,
        producer_epoch=str(uuid4()),
        session_id=session_id,
        task_id=task_id,
        kind="task.closed",
        payload={
            "status": "unknown",
            "result_ref": result_ref,
            "cleanup": "unknown",
        },
    )
    store.finalize_task(verdict, closed, expected_sequence=2)

    rendered = DurableTaskHistory(store, stream_id=stream_id).render(session)

    assert rendered is not None
    assert "STALE CONTROLLER RESULT" not in rendered
    assert "controller restarted before a durable terminal result" in rendered
    assert "reason=controller_crash" in rendered
