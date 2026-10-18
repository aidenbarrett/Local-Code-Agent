from __future__ import annotations

import asyncio
import time
from dataclasses import replace
from uuid import uuid4

from rich.text import Text
from textual.widgets import Button, Input, OptionList, Select, Static

from local_agent.session.candidate_facts import CandidateFacts
from local_agent.session.task_read_model import TaskSnapshot
from local_agent.session.textual_hub import HubViewState, SessionHubApp
from local_agent.session.intents import decide_route, RouteAction


def task(candidate_id, *, sequence=1, role="prepared", verified=True):
    return TaskSnapshot(
        task_id=str(uuid4()), admitted_sequence=sequence, last_sequence=sequence,
        state="completed", execution_epoch=0, origin_kind="user_direct",
        repository_id="repo", skill="fix-build-failure", deadline_utc="2030-01-01T00:00:00Z",
        verdict="VERIFIED" if verified else "NO_VERDICT", result_verified_at_completion=verified,
        closed_sequence=sequence,
        candidate=CandidateFacts(role, candidate_id, role == "prepared", ("src/x.cpp",), "a"*64, "b"*40, None),
    )


async def _press(pilot, selector):
    """Click a button only once it can actually receive the click.

    Selecting a task toggles the action row's display; on a slow runner the
    layout can lag the message that enabled the button, so a click aimed at the
    old geometry lands elsewhere and is silently lost. A button also ignores
    clicks while its press effect (``-active``) is still showing. Wait for the
    button to be shown, enabled, idle and topmost at its own centre, then
    require the click to hit.
    """
    app = pilot.app
    button = app.query_one(selector, Button)
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        await pilot.pause(0.02)
        region = button.region
        if (button.display and not button.disabled and not button.has_class("-active")
                and region.area
                and app.get_widget_at(*region.center)[0] is button):
            break
    else:
        raise AssertionError(f"{selector} never became clickable within 10 s")
    assert await pilot.click(selector), f"click on {selector} landed on another widget"


def test_selected_candidate_actions_bind_exact_id_preserve_draft_and_follow_durable_state():
    async def scenario():
        first, second = str(uuid4()), str(uuid4())
        a, b = task(first), task(second, sequence=2)
        commands = []
        class App(SessionHubApp):
            def on_hub_input_submitted(self, message):
                commands.append((message.text, message.from_composer))
        app = App(HubViewState(tasks=(a,b)))
        async with app.run_test(size=(120,40)) as pilot:
            composer = app.query_one("#composer", Input)
            composer.value = "unfinished user message"
            selector = app.query_one("#task-select", Select)
            assert app.query_one("#apply-candidate", Button).disabled
            assert app.query_one("#commit-candidate", Button).disabled
            selector.value = first
            await pilot.pause()
            assert first in str(app.query_one("#activity", Static).render())
            assert second not in str(app.query_one("#activity", Static).render())
            await _press(pilot, "#review-candidate")
            await _press(pilot, "#apply-candidate")
            await pilot.pause()
            assert commands == [(f"/diff {first}", False),(f"/apply {first}",False)]
            assert app.query_one("#commit-candidate", Button).disabled
            assert composer.value == "unfinished user message"
            refused = task(first, sequence=3, role="apply_refused", verified=False)
            app.replace_state(HubViewState(tasks=(a,b,refused)))
            await pilot.pause()
            assert selector.value == first
            assert not app.query_one("#review-candidate", Button).disabled
            assert not app.query_one("#apply-candidate", Button).disabled
            await _press(pilot, "#review-candidate")
            await _press(pilot, "#apply-candidate")
            await pilot.pause()
            assert commands[-2:] == [(f"/diff {first}",False),(f"/apply {first}",False)]
            assert composer.value == "unfinished user message"
            applied = task(first, sequence=4, role="applied")
            app.replace_state(HubViewState(tasks=(a,b,applied)))
            await pilot.pause()
            assert selector.value == first
            assert app.query_one("#apply-candidate", Button).disabled
            assert not app.query_one("#undo-candidate", Button).disabled
            assert not app.query_one("#commit-candidate", Button).disabled
            await _press(pilot, "#commit-candidate")
            await pilot.pause()
            assert commands[-1] == (f"/commit {first}", False)
            assert composer.value == "unfinished user message"
            await _press(pilot, "#undo-candidate")
            await pilot.pause()
            assert commands[-1] == (f"/undo {first}",False)
            selector.value = second
            await pilot.pause()
            assert app.query_one("#commit-candidate", Button).disabled
            await _press(pilot, "#apply-candidate")
            await pilot.pause()
            assert commands[-1] == (f"/apply {second}",False)
            app.replace_state(HubViewState())
            await pilot.pause()
            assert selector.value not in {first, second}
            assert all(app.query_one('#'+name,Button).disabled for name in
                       ('review-candidate','apply-candidate','undo-candidate','commit-candidate'))
    asyncio.run(scenario())


