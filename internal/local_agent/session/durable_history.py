"""Durable conversation-to-task history for Session Hub.

Raw conversation turns remain authoritative in ``conversation_store``. This module
adds a sibling SQLite index from trusted TurnRef values to durable task ids and a
bounded historical result summary. The index is committed with task admission;
result text is staged after controller completion but before terminalization and is
used only when the eventual durable verdict matches its expected completion.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from types import SimpleNamespace
from typing import Any

from .contracts import MAX_MESSAGE_CHARS, TaskOutcome, TaskResult, TaskVerdict
from .conversation_gateway import ConversationGateway
from .conversation_store import ContextRefusal, Session, turn_ref
from .event_contract import validate_event
from .session_store import SQLiteSessionStore, TaskStateConflict


class TurnTaskSessionStore(SQLiteSessionStore):
    """SQLite Session Hub store with an atomic TurnRef -> task sidecar index."""

    def _initialise(self) -> None:
        super()._initialise()
        with self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS turn_tasks (
                    task_id TEXT PRIMARY KEY,
                    stream_id TEXT NOT NULL,
                    conversation_id TEXT NOT NULL,
                    turn_index INTEGER NOT NULL CHECK(turn_index >= 0),
                    turn_sha256 TEXT NOT NULL,
                    admitted_sequence INTEGER NOT NULL CHECK(admitted_sequence >= 1),
                    FOREIGN KEY(task_id) REFERENCES tasks(task_id) ON DELETE CASCADE
                );
                CREATE INDEX IF NOT EXISTS idx_turn_tasks_conversation
                    ON turn_tasks(stream_id, conversation_id, admitted_sequence);
                CREATE TABLE IF NOT EXISTS task_summaries (
                    task_id TEXT PRIMARY KEY,
                    expected_status TEXT NOT NULL,
                    expected_verdict TEXT NOT NULL,
                    expected_reason TEXT NOT NULL,
                    answer TEXT NOT NULL,
                    answer_sha256 TEXT NOT NULL,
                    FOREIGN KEY(task_id) REFERENCES tasks(task_id) ON DELETE CASCADE
                );
                """
            )
            # Databases created by the previous Session Hub slice already contain
            # trustworthy task.admitted events. Rebuild the derived sidecar from
            # those events so upgrading and restarting does not lose associations.
            rows = conn.execute(
                "SELECT envelope_json FROM events WHERE kind = 'task.admitted' "
                "ORDER BY stream_id, sequence"
            ).fetchall()
            conn.execute("BEGIN IMMEDIATE")
            try:
                for row in rows:
                    envelope = json.loads(row["envelope_json"])
                    validate_event(envelope)
                    self._index_turn_task(conn, envelope)
                conn.execute("COMMIT")
            except BaseException:
                conn.execute("ROLLBACK")
                raise

    @staticmethod
    def _admission_turn_ref(envelope: dict[str, Any]) -> dict[str, object] | None:
        if envelope.get("kind") != "task.admitted":
            return None
        payload = envelope.get("payload")
        origin = payload.get("origin") if isinstance(payload, dict) else None
        value = origin.get("turn_ref") if isinstance(origin, dict) else None
        if value is None:
            # Watch-origin tasks intentionally have no conversation turn.
            return None
        required = {"conversation_id", "turn_index", "turn_sha256"}
        if not isinstance(value, dict) or set(value) != required:
            raise TaskStateConflict("task admission contains an invalid durable TurnRef")
        conversation_id = value["conversation_id"]
        turn_index = value["turn_index"]
        turn_sha256 = value["turn_sha256"]
        if not isinstance(conversation_id, str) or not conversation_id:
            raise TaskStateConflict("task admission TurnRef has an invalid conversation id")
        if not isinstance(turn_index, int) or isinstance(turn_index, bool) or turn_index < 0:
            raise TaskStateConflict("task admission TurnRef has an invalid turn index")
        if (
            not isinstance(turn_sha256, str)
            or len(turn_sha256) != 64
            or any(ch not in "0123456789abcdef" for ch in turn_sha256)
        ):
            raise TaskStateConflict("task admission TurnRef has an invalid turn hash")
        return {
            "conversation_id": conversation_id,
            "turn_index": turn_index,
            "turn_sha256": turn_sha256,
        }

    @classmethod
    def _index_turn_task(cls, conn: sqlite3.Connection, envelope: dict[str, Any]) -> None:
        ref = cls._admission_turn_ref(envelope)
        if ref is None:
            return
        task_id = str(envelope["task_id"])
        values = (
            task_id,
            str(envelope["stream_id"]),
            str(ref["conversation_id"]),
            int(ref["turn_index"]),
            str(ref["turn_sha256"]),
            int(envelope["sequence"]),
        )
        existing = conn.execute(
            "SELECT task_id, stream_id, conversation_id, turn_index, turn_sha256, admitted_sequence "
            "FROM turn_tasks WHERE task_id = ?",
            (task_id,),
        ).fetchone()
        if existing is not None:
            actual = (
                str(existing["task_id"]),
                str(existing["stream_id"]),
                str(existing["conversation_id"]),
                int(existing["turn_index"]),
                str(existing["turn_sha256"]),
                int(existing["admitted_sequence"]),
            )
            if actual != values:
                raise TaskStateConflict("task id is already associated with a different durable TurnRef")
            return
        conn.execute(
            "INSERT INTO turn_tasks(task_id, stream_id, conversation_id, turn_index, "
            "turn_sha256, admitted_sequence) VALUES(?, ?, ?, ?, ?, ?)",
            values,
        )

    @classmethod
    def _insert_event(cls, conn: sqlite3.Connection, envelope: dict[str, Any]) -> None:
        # SQLiteSessionStore.admit invokes this method inside the same BEGIN IMMEDIATE
        # transaction that inserts the task row. Dynamic dispatch therefore makes
        # the TurnRef association part of the admission commit, not a later repair.
        super()._insert_event(conn, envelope)
        cls._index_turn_task(conn, envelope)

    def task_ids_for_turn(
        self,
        stream_id: str,
        ref: dict[str, object],
    ) -> list[str]:
        checked = self._admission_turn_ref({
            "kind": "task.admitted",
            "task_id": "lookup",
            "stream_id": stream_id,
            "sequence": 1,
            "payload": {"origin": {"turn_ref": ref}},
        })
        assert checked is not None
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT task_id FROM turn_tasks WHERE stream_id = ? AND conversation_id = ? "
                "AND turn_index = ? AND turn_sha256 = ? ORDER BY admitted_sequence ASC",
                (
                    stream_id,
                    checked["conversation_id"],
                    checked["turn_index"],
                    checked["turn_sha256"],
                ),
            ).fetchall()
        return [str(row["task_id"]) for row in rows]

    def stage_task_summary(
        self,
        task_id: str,
        *,
        expected_status: str,
        expected_verdict: str,
        expected_reason: str,
        answer: str,
    ) -> None:
        """Persist bounded historical text before terminalization.

        A staged summary is not a verdict. Readers expose it only if the later
        durable terminal event exactly matches status/verdict/reason, so a crash
        between staging and terminalization cannot turn stale worker text into proof.
        """
        if not isinstance(answer, str):
            raise TypeError("task summary answer must be text")
        clipped = answer[:MAX_MESSAGE_CHARS]
        answer_sha256 = hashlib.sha256(clipped.encode("utf-8")).hexdigest()
        values = (
            task_id,
            expected_status,
            expected_verdict,
            expected_reason,
            clipped,
            answer_sha256,
        )
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT terminal FROM tasks WHERE task_id = ?", (task_id,)
            ).fetchone()
            if row is None:
                conn.execute("ROLLBACK")
                raise TaskStateConflict("cannot stage a summary for an unknown task")
            if int(row["terminal"]):
                conn.execute("ROLLBACK")
                raise TaskStateConflict("cannot stage a summary after task terminalization")
            existing = conn.execute(
                "SELECT task_id, expected_status, expected_verdict, expected_reason, answer, answer_sha256 "
                "FROM task_summaries WHERE task_id = ?",
                (task_id,),
            ).fetchone()
            if existing is None:
                conn.execute(
                    "INSERT INTO task_summaries(task_id, expected_status, expected_verdict, "
                    "expected_reason, answer, answer_sha256) VALUES(?, ?, ?, ?, ?, ?)",
                    values,
                )
            else:
                actual = tuple(str(existing[key]) for key in (
                    "task_id", "expected_status", "expected_verdict", "expected_reason",
                    "answer", "answer_sha256",
                ))
                if actual != values:
                    conn.execute("ROLLBACK")
                    raise TaskStateConflict("task summary was already staged with different bytes")
            conn.execute("COMMIT")

    def recent_turn_tasks(
        self,
        stream_id: str,
        conversation_id: str,
        *,
        limit: int = 4,
    ) -> list[dict[str, Any]]:
        if limit < 1 or limit > 32:
            raise ValueError("task history limit must be between 1 and 32")
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT tt.task_id, tt.conversation_id, tt.turn_index, tt.turn_sha256, "
                "tt.admitted_sequence, t.state, t.terminal, t.verdict_sequence, "
                "s.expected_status, s.expected_verdict, s.expected_reason, s.answer, "
                "s.answer_sha256, v.envelope_json AS verdict_json "
                "FROM turn_tasks tt JOIN tasks t ON t.task_id = tt.task_id "
                "LEFT JOIN task_summaries s ON s.task_id = tt.task_id "
                "LEFT JOIN events v ON v.stream_id = t.stream_id "
                "AND v.sequence = t.verdict_sequence AND v.task_id = t.task_id "
                "AND v.kind = 'task.verdict' "
                "WHERE tt.stream_id = ? AND tt.conversation_id = ? "
                "ORDER BY tt.admitted_sequence DESC LIMIT ?",
                (stream_id, conversation_id, limit),
            ).fetchall()
        out: list[dict[str, Any]] = []
        for row in rows:
            verdict = None
            if row["verdict_json"] is not None:
                verdict = json.loads(row["verdict_json"])
                validate_event(verdict)
            if int(row["terminal"]) and verdict is None:
                raise TaskStateConflict("terminal task is missing its durable verdict event")
            out.append({
                "task_id": str(row["task_id"]),
                "turn_ref": {
                    "conversation_id": str(row["conversation_id"]),
                    "turn_index": int(row["turn_index"]),
                    "turn_sha256": str(row["turn_sha256"]),
                },
                "admitted_sequence": int(row["admitted_sequence"]),
                "state": str(row["state"]),
                "terminal": bool(row["terminal"]),
                "verdict_event": verdict,
                "summary": None if row["answer"] is None else {
                    "expected_status": str(row["expected_status"]),
                    "expected_verdict": str(row["expected_verdict"]),
                    "expected_reason": str(row["expected_reason"]),
                    "answer": str(row["answer"]),
                    "answer_sha256": str(row["answer_sha256"]),
                },
            })
        return out


