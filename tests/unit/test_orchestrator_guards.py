"""The orchestrator's job is to stop the model doing daft things.

These tests all drive it with a ScriptedClient, so no model, no server and no
network are involved.
"""

from __future__ import annotations

import json
from pathlib import Path

from local_agent.agent import Orchestrator, SkillLibrary
from local_agent.agent.context import approximate_tokens, trim
from local_agent.config import load_repo_config
from local_agent.llm.client import ScriptedClient, tool_call
from local_agent.llm.models import ChatResponse
from local_agent.tools import build_registry

REPO = Path(__file__).resolve().parent.parent.parent


def _orch(root: Path, turns, approval=None):
    repo = load_repo_config(root)
    registry, _, _ = build_registry(repo)
    skills = SkillLibrary.discover(REPO / ".github" / "skills")
    client = ScriptedClient(turns)
    return (
        Orchestrator(repo, registry, client, skills, approval=approval),
        client,
    )


def test_identical_repeated_call_halts_the_loop(sandbox):
    turns = [
        ChatResponse(tool_calls=[tool_call("git_status", {}, f"c{i}")])
        for i in range(10)
    ]
    orch, _ = _orch(sandbox.root, turns)
    result = orch.run("look at the repository", skill_name="git-review")
    assert result.state.halt_reason is not None
    assert "identical arguments" in result.state.halt_reason
    assert result.state.tool_calls <= 4


def test_unknown_tool_halts_after_three_attempts(sandbox):
    turns = [
        ChatResponse(tool_calls=[tool_call("rm_rf_everything", {}, f"c{i}")])
        for i in range(6)
    ]
    orch, _ = _orch(sandbox.root, turns)
    result = orch.run("delete it all", skill_name="repo-navigation")
    assert result.state.halt_reason == "model repeatedly called tools it could not use"


def test_tool_budget_is_enforced(sandbox):
    turns = [
        ChatResponse(tool_calls=[tool_call("read_file", {"path": "src/ring_buffer.cpp",
                                                         "start_line": i, "end_line": i + 2},
                                           f"c{i}")])
        for i in range(60)
    ]
    orch, _ = _orch(sandbox.root, turns)
    result = orch.run("read the whole thing slowly", skill_name="repo-navigation")
    assert "budget exhausted" in (result.state.halt_reason or "")
    assert result.state.tool_calls == 30  # from the sandbox .local-agent.toml


def test_toolset_is_narrowed_to_the_active_skill(sandbox):
    orch, client = _orch(sandbox.root, [ChatResponse(content="nothing to do")])
    orch.run("review my changes", skill_name="git-review")
    exposed = {s["function"]["name"] for s in client.tool_schemas[0]}
    assert "git_diff" in exposed
    assert "build_target" not in exposed
    assert "apply_patch" not in exposed
    assert len(exposed) < 10


def test_a_prose_answer_is_asked_to_state_a_checkable_claim(sandbox):
    turns = [
        ChatResponse(content="All tests pass, it is fixed."),
        ChatResponse(
            tool_calls=[tool_call("submit_answer",
                                  {"claim": "needs_action",
                                   "summary": "I have not run the tests."}, "c1")]
        ),
    ]
    orch, client = _orch(sandbox.root, turns)
    result = orch.run("is the suite green", skill_name="build-and-test")
    assert "finished without calling submit_answer" in result.state.warnings[0]
    assert "have not run the tests" in result.answer
    assert result.state.verified is False
    # The nudge was appended as a user turn before the second call.
    assert client.calls[1][-1]["role"] == "user"


def test_denied_tool_returns_a_recoverable_result(sandbox):
    turns = [
        ChatResponse(tool_calls=[tool_call("git_commit", {"message": "do the thing"})]),
        ChatResponse(content="Commits are disabled, so here is the message instead."),
    ]
    orch, _ = _orch(sandbox.root, turns)
    result = orch.run("commit my work", skill_name="prepare-commit")
    assert result.state.halt_reason is None
    assert result.state.history[0].verdict == "denied"
    assert "Commits are disabled" in result.answer


def test_approval_refusal_does_not_kill_the_run(sandbox, monkeypatch):
    sandbox.scenario("test_failure")
    turns = [
        ChatResponse(
            tool_calls=[
                tool_call(
                    "propose_patch",
                    {
                        "path": "src/ring_buffer.cpp",
                        "find": "count_ + 1 == slots_.size()",
                        "replace": "count_ == slots_.size()",
                    },
                )
            ]
        ),
        ChatResponse(tool_calls=[tool_call("apply_patch", {"patch_id": "PLACEHOLDER"})]),
        ChatResponse(content="Patch proposed but not applied."),
    ]
    orch, _ = _orch(sandbox.root, turns, approval=lambda *a: False)
    result = orch.run("fix the ring buffer", skill_name="fix-test-failure")
    assert result.state.halt_reason is None
    assert any(h.verdict == "not-approved" for h in result.state.history)
    # Nothing was written.
    assert "count_ + 1 == slots_.size()" in (
        sandbox.root / "src" / "ring_buffer.cpp"
    ).read_text()