def test_unverified_candidate_actions_remain_unavailable():
    async def scenario():
        candidate_id=str(uuid4())
        app=SessionHubApp(HubViewState(tasks=(task(candidate_id,verified=False),)))
        async with app.run_test() as pilot:
            app.query_one("#task-select",Select).value=candidate_id
            await pilot.pause()
            assert app.query_one("#review-candidate",Button).disabled
            assert app.query_one("#apply-candidate",Button).disabled
            assert app.query_one("#undo-candidate",Button).disabled
            assert app.query_one("#commit-candidate",Button).disabled
    asyncio.run(scenario())


def test_reviewed_activity_does_not_advertise_apply_without_prepared_proof():
    async def scenario():
        candidate_id = str(uuid4())
        prepared = task(candidate_id, verified=False)
        reviewed = task(candidate_id, sequence=2, role="reviewed", verified=False)
        app = SessionHubApp(HubViewState(tasks=(prepared, reviewed)))
        async with app.run_test() as pilot:
            app.query_one("#task-select", Select).value = candidate_id
            await pilot.pause()
            activity = str(app.query_one("#activity", Static).render())
            assert "Candidate: REVIEWED · NOT APPLIED" in activity
            assert f"/apply {candidate_id}" not in activity
            assert app.query_one("#review-candidate", Button).disabled
            assert app.query_one("#apply-candidate", Button).disabled

    asyncio.run(scenario())


def test_commit_requires_verified_applied_candidate_and_disables_after_terminal_state():
    async def scenario():
        candidate_id = str(uuid4())
        commands = []
        class App(SessionHubApp):
            def on_hub_input_submitted(self, message):
                commands.append(message.text)
        app = App(HubViewState(tasks=(task(candidate_id, role="applied", verified=False),)))
        async with app.run_test(size=(80, 30)) as pilot:
            app.query_one("#task-select", Select).value = candidate_id
            await pilot.pause()
            assert app.query_one("#commit-candidate", Button).disabled
            await pilot.click("#commit-candidate")
            await pilot.pause()
            assert commands == []
            app.replace_state(HubViewState(tasks=(task(candidate_id, role="applied"),)))
            await pilot.pause()
            await _press(pilot, "#commit-candidate")
            await pilot.pause()
            assert commands == [f"/commit {candidate_id}"]
            for role in ("committed", "undone", "apply_refused"):
                app.replace_state(HubViewState(tasks=(task(candidate_id, role=role),)))
                await pilot.pause()
                assert app.query_one("#commit-candidate", Button).disabled
    asyncio.run(scenario())


def test_candidate_menu_shows_literal_changed_files_without_changing_action_identity():
    async def scenario():
        first, second = str(uuid4()), str(uuid4())
        third = str(uuid4())
        a, b = task(first), task(second, sequence=2)
        reviewed = task(third, sequence=3, role="reviewed")
        a = replace(a, candidate=replace(a.candidate, paths=("src/[red]one\n.cpp", "include/one.hpp")))
        b = replace(b, candidate=replace(b.candidate, paths=("src/two.cpp",)))
        commands = []
        class App(SessionHubApp):
            def on_hub_input_submitted(self, message):
                commands.append(message.text)
        app = App(HubViewState(tasks=(a, b, reviewed)))
        async with app.run_test(size=(120, 40)) as pilot:
            selector = app.query_one("#task-select", Select)
            selector.focus()
            await pilot.press("enter")
            await pilot.pause()
            options = app.query_one(OptionList)
            prompts = [str(options.get_option_at_index(i).prompt) for i in range(options.option_count)]
            assert any("src/[red]one\\x0a.cpp (+1)" in prompt and first[:8] in prompt
                       for prompt in prompts)
            assert any("src/two.cpp" in prompt and second[:8] in prompt for prompt in prompts)
            assert any("src/x.cpp · reviewed" in prompt and third[:8] in prompt
                       for prompt in prompts)
            literal = next(options.get_option_at_index(i).prompt for i in range(options.option_count)
                           if "src/[red]one" in str(options.get_option_at_index(i).prompt))
            assert isinstance(literal, Text) and "[red]" in literal.plain and literal.spans == []
            assert "\n" not in literal.plain
            await pilot.press("escape")
            selector.value = second
            await pilot.pause()
            await _press(pilot, "#review-candidate")
            await pilot.pause()
            assert commands == [f"/diff {second}"]
    asyncio.run(scenario())


