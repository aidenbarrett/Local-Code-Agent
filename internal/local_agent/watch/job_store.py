"""Durable configuration registry for fixed watch jobs.

A watch job definition is mutable configuration with immutable historical versions.
The procedure revision remains the ``FixedWatchJob.revision_sha256`` authority; UI
state such as enabled/disabled and schedule text is stored alongside it but is never
folded into the procedure hash.
"""
from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import sqlite3
from typing import Any

from .jobs import FixedWatchJob


class WatchJobConflict(RuntimeError):
    """Optimistic watch-job update failed or an identity was reused inconsistently."""


class WatchJobNotFound(KeyError):
    pass


class _ClosingConnection(sqlite3.Connection):
    def __exit__(self, exc_type, exc, tb):
        try:
            return super().__exit__(exc_type, exc, tb)
        finally:
            self.close()


def _job_payload(job: FixedWatchJob) -> dict[str, Any]:
    return {
        "job_id": job.job_id,
        "display_name": job.display_name,
        "repository_id": job.repository_id,
        "repository_host": job.repository_host,
        "source_selection": job.source_selection.value,
        "source_selector": job.source_selector,
        "dirty_tree_policy": job.dirty_tree_policy.value,
        "command_profile": job.command_profile,
        "environment_allowlist": list(job.environment_allowlist),
        "verification_contract_sha256": job.verification_contract_sha256,
        "scope": list(job.scope),
        "timeout_seconds": job.timeout_seconds,
        "skill_contract_sha256": job.skill_contract_sha256,
        "endpoint_policy": job.endpoint_policy,
        "delta_schema_version": job.delta_schema_version,
    }


def _encoded_job(job: FixedWatchJob) -> str:
    return json.dumps(
        _job_payload(job),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )


def _decode_job(payload_json: str) -> FixedWatchJob:
    raw = json.loads(payload_json)
    if not isinstance(raw, dict):
        raise ValueError("stored watch job must be a JSON object")
    return FixedWatchJob(
        job_id=str(raw["job_id"]),
        display_name=str(raw["display_name"]),
        repository_id=str(raw["repository_id"]),
        repository_host=str(raw["repository_host"]),
        source_selection=str(raw["source_selection"]),
        source_selector=str(raw["source_selector"]),
        dirty_tree_policy=str(raw["dirty_tree_policy"]),
        command_profile=str(raw["command_profile"]),
        environment_allowlist=tuple(str(value) for value in raw["environment_allowlist"]),
        verification_contract_sha256=str(raw["verification_contract_sha256"]),
        scope=tuple(str(value) for value in raw["scope"]),
        timeout_seconds=int(raw["timeout_seconds"]),
        skill_contract_sha256=(
            None if raw.get("skill_contract_sha256") is None else str(raw["skill_contract_sha256"])
        ),
        endpoint_policy=None if raw.get("endpoint_policy") is None else str(raw["endpoint_policy"]),
        delta_schema_version=str(raw["delta_schema_version"]),
    )


def _normalise_schedule(schedule_spec: str | None) -> str | None:
    if schedule_spec is None:
        return None
    if not isinstance(schedule_spec, str) or not schedule_spec.strip():
        raise ValueError("watch schedule must be a nonempty string or None")
    return schedule_spec.strip()


@dataclass(frozen=True)
class StoredWatchJob:
    job: FixedWatchJob
    version: int
    enabled: bool
    schedule_spec: str | None

    @property
    def revision_sha256(self) -> str:
        return self.job.revision_sha256


