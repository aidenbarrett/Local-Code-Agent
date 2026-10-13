from __future__ import annotations

import asyncio
from dataclasses import replace
from uuid import uuid4

from rich.text import Text
from textual.widgets import Button, Input, OptionList, Select, Static

from local_agent.session.candidate_facts import CandidateFacts
from local_agent.session.task_read_model import TaskSnapshot
from local_agent.session.textual_hub import HubViewState, SessionHubApp


def task(candidate_id, *, sequence=1, role="prepared", verified=True):
    return TaskSnapshot(
        task_id=str(uuid4()), admitted_sequence=sequence, last_sequence=sequence,
        state="completed", execution_epoch=0, origin_kind="user_direct",
        repository_id="repo", skill="fix-build-failure", deadline_utc="2030-01-01T00:00:00Z",
        verdict="VERIFIED" if verified else "NO_VERDICT", result_verified_at_completion=verified,
        closed_sequence=sequence,
        candidate=CandidateFacts(role, candidate_id, role == "prepared", ("src/x.cpp",), "a"*64, "b"*40, None),
    )


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
            selector = app.query_one("#candidate-select", Select)
            assert app.query_one("#apply-candidate", Button).disabled
            selector.value = first
            await pilot.pause()
            assert first in str(app.query_one("#activity", Static).render())
            assert second not in str(app.query_one("#activity", Static).render())
            await pilot.click("#review-candidate")
            await pilot.click("#apply-candidate")
            await pilot.pause()
            assert commands == [(f"/diff {first}", False),(f"/apply {first}",False)]
            assert composer.value == "unfinished user message"
            refused = task(first, sequence=3, role="apply_refused", verified=False)
            app.replace_state(HubViewState(tasks=(a,b,refused)))
            await pilot.pause()
            assert selector.value == first
            assert not app.query_one("#review-candidate", Button).disabled
            assert not app.query_one("#apply-candidate", Button).disabled
            await pilot.click("#review-candidate")
            await pilot.click("#apply-candidate")
            await pilot.pause()
            assert commands[-2:] == [(f"/diff {first}",False),(f"/apply {first}",False)]
            assert composer.value == "unfinished user message"
            applied = task(first, sequence=4, role="applied")
            app.replace_state(HubViewState(tasks=(a,b,applied)))
            await pilot.pause()
            assert selector.value == first
            assert app.query_one("#apply-candidate", Button).disabled
            assert not app.query_one("#undo-candidate", Button).disabled
            await pilot.click("#undo-candidate")
            await pilot.pause()
            assert commands[-1] == (f"/undo {first}",False)
            selector.value = second
            await pilot.pause()
            await pilot.click("#apply-candidate")
            await pilot.pause()
            assert commands[-1] == (f"/apply {second}",False)
            app.replace_state(HubViewState())
            await pilot.pause()
            assert selector.value not in {first, second}
            assert all(app.query_one('#'+name,Button).disabled for name in
                       ('review-candidate','apply-candidate','undo-candidate'))
    asyncio.run(scenario())


def test_unverified_candidate_actions_remain_unavailable():
    async def scenario():
        candidate_id=str(uuid4())
        app=SessionHubApp(HubViewState(tasks=(task(candidate_id,verified=False),)))
        async with app.run_test() as pilot:
            app.query_one("#candidate-select",Select).value=candidate_id
            await pilot.pause()
            assert app.query_one("#review-candidate",Button).disabled
            assert app.query_one("#apply-candidate",Button).disabled
            assert app.query_one("#undo-candidate",Button).disabled
    asyncio.run(scenario())


def test_candidate_menu_shows_literal_changed_files_without_changing_action_identity():
    async def scenario():
        first, second = str(uuid4()), str(uuid4())
        a, b = task(first), task(second, sequence=2)
        a = replace(a, candidate=replace(a.candidate, paths=("src/[red]one\n.cpp", "include/one.hpp")))
        b = replace(b, candidate=replace(b.candidate, paths=("src/two.cpp",)))
        commands = []
        class App(SessionHubApp):
            def on_hub_input_submitted(self, message):
                commands.append(message.text)
        app = App(HubViewState(tasks=(a, b)))
        async with app.run_test(size=(120, 40)) as pilot:
            selector = app.query_one("#candidate-select", Select)
            selector.focus()
            await pilot.press("enter")
            await pilot.pause()
            options = app.query_one(OptionList)
            prompts = [str(options.get_option_at_index(i).prompt) for i in range(options.option_count)]
            assert any("src/[red]one\\x0a.cpp (+1)" in prompt and first[:8] in prompt
                       for prompt in prompts)
            assert any("src/two.cpp" in prompt and second[:8] in prompt for prompt in prompts)
            literal = next(options.get_option_at_index(i).prompt for i in range(options.option_count)
                           if "src/[red]one" in str(options.get_option_at_index(i).prompt))
            assert isinstance(literal, Text) and "[red]" in literal.plain and literal.spans == []
            assert "\n" not in literal.plain
            await pilot.press("escape")
            selector.value = second
            await pilot.pause()
            await pilot.click("#review-candidate")
            await pilot.pause()
            assert commands == [f"/diff {second}"]
    asyncio.run(scenario())
