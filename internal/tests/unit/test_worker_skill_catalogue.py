"""A worker with an active skill is not shown the other skills' names.

Nothing lets the worker switch skills, so listing them is only bait: on the PTL
acceptance run the model called ``diagnose-build-failure`` (J08) and
``repo-navigation`` (J11) as tools and gave up. These tests look at the bytes of
the first request the model receives.
"""
from __future__ import annotations

from uuid import uuid4

from local_agent.agent import Orchestrator, SkillLibrary, default_search_path
from local_agent.agent.policy import deny_all_approvals
from local_agent.config import load_repo_config
from local_agent.llm.client import ScriptedClient, tool_call
from local_agent.llm.protocol import ChatResponse
from local_agent.session.event_buffer import EventBuffer
from local_agent.session.task_controller import TaskController
from local_agent.session.workspaces import GitWorkspaceManager
from local_agent.tools import build_registry

# Imports stay at module scope: test_session_import_boundary purges and re-imports
# local_agent modules, and a function-local import can bind a newer class.


def _finish():
    return [ChatResponse(tool_calls=[tool_call(
        "submit_answer", {"claim": "failure", "summary": "captured"}, "c0")])]


def _system_text(client: ScriptedClient) -> str:
    assert client.calls, "the model was never asked anything"
    return "\n".join(m["content"] for m in client.calls[0] if m.get("role") == "system")


def test_the_product_worker_sees_its_active_skill_and_no_catalogue(sandbox, tmp_path):
    client = ScriptedClient(_finish())
    controller = TaskController(
        load_repo_config(sandbox.root), lambda: client, EventBuffer(uuid4().hex),
        allow_execution=True,
        workspaces=GitWorkspaceManager(tmp_path / "lca-ws", controller_commit="c" * 40),
    )

    controller.run("fix the build", task_id=str(uuid4()), skill_name="fix-build-failure")

    system = _system_text(client)
    assert "Active skill: fix-build-failure" in system
    assert "Available skills:" not in system
    for other in ("repo-navigation", "diagnose-build-failure", "build-and-test"):
        assert other not in system, f"the worker was shown the {other} skill name"


def test_the_catalogue_is_still_offered_when_no_skill_is_active(sandbox):
    repo = load_repo_config(sandbox.root)
    library = SkillLibrary.discover_many(default_search_path(repo.root, repo.skills_dir))
    registry, _ctx, _store = build_registry(repo)
    client = ScriptedClient(_finish())
    Orchestrator(repo=repo, registry=registry, client=client, skills=library,
                 approval=deny_all_approvals, allow_escalation=False).run(
        "what is in this repository?", condition="control")

    system = _system_text(client)
    assert "Available skills:" in system
    assert "Active skill:" not in system
