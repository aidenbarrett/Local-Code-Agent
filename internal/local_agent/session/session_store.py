"""Durable Session Hub event/task state.

The store is deliberately boring: SQLite WAL, transactional stream sequencing,
and validated JSON envelopes. It is not a verification oracle and it never
replays task effects. Admission and terminalization are durable atomic fences.
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from .event_contract import validate_event


class SequenceConflict(RuntimeError):
    pass


class AdmissionConflict(RuntimeError):
    pass


class TaskStateConflict(RuntimeError):
    pass


_TERMINAL_TASK_STATES = frozenset({
    "completed", "failed", "blocked", "cancelled", "timed_out", "interrupted", "unknown",
})


class _ClosingConnection(sqlite3.Connection):
    """Commit/rollback like sqlite3.Connection, then always release the handle."""

    def __exit__(self, exc_type, exc, tb):
        try:
            return super().__exit__(exc_type, exc, tb)
        finally:
            self.close()


class SQLiteSessionStore:
    def __init__(self, path: Path | str):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialise()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(
            self.path,
            timeout=30,
            isolation_level=None,
            factory=_ClosingConnection,
        )
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=30000")
        return conn

    def _initialise(self) -> None:
        with self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS streams (
                    stream_id TEXT PRIMARY KEY,
                    next_sequence INTEGER NOT NULL CHECK(next_sequence >= 1)
                );
                CREATE TABLE IF NOT EXISTS events (
                    stream_id TEXT NOT NULL,
                    sequence INTEGER NOT NULL CHECK(sequence >= 1),
                    event_id TEXT NOT NULL UNIQUE,
                    task_id TEXT,
                    kind TEXT NOT NULL,
                    envelope_json TEXT NOT NULL,
                    PRIMARY KEY(stream_id, sequence)
                );
                CREATE INDEX IF NOT EXISTS idx_events_task ON events(task_id, sequence);
                CREATE TABLE IF NOT EXISTS tasks (
                    task_id TEXT PRIMARY KEY,
                    session_id TEXT,
                    request_id TEXT NOT NULL UNIQUE,
                    payload_sha256 TEXT NOT NULL,
                    stream_id TEXT NOT NULL,
                    admitted_sequence INTEGER NOT NULL,
                    execution_epoch INTEGER NOT NULL,
                    state TEXT NOT NULL,
                    terminal INTEGER NOT NULL CHECK(terminal IN (0, 1)),
                    verdict_sequence INTEGER,
                    closed_sequence INTEGER,
                    result_ref_json TEXT
                );
                """
            )
            # Forward-only compatibility for databases created before later task
            # index fields became load-bearing. Existing rows predate epoch fencing,
            # so epoch zero is their only truthful reconstructable value.
            columns = {
                str(row["name"])
                for row in conn.execute("PRAGMA table_info(tasks)").fetchall()
            }
            if "execution_epoch" not in columns:
                conn.execute(
                    "ALTER TABLE tasks ADD COLUMN execution_epoch INTEGER NOT NULL DEFAULT 0"
                )
            if "verdict_sequence" not in columns:
                conn.execute("ALTER TABLE tasks ADD COLUMN verdict_sequence INTEGER")
            if "result_ref_json" not in columns:
                conn.execute("ALTER TABLE tasks ADD COLUMN result_ref_json TEXT")

    @staticmethod
    def _encoded(envelope: dict[str, Any]) -> str:
        return json.dumps(envelope, sort_keys=True, separators=(",", ":"), allow_nan=False)

    @staticmethod
    def _encoded_value(value: Any) -> str:
        return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)

    @staticmethod
    def _next_sequence(conn: sqlite3.Connection, stream_id: str) -> int:
        row = conn.execute(
            "SELECT next_sequence FROM streams WHERE stream_id = ?", (stream_id,)
        ).fetchone()
        return int(row["next_sequence"]) if row else 1

    @staticmethod
    def _advance_sequence(conn: sqlite3.Connection, stream_id: str, sequence: int) -> None:
        conn.execute(
            "INSERT INTO streams(stream_id, next_sequence) VALUES(?, ?) "
            "ON CONFLICT(stream_id) DO UPDATE SET next_sequence = excluded.next_sequence",
            (stream_id, sequence + 1),
        )

    @classmethod
    def _insert_event(cls, conn: sqlite3.Connection, envelope: dict[str, Any]) -> None:
        conn.execute(
            "INSERT INTO events(stream_id, sequence, event_id, task_id, kind, envelope_json) "
            "VALUES(?, ?, ?, ?, ?, ?)",
            (
                envelope["stream_id"], envelope["sequence"], envelope["event_id"],
                envelope.get("task_id"), envelope["kind"], cls._encoded(envelope),
            ),
        )
        cls._advance_sequence(conn, envelope["stream_id"], int(envelope["sequence"]))

    @staticmethod
    def _task_row(conn: sqlite3.Connection, task_id: str) -> sqlite3.Row | None:
        return conn.execute(
            "SELECT task_id, session_id, stream_id, execution_epoch, state, terminal "
            "FROM tasks WHERE task_id = ?",
            (task_id,),
        ).fetchone()

    @classmethod
    def _validate_epoch_scoped_task_event(
        cls,
        conn: sqlite3.Connection,
        envelope: dict[str, Any],
    ) -> sqlite3.Row | None:
        task_id = envelope.get("task_id")
        payload = envelope.get("payload")
        if not task_id or not isinstance(payload, dict) or "execution_epoch" not in payload:
            return None

        row = cls._task_row(conn, str(task_id))
        if not row:
            raise TaskStateConflict("epoch-scoped event names an unknown task")
        if int(row["terminal"]):
            raise TaskStateConflict("epoch-scoped event cannot mutate a terminal task")
        if row["stream_id"] != envelope["stream_id"]:
            raise TaskStateConflict("task belongs to a different durable stream")
        if row["session_id"] != envelope.get("session_id"):
            raise TaskStateConflict("task belongs to a different durable session")
        if int(payload["execution_epoch"]) != int(row["execution_epoch"]):
            raise TaskStateConflict("event execution_epoch does not match the durable task epoch")
        return row

    def next_sequence(self, stream_id: str) -> int:
        with self._connect() as conn:
            return self._next_sequence(conn, stream_id)

    def append(self, envelope: dict[str, Any], *, expected_sequence: int) -> None:
        """Append one validated non-terminal event with CAS sequencing.

        Final verdict and closure are intentionally excluded: callers cannot
        create a half-terminal task by committing one without the other. Events
        carrying an execution epoch are fenced against the durable task index.
        State-change events also advance that index in the same transaction.
        """
        validate_event(envelope)
        if envelope["kind"] in {"task.verdict", "task.closed"}:
            raise ValueError("task verdict and closure must use atomic finalize_task")
        stream_id = envelope["stream_id"]
        if int(envelope["sequence"]) != expected_sequence:
            raise SequenceConflict("envelope sequence does not match expected sequence")
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            actual = self._next_sequence(conn, stream_id)
            if actual != expected_sequence:
                conn.execute("ROLLBACK")
                raise SequenceConflict(
                    f"stream {stream_id} expected sequence {expected_sequence}, actual {actual}"
                )

            row = self._validate_epoch_scoped_task_event(conn, envelope)
            if envelope["kind"] == "task.state_changed":
                if row is None:
                    conn.execute("ROLLBACK")
                    raise TaskStateConflict("task.state_changed requires a known task and execution epoch")
                previous = str(envelope["payload"]["previous"])
                current = str(envelope["payload"]["current"])
                if previous != str(row["state"]):
                    conn.execute("ROLLBACK")
                    raise TaskStateConflict(
                        f"task state transition expected previous={row['state']!s}, got {previous!s}"
                    )
                if current == previous:
                    conn.execute("ROLLBACK")
                    raise TaskStateConflict("task state transition must change state")
                if current in _TERMINAL_TASK_STATES:
                    conn.execute("ROLLBACK")
                    raise TaskStateConflict("terminal task state requires atomic finalize_task")

            self._insert_event(conn, envelope)
            if envelope["kind"] == "task.state_changed":
                conn.execute(
                    "UPDATE tasks SET state = ? WHERE task_id = ?",
                    (str(envelope["payload"]["current"]), str(envelope["task_id"])),
                )
            conn.execute("COMMIT")

    def admit(
        self,
        *,
        request_id: str,
        payload_sha256: str,
        envelope: dict[str, Any],
        expected_sequence: int,
    ) -> tuple[str, bool]:
        """Atomically create a task and persist its task.admitted event.

        Identical request retries return the original task id without a second
        event. Reusing a request id for different bytes fails closed.
        """
        validate_event(envelope)
        if envelope["kind"] != "task.admitted" or not envelope.get("task_id"):
            raise ValueError("admission requires a task.admitted event with task_id")
        if int(envelope["sequence"]) != expected_sequence:
            raise SequenceConflict("admission envelope sequence does not match expected sequence")
        task_id = str(envelope["task_id"])
        execution_epoch = int(envelope["payload"]["execution_epoch"])
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            existing = conn.execute(
                "SELECT task_id, payload_sha256 FROM tasks WHERE request_id = ?", (request_id,)
            ).fetchone()
            if existing:
                if existing["payload_sha256"] != payload_sha256:
                    conn.execute("ROLLBACK")
                    raise AdmissionConflict("request_id was already used for different task bytes")
                conn.execute("COMMIT")
                return str(existing["task_id"]), False

            actual = self._next_sequence(conn, envelope["stream_id"])
            if actual != expected_sequence:
                conn.execute("ROLLBACK")
                raise SequenceConflict(
                    f"stream {envelope['stream_id']} expected sequence {expected_sequence}, actual {actual}"
                )
            conn.execute(
                "INSERT INTO tasks(task_id, session_id, request_id, payload_sha256, stream_id, "
                "admitted_sequence, execution_epoch, state, terminal, verdict_sequence, "
                "closed_sequence, result_ref_json) "
                "VALUES(?, ?, ?, ?, ?, ?, ?, ?, 0, NULL, NULL, NULL)",
                (
                    task_id, envelope.get("session_id"), request_id, payload_sha256,
                    envelope["stream_id"], expected_sequence, execution_epoch, "admitted",
                ),
            )
            self._insert_event(conn, envelope)
            conn.execute("COMMIT")
            return task_id, True

    def finalize_task(
        self,
        verdict_envelope: dict[str, Any],
        closed_envelope: dict[str, Any],
        *,
        expected_sequence: int,
    ) -> None:
        """Atomically commit final verdict, closure, terminal state and result index."""
        validate_event(verdict_envelope)
        validate_event(closed_envelope)
        if verdict_envelope["kind"] != "task.verdict":
            raise ValueError("finalization requires task.verdict first")
        if closed_envelope["kind"] != "task.closed":
            raise ValueError("finalization requires task.closed second")

        task_id = verdict_envelope.get("task_id")
        if not task_id or closed_envelope.get("task_id") != task_id:
            raise ValueError("finalization envelopes must name the same task_id")
        stream_id = verdict_envelope["stream_id"]
        if closed_envelope["stream_id"] != stream_id:
            raise ValueError("finalization envelopes must use the same durable stream")
        if int(verdict_envelope["sequence"]) != expected_sequence:
            raise SequenceConflict("verdict sequence does not match expected sequence")
        if int(closed_envelope["sequence"]) != expected_sequence + 1:
            raise SequenceConflict("closure sequence must immediately follow verdict")

        completion = verdict_envelope["payload"]["completion"]
        closure = closed_envelope["payload"]
        if completion["task_id"] != task_id:
            raise ValueError("verdict completion task_id must equal the envelope task_id")
        if completion["status"] != closure["status"]:
            raise ValueError("verdict and closure terminal status must agree")
        if completion["result_ref"] != closure["result_ref"]:
            raise ValueError("verdict and closure result_ref must agree")

        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT terminal, stream_id, session_id, state FROM tasks WHERE task_id = ?", (task_id,)
            ).fetchone()
            if not row:
                conn.execute("ROLLBACK")
                raise TaskStateConflict("cannot finalize an unknown task")
            if int(row["terminal"]):
                conn.execute("ROLLBACK")
                raise TaskStateConflict("task is already terminal")
            if str(row["state"]) in _TERMINAL_TASK_STATES:
                conn.execute("ROLLBACK")
                raise TaskStateConflict("task index contains a terminal state without terminal closure")
            if row["stream_id"] != stream_id:
                conn.execute("ROLLBACK")
                raise TaskStateConflict("task belongs to a different durable stream")
            if row["session_id"] != verdict_envelope.get("session_id") or row["session_id"] != closed_envelope.get("session_id"):
                conn.execute("ROLLBACK")
                raise TaskStateConflict("task belongs to a different durable session")
            prior = conn.execute(
                "SELECT kind FROM events WHERE task_id = ? AND kind IN ('task.verdict', 'task.closed') LIMIT 1",
                (task_id,),
            ).fetchone()
            if prior:
                conn.execute("ROLLBACK")
                raise TaskStateConflict("task already has a terminal event")
            actual = self._next_sequence(conn, stream_id)
            if actual != expected_sequence:
                conn.execute("ROLLBACK")
                raise SequenceConflict(
                    f"stream {stream_id} expected sequence {expected_sequence}, actual {actual}"
                )

            self._insert_event(conn, verdict_envelope)
            self._insert_event(conn, closed_envelope)
            conn.execute(
                "UPDATE tasks SET state = ?, terminal = 1, verdict_sequence = ?, closed_sequence = ?, "
                "result_ref_json = ? WHERE task_id = ?",
                (
                    str(closure["status"]), expected_sequence, expected_sequence + 1,
                    self._encoded_value(closure["result_ref"]), task_id,
                ),
            )
            conn.execute("COMMIT")

    def replay(self, stream_id: str, *, after: int = 0, limit: int = 1000) -> list[dict[str, Any]]:
        if after < 0:
            raise ValueError("replay cursor cannot be negative")
        if limit < 1 or limit > 10_000:
            raise ValueError("replay limit must be between 1 and 10000")
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT envelope_json FROM events WHERE stream_id = ? AND sequence > ? "
                "ORDER BY sequence ASC LIMIT ?",
                (stream_id, after, limit),
            ).fetchall()
        events = [json.loads(row["envelope_json"]) for row in rows]
        for event in events:
            validate_event(event)
        return events

    def task_record(self, task_id: str) -> dict[str, Any] | None:
        """Return durable task-index state without treating it as verification proof."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT task_id, session_id, request_id, payload_sha256, stream_id, admitted_sequence, "
                "execution_epoch, state, terminal, verdict_sequence, closed_sequence, result_ref_json "
                "FROM tasks WHERE task_id = ?",
                (task_id,),
            ).fetchone()
        if not row:
            return None
        record = dict(row)
        record["terminal"] = bool(record["terminal"])
        encoded = record.pop("result_ref_json")
        record["result_ref"] = json.loads(encoded) if encoded is not None else None
        return record

    def unterminated_tasks(self, stream_id: str) -> list[dict[str, Any]]:
        """Return this stream's durable admissions needing crash reconciliation.

        Recovery is explicitly stream-scoped: one service may never terminalize a
        task owned by another stream merely because both share the same database.
        This method does not execute or retry effects.
        """
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT task_id, session_id, request_id, payload_sha256, stream_id, admitted_sequence, "
                "execution_epoch, state FROM tasks WHERE terminal = 0 AND stream_id = ? "
                "ORDER BY admitted_sequence ASC",
                (stream_id,),
            ).fetchall()
        return [dict(row) for row in rows]
