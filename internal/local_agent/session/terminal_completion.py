"""Semantic invariant checks for one durable task terminal bundle.

Schema validation proves each event is shaped correctly. This module proves the
retained result, verdict event and closed event describe the *same* terminal fact
before the writer is allowed to commit them atomically.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any

from .contracts import TaskOutcome, TaskResult

_RESULT_SCHEMA = "lca.task-result/1"
_RESULT_MEDIA_TYPE = "application/vnd.lca.task-result+json"


def _mapping(value: object, name: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{name} must be an object")
    return value


def _retained_result(task_id: str, result_bytes: bytes | None) -> tuple[TaskResult, dict[str, Any]]:
    if not isinstance(result_bytes, bytes) or not result_bytes:
        raise ValueError("durable task completion requires retained result bytes")
    try:
        raw = json.loads(result_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("retained task result must be UTF-8 JSON") from exc
    result = _mapping(raw, "retained task result")
    required = {
        "schema",
        "task_id",
        "outcome",
        "terminal_state",
        "verdict",
        "verification_ran",
        "verified_at_completion",
        "evidence_ids",
        "answer",
    }
    if set(result) != required:
        raise ValueError("retained task result fields do not match lca.task-result/1")
    if result["schema"] != _RESULT_SCHEMA:
        raise ValueError("retained task result schema mismatch")
    if result["task_id"] != task_id:
        raise ValueError("retained task result task_id disagrees with terminal task")
    if not isinstance(result["answer"], str):
        raise ValueError("retained task result answer must be text")
    evidence = result["evidence_ids"]
    if not isinstance(evidence, list) or any(not isinstance(item, str) or not item for item in evidence):
        raise ValueError("retained task result evidence_ids must be nonempty strings")
    if not isinstance(result["verification_ran"], bool) or not isinstance(
        result["verified_at_completion"], bool
    ):
        raise ValueError("retained task result verification flags must be booleans")

    try:
        outcome = TaskOutcome(result["outcome"])
        typed = TaskResult(
            task_id=task_id,
            outcome=outcome,
            answer=result["answer"],
            verified_at_completion=result["verified_at_completion"],
            evidence_ids=tuple(evidence),
            verification_ran=result["verification_ran"],
        )
    except (TypeError, ValueError) as exc:
        raise ValueError("retained task result violates TaskResult semantics") from exc
    if result["terminal_state"] != typed.projection.terminal_state.value:
        raise ValueError("retained task result terminal_state contradicts outcome")
    if result["verdict"] != typed.projection.verdict.value:
        raise ValueError("retained task result verdict contradicts outcome")
    return typed, result


def _validate_result_ref(ref: object, result_bytes: bytes) -> dict[str, Any]:
    value = _mapping(ref, "terminal result_ref")
    required = {"artifact_id", "sha256", "media_type", "size_bytes", "availability"}
    if set(value) != required:
        raise ValueError("terminal result_ref fields are not canonical")
    if value["media_type"] != _RESULT_MEDIA_TYPE or value["availability"] != "retained":
        raise ValueError("terminal result_ref must identify a retained task-result artifact")
    if value["sha256"] != hashlib.sha256(result_bytes).hexdigest():
        raise ValueError("terminal result_ref sha256 disagrees with retained result bytes")
    if value["size_bytes"] != len(result_bytes):
        raise ValueError("terminal result_ref size disagrees with retained result bytes")
    if not isinstance(value["artifact_id"], str) or not value["artifact_id"]:
        raise ValueError("terminal result_ref artifact_id must be nonempty")
    return value


def validate_terminal_completion(
    task_id: str,
    *,
    verdict_payload: dict[str, Any],
    closed_payload: dict[str, Any],
    result_bytes: bytes | None,
) -> None:
    """Reject a semantically contradictory terminal bundle before enqueue/commit."""
    typed, retained = _retained_result(task_id, result_bytes)
    assert result_bytes is not None

    verdict_root = _mapping(verdict_payload, "task.verdict payload")
    if set(verdict_root) != {"completion"}:
        raise ValueError("task.verdict payload must contain exactly completion")
    completion = _mapping(verdict_root["completion"], "task.verdict completion")
    closed = _mapping(closed_payload, "task.closed payload")
    verdict_block = _mapping(completion.get("verdict_block"), "task.verdict verdict_block")

    if completion.get("task_id") != task_id:
        raise ValueError("task.verdict completion task_id disagrees with terminal task")
    status = typed.projection.terminal_state.value
    verdict = typed.projection.verdict.value
    if completion.get("status") != status or closed.get("status") != status:
        raise ValueError("terminal status disagrees with retained task result")
    if verdict_block.get("verdict") != verdict:
        raise ValueError("verdict block disagrees with retained task result")
    if verdict_block.get("evidence_ids") != retained["evidence_ids"]:
        raise ValueError("verdict evidence_ids disagree with retained task result")

    completion_ref = _validate_result_ref(completion.get("result_ref"), result_bytes)
    closed_ref = _validate_result_ref(closed.get("result_ref"), result_bytes)
    if completion_ref != closed_ref:
        raise ValueError("task.verdict and task.closed must reference the same retained result")

    cleanup = closed.get("cleanup")
    expected_cleanup = "unknown" if status == "unknown" else "not_needed"
    if cleanup != expected_cleanup:
        raise ValueError("task.closed cleanup contradicts terminal status")

    lines = verdict_block.get("rendered_lines")
    if not isinstance(lines, list) or not lines or any(not isinstance(line, str) for line in lines):
        raise ValueError("verdict rendered_lines must contain text")
    if not lines[0].startswith(f"{verdict}:"):
        raise ValueError("rendered verdict contradicts structured verdict")

    verification_lines = [line for line in lines if line.startswith("Verification:")]
    if verification_lines:
        if typed.verified_at_completion:
            expected = "Verification: passed at task completion."
        elif typed.verification_ran:
            expected = "Verification: ran but did not establish success."
        else:
            expected = "Verification: not established."
        if verification_lines != [expected]:
            raise ValueError("rendered verification contradicts retained verification facts")
