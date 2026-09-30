from __future__ import annotations

import hashlib
import json
import sys
import traceback
from time import monotonic
from uuid import uuid4

import pytest

from local_agent.session.session_event_service import DurableSessionService, WriteReceipt
from local_agent.session.session_store import SQLiteSessionStore
from local_agent.session.textual_feed import DurableHubFeed
from local_agent.session.textual_hub import render_activity
from local_agent.session.textual_live_app import HubLiveBinding

# Durable routes allow 30 seconds; these tests measure result projection, not
# writer scheduling latency under a busy Windows CI runner.
WRITE_WAIT_SECONDS = 30


def _wait_fixture_write(
    service: DurableSessionService,
    receipt: WriteReceipt,
    *,
    operation: str,
    index: int | None = None,
) -> None:
    """Keep a Windows fixture timeout tied to the writer's actual location."""
    started = monotonic()
    try:
        receipt.wait(WRITE_WAIT_SECONDS)
    except TimeoutError as exc:
        writer = service._thread
        frame = sys._current_frames().get(writer.ident) if writer.ident is not None else None
        writer_stack = "".join(traceback.format_stack(frame)) if frame else "unavailable"
        raise AssertionError(
            f"retained-result fixture {operation} timed out at index {index}; "
            f"elapsed={monotonic() - started:.1f}s; "
            f"receipt_committed={receipt.committed.is_set()}; "
            f"writer_alive={writer.is_alive()}; "
            f"queue_depth={service._commands.qsize()}; "
            f"writer_stack:\n{writer_stack}"
        ) from exc


def _service(tmp_path) -> DurableSessionService:
    return DurableSessionService(
        SQLiteSessionStore(tmp_path / "session.db"),
        stream_id=str(uuid4()),
        session_id=str(uuid4()),
    )


def test_fixture_timeout_names_the_write_and_writer_state(tmp_path, monkeypatch):
    service = _service(tmp_path)
    try:
        receipt = WriteReceipt()

        def timed_out(_timeout):
            raise TimeoutError("durable write did not commit before timeout")

        monkeypatch.setattr(receipt, "wait", timed_out)
        with pytest.raises(AssertionError, match="fixture finalize timed out at index 17") as error:
            _wait_fixture_write(service, receipt, operation="finalize", index=17)
        assert "writer_alive=True" in str(error.value)
        assert "writer_stack:" in str(error.value)
        assert isinstance(error.value.__cause__, TimeoutError)
    finally:
        service.close()


def _admit(
    service: DurableSessionService,
    *,
    request_id: str = "retained-result-fixture",
    turn_index: int = 0,
) -> str:
    receipt = service.submit_task(
        request_id=request_id,
        payload_sha256="a" * 64,
        admission_payload={
            "origin": {
                "kind": "user_direct",
                "turn_ref": {
                    "conversation_id": "conv-1",
                    "turn_index": turn_index,
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
    _wait_fixture_write(service, receipt, operation="admit", index=turn_index)
    assert receipt.task_id is not None
    return receipt.task_id


def _finish(
    service: DurableSessionService,
    task_id: str,
    *,
    answer: str,
    fixture_index: int | None = None,
    result_evidence: list[str] | None = None,
    verdict_evidence: list[str] | None = None,
) -> None:
    result_evidence = ["tool:0"] if result_evidence is None else result_evidence
    verdict_evidence = result_evidence if verdict_evidence is None else verdict_evidence
    payload = json.dumps(
        {
            "schema": "lca.task-result/1",
            "task_id": task_id,
            "outcome": "fail",
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
    _wait_fixture_write(service, receipt, operation="finalize", index=fixture_index)


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
        assert "Retained result (unverified as completion):" in rendered
        assert answer in rendered
        assert "Result verification: ran but did not establish success" in rendered
    finally:
        service.close()


def test_idle_hub_poll_does_not_reread_500_retained_results(tmp_path, monkeypatch):
    service = _service(tmp_path)
    try:
        for index in range(500):
            task_id = _admit(
                service,
                request_id=f"retained-result-{index}",
                turn_index=index,
            )
            _finish(service, task_id, answer=f"historical result {index}", fixture_index=index)

        binding = HubLiveBinding(DurableHubFeed(service))
        original_artifact_bytes = service.store.artifact_bytes
        reads = {"count": 0}

        def counted_artifact_bytes(ref):
            reads["count"] += 1
            return original_artifact_bytes(ref)

        monkeypatch.setattr(service.store, "artifact_bytes", counted_artifact_bytes)

        for _ in range(50):
            assert binding.poll() is None
        assert reads["count"] == 0

        receipt = service.append(
            "session.opened",
            {
                "conversation_id": "conv-1",
                "repository_id": "repo-1",
                "controller_commit": "fixture",
                "capabilities": [],
                "recovered": False,
            },
        )
        receipt.wait(WRITE_WAIT_SECONDS)
        assert binding.poll() is not None
        assert reads["count"] == 0
    finally:
        service.close()


def test_contradictory_retained_result_is_rejected_before_durable_commit(tmp_path):
    service = _service(tmp_path)
    try:
        task_id = _admit(service)
        with pytest.raises(ValueError, match="verdict evidence_ids disagree"):
            _finish(
                service,
                task_id,
                answer="historical answer",
                result_evidence=["result:0"],
                verdict_evidence=["different:0"],
            )

        record = service.store.task_record(task_id)
        assert record is not None
        assert record["terminal"] is False
        assert [event["kind"] for event in service.replay()] == ["task.admitted"]
    finally:
        service.close()


def test_live_hub_labels_prepared_candidate_as_not_applied(tmp_path):
    service = _service(tmp_path)
    try:
        task_id = _admit(service, request_id="candidate-ready")
        candidate = {
            "role": "prepared",
            "candidate_task_id": task_id,
            "retained": True,
            "paths": ["src/widget.cpp"],
            "patch_sha256": "e" * 64,
            "base_commit": "f" * 40,
            "commit": None,
        }
        payload = json.dumps(
            {
                "schema": "lca.task-result/2",
                "task_id": task_id,
                "outcome": "pass",
                "terminal_state": "completed",
                "verdict": "VERIFIED",
                "verification_ran": True,
                "verified_at_completion": True,
                "evidence_ids": ["candidate-proof:0"],
                "answer": "Candidate prepared and verified.",
                "candidate": candidate,
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
        service.finalize_task(
            task_id,
            verdict_payload={
                "completion": {
                    "task_id": task_id,
                    "status": "completed",
                    "verdict_block": {
                        "verdict": "VERIFIED",
                        "reason_code": "verification_passed",
                        "scope": "full_test",
                        "evidence_ids": ["candidate-proof:0"],
                        "tree_sha256": None,
                        "rendered_lines": ["VERIFIED: candidate checks passed."],
                    },
                    "worker_artifact_ref": None,
                    "result_ref": ref,
                }
            },
            closed_payload={"status": "completed", "result_ref": ref, "cleanup": "not_needed"},
            result_bytes=payload,
        ).wait(5)

        state = DurableHubFeed(service).start()
        rendered = render_activity(state)

        assert state.tasks[0].candidate is not None
        assert "Candidate: READY · NOT APPLIED" in rendered
        assert "isolated candidate tree, not the checkout" in rendered
        assert f"Apply: /apply {task_id}" in rendered
    finally:
        service.close()
