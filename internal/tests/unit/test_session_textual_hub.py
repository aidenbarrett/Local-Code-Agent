from __future__ import annotations

import asyncio
from uuid import uuid4

import pytest
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
            assert app.query_one("#watch", Static).display is False
            assert app.query_one("#composer", Input).display is True

    asyncio.run(scenario())


def test_watch_pane_appears_when_a_watch_arrives_after_mount():
    async def scenario() -> None:
        app = SessionHubApp(HubViewState())
        async with app.run_test(size=(130, 40)) as pilot:
            await pilot.pause()
            watch_widget = app.query_one("#watch", Static)
            assert watch_widget.display is False

            app.replace_state(HubViewState(watches=(_watch(),)))
            await pilot.pause()

            assert watch_widget.display is True
            assert "enabled" in str(watch_widget.render())

    asyncio.run(scenario())


@pytest.mark.parametrize("size", [(72, 24), (100, 24), (130, 40)])
def test_first_keystrokes_reach_composer_without_clicking(size):
    async def scenario() -> None:
        app = SessionHubApp(HubViewState())
        async with app.run_test(size=size) as pilot:
            composer = app.query_one("#composer", Input)
            assert app.focused is composer
            await pilot.press("h", "i")
            assert composer.value == "hi"

    asyncio.run(scenario())


@pytest.mark.parametrize("size", [(72, 24), (72, 30), (100, 24), (100, 30), (130, 40)])
def test_outage_and_long_path_keep_task_watch_and_composer_inside_viewport(size, tmp_path):
    async def scenario() -> None:
        task = _task()
        task.state = "failed"
        task.verdict = "NO_VERDICT"
        task.verdict_reason = "endpoint_unavailable"
        task.verdict_lines = ("NO_VERDICT", "Scope: endpoint unavailable", "Evidence: unavailable")
        long_path = "repository-with-long-path/" * 30 + "last-file.cpp"
        task.faults.append(("endpoint_unavailable", f"Endpoint dropped while inspecting {long_path}"))
        watch = _watch()
        state = HubViewState(tasks=(task,), watches=(watch,), status="Endpoint unavailable")
        app = SessionHubApp(state)
        async with app.run_test(size=size) as pilot:
            await pilot.pause()
            status = app.query_one("#status", Static)
            for name in ("attention-pane", "activity-pane", "watch-pane", "composer"):
                widget = app.query_one(f"#{name}")
                assert widget.display and widget.visible
                assert widget.region.y >= 0
                assert widget.region.bottom <= status.region.y, (name, widget.region, status.region)
            attention = app.query_one("#attention", Static)
            assert long_path in str(attention.render())
            attention_pane = app.query_one("#attention-pane")
            assert attention_pane.max_scroll_y > 0
            await pilot.click("#composer")
            await pilot.press("tab")
            assert app.focused is attention_pane
            await pilot.press("end")
            await pilot.pause()
            await app.animator.wait_for_idle()
            assert attention_pane.scroll_y == attention_pane.max_scroll_y
            assert "completed/VERIFIED" in str(app.query_one("#watch", Static).render())
            app.save_screenshot(filename=f"hub-outage-{size[0]}x{size[1]}.svg", path=tmp_path)
            await pilot.click("#composer")
            await pilot.press("i", "n", "s", "p", "e", "c", "t")
            assert app.query_one("#composer", Input).value == "inspect"
            app.replace_state(HubViewState(tasks=(_task(),), watches=(watch,), status="Ready"))
            await pilot.pause()
            assert attention.display is False
            assert app.query_one("#composer", Input).value == "inspect"
            assert app.query_one("#watch-pane").region.bottom <= status.region.y

    asyncio.run(scenario())
