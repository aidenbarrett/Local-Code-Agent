"""Regression tests for the post-refactor contract review."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from local_agent.agent import Orchestrator, SkillLibrary
from local_agent.agent.skills import Skill
from local_agent.config import load_repo_config
from local_agent.llm.client import ScriptedClient, tool_call
from local_agent.llm.models import ChatResponse
from local_agent.tools import build_registry

REPO = Path(__file__).resolve().parent.parent.parent


def _orch(root: Path, turns, skills=None):
    repo = load_repo_config(root)
    registry, _, _ = build_registry(repo)
    return Orchestrator(
        repo,
        registry,
        ScriptedClient(turns),
        skills or SkillLibrary.discover(REPO / "skills"),
    )


def test_skill_toolset_fails_closed_on_unknown_tool(sandbox, tmp_path):
    skill = Skill(
        name="broken-skill",
        description="test",
        path=tmp_path,
        body="test",
        tools=["read_file", "definitely_not_a_tool"],
    )
    orch = _orch(sandbox.root, [ChatResponse(content="unused")], SkillLibrary([skill]))
    with pytest.raises(ValueError, match="unknown tools"):
        orch.run("do it", skill_name="broken-skill")


def test_explicit_empty_skill_toolset_does_not_fall_back(sandbox, tmp_path):
    skill = Skill(
        name="diagnose-build-failure",
        description="test",
        path=tmp_path,
        body="test",
        tools=[],
    )
    client = ScriptedClient([ChatResponse(content="done")])
    repo = load_repo_config(sandbox.root)
    registry, _, _ = build_registry(repo)
    orch = Orchestrator(repo, registry, client, SkillLibrary([skill]))
    orch.run("do it", skill_name="diagnose-build-failure")
    exposed = {schema["function"]["name"] for schema in client.tool_schemas[0]}
    assert exposed == {"submit_answer"}
    assert "apply_patch" not in exposed


def test_tool_result_exposes_canonical_evidence_id(sandbox):
    turns = [
        ChatResponse(tool_calls=[tool_call("git_status", {}, "c1")]),
        ChatResponse(
            tool_calls=[tool_call(
                "submit_answer",
                {"claim": "success", "summary": "status checked", "evidence_ids": ["git_status:0"]},
                "c2",
            )]
        ),
    ]
    orch = _orch(sandbox.root, turns)
    result = orch.run("review my changes", skill_name="git-review")
    tool_messages = [m for m in result.messages if m.get("role") == "tool"]
    first = json.loads(tool_messages[0]["content"])
    assert first["data"]["evidence_id"] == "git_status:0"
    assert result.state.cited_unknown == []
    assert result.state.citation_schemes["chosen"] == "canonical"


def test_active_skill_can_read_advertised_reference(sandbox):
    turns = [
        ChatResponse(tool_calls=[tool_call(
            "read_skill_reference", {"name": "failure-taxonomy.md"}, "c1"
        )]),
        ChatResponse(tool_calls=[tool_call(
            "submit_answer", {"claim": "diagnosis", "summary": "reference loaded"}, "c2"
        )]),
    ]
    orch = _orch(sandbox.root, turns)
    result = orch.run("build and test it", skill_name="build-and-test")
    assert result.state.history[0].name == "read_skill_reference"
    assert result.state.history[0].execution == "ok"
    assert "read_skill_reference" in result.state.toolset


def test_low_confidence_automatic_route_abstains(sandbox):
    client = ScriptedClient([ChatResponse(content="done")])
    repo = load_repo_config(sandbox.root)
    registry, _, _ = build_registry(repo)
    skills = SkillLibrary.discover(REPO / "skills")
    orch = Orchestrator(repo, registry, client, skills)
    result = orch.run("ponder the architecture")
    assert result.routing.skill_confident is False
    assert result.state.active_skill is None
    assert result.state.narrowed_by is None
    exposed = {schema["function"]["name"] for schema in client.tool_schemas[0]}
    assert "build_target" in exposed and "git_diff" in exposed
    assert any("routing abstained" in warning for warning in result.state.warnings)
