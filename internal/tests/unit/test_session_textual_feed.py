from __future__ import annotations

import hashlib
import json
from uuid import uuid4

import pytest

from local_agent.session.session_event_service import DurableSessionService
from local_agent.session.session_store import SQLiteSessionStore
from local_agent.session.textual_feed import DurableHubFeed, HubFeedError
from local_agent.session.textual_hub import ConversationEntry


def _service(tmp_path) -> DurableSessionService:
    return DurableSessionService(
        SQLiteSessionStore(tmp_path / "session.db"),
        stream_id=str(uuid4()),
        session_id=str(uuid4()),
    )


def _opened(service: DurableSessionService, label: str):
    receipt = service.append(
        "session.opened",
        {
            "conversation_id": label,
            "repository_id": "repo-1",
            "controller_commit": "fixture",
            "capabilities": [],
            "recovered": False,
        },
    )
    receipt.wait(5)


def _admit_retained_request(
    service: DurableSessionService,
    outcome: str,
    media_type: str = "application/vnd.lca.task-request+json",
) -> str:
    request_bytes = json.dumps({"task": outcome}, separators=(",", ":")).encode()
    digest = hashlib.sha256(request_bytes).hexdigest()
    receipt = service.submit_task(
        request_id="request-with-outcome",
        payload_sha256=digest,
        admission_payload={
            "origin": {
                "kind": "user_direct",
                "turn_ref": {
                    "conversation_id": "conv-1",
                    "turn_index": 0,
                    "turn_sha256": "b" * 64,
                },
            },
            "request_ref": {
                "artifact_id": str(uuid4()),
                "sha256": digest,
                "media_type": media_type,
                "size_bytes": len(request_bytes),
                "availability": "retained",
            },
            "contract_sha256": "d" * 64,
            "repository_id": "repo-1",
            "skill": "build-and-test",
            "execution_epoch": 0,
            "deadline_utc": "2030-01-01T00:00:00Z",
        },
        request_bytes=request_bytes,
    )
    receipt.wait(5)
    assert receipt.task_id is not None
    return receipt.task_id


def test_start_replays_to_exact_handoff_boundary_and_projects_conversation(tmp_path):
    service = _service(tmp_path)
    try:
        _opened(service, "first")
        _opened(service, "second")
        feed = DurableHubFeed(
            service,
            conversation_provider=lambda: (
                ConversationEntry("user", "hello"),
                ConversationEntry("assistant", "hi"),
            ),
            replay_page=1,
        )
        state = feed.start()
        assert feed.cursor == 2
        assert [event["sequence"] for event in feed.events] == [1, 2]
        assert [entry.text for entry in state.conversation] == ["hello", "hi"]
        assert state.status == "Live · durable seq 2"
    finally:
        service.close()


def test_live_poll_accepts_only_new_committed_events(tmp_path):
    service = _service(tmp_path)
    try:
        _opened(service, "initial")
        feed = DurableHubFeed(service)
        feed.start()
        assert feed.poll() is False

        _opened(service, "live")
        assert feed.poll() is True
        assert feed.cursor == 2
        assert feed.state().status == "Live · durable seq 2"
    finally:
        service.close()


def test_feed_hydrates_original_requested_outcome_from_retained_artifact(tmp_path):
    service = _service(tmp_path)
    try:
        task_id = _admit_retained_request(service, "Build and test this repository.")
        state = DurableHubFeed(service).start()
        task = next(item for item in state.tasks if item.task_id == task_id)
        assert task.requested_outcome == "Build and test this repository."
        assert task.repository_id == "repo-1"
    finally:
        service.close()


@pytest.mark.parametrize("media_type", [
    "application/vnd.lca.task-request+json",
    "application/vnd.lca.watch-task-request+json",
])
def test_feed_hydrates_the_outcome_from_both_admission_routes(tmp_path, media_type):
    """Review of #460: a retained watch request is not \"unavailable\"."""
    service = _service(tmp_path)
    try:
        task_id = _admit_retained_request(service, "Run the nightly tests.", media_type)
        state = DurableHubFeed(service).start()
        task = next(item for item in state.tasks if item.task_id == task_id)
        assert task.requested_outcome == "Run the nightly tests."
    finally:
        service.close()


def test_feed_does_not_read_an_unrecognised_request_media_type(tmp_path):
    service = _service(tmp_path)
    try:
        task_id = _admit_retained_request(
            service, "Run the nightly tests.", "application/vnd.lca.other+json")
        state = DurableHubFeed(service).start()
        task = next(item for item in state.tasks if item.task_id == task_id)
        assert task.requested_outcome is not None
        assert task.requested_outcome.startswith("unavailable")
    finally:
        service.close()


def test_subscription_overflow_replays_from_last_accepted_cursor(tmp_path):
    service = _service(tmp_path)
    try:
        _opened(service, "initial")
        feed = DurableHubFeed(service, capacity=1, replay_page=1)
        feed.start()

        _opened(service, "one")
        _opened(service, "two")
        assert feed.poll() is True
        assert feed.cursor == 3
        assert feed.recovered_gap is True
        assert [event["sequence"] for event in feed.events] == [1, 2, 3]
        assert "replay recovered" in feed.state().status
    finally:
        service.close()


def test_feed_fails_closed_instead_of_truncating_required_projection_history(tmp_path):
    service = _service(tmp_path)
    try:
        _opened(service, "initial")
        _opened(service, "second")
        feed = DurableHubFeed(service, max_events=1)
        with pytest.raises(HubFeedError, match="bounded event history"):
            feed.start()
    finally:
        service.close()


def test_feed_rejects_poll_before_start(tmp_path):
    service = _service(tmp_path)
    try:
        feed = DurableHubFeed(service)
        with pytest.raises(HubFeedError, match="started"):
            feed.poll()
    finally:
        service.close()
