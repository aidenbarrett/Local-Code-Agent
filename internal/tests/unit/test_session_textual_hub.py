from __future__ import annotations

import asyncio
from uuid import uuid4

from textual.widgets import Input, Static

from local_agent.session.task_read_model import TaskSnapshot, ToolActivity
from local_agent.session.textual_hub import (
    ConversationEntry,
    HubViewState,
    SessionHubApp,
    layout_mode,
    load_palette,
    render_activity,
    render_conversation,
    render_watches,
)
from local_agent.session.watch_read_model import WatchRunSummary, WatchSnapshot


def _task() -> TaskSnapshot:
    task = TaskSnapshot(
        task_id=str(uuid4()),
        admitted_sequence=1,
        last_sequence=4,
        state="completed",
        execution_epoch=1,
        origin_kind="rule",
        repository_id="repo-1",
        skill="build-and-test",
        deadline_utc="2026-09-21T18:00:00Z",
    )
    task.last_tool = ToolActivity(
        call_id="call-1",
        tool_name="run_tests",
        execution="completed",
        domain="success",
        reason_code="ok",
        exit_code=0,
        duration_ms=123,
        evidence_ids=("evidence:test",),
    )
    task.verdict = "VERIFIED"
    task.verdict_reason = "verification_passed"
    task.verdict_lines = (
        "VERIFIED",
        "Scope: configured build and tests",
        "Evidence: evidence:test",
    )
    return task


def _watch() -> WatchSnapshot:
    run_id = str(uuid4())
    snapshot = WatchSnapshot(
        job_id=str(uuid4()),
        first_sequence=10,
        last_sequence=11,
        job_revision="a" * 64,
        schedule_revision="b" * 64,
        state="enabled",
        reason_code="configured",
        due_utc=None,
        next_due_utc="2026-09-21T19:00:00Z",
    )
    snapshot.runs.append(
        WatchRunSummary(
            sequence=11,
            run_id=run_id,
            task_id=str(uuid4()),
            job_revision="a" * 64,
            schedule_revision="b" * 64,
            execution_contract_sha256="c" * 64,
            status="completed",
            verdict="VERIFIED",
            comparison="UNCHANGED",
            previous_attempt_id=None,
            comparison_run_id=None,
            due_utc=None,
            started_utc="2026-09-21T17:00:00Z",
            finished_utc="2026-09-21T17:00:05Z",
            next_due_utc="2026-09-21T19:00:00Z",
            counts={"passed": 12, "failed": 0},
            delta_ref=None,
        )
    )
    return snapshot


def test_palette_has_exact_named_roles_in_both_variants():
    expected = {
        "background",
        "surface",
        "foreground",
        "accent",
        "secondary",
        "warn",
        "fail",
        "verified",
        "muted",
        "focus",
    }
    assert set(load_palette("neon")) == expected
    assert set(load_palette("high_contrast")) == expected


def test_layout_breakpoints_match_accepted_design():
    assert layout_mode(120) == "wide"
    assert layout_mode(119) == "medium"
    assert layout_mode(80) == "medium"
    assert layout_mode(79) == "compact"


def test_conversation_is_prose_only_and_keeps_original_text():
    entries = (
        ConversationEntry("user", "Build it"),
        ConversationEntry("assistant", "I can inspect that."),
    )
    rendered = render_conversation(entries)
    assert "YOU\nBuild it" in rendered
    assert "LCA\nI can inspect that." in rendered


def test_activity_shows_route_skill_tool_and_controller_verdict_lines_verbatim():
    task = _task()
    state = HubViewState(tasks=(task,), route_summary="rule · build-and-test/v1")
    rendered = render_activity(state)
    assert "Route: rule · build-and-test/v1" in rendered
    assert "Origin: rule" in rendered
    assert "Skill: build-and-test" in rendered
    assert "Tool: run_tests · completed" in rendered
    assert "Verdict: VERIFIED · verification_passed" in rendered
    for line in task.verdict_lines:
        assert line in rendered


def test_watch_pane_has_mandatory_empty_state_and_latest_attempt_row():
    assert render_watches(()) == "No watches configured"
    watch = _watch()
    rendered = render_watches((watch,))
    assert watch.job_id[:8] in rendered
    assert "enabled" in rendered
    assert "completed/VERIFIED" in rendered
    assert "UNCHANGED" in rendered
    assert "2026-09-21T19:00:00Z" in rendered


def test_textual_shell_mounts_all_mandatory_panes_and_applies_wide_layout():
    async def scenario() -> None:
        state = HubViewState(
            conversation=(ConversationEntry("user", "What changed on my branch?"),),
            tasks=(_task(),),
            watches=(_watch(),),
            status="Fixture mode",
        )
        app = SessionHubApp(state)
        async with app.run_test(size=(130, 40)) as pilot:
            await pilot.pause()
            workspace = app.query_one("#workspace")
            assert workspace.has_class("wide")
            assert app.query_one("#conversation", Static)
            assert app.query_one("#activity", Static)
            assert app.query_one("#watch", Static)
            assert app.query_one("#composer", Input)
            assert app.query_one("#status", Static)

    asyncio.run(scenario())


def test_compact_layout_keeps_activity_watch_and_composer_present():
    async def scenario() -> None:
        app = SessionHubApp(HubViewState())
        async with app.run_test(size=(72, 30)) as pilot:
            await pilot.pause()
            workspace = app.query_one("#workspace")
            assert workspace.has_class("compact")
            assert app.query_one("#layout-warning", Static).display is True
            assert app.query_one("#activity", Static).display is True
            assert app.query_one("#watch", Static).display is True
            assert app.query_one("#composer", Input).display is True

    asyncio.run(scenario())
