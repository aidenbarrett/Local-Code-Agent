from __future__ import annotations

import hashlib
import sqlite3
import threading
from uuid import uuid4

import pytest

from local_agent.session.event_contract import EventContractError, build_event, validate_event
from local_agent.session.session_store import (
    AdmissionConflict,
    SequenceConflict,
    SQLiteSessionStore,
    TaskStateConflict,
)


def _artifact_ref(data: bytes = b"request") -> dict:
    return {
        "artifact_id": str(uuid4()),
        "sha256": hashlib.sha256(data).hexdigest(),
        "media_type": "application/json",
        "size_bytes": len(data),
        "availability": "retained",
    }


def _turn_ref() -> dict:
    return {
        "conversation_id": "conv-1",
        "turn_index": 0,
        "turn_sha256": "a" * 64,
    }


def _ids() -> tuple[str, str, str]:
    return str(uuid4()), str(uuid4()), str(uuid4())


def _session_opened(stream_id: str, epoch: str, session_id: str, sequence: int = 1) -> dict:
    return build_event(
        stream_id=stream_id,
        sequence=sequence,
        producer_epoch=epoch,
        session_id=session_id,
        kind="session.opened",
        payload={
            "conversation_id": "conv-1",
            "repository_id": "repo-1",
            "controller_commit": "deadbeef",
            "capabilities": ["inspect"],
            "recovered": False,
        },
    )


def _admitted(stream_id: str, epoch: str, session_id: str, task_id: str, sequence: int) -> dict:
    return build_event(
        stream_id=stream_id,
        sequence=sequence,
        producer_epoch=epoch,
        session_id=session_id,
        task_id=task_id,
        kind="task.admitted",
        payload={
            "origin": {"kind": "user_direct", "turn_ref": _turn_ref()},
            "request_ref": _artifact_ref(),
            "contract_sha256": "b" * 64,
            "repository_id": "repo-1",
            "skill": "inspect",
            "execution_epoch": 0,
            "deadline_utc": "2030-01-01T00:00:00Z",
        },
    )


def _terminal_pair(
    stream_id: str,
    epoch: str,
    session_id: str,
    task_id: str,
    sequence: int,
    *,
    result_ref: dict | None = None,
) -> tuple[dict, dict]:
    result_ref = result_ref or _artifact_ref(b"unknown")
    verdict = build_event(
        stream_id=stream_id,
        sequence=sequence,
        producer_epoch=epoch,
        session_id=session_id,
        task_id=task_id,
        kind="task.verdict",
        payload={
            "completion": {
                "task_id": task_id,
                "status": "interrupted",
                "verdict_block": {
                    "verdict": "NO_VERDICT",
                    "reason_code": "cleanup_unknown",
                    "scope": "test terminalization",
                    "evidence_ids": [],
                    "tree_sha256": None,
                    "rendered_lines": ["NO_VERDICT: cleanup unknown."],
                },
                "worker_artifact_ref": None,
                "result_ref": result_ref,
            }
        },
    )
    closed = build_event(
        stream_id=stream_id,
        sequence=sequence + 1,
        producer_epoch=epoch,
        session_id=session_id,
        task_id=task_id,
        kind="task.closed",
        payload={
            "status": "interrupted",
            "result_ref": result_ref,
            "cleanup": "unknown",
        },
    )
    return verdict, closed


def _admit_one(store, stream_id, epoch, session_id, task_id, *, request_id="request-1", sequence=2):
    store.admit(
        request_id=request_id,
        payload_sha256="c" * 64,
        envelope=_admitted(stream_id, epoch, session_id, task_id, sequence),
        expected_sequence=sequence,
    )


def test_event_contract_rejects_invalid_payload_before_storage():
    stream_id, epoch, session_id = _ids()
    event = _session_opened(stream_id, epoch, session_id)
    event["payload"]["recovered"] = "yes"
    with pytest.raises(EventContractError):
        validate_event(event)


def test_durable_replay_is_byte_semantically_equal_and_sequence_checked(tmp_path):
    stream_id, epoch, session_id = _ids()
    store = SQLiteSessionStore(tmp_path / "session.db")
    event = _session_opened(stream_id, epoch, session_id)
    store.append(event, expected_sequence=1)

    assert store.replay(stream_id) == [event]
    assert store.next_sequence(stream_id) == 2
    with pytest.raises(SequenceConflict):
        store.append(event, expected_sequence=1)


def test_admission_is_atomic_idempotent_and_payload_bound(tmp_path):
    stream_id, epoch, session_id = _ids()
    task_id = str(uuid4())
    store = SQLiteSessionStore(tmp_path / "session.db")
    store.append(_session_opened(stream_id, epoch, session_id), expected_sequence=1)
    event = _admitted(stream_id, epoch, session_id, task_id, 2)

    got, created = store.admit(
        request_id="request-1", payload_sha256="c" * 64,
        envelope=event, expected_sequence=2,
    )
    assert (got, created) == (task_id, True)
    assert store.next_sequence(stream_id) == 3

    got, created = store.admit(
        request_id="request-1", payload_sha256="c" * 64,
        envelope=event, expected_sequence=2,
    )
    assert (got, created) == (task_id, False)
    assert [item["kind"] for item in store.replay(stream_id)] == ["session.opened", "task.admitted"]

    with pytest.raises(AdmissionConflict):
        store.admit(
            request_id="request-1", payload_sha256="d" * 64,
            envelope=event, expected_sequence=2,
        )


