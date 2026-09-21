"""Append-only durable storage for fixed-watch attempts.

The store is deliberately not a scheduler and not an evaluator. It persists the typed
attempt contract from ``watch.delta`` so restart-safe comparison never depends on a
process-local "last run" pointer. Reusing a run UUID for different bytes is a hard
conflict; exact replay is idempotent.
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from .delta import WatchAttempt


class WatchRunConflict(RuntimeError):
    """A durable watch run identity was reused for different attempt data."""


class _ClosingConnection(sqlite3.Connection):
    def __exit__(self, exc_type, exc, tb):
        try:
            return super().__exit__(exc_type, exc, tb)
        finally:
            self.close()


def _attempt_payload(attempt: WatchAttempt) -> dict[str, Any]:
    return {
        "run_id": attempt.run_id,
        "job_id": attempt.job_id,
        "job_revision": attempt.job_revision,
        "execution_contract_sha256": attempt.execution_contract_sha256,
        "result_schema": attempt.result_schema,
        "source_head": attempt.source_head,
        "source_digest": attempt.source_digest,
        "complete": attempt.complete,
        "observations": [[key, value] for key, value in attempt.observations],
    }


def _encoded(attempt: WatchAttempt) -> str:
    return json.dumps(
        _attempt_payload(attempt),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )


def _decode(encoded: str) -> WatchAttempt:
    raw = json.loads(encoded)
    if not isinstance(raw, dict):
        raise ValueError("stored watch attempt must be an object")
    observations = raw.get("observations")
    if not isinstance(observations, list):
        raise ValueError("stored watch observations must be a list")
    return WatchAttempt(
        run_id=str(raw["run_id"]),
        job_id=str(raw["job_id"]),
        job_revision=str(raw["job_revision"]),
        execution_contract_sha256=str(raw["execution_contract_sha256"]),
        result_schema=str(raw["result_schema"]),
        source_head=str(raw["source_head"]),
        source_digest=str(raw["source_digest"]),
        complete=raw["complete"],
        observations=tuple((str(item[0]), str(item[1])) for item in observations),
    )


class SQLiteWatchRunStore:
    """Small append-only run ledger keyed by immutable ``run_id``."""

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
        conn.execute("PRAGMA busy_timeout=30000")
        return conn

    def _initialise(self) -> None:
        with self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS watch_attempts (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id TEXT NOT NULL UNIQUE,
                    job_id TEXT NOT NULL,
                    job_revision TEXT NOT NULL,
                    execution_contract_sha256 TEXT NOT NULL,
                    complete INTEGER NOT NULL CHECK(complete IN (0, 1)),
                    payload_json TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_watch_attempts_job_sequence
                    ON watch_attempts(job_id, sequence);
                CREATE INDEX IF NOT EXISTS idx_watch_attempts_job_revision_sequence
                    ON watch_attempts(job_id, job_revision, sequence);
                """
            )

    def record_attempt(self, attempt: WatchAttempt) -> tuple[int, bool]:
        """Append once; return ``(sequence, created)`` for idempotent replay."""
        payload = _encoded(attempt)
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            existing = conn.execute(
                "SELECT sequence, payload_json FROM watch_attempts WHERE run_id = ?",
                (attempt.run_id,),
            ).fetchone()
            if existing is not None:
                if str(existing["payload_json"]) != payload:
                    conn.execute("ROLLBACK")
                    raise WatchRunConflict("run_id was already recorded with different attempt data")
                conn.execute("COMMIT")
                return int(existing["sequence"]), False
            cursor = conn.execute(
                "INSERT INTO watch_attempts(run_id, job_id, job_revision, "
                "execution_contract_sha256, complete, payload_json) VALUES(?, ?, ?, ?, ?, ?)",
                (
                    attempt.run_id,
                    attempt.job_id,
                    attempt.job_revision,
                    attempt.execution_contract_sha256,
                    1 if attempt.complete else 0,
                    payload,
                ),
            )
            sequence = int(cursor.lastrowid)
            conn.execute("COMMIT")
            return sequence, True

    def attempt(self, run_id: str) -> WatchAttempt | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT payload_json FROM watch_attempts WHERE run_id = ?",
                (run_id,),
            ).fetchone()
        return None if row is None else _decode(str(row["payload_json"]))

    def latest_attempt(self, job_id: str, *, before_sequence: int | None = None) -> WatchAttempt | None:
        """Return the latest durable attempt for one job, optionally before a row."""
        if before_sequence is not None and before_sequence < 1:
            raise ValueError("before_sequence must be positive")
        with self._connect() as conn:
            if before_sequence is None:
                row = conn.execute(
                    "SELECT payload_json FROM watch_attempts WHERE job_id = ? "
                    "ORDER BY sequence DESC LIMIT 1",
                    (job_id,),
                ).fetchone()
            else:
                row = conn.execute(
                    "SELECT payload_json FROM watch_attempts WHERE job_id = ? AND sequence < ? "
                    "ORDER BY sequence DESC LIMIT 1",
                    (job_id, before_sequence),
                ).fetchone()
        return None if row is None else _decode(str(row["payload_json"]))

    def history(self, job_id: str, *, limit: int = 100) -> list[WatchAttempt]:
        if not isinstance(limit, int) or isinstance(limit, bool) or limit < 1 or limit > 1000:
            raise ValueError("watch history limit must be between 1 and 1000")
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT payload_json FROM watch_attempts WHERE job_id = ? "
                "ORDER BY sequence DESC LIMIT ?",
                (job_id, limit),
            ).fetchall()
        return [_decode(str(row["payload_json"])) for row in rows]