def test_select_earlier_failure_explains_and_fixes_exact_task_without_copying_ids():
    async def scenario():
        first, second = str(uuid4()), str(uuid4())
        a = replace(task(first), task_id=first, candidate=None, skill="run-build",
                    configured_check="run-build", state="failed", verdict="FAILED",
                    result_answer="Earlier compiler error")
        b = replace(task(second, sequence=2), task_id=second, candidate=None, skill="run-tests",
                    configured_check="run-tests", state="failed", verdict="FAILED",
                    result_answer="Later assertion failure")
        commands = []
        class App(SessionHubApp):
            def on_hub_input_submitted(self, message):
                commands.append((message.text, message.from_composer))
        app = App(HubViewState(tasks=(a, b)))
        async with app.run_test(size=(80, 30)) as pilot:
            composer = app.query_one("#composer", Input)
            composer.value = "unfinished user message"
            selector = app.query_one("#task-select", Select)
            selector.value = first
            await pilot.pause()
            activity = str(app.query_one("#activity", Static).render())
            assert "Earlier compiler error" in activity and "Later assertion failure" not in activity
            assert not app.query_one("#review-candidate", Button).display
            await _press(pilot, "#explain-task")
            await _press(pilot, "#fix-task")
            await pilot.pause()
            assert commands == [(f"Why did task {first} fail?", False), (f"Fix task {first}", False)]
            assert composer.value == "unfinished user message"
            for text, _ in commands:
                decision = decide_route(
                    text, active_repo_count=1, eligible_task_ids=(first, second),
                    fixable_failure_kinds={first: "build", second: "test"},
                )
                assert decision.action == RouteAction.WORK and decision.reference_ids == (first,)
            selector.value = second
            await pilot.pause()
            await _press(pilot, "#fix-task")
            await pilot.pause()
            decision = decide_route(commands[-1][0], active_repo_count=1,
                                    eligible_task_ids=(first, second),
                                    fixable_failure_kinds={first: "build", second: "test"})
            assert decision.reference_ids == (second,) and decision.skill == "fix-test-failure"
            app.replace_state(HubViewState(tasks=(a, b)))
            await pilot.pause()
            assert selector.value == second and composer.value == "unfinished user message"
            app.replace_state(HubViewState())
            await pilot.pause()
            assert selector.value not in {first, second}
            assert app.query_one("#explain-task", Button).disabled
            assert app.query_one("#fix-task", Button).disabled
    asyncio.run(scenario())


def test_unknown_running_and_unrelated_tasks_offer_no_failure_actions():
    async def scenario():
        task_id = str(uuid4())
        original = replace(task(task_id), task_id=task_id, candidate=None, skill="run-build",
                           configured_check="run-build", state="failed", verdict="FAILED")
        app = SessionHubApp(HubViewState(tasks=(original,)))
        async with app.run_test() as pilot:
            selector = app.query_one("#task-select", Select)
            selector.value = task_id
            await pilot.pause()
            assert not app.query_one("#explain-task", Button).disabled
            for changed in (replace(original, verdict="NO_VERDICT"),
                            replace(original, closed_sequence=None),
                            replace(original, skill="inspect", configured_check=None),
                            replace(original, configured_check=None),
                            replace(original, verdict="VERIFIED")):
                app.replace_state(HubViewState(tasks=(changed,)))
                await pilot.pause()
                assert selector.value == task_id
                assert app.query_one("#explain-task", Button).disabled
                assert app.query_one("#fix-task", Button).disabled
                assert not app.query_one("#task-actions").display
    asyncio.run(scenario())