def test_task_verdict_and_close_cannot_be_appended_independently(tmp_path):
    stream_id, epoch, session_id = _ids()
    task_id = str(uuid4())
    store = SQLiteSessionStore(tmp_path / "session.db")
    store.append(_session_opened(stream_id, epoch, session_id), expected_sequence=1)
    _admit_one(store, stream_id, epoch, session_id, task_id)
    verdict, closed = _terminal_pair(stream_id, epoch, session_id, task_id, 3)

    with pytest.raises(ValueError, match="atomic finalize_task"):
        store.append(verdict, expected_sequence=3)
    with pytest.raises(ValueError, match="atomic finalize_task"):
        store.append(closed, expected_sequence=4)
    assert [event["kind"] for event in store.replay(stream_id)] == ["session.opened", "task.admitted"]


def test_terminalization_commits_verdict_close_state_and_result_index_atomically(tmp_path):
    stream_id, epoch, session_id = _ids()
    task_id = str(uuid4())
    result_ref = _artifact_ref(b"terminal-result")
    store = SQLiteSessionStore(tmp_path / "session.db")
    store.append(_session_opened(stream_id, epoch, session_id), expected_sequence=1)
    _admit_one(store, stream_id, epoch, session_id, task_id)
    verdict, closed = _terminal_pair(
        stream_id, epoch, session_id, task_id, 3, result_ref=result_ref
    )

    store.finalize_task(verdict, closed, expected_sequence=3)

    assert [event["kind"] for event in store.replay(stream_id)] == [
        "session.opened", "task.admitted", "task.verdict", "task.closed"
    ]
    record = store.task_record(task_id)
    assert record is not None
    assert record["terminal"] is True
    assert record["state"] == "interrupted"
    assert record["verdict_sequence"] == 3
    assert record["closed_sequence"] == 4
    assert record["result_ref"] == result_ref
    assert store.unterminated_tasks(stream_id) == []

    with pytest.raises(TaskStateConflict, match="already terminal"):
        store.finalize_task(verdict, closed, expected_sequence=3)


def test_terminalization_rolls_back_both_events_and_index_if_second_insert_fails(tmp_path):
    stream_id, epoch, session_id = _ids()
    task_id = str(uuid4())
    store = SQLiteSessionStore(tmp_path / "session.db")
    store.append(_session_opened(stream_id, epoch, session_id), expected_sequence=1)
    _admit_one(store, stream_id, epoch, session_id, task_id)
    verdict, closed = _terminal_pair(stream_id, epoch, session_id, task_id, 3)
    closed["event_id"] = verdict["event_id"]  # second insert violates UNIQUE(event_id)

    with pytest.raises(sqlite3.IntegrityError):
        store.finalize_task(verdict, closed, expected_sequence=3)

    assert [event["kind"] for event in store.replay(stream_id)] == ["session.opened", "task.admitted"]
    assert store.next_sequence(stream_id) == 3
    record = store.task_record(task_id)
    assert record is not None and record["terminal"] is False
    assert record["verdict_sequence"] is None
    assert record["closed_sequence"] is None
    assert record["result_ref"] is None


def test_terminalization_rejects_semantic_task_id_mismatch_even_when_schema_is_valid(tmp_path):
    stream_id, epoch, session_id = _ids()
    task_id = str(uuid4())
    store = SQLiteSessionStore(tmp_path / "session.db")
    store.append(_session_opened(stream_id, epoch, session_id), expected_sequence=1)
    _admit_one(store, stream_id, epoch, session_id, task_id)
    verdict, closed = _terminal_pair(stream_id, epoch, session_id, task_id, 3)
    verdict["payload"]["completion"]["task_id"] = str(uuid4())

    with pytest.raises(ValueError, match="completion task_id"):
        store.finalize_task(verdict, closed, expected_sequence=3)
    assert store.next_sequence(stream_id) == 3


def test_recovery_query_is_scoped_to_its_durable_stream(tmp_path):
    stream_one, epoch_one, session_one = _ids()
    stream_two, epoch_two, session_two = _ids()
    task_one, task_two = str(uuid4()), str(uuid4())
    store = SQLiteSessionStore(tmp_path / "session.db")
    store.append(_session_opened(stream_one, epoch_one, session_one), expected_sequence=1)
    store.append(_session_opened(stream_two, epoch_two, session_two), expected_sequence=1)
    _admit_one(store, stream_one, epoch_one, session_one, task_one, request_id="one")
    _admit_one(store, stream_two, epoch_two, session_two, task_two, request_id="two")

    assert [row["task_id"] for row in store.unterminated_tasks(stream_one)] == [task_one]
    assert [row["task_id"] for row in store.unterminated_tasks(stream_two)] == [task_two]


def test_two_writers_cannot_claim_the_same_stream_sequence(tmp_path):
    stream_id, epoch, session_id = _ids()
    store = SQLiteSessionStore(tmp_path / "session.db")
    first = _session_opened(stream_id, epoch, session_id)
    barrier = threading.Barrier(2)
    outcomes: list[str] = []

    def write(event):
        barrier.wait()
        try:
            store.append(event, expected_sequence=1)
            outcomes.append("won")
        except SequenceConflict:
            outcomes.append("lost")

    other = dict(first)
    other["event_id"] = str(uuid4())
    threads = [threading.Thread(target=write, args=(first,)), threading.Thread(target=write, args=(other,))]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(5)

    assert all(not thread.is_alive() for thread in threads)
    assert sorted(outcomes) == ["lost", "won"]
    assert len(store.replay(stream_id)) == 1
