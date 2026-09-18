"""Durable Session Hub event/task state.

The store is deliberately boring: SQLite WAL, one transactional sequence check,
and validated JSON envelopes. It is not a verification oracle and it never
replays task effects. Admission and its event commit atomically.
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


class SQLiteSessionStore:
    def __init__(self, path: Path | str):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialise()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=30, isolation_level=None)
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
                    state TEXT NOT NULL,
                    terminal INTEGER NOT NULL CHECK(terminal IN (0, 1)),
                    closed_sequence INTEGER
                );
                """
            )

    @staticmethod
    def _encoded(envelope: dict[str, Any]) -> str:
        return json.dumps(envelope, sort_keys=True, separators=(",", ":"), allow_nan=False)

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

    def next_sequence(self, stream_id: str) -> int:
        with self._connect() as conn:
            return self._next_sequence(conn, stream_id)

    def append(self, envelope: dict[str, Any], *, expected_sequence: int) -> None:
        """Append exactly one validated event with compare-and-swap sequencing."""
        validate_event(envelope)
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
            self._insert_event(conn, envelope)
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
                "admitted_sequence, state, terminal, closed_sequence) VALUES(?, ?, ?, ?, ?, ?, ?, 0, NULL)",
                (
                    task_id, envelope.get("session_id"), request_id, payload_sha256,
                    envelope["stream_id"], expected_sequence, "admitted",
                ),
            )
            self._insert_event(conn, envelope)
            conn.execute("COMMIT")
            return task_id, True

    def close_task(self, envelope: dict[str, Any], *, expected_sequence: int) -> None:
        """Commit a terminal task.closed event and task row in one transaction."""
        validate_event(envelope)
        if envelope["kind"] != "task.closed" or not envelope.get("task_id"):
            raise ValueError("task closure requires a task.closed event with task_id")
        if int(envelope["sequence"]) != expected_sequence:
            raise SequenceConflict("closure envelope sequence does not match expected sequence")
        task_id = str(envelope["task_id"])
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT terminal, stream_id FROM tasks WHERE task_id = ?", (task_id,)
            ).fetchone()
            if not row:
                conn.execute("ROLLBACK")
                raise TaskStateConflict("cannot close an unknown task")
            if int(row["terminal"]):
                conn.execute("ROLLBACK")
                raise TaskStateConflict("task is already terminal")
            if row["stream_id"] != envelope["stream_id"]:
                conn.execute("ROLLBACK")
                raise TaskStateConflict("task belongs to a different durable stream")
            actual = self._next_sequence(conn, envelope["stream_id"])
            if actual != expected_sequence:
                conn.execute("ROLLBACK")
                raise SequenceConflict(
                    f"stream {envelope['stream_id']} expected sequence {expected_sequence}, actual {actual}"
                )
            status = str(envelope["payload"].get("status", "closed"))
            self._insert_event(conn, envelope)
            conn.execute(
                "UPDATE tasks SET state = ?, terminal = 1, closed_sequence = ? WHERE task_id = ?",
                (status, expected_sequence, task_id),
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

    def unterminated_tasks(self) -> list[dict[str, Any]]:
        """Return durable admissions that need crash reconciliation.

        This method deliberately does not execute, retry or fabricate a terminal
        result. The service layer must append an explicit NO_VERDICT recovery
        event/closure before allowing replacement work.
        """
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT task_id, session_id, request_id, payload_sha256, stream_id, admitted_sequence, state "
                "FROM tasks WHERE terminal = 0 ORDER BY admitted_sequence ASC"
            ).fetchall()
        return [dict(row) for row in rows]
