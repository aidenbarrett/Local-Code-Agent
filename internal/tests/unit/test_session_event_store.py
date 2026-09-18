from __future__ import annotations

import hashlib
import threading
from uuid import uuid4

import pytest

from local_agent.session.event_contract import EventContractError, build_event, validate_event
from local_agent.session.storage import AdmissionConflict, SequenceConflict, SQLiteSessionStore


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


def _closed(stream_id: str, epoch: str, session_id: str, task_id: str, sequence: int) -> dict:
    return build_event(
        stream_id=stream_id,
        sequence=sequence,
        producer_epoch=epoch,
        session_id=session_id,
        task_id=task_id,
        kind="task.closed",
        payload={
            "status": "interrupted",
            "result_ref": _artifact_ref(b"unknown"),
            "cleanup": "unknown",
        },
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


def test_recovery_query_never_reexecutes_and_terminal_close_removes_task(tmp_path):
    stream_id, epoch, session_id = _ids()
    task_id = str(uuid4())
    store = SQLiteSessionStore(tmp_path / "session.db")
    store.append(_session_opened(stream_id, epoch, session_id), expected_sequence=1)
    store.admit(
        request_id="request-1", payload_sha256="c" * 64,
        envelope=_admitted(stream_id, epoch, session_id, task_id, 2), expected_sequence=2,
    )

    pending = store.unterminated_tasks()
    assert [row["task_id"] for row in pending] == [task_id]
    assert pending[0]["state"] == "admitted"

    store.close_task(_closed(stream_id, epoch, session_id, task_id, 3), expected_sequence=3)
    assert store.unterminated_tasks() == []
    assert [item["kind"] for item in store.replay(stream_id)] == [
        "session.opened", "task.admitted", "task.closed"
    ]


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
