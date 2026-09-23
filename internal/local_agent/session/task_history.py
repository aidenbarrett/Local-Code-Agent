"""Read-only durable task history for conversation follow-up context.

Task artifacts remain siblings of raw conversation turns. This module projects bounded
historical observations for chat/task context; it never upgrades stored worker prose or
an old verdict into current verification or referent authority by itself.
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
class TaskCandidate:
    task_id: str
    turn_ref: dict[str, object]


@dataclass(frozen=True)
class RetainedTaskResult:
    """Integrity-checked historical terminal result; never current verification authority."""

    task_id: str
    status: str
    verdict: str
    answer: str
    evidence_ids: tuple[str, ...]
    verification_ran: bool
    verified_at_completion: bool


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

    def __init__(self, store: SQLiteSessionStore, *, stream_id: str | None = None):
        self.store = store
        self.stream_id = stream_id

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

    def _result_from_record(self, record: dict[str, Any]) -> RetainedTaskResult | None:
        ref = record.get("result_ref")
        if not isinstance(ref, dict) or ref.get("availability") != "retained":
            return None
        if ref.get("media_type") != _RESULT_MEDIA_TYPE:
            raise ArtifactIntegrityError("retained task result has an unexpected media type")
        task_id = str(record["task_id"])
        raw = self.store.artifact_bytes(ref)
        value = self._parse_result(raw, task_id=task_id)
        if value["terminal_state"] != str(record["state"]):
            raise ArtifactIntegrityError("retained task result state disagrees with task index")
        return RetainedTaskResult(
            task_id=task_id,
            status=str(value["terminal_state"]),
            verdict=str(value["verdict"]),
            answer=str(value["answer"]),
            evidence_ids=tuple(value["evidence_ids"]),
            verification_ran=bool(value["verification_ran"]),
            verified_at_completion=bool(value["verified_at_completion"]),
        )

    def result_for_task(self, task_id: str) -> RetainedTaskResult | None:
        """Return one retained terminal result only after artifact/index integrity checks."""
        record = self.store.task_record(task_id)
        if record is None or not bool(record.get("terminal")):
            return None
        return self._result_from_record(record)

    def _observation_from_record(
        self,
        record: dict[str, Any],
        *,
        turn_ref: dict[str, object],
    ) -> TaskObservation | None:
        result = self._result_from_record(record)
        if result is None:
            return None
        return TaskObservation(
            task_id=result.task_id,
            turn_ref=dict(turn_ref),
            status=result.status,
            verdict=result.verdict,
            answer=result.answer,
            evidence_ids=result.evidence_ids,
        )

    def latest(self, conversation_id: str) -> TaskObservation | None:
        record = self.store.latest_terminal_task_for_conversation(conversation_id)
        if record is None:
            return None
        return self._observation_from_record(record, turn_ref=dict(record["turn_ref"]))

    def observation_for(self, candidate: TaskCandidate) -> TaskObservation | None:
        record = self.store.task_record(candidate.task_id)
        if record is None or not bool(record.get("terminal")):
            return None
        return self._observation_from_record(record, turn_ref=dict(candidate.turn_ref))

    def _stream_events(self) -> list[dict[str, Any]]:
        if self.stream_id is None:
            return []
        out: list[dict[str, Any]] = []
        after = 0
        while True:
            batch = self.store.replay(self.stream_id, after=after, limit=1000)
            if not batch:
                break
            out.extend(batch)
            after = int(batch[-1]["sequence"])
            if len(batch) < 1000:
                break
        return out

    def failure_candidates(self, conversation_id: str) -> tuple[TaskCandidate, ...]:
        """Return all durable FAILED task candidates for deterministic referent routing.

        No recency guess is made here. If more than one candidate exists the routing
        policy must clarify rather than silently picking the newest task.
        """
        admissions: dict[str, TaskCandidate] = {}
        order: list[str] = []
        failed: set[str] = set()
        for event in self._stream_events():
            task_id = event.get("task_id")
            if event.get("kind") == "task.admitted" and isinstance(task_id, str):
                origin = event["payload"]["origin"]
                if origin.get("kind") == "watch":
                    continue
                ref = origin.get("turn_ref")
                if not isinstance(ref, dict) or ref.get("conversation_id") != conversation_id:
                    continue
                candidate = TaskCandidate(task_id=task_id, turn_ref=dict(ref))
                admissions[task_id] = candidate
                order.append(task_id)
            elif event.get("kind") == "task.verdict" and isinstance(task_id, str):
                completion = event["payload"]["completion"]
                if completion["verdict_block"]["verdict"] == TaskVerdict.FAILED.value:
                    failed.add(task_id)
        return tuple(admissions[task_id] for task_id in order if task_id in failed)
