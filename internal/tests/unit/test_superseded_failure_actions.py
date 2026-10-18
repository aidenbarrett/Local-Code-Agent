"""A check failure that a later run of the same check replaced can be explained, not fixed.

After "run the tests" fails, a fix is applied and the tests pass on a later run, the old
failure is still selectable as history. Explain stays available; Fix is disabled,
because nothing on the checkout is left for it to repair. Only a later run that
actually executed the same check, in the same repository, replaces the failure.
"""
from __future__ import annotations

import asyncio
from uuid import uuid4

import pytest
from textual.widgets import Button, Select

from local_agent.session.attention import superseded_check
from local_agent.session.task_read_model import TaskSnapshot
from local_agent.session.textual_hub import HubViewState, SessionHubApp


def _run(verdict: str, sequence: int, *, ran: bool = True, check: str = "run-tests",
         repository: str = "repo") -> TaskSnapshot:
    return TaskSnapshot(
        task_id=str(uuid4()), admitted_sequence=sequence, last_sequence=sequence,
        state="failed" if verdict == "FAILED" else "completed", execution_epoch=0,
        origin_kind="user_rule", repository_id=repository, skill=check,
        deadline_utc="2030-01-01T00:00:00Z", verdict=verdict, closed_sequence=sequence,
        configured_check=check, result_verification_ran=ran,
        verdict_reason="verification_failed" if verdict == "FAILED" else "verification_passed",
    )


def test_only_a_later_executed_run_of_the_same_check_replaces_a_failure():
    failed = _run("FAILED", 2)
    assert superseded_check((failed, _run("VERIFIED", 9)), failed)
    assert superseded_check((failed, _run("FAILED", 9)), failed)
    for later in (_run("VERIFIED", 1), _run("VERIFIED", 9, ran=False),
                  _run("REFUSED", 9), _run("VERIFIED", 9, check="run-build"),
                  _run("VERIFIED", 9, repository="other")):
        assert not superseded_check((failed, later), failed), later


@pytest.mark.parametrize(("later", "fix_enabled"), [
    (None, True),
    ("passed", False),
    ("refused", True),
])
def test_fix_is_offered_only_for_a_failure_nothing_has_replaced(later, fix_enabled):
    failed = _run("FAILED", 2)
    tasks = [failed]
    if later == "passed":
        tasks.append(_run("VERIFIED", 9))
    elif later == "refused":
        tasks.append(_run("REFUSED", 9, ran=False))

    async def scenario():
        sent = []

        class App(SessionHubApp):
            def on_hub_input_submitted(self, message):
                sent.append(message.text)

        app = App(HubViewState(tasks=tuple(tasks)))
        async with app.run_test(size=(120, 40)) as pilot:
            app.query_one("#task-select", Select).value = failed.task_id
            await pilot.pause()
            assert not app.query_one("#explain-task", Button).disabled
            fix = app.query_one("#fix-task", Button)
            assert fix.disabled is (not fix_enabled)
            # A press that reaches a disabled Fix (for example a stale event) sends nothing.
            fix.disabled = False
            fix.press()
            await pilot.pause()
            assert sent == ([f"Fix task {failed.task_id}"] if fix_enabled else [])
    asyncio.run(scenario())
