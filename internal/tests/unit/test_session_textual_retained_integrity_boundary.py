from __future__ import annotations

from uuid import uuid4

from local_agent.session.session_event_service import DurableSessionService
from local_agent.session.session_store import ArtifactIntegrityError, SQLiteSessionStore
from local_agent.session.task_read_model import TaskSnapshot
from local_agent.session.textual_feed import DurableHubFeed
from local_agent.session.textual_hub import HubViewState, render_activity


def test_retained_result_integrity_failure_preserves_durable_verdict(tmp_path, monkeypatch):
    service = DurableSessionService(
        SQLiteSessionStore(tmp_path / "session.db"),
        stream_id=str(uuid4()),
        session_id=str(uuid4()),
    )
    try:
        feed = DurableHubFeed(service)
        task = TaskSnapshot(
            task_id=str(uuid4()),
            admitted_sequence=1,
            last_sequence=3,
            state="failed",
            execution_epoch=0,
            origin_kind="user_direct",
            repository_id="repo-1",
            skill="build",
            deadline_utc="2030-01-01T00:00:00Z",
            verdict="FAILED",
            verdict_reason="verification_failed",
            verdict_scope="full_build",
            closed_sequence=3,
            result_ref={"sha256": "a" * 64, "availability": "retained"},
        )
        state = HubViewState(tasks=(task,))

        def reject_retained_result(self, task_id):
            raise ArtifactIntegrityError("fixture integrity failure")

        monkeypatch.setattr(
            "local_agent.session.textual_feed.DurableTaskHistory.result_for_task",
            reject_retained_result,
        )

        projected = feed._hydrate_retained_results(state)

        snapshot = projected.tasks[0]
        assert snapshot.task_id == task.task_id
        assert snapshot.verdict == "FAILED"
        assert snapshot.verdict_scope == "full_build"
        assert snapshot.result_answer is not None
        assert "retained worker result failed integrity validation" in snapshot.result_answer
        assert snapshot.result_verification_ran is None
        assert snapshot.result_verified_at_completion is None
        assert feed._retained_results_degraded is True

        rendered = render_activity(projected)
        assert "Verdict: FAILED" in rendered
        assert "Retained result:" in rendered
        assert "durable controller verdict above remains authoritative" in rendered
    finally:
        service.close()