class SQLiteWatchJobStore:
    """Current watch configuration plus an append-only version history."""

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
                CREATE TABLE IF NOT EXISTS watch_jobs (
                    job_id TEXT PRIMARY KEY,
                    version INTEGER NOT NULL CHECK(version >= 1),
                    revision_sha256 TEXT NOT NULL,
                    enabled INTEGER NOT NULL CHECK(enabled IN (0, 1)),
                    schedule_spec TEXT,
                    payload_json TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS watch_job_versions (
                    job_id TEXT NOT NULL,
                    version INTEGER NOT NULL CHECK(version >= 1),
                    revision_sha256 TEXT NOT NULL,
                    enabled INTEGER NOT NULL CHECK(enabled IN (0, 1)),
                    schedule_spec TEXT,
                    payload_json TEXT NOT NULL,
                    PRIMARY KEY(job_id, version)
                );
                CREATE INDEX IF NOT EXISTS idx_watch_jobs_enabled
                    ON watch_jobs(enabled, job_id);
                """
            )

    @staticmethod
    def _record(row: sqlite3.Row) -> StoredWatchJob:
        return StoredWatchJob(
            job=_decode_job(str(row["payload_json"])),
            version=int(row["version"]),
            enabled=bool(row["enabled"]),
            schedule_spec=None if row["schedule_spec"] is None else str(row["schedule_spec"]),
        )

    def create(
        self,
        job: FixedWatchJob,
        *,
        enabled: bool = False,
        schedule_spec: str | None = None,
    ) -> tuple[StoredWatchJob, bool]:
        if not isinstance(enabled, bool):
            raise TypeError("enabled must be boolean")
        schedule_spec = _normalise_schedule(schedule_spec)
        payload_json = _encoded_job(job)
        revision = job.revision_sha256
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT * FROM watch_jobs WHERE job_id = ?", (job.job_id,)).fetchone()
            if row is not None:
                existing = self._record(row)
                if (
                    _encoded_job(existing.job) != payload_json
                    or existing.enabled != enabled
                    or existing.schedule_spec != schedule_spec
                ):
                    conn.execute("ROLLBACK")
                    raise WatchJobConflict("watch job id already exists with different configuration")
                conn.execute("COMMIT")
                return existing, False
            conn.execute(
                "INSERT INTO watch_jobs(job_id, version, revision_sha256, enabled, schedule_spec, payload_json) "
                "VALUES(?, 1, ?, ?, ?, ?)",
                (job.job_id, revision, 1 if enabled else 0, schedule_spec, payload_json),
            )
            conn.execute(
                "INSERT INTO watch_job_versions(job_id, version, revision_sha256, enabled, schedule_spec, payload_json) "
                "VALUES(?, 1, ?, ?, ?, ?)",
                (job.job_id, revision, 1 if enabled else 0, schedule_spec, payload_json),
            )
            conn.execute("COMMIT")
        return StoredWatchJob(job=job, version=1, enabled=enabled, schedule_spec=schedule_spec), True

    def get(self, job_id: str) -> StoredWatchJob | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM watch_jobs WHERE job_id = ?", (job_id,)).fetchone()
        return None if row is None else self._record(row)

    def require(self, job_id: str) -> StoredWatchJob:
        record = self.get(job_id)
        if record is None:
            raise WatchJobNotFound(job_id)
        return record

    def _replace(
        self,
        current: StoredWatchJob,
        *,
        job: FixedWatchJob,
        enabled: bool,
        schedule_spec: str | None,
        expected_version: int,
    ) -> StoredWatchJob:
        if expected_version != current.version:
            raise WatchJobConflict(
                f"watch job version changed: expected {expected_version}, current {current.version}"
            )
        payload_json = _encoded_job(job)
        revision = job.revision_sha256
        if (
            payload_json == _encoded_job(current.job)
            and enabled == current.enabled
            and schedule_spec == current.schedule_spec
        ):
            return current
        version = current.version + 1
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            cursor = conn.execute(
                "UPDATE watch_jobs SET version = ?, revision_sha256 = ?, enabled = ?, "
                "schedule_spec = ?, payload_json = ? WHERE job_id = ? AND version = ?",
                (
                    version,
                    revision,
                    1 if enabled else 0,
                    schedule_spec,
                    payload_json,
                    job.job_id,
                    expected_version,
                ),
            )
            if cursor.rowcount != 1:
                conn.execute("ROLLBACK")
                raise WatchJobConflict("watch job changed concurrently")
            conn.execute(
                "INSERT INTO watch_job_versions(job_id, version, revision_sha256, enabled, schedule_spec, payload_json) "
                "VALUES(?, ?, ?, ?, ?, ?)",
                (job.job_id, version, revision, 1 if enabled else 0, schedule_spec, payload_json),
            )
            conn.execute("COMMIT")
        return StoredWatchJob(job=job, version=version, enabled=enabled, schedule_spec=schedule_spec)

    def update_job(self, job: FixedWatchJob, *, expected_version: int) -> StoredWatchJob:
        current = self.require(job.job_id)
        return self._replace(
            current,
            job=job,
            enabled=current.enabled,
            schedule_spec=current.schedule_spec,
            expected_version=expected_version,
        )

    def set_enabled(self, job_id: str, enabled: bool, *, expected_version: int) -> StoredWatchJob:
        if not isinstance(enabled, bool):
            raise TypeError("enabled must be boolean")
        current = self.require(job_id)
        return self._replace(
            current,
            job=current.job,
            enabled=enabled,
            schedule_spec=current.schedule_spec,
            expected_version=expected_version,
        )

    def set_schedule(
        self,
        job_id: str,
        schedule_spec: str | None,
        *,
        expected_version: int,
    ) -> StoredWatchJob:
        current = self.require(job_id)
        return self._replace(
            current,
            job=current.job,
            enabled=current.enabled,
            schedule_spec=_normalise_schedule(schedule_spec),
            expected_version=expected_version,
        )

    def list_jobs(self, *, enabled_only: bool = False) -> list[StoredWatchJob]:
        if not isinstance(enabled_only, bool):
            raise TypeError("enabled_only must be boolean")
        with self._connect() as conn:
            if enabled_only:
                rows = conn.execute(
                    "SELECT * FROM watch_jobs WHERE enabled = 1 ORDER BY job_id"
                ).fetchall()
            else:
                rows = conn.execute("SELECT * FROM watch_jobs ORDER BY job_id").fetchall()
        return [self._record(row) for row in rows]

    def version(self, job_id: str, version: int) -> StoredWatchJob | None:
        if not isinstance(version, int) or isinstance(version, bool) or version < 1:
            raise ValueError("watch job version must be a positive integer")
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM watch_job_versions WHERE job_id = ? AND version = ?",
                (job_id, version),
            ).fetchone()
        return None if row is None else self._record(row)