def test_prompt_is_append_only_across_turns(sandbox):
    """Regression: rewriting history mid-run discards the server's KV prefix.

    Each request must start with exactly the messages of the previous request,
    otherwise the prefix cache misses and every turn pays a full re-prefill.
    """
    turns = [
        ChatResponse(tool_calls=[tool_call("git_status", {}, "c1")]),
        ChatResponse(tool_calls=[tool_call("git_log", {"limit": 3}, "c2")]),
        ChatResponse(tool_calls=[tool_call("git_diff", {"stat_only": True}, "c3")]),
        ChatResponse(content="Nothing is modified in the working tree."),
    ]
    orch, client = _orch(sandbox.root, turns)
    result = orch.run("what has changed", skill_name="git-review")

    assert result.state.halt_reason is None
    assert len(client.calls) == 4
    for earlier, later in zip(client.calls, client.calls[1:]):
        assert later[: len(earlier)] == earlier, "history was rewritten between turns"
    assert result.state.metrics.compactions == 0


def test_compaction_is_recorded_and_warned_about(sandbox):
    """When the budget really is exceeded, the cost is made visible."""
    big = "y" * 9000

    def fat_tool_turn(index):
        return ChatResponse(
            tool_calls=[tool_call("read_file", {"path": "src/ring_buffer.cpp",
                                                "start_line": index,
                                                "end_line": index + 40}, f"c{index}")]
        )

    turns = [fat_tool_turn(i) for i in range(1, 8)] + [ChatResponse(content="done")]
    repo = load_repo_config(sandbox.root)
    registry, _, _ = build_registry(repo)
    skills = SkillLibrary.discover(REPO / ".github" / "skills")
    orch = Orchestrator(
        repo, registry, ScriptedClient(turns), skills, context_budget_tokens=900
    )
    result = orch.run("read the file in pieces", skill_name="repo-navigation")

    assert result.state.metrics.compactions >= 1
    assert any("prompt cache" in w for w in result.state.warnings)
    assert result.state.metrics.context_peak_tokens > 0
    assert big not in json.dumps(result.messages)  # nothing invented


def test_metrics_are_attributed_to_model_tools_and_overhead(sandbox):
    from local_agent.llm.models import CallStats

    turns = [
        ChatResponse(
            tool_calls=[tool_call("build_target", {}, "c1")],
            stats=CallStats(total_s=2.0, ttft_s=1.5, prompt_tokens=1800,
                            completion_tokens=21, cached_tokens=0, streamed=True),
        ),
        ChatResponse(
            tool_calls=[tool_call("submit_answer",
                                  {"claim": "success", "summary": "The build succeeded.",
                                   "evidence_ids": ["build_target:0"]}, "c2")],
            stats=CallStats(total_s=1.0, ttft_s=0.4, prompt_tokens=2400,
                            completion_tokens=11, cached_tokens=1800, streamed=True),
        ),
    ]
    orch, _ = _orch(sandbox.root, turns)
    result = orch.run("build it", skill_name="build-and-test")

    m = result.state.metrics
    assert m.llm_calls == 2
    assert m.llm_seconds == 3.0
    assert m.tool_seconds > 0, "a real cmake build should have been timed"
    assert m.wall_seconds >= m.tool_seconds
    assert m.cache_hit_ratio == round(1800 / 4200, 3)

    out = m.as_dict()
    assert out["median_ttft_s"] == 0.95
    assert out["median_prefill_tok_s"] is not None
    assert "wall" in "\n".join(result.state.summary_lines())


# ------------------------------------------------------------------ context


def test_trim_collapses_old_tool_payloads():
    messages = [{"role": "system", "content": "sys"}]
    for i in range(10):
        messages.append({"role": "assistant", "content": None})
        messages.append(
            {
                "role": "tool",
                "tool_call_id": f"c{i}",
                "name": "build_target",
                "content": '{"ok": false, "summary": "build FAILED", '
                           '"artifacts": ["log"], "data": {"blob": "'
                           + "x" * 4000
                           + '"}}',
            }
        )
    before = approximate_tokens(messages)
    after = trim(messages, budget_tokens=3000)
    assert approximate_tokens(after) < before
    assert '"collapsed": true' in after[2]["content"]
    # The most recent results survive intact.
    assert "x" * 100 in after[-1]["content"]
