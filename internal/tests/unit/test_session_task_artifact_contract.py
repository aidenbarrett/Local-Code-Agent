from __future__ import annotations

import hashlib
from uuid import uuid4

import pytest

from local_agent.session.event_contract import build_event
from local_agent.session.session_store import ArtifactIntegrityError, SQLiteSessionStore


def test_retained_admission_requires_bytes_and_does_not_consume_sequence(tmp_path):
    store = SQLiteSessionStore(tmp_path / "session.db")
    stream_id, epoch, session_id, task_id = (str(uuid4()) for _ in range(4))
    request = b"request"
    ref = {
        "artifact_id": str(uuid4()),
        "sha256": hashlib.sha256(request).hexdigest(),
        "media_type": "application/vnd.lca.task-request+json",
        "size_bytes": len(request),
        "availability": "retained",
    }
    event = build_event(
        stream_id=stream_id,
        sequence=1,
        producer_epoch=epoch,
        session_id=session_id,
        task_id=task_id,
        kind="task.admitted",
        payload={
            "origin": {
                "kind": "user_direct",
                "turn_ref": {
                    "conversation_id": "conv",
                    "turn_index": 0,
                    "turn_sha256": "a" * 64,
                },
            },
            "request_ref": ref,
            "contract_sha256": "b" * 64,
            "repository_id": "repo",
            "skill": None,
            "execution_epoch": 0,
            "deadline_utc": "2030-01-01T00:00:00Z",
        },
    )

    with pytest.raises(ArtifactIntegrityError, match="requires payload bytes"):
        store.admit(
            request_id="request",
            payload_sha256=ref["sha256"],
            envelope=event,
            expected_sequence=1,
        )
    assert store.next_sequence(stream_id) == 1
    assert store.replay(stream_id) == []
