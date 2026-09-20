"""Durable Session Hub event/task state.

The store is deliberately boring: SQLite WAL, transactional stream sequencing,
and validated JSON envelopes. It is not a verification oracle and it never
replays task effects. Admission and terminalization are durable atomic fences.
"""
from __future__ import annotations

import hashlib
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


class ArtifactIntegrityError(RuntimeError):
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
                CREATE TABLE IF NOT EXISTS artifacts (
                    artifact_id TEXT PRIMARY KEY,
                    task_id TEXT NOT NULL,
                    sha256 TEXT NOT NULL,
                    media_type TEXT NOT NULL,
                    size_bytes INTEGER NOT NULL CHECK(size_bytes >= 0),
                    payload BLOB NOT NULL,
                    FOREIGN KEY(task_id) REFERENCES tasks(task_id)
                );
                CREATE INDEX IF NOT EXISTS idx_artifacts_task ON artifacts(task_id);
                CREATE TABLE IF NOT EXISTS turn_tasks (
                    conversation_id TEXT NOT NULL,
                    turn_index INTEGER NOT NULL CHECK(turn_index >= 0),
                    turn_sha256 TEXT NOT NULL,
                    task_id TEXT NOT NULL UNIQUE,
                    PRIMARY KEY(conversation_id, turn_index, turn_sha256, task_id),
                    FOREIGN KEY(task_id) REFERENCES tasks(task_id)
                );
                CREATE INDEX IF NOT EXISTS idx_turn_tasks_conversation
                    ON turn_tasks(conversation_id, turn_index, task_id);
                """
            )
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

            # Older durable tasks already carry the exact TurnRef in their validated
            # task.admitted event. Rebuild only that index; never invent artifact bytes.
            rows = conn.execute(
                "SELECT task_id, envelope_json FROM events WHERE kind = 'task.admitted' "
                "ORDER BY stream_id, sequence"
            ).fetchall()
            for row in rows:
                envelope = json.loads(row["envelope_json"])
                ref = self._origin_turn_ref(envelope)
                if ref is None:
                    continue
                conn.execute(
                    "INSERT OR IGNORE INTO turn_tasks(conversation_id, turn_index, turn_sha256, task_id) "
                    "VALUES(?, ?, ?, ?)",
                    (
                        str(ref["conversation_id"]), int(ref["turn_index"]),
                        str(ref["turn_sha256"]), str(row["task_id"]),
                    ),
                )

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

    @staticmethod
    def _artifact_row_for_bytes(
        ref: dict[str, Any],
        payload: bytes | None,
        *,
        payload_sha256: str | None = None,
    ) -> tuple[str, str, str, int, bytes] | None:
        availability = ref.get("availability")
        if availability != "retained":
            if payload is not None:
                raise ArtifactIntegrityError("non-retained artifact cannot persist payload bytes")
            return None
        if payload is None:
            raise ArtifactIntegrityError("retained artifact requires payload bytes")
        digest = hashlib.sha256(payload).hexdigest()
        size = len(payload)
        if ref.get("sha256") != digest:
            raise ArtifactIntegrityError("artifact payload digest does not match its reference")
        if int(ref.get("size_bytes", -1)) != size:
            raise ArtifactIntegrityError("artifact payload size does not match its reference")
        if payload_sha256 is not None and payload_sha256 != digest:
            raise ArtifactIntegrityError("task payload digest does not match retained request bytes")
        return (
            str(ref["artifact_id"]), digest, str(ref["media_type"]), size, bytes(payload)
        )

    @classmethod
    def _insert_artifact(
        cls,
        conn: sqlite3.Connection,
        task_id: str,
        ref: dict[str, Any],
        payload: bytes | None,
        *,
        payload_sha256: str | None = None,
    ) -> None:
        row = cls._artifact_row_for_bytes(ref, payload, payload_sha256=payload_sha256)
        if row is None:
            return
        artifact_id, digest, media_type, size, stored = row
        conn.execute(
            "INSERT INTO artifacts(artifact_id, task_id, sha256, media_type, size_bytes, payload) "
            "VALUES(?, ?, ?, ?, ?, ?)",
            (artifact_id, task_id, digest, media_type, size, stored),
        )

    @staticmethod
    def _origin_turn_ref(envelope: dict[str, Any]) -> dict[str, Any] | None:
        origin = envelope["payload"]["origin"]
        if origin["kind"] == "watch":
            return None
        return origin["turn_ref"]

    @classmethod
    def _insert_turn_association(
        cls,
        conn: sqlite3.Connection,
        task_id: str,
        envelope: dict[str, Any],
    ) -> None:
        ref = cls._origin_turn_ref(envelope)
        if ref is None:
            return
        conn.execute(
            "INSERT INTO turn_tasks(conversation_id, turn_index, turn_sha256, task_id) "
            "VALUES(?, ?, ?, ?)",
            (
                str(ref["conversation_id"]), int(ref["turn_index"]),
                str(ref["turn_sha256"]), task_id,
            ),
        )

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
        request_bytes: bytes | None = None,
    ) -> tuple[str, bool]:
        """Atomically admit task, exact TurnRef association and retained request."""
        validate_event(envelope)
        if envelope["kind"] != "task.admitted" or not envelope.get("task_id"):
            raise ValueError("admission requires a task.admitted event with task_id")
        if int(envelope["sequence"]) != expected_sequence:
            raise SequenceConflict("admission envelope sequence does not match expected sequence")
        task_id = str(envelope["task_id"])
        execution_epoch = int(envelope["payload"]["execution_epoch"])
        request_ref = envelope["payload"]["request_ref"]
        self._artifact_row_for_bytes(
            request_ref,
            request_bytes,
            payload_sha256=payload_sha256 if request_bytes is not None else None,
        )
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
            self._insert_artifact(
                conn, task_id, request_ref, request_bytes, payload_sha256=payload_sha256
            )
            self._insert_turn_association(conn, task_id, envelope)
            self._insert_event(conn, envelope)
            conn.execute("COMMIT")
            return task_id, True

    def finalize_task(
        self,
        verdict_envelope: dict[str, Any],
        closed_envelope: dict[str, Any],
        *,
        expected_sequence: int,
        result_bytes: bytes | None = None,
    ) -> None:
        """Atomically commit terminal state, result index and retained result bytes."""
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
        result_ref = completion["result_ref"]
        self._artifact_row_for_bytes(result_ref, result_bytes)

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
            self._insert_artifact(conn, str(task_id), result_ref, result_bytes)
            self._insert_event(conn, verdict_envelope)
            self._insert_event(conn, closed_envelope)
            conn.execute(
                "UPDATE tasks SET state = ?, terminal = 1, verdict_sequence = ?, closed_sequence = ?, "
                "result_ref_json = ? WHERE task_id = ?",
                (
                    str(closure["status"]), expected_sequence, expected_sequence + 1,
                    self._encoded_value(result_ref), task_id,
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

    def task_ids_for_turn(self, turn_ref: dict[str, Any]) -> list[str]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT tt.task_id FROM turn_tasks AS tt JOIN tasks AS t ON t.task_id = tt.task_id "
                "WHERE tt.conversation_id = ? AND tt.turn_index = ? AND tt.turn_sha256 = ? "
                "ORDER BY t.admitted_sequence ASC",
                (
                    str(turn_ref["conversation_id"]), int(turn_ref["turn_index"]),
                    str(turn_ref["turn_sha256"]),
                ),
            ).fetchall()
        return [str(row["task_id"]) for row in rows]

    def latest_terminal_task_for_conversation(self, conversation_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT t.task_id, t.state, t.result_ref_json, tt.turn_index, tt.turn_sha256 "
                "FROM turn_tasks AS tt JOIN tasks AS t ON t.task_id = tt.task_id "
                "WHERE tt.conversation_id = ? AND t.terminal = 1 "
                "ORDER BY t.admitted_sequence DESC LIMIT 1",
                (conversation_id,),
            ).fetchone()
        if row is None:
            return None
        record = dict(row)
        record["turn_ref"] = {
            "conversation_id": conversation_id,
            "turn_index": int(record.pop("turn_index")),
            "turn_sha256": str(record.pop("turn_sha256")),
        }
        encoded = record.pop("result_ref_json")
        record["result_ref"] = json.loads(encoded) if encoded is not None else None
        return record

    def artifact_bytes(self, ref: dict[str, Any]) -> bytes:
        if ref.get("availability") != "retained":
            raise ArtifactIntegrityError("artifact is not retained")
        with self._connect() as conn:
            row = conn.execute(
                "SELECT sha256, media_type, size_bytes, payload FROM artifacts WHERE artifact_id = ?",
                (str(ref["artifact_id"]),),
            ).fetchone()
        if row is None:
            raise ArtifactIntegrityError("retained artifact payload is missing")
        payload = bytes(row["payload"])
        if str(row["sha256"]) != str(ref["sha256"]):
            raise ArtifactIntegrityError("stored artifact digest metadata does not match reference")
        if str(row["media_type"]) != str(ref["media_type"]):
            raise ArtifactIntegrityError("stored artifact media type does not match reference")
        if int(row["size_bytes"]) != int(ref["size_bytes"]):
            raise ArtifactIntegrityError("stored artifact size metadata does not match reference")
        if len(payload) != int(ref["size_bytes"]):
            raise ArtifactIntegrityError("stored artifact payload size is corrupt")
        if hashlib.sha256(payload).hexdigest() != str(ref["sha256"]):
            raise ArtifactIntegrityError("stored artifact payload digest is corrupt")
        return payload

    def unterminated_tasks(self, stream_id: str) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT task_id, session_id, request_id, payload_sha256, stream_id, admitted_sequence, "
                "execution_epoch, state FROM tasks WHERE terminal = 0 AND stream_id = ? "
                "ORDER BY admitted_sequence ASC",
                (stream_id,),
            ).fetchall()
        return [dict(row) for row in rows]