class ResultSummaryRecordingController:
    """Controller proxy that durably stages user-visible result text before close."""

    def __init__(self, controller, store: TurnTaskSessionStore):
        self._controller = controller
        self._store = store

    def __getattr__(self, name: str):
        return getattr(self._controller, name)

    @staticmethod
    def _reason_for(result: TaskResult) -> str:
        if result.projection.verdict == TaskVerdict.VERIFIED:
            return "verification_passed"
        if result.projection.verdict == TaskVerdict.FAILED:
            return "verification_failed"
        if result.outcome == TaskOutcome.BLOCKED:
            return "policy_denied"
        return "cleanup_unknown"

    def run(self, *args, **kwargs) -> TaskResult:
        result = self._controller.run(*args, **kwargs)
        self._store.stage_task_summary(
            result.task_id,
            expected_status=result.projection.terminal_state.value,
            expected_verdict=result.projection.verdict.value,
            expected_reason=self._reason_for(result),
            answer=result.answer,
        )
        return result


class DurableTaskHistory:
    """Render bounded, restart-safe historical observations for conversation routing."""

    def __init__(
        self,
        store: TurnTaskSessionStore,
        *,
        stream_id: str,
        max_tasks: int = 4,
        max_chars: int = 6_000,
        max_chars_per_task: int = 3_000,
    ):
        if max_tasks < 1 or max_chars < 1 or max_chars_per_task < 1:
            raise ValueError("durable task history budgets must be positive")
        self.store = store
        self.stream_id = stream_id
        self.max_tasks = max_tasks
        self.max_chars = min(max_chars, MAX_MESSAGE_CHARS)
        self.max_chars_per_task = min(max_chars_per_task, self.max_chars)

    @staticmethod
    def _matched_summary(record: dict[str, Any]) -> str | None:
        event = record["verdict_event"]
        summary = record["summary"]
        if event is None or summary is None:
            return None
        completion = event["payload"]["completion"]
        block = completion["verdict_block"]
        if (
            summary["expected_status"] != completion["status"]
            or summary["expected_verdict"] != block["verdict"]
            or summary["expected_reason"] != block["reason_code"]
        ):
            return None
        answer = summary["answer"]
        digest = hashlib.sha256(answer.encode("utf-8")).hexdigest()
        if digest != summary["answer_sha256"]:
            raise TaskStateConflict("durable task summary hash does not match its stored text")
        return answer

    def _render_record(self, record: dict[str, Any]) -> str:
        task_id = record["task_id"]
        turn_index = record["turn_ref"]["turn_index"]
        event = record["verdict_event"]
        if event is None:
            text = (
                f"Task {task_id} from conversation turn {turn_index}: "
                f"durable state={record['state']}; no terminal verdict exists."
            )
        else:
            completion = event["payload"]["completion"]
            block = completion["verdict_block"]
            lines = [
                f"Task {task_id} from conversation turn {turn_index}: "
                f"durable state={record['state']}; terminal status={completion['status']}; "
                f"verdict={block['verdict']}; reason={block['reason_code']}.",
            ]
            summary = self._matched_summary(record)
            if summary is not None:
                lines.append("Controller result at that completion:\n" + summary)
            else:
                lines.append("Durable terminal rendering:\n" + "\n".join(block["rendered_lines"]))
            text = "\n".join(lines)
        if len(text) > self.max_chars_per_task:
            text = text[: self.max_chars_per_task - 3] + "..."
        return text

    def render(self, session: Session) -> str | None:
        records = self.store.recent_turn_tasks(
            self.stream_id,
            session.conversation_id,
            limit=self.max_tasks,
        )
        selected: list[str] = []
        used = 0
        # Records arrive newest-first. Fill the budget with the newest information,
        # then present the selected window in chronological order.
        for record in records:
            ref = record["turn_ref"]
            index = int(ref["turn_index"])
            if not 0 <= index < len(session.turns):
                raise ContextRefusal("durable task history names a missing conversation turn")
            if session.turns[index].role != "user" or turn_ref(session, index) != ref:
                raise ContextRefusal(
                    "durable task history no longer matches the canonical conversation turn"
                )
            entry = self._render_record(record)
            extra = len(entry) + (2 if selected else 0)
            if selected and used + extra > self.max_chars:
                break
            if not selected and extra > self.max_chars:
                entry = entry[: self.max_chars]
                extra = len(entry)
            selected.append(entry)
            used += extra
        if not selected:
            return None
        selected.reverse()
        return "\n\n".join(selected)


class DurableConversationGateway(ConversationGateway):
    """Conversation gateway whose follow-up context comes only from durable history."""

    def __init__(self, *args, task_history: DurableTaskHistory, **kwargs):
        self.task_history = task_history
        super().__init__(*args, **kwargs)

    def _messages(self, said: str) -> list[dict[str, str]]:
        # The base gateway keeps last_result for immediate CLI status and legacy
        # non-durable use. Product Session Hub must not depend on that process-local
        # pointer. Replace it for prompt composition with a fresh durable projection
        # on every turn, then restore it for callers such as --check exit handling.
        process_local = self.last_result
        try:
            observation = self.task_history.render(self.session)
            self.last_result = (
                SimpleNamespace(answer=observation) if observation is not None else None
            )
            return super()._messages(said)
        finally:
            self.last_result = process_local