def _check_task(task_id, *, check="run-build", state="failed", verdict="FAILED", closed=True):
    return TaskSnapshot(
        task_id=task_id, admitted_sequence=1, last_sequence=2, state=state, execution_epoch=0,
        origin_kind="user_rule", repository_id="repo", skill=check,
        deadline_utc="2030-01-01T00:00:00Z", verdict=verdict,
        result_verified_at_completion=False, closed_sequence=2 if closed else None,
        configured_check=check, result_answer="Recorded compiler error",
    )


async def _choose_only_task_by_keyboard(app, pilot):
    selector = app.query_one("#task-select", Select)
    assert selector.display, "the only failed task has no visible selector"
    assert selector.is_blank(), "a task was selected for the user"
    assert app.query_one("#explain-task", Button).disabled
    selector.focus()
    await pilot.press("enter")
    await pilot.pause()
    await pilot.press("down", "enter")
    await pilot.pause()


def test_the_only_failed_check_is_selectable_and_explained_without_new_work():
    """Fresh mount with one failed Build: reachable by keyboard and click only."""
    async def scenario():
        task_id = str(uuid4())
        commands = []
        class App(SessionHubApp):
            def on_hub_input_submitted(self, message):
                commands.append((message.text, message.from_composer))
        app = App(HubViewState(tasks=(_check_task(task_id),)))
        async with app.run_test(size=(100, 30)) as pilot:
            composer = app.query_one("#composer", Input)
            composer.value = "unfinished user message"
            await _choose_only_task_by_keyboard(app, pilot)
            assert app.query_one("#task-select", Select).value == task_id
            assert not app.query_one("#explain-task", Button).disabled
            await _press(pilot, "#explain-task")
            await pilot.pause()
            await _press(pilot, "#fix-task")
            await pilot.pause()
            assert commands == [(f"Why did task {task_id} fail?", False), (f"Fix task {task_id}", False)]
            assert composer.value == "unfinished user message"
            decision = decide_route(commands[1][0], active_repo_count=1,
                                    eligible_task_ids=(task_id,),
                                    fixable_failure_kinds={task_id: "build"})
            assert decision.action == RouteAction.WORK and decision.reference_ids == (task_id,)
    asyncio.run(scenario())


def test_a_build_that_fails_while_the_hub_is_open_becomes_selectable():
    """Empty -> running -> failed, with no direct selector assignment."""
    async def scenario():
        task_id = str(uuid4())
        commands = []
        class App(SessionHubApp):
            def on_hub_input_submitted(self, message):
                commands.append(message.text)
        app = App(HubViewState())
        async with app.run_test(size=(100, 30)) as pilot:
            selector = app.query_one("#task-select", Select)
            assert not selector.display
            app.replace_state(HubViewState(tasks=(
                _check_task(task_id, state="running", verdict=None, closed=False),)))
            await pilot.pause()
            assert not selector.display, "a running task offered failure actions"
            assert app.query_one("#explain-task", Button).disabled
            app.replace_state(HubViewState(tasks=(_check_task(task_id),)))
            await pilot.pause()
            await _choose_only_task_by_keyboard(app, pilot)
            await _press(pilot, "#explain-task")
            await pilot.pause()
            assert commands == [f"Why did task {task_id} fail?"]
    asyncio.run(scenario())


def test_a_lone_task_that_is_not_a_failed_check_still_offers_no_selector():
    async def scenario():
        for changed in (_check_task(str(uuid4()), verdict="VERIFIED"),
                        _check_task(str(uuid4()), verdict="NO_VERDICT"),
                        replace(_check_task(str(uuid4())), configured_check=None, skill="inspect")):
            app = SessionHubApp(HubViewState(tasks=(changed,)))
            async with app.run_test(size=(100, 30)) as pilot:
                await pilot.pause()
                assert not app.query_one("#task-select", Select).display
                assert app.query_one("#explain-task", Button).disabled
                assert app.query_one("#fix-task", Button).disabled
    asyncio.run(scenario())
