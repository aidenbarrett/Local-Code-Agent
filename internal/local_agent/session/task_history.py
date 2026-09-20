"""Read-only durable task history for conversation follow-up context.

Task artifacts remain siblings of raw conversation turns. This module projects one
bounded historical observation for chat context; it never upgrades stored worker prose
or an old verdict into current verification.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from .contracts import MAX_MESSAGE_CHARS, TaskOutcome, TaskVerdict, TerminalState
from .session_store import ArtifactIntegrityError, SQLiteSessionStore


_RESULT_SCHEMA = "lca.task-result/1"
_RESULT_MEDIA_TYPE = "application/vnd.lca.task-result+json"


@dataclass(frozen=True)
class TaskObservation:
    task_id: str
    turn_ref: dict[str, object]
    status: str
    verdict: str
    answer: str
    evidence_ids: tuple[str, ...]

    def prompt_text(self) -> str:
        evidence = ", ".join(self.evidence_ids) or "none"
        header = (
            f"Task {self.task_id}; historical status={self.status}; "
            f"historical verdict={self.verdict}; evidence={evidence}.\n"
        )
        return (header + self.answer)[:MAX_MESSAGE_CHARS]


class DurableTaskHistory:
    """Integrity-check retained task results before exposing them as history."""

    def __init__(self, store: SQLiteSessionStore):
        self.store = store

    @staticmethod
    def _parse_result(raw: bytes, *, task_id: str) -> dict[str, Any]:
        try:
            value = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as exc:
            raise ArtifactIntegrityError("retained task result is not valid UTF-8 JSON") from exc
        expected = {
            "schema", "task_id", "outcome", "terminal_state", "verdict",
            "verification_ran", "verified_at_completion", "evidence_ids", "answer",
        }
        if not isinstance(value, dict) or set(value) != expected:
            raise ArtifactIntegrityError("retained task result has an unexpected shape")
        if value["schema"] != _RESULT_SCHEMA or value["task_id"] != task_id:
            raise ArtifactIntegrityError("retained task result identity does not match task index")
        try:
            TaskOutcome(value["outcome"])
            TerminalState(value["terminal_state"])
            TaskVerdict(value["verdict"])
        except (TypeError, ValueError) as exc:
            raise ArtifactIntegrityError("retained task result has invalid typed state") from exc
        if not isinstance(value["verification_ran"], bool) or not isinstance(
            value["verified_at_completion"], bool
        ):
            raise ArtifactIntegrityError("retained task result has invalid verification flags")
        if not isinstance(value["answer"], str) or len(value["answer"]) > MAX_MESSAGE_CHARS:
            raise ArtifactIntegrityError("retained task result answer is invalid or unbounded")
        evidence = value["evidence_ids"]
        if not isinstance(evidence, list) or len(evidence) > 128 or not all(
            isinstance(item, str) and 0 < len(item) <= 256 for item in evidence
        ):
            raise ArtifactIntegrityError("retained task result evidence ids are invalid")
        return value

    def latest(self, conversation_id: str) -> TaskObservation | None:
        record = self.store.latest_terminal_task_for_conversation(conversation_id)
        if record is None:
            return None
        ref = record.get("result_ref")
        if not isinstance(ref, dict) or ref.get("availability") != "retained":
            # Pre-retention tasks remain durable facts, but no inspectable result
            # bytes exist to place into follow-up model context.
            return None
        if ref.get("media_type") != _RESULT_MEDIA_TYPE:
            raise ArtifactIntegrityError("retained task result has an unexpected media type")
        raw = self.store.artifact_bytes(ref)
        value = self._parse_result(raw, task_id=str(record["task_id"]))
        if value["terminal_state"] != str(record["state"]):
            raise ArtifactIntegrityError("retained task result state disagrees with task index")
        return TaskObservation(
            task_id=str(record["task_id"]),
            turn_ref=dict(record["turn_ref"]),
            status=str(value["terminal_state"]),
            verdict=str(value["verdict"]),
            answer=str(value["answer"]),
            evidence_ids=tuple(value["evidence_ids"]),
        )
