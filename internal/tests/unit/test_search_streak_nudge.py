"""A worker that only searches is told, once per streak, to read and act.

Found on the Panther Lake run (J08, Qwen3-8B): after the build and the log, the
model made 13 search_text calls in a row and never opened the file the compiler
named. The orchestrator now says so after every fourth consecutive search, after
all tool results of that turn, and says nothing while the model reads or edits.
"""
from __future__ import annotations

from pathlib import Path

from local_agent.agent import Orchestrator, SkillLibrary
from local_agent.agent.orchestrator import SEARCH_STREAK_NUDGE
from local_agent.config import load_repo_config
from local_agent.llm.client import ScriptedClient, tool_call
from local_agent.llm.protocol import ChatResponse
from local_agent.tools import build_registry

REPO = Path(__file__).resolve().parent.parent.parent
NUDGE = "searches in a row without reading any code"


def _orch(root: Path, client):
    repo = load_repo_config(root)
    registry, _, _ = build_registry(repo)
    return Orchestrator(repo, registry, client, SkillLibrary.discover(REPO / "skills"))


def _search(i: int) -> ChatResponse:
    return ChatResponse(tool_calls=[tool_call("search_text", {"pattern": f"count{i}"}, f"s{i}")])


def _nudges(messages) -> int:
    return sum(1 for m in messages if m.get("role") == "user" and NUDGE in str(m.get("content")))


def test_a_search_streak_is_nudged_once_per_four_and_never_between_tool_results(sandbox):
    sandbox.scenario("compile_error")
    seen: list[list[dict]] = []

    def record_then(response):
        def turn(messages):
            seen.append(list(messages))
            return response
        return turn

    turns = [record_then(_search(i)) for i in range(2 * SEARCH_STREAK_NUDGE)]
    turns.append(record_then(ChatResponse(tool_calls=[tool_call("submit_answer", {
        "claim": "diagnosis", "summary": "searched", "evidence_ids": []}, "done")])))
    _orch(sandbox.root, ScriptedClient(turns)).run("find the counter", skill_name="fix-build-failure")

    counts = [_nudges(m) for m in seen]
    # No nudge before the fourth search, one after it, a second after the eighth.
    assert counts[:SEARCH_STREAK_NUDGE] == [0] * SEARCH_STREAK_NUDGE
    assert counts[SEARCH_STREAK_NUDGE] == 1
    assert counts[-1] == 2
    # Every tool result directly follows the assistant turn that called it.
    final = seen[-1]
    for i, m in enumerate(final):
        if m.get("role") == "tool":
            assert final[i - 1].get("role") in ("assistant", "tool")


def test_reading_breaks_the_streak(sandbox):
    sandbox.scenario("compile_error")
    seen: list[list[dict]] = []

    def record_then(response):
        def turn(messages):
            seen.append(list(messages))
            return response
        return turn

    read = ChatResponse(tool_calls=[tool_call("read_file", {"path": "src/ring_buffer.cpp"}, "r")])
    turns = [record_then(r) for r in (_search(0), _search(1), _search(2), read, _search(3),
                                      _search(4), _search(5))]
    turns.append(record_then(ChatResponse(tool_calls=[tool_call("submit_answer", {
        "claim": "diagnosis", "summary": "read", "evidence_ids": []}, "done")])))
    _orch(sandbox.root, ScriptedClient(turns)).run("find the counter", skill_name="fix-build-failure")
    assert _nudges(seen[-1]) == 0
