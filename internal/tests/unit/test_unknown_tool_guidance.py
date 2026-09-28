"""A model that invents a tool is told what it can actually call.

Found on the Panther Lake run (J11): Qwen3-8B called `repo-navigation`, a skill
name, as a tool. The error listed every registered tool, most of which the active
skill does not allow, so the next guess could only be refused too.
"""
from __future__ import annotations

from pathlib import Path

from local_agent.agent import Orchestrator, SkillLibrary
from local_agent.config import load_repo_config
from local_agent.llm.client import ScriptedClient, tool_call
from local_agent.llm.protocol import ChatResponse
from local_agent.tools import build_registry

REPO = Path(__file__).resolve().parent.parent.parent


def test_an_invented_skill_name_is_named_as_a_skill_and_only_usable_tools_are_listed(sandbox):
    repo = load_repo_config(sandbox.root)
    registry, _, _ = build_registry(repo)
    skills = SkillLibrary.discover(REPO / "skills")
    client = ScriptedClient([
        ChatResponse(tool_calls=[tool_call("repo-navigation", {}, "x")]),
        ChatResponse(tool_calls=[tool_call("submit_answer", {
            "claim": "diagnosis", "summary": "stopped", "evidence_ids": []}, "done")]),
    ])
    result = Orchestrator(repo, registry, client, skills).run(
        "where is the ring buffer", skill_name="repo-navigation")
    (record,) = [h for h in result.state.history if h.name == "repo-navigation"]
    assert record.reason == "unknown_tool"
    assert "'repo-navigation' is a skill, not a tool" in record.summary
    allowed = set(result.state.toolset)
    listed = record.summary.split("Call one of: ", 1)[1]
    assert all(f"'{name}'" in listed for name in allowed)
    assert "'apply_patch'" not in listed  # a read-only skill cannot patch
