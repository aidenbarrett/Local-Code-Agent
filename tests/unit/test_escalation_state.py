"""Escalation must hand the strong tier a clean tree and real evidence.

The nastiest failure this defends against: the cheap tier applies a bad patch,
fails, and the strong tier then diagnoses a defect the agent itself introduced.
That looks like evidence and is not.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from local_agent.agent import Orchestrator, SkillLibrary
from local_agent.agent.journal import (
    MutationJournal,
    RepoFingerprint,
    build_evidence_packet,
)
from local_agent.agent.state import ToolCallRecord
from local_agent.config import load_repo_config
from local_agent.llm.client import ScriptedClient, tool_call
from local_agent.llm.models import CallStats, ChatResponse
from local_agent.llm.router import CHEAP, STRONG, TieredClient
from local_agent.tools import build_registry

REPO = Path(__file__).resolve().parent.parent.parent


def _stats() -> CallStats:
    return CallStats(total_s=1.0, ttft_s=0.4, prompt_tokens=700,
                     completion_tokens=12, streamed=True)


def _orch(root: Path, cheap_turns, strong_turns, **kwargs):
    journal = MutationJournal()
    repo = load_repo_config(root)
    registry, _, _ = build_registry(repo, journal=journal)
    skills = SkillLibrary.discover(REPO / ".github" / "skills")
    client = TieredClient(
        {CHEAP: ScriptedClient(cheap_turns), STRONG: ScriptedClient(strong_turns)},
        default_tier=STRONG,
    )
    orch = Orchestrator(repo, registry, client, skills, journal=journal,
                        approval=lambda *a: True, **kwargs)
    return orch, client, journal


# ---------------------------------------------------------- fingerprinting


def test_fingerprint_notices_every_kind_of_change(sandbox):
    root = sandbox.root
    start = RepoFingerprint.capture(root)
    assert start.differs_from(start) == []

    (root / "src" / "ring_buffer.cpp").write_text("// wrecked\n")
    assert "tracked files differ" in RepoFingerprint.capture(root).differs_from(start)

    (root / "src" / "ring_buffer.cpp").write_text(
        (root / "scenarios" / "clean" / "src" / "ring_buffer.cpp").read_text()
    )
    (root / "stray.txt").write_text("new file")
    drift = RepoFingerprint.capture(root).differs_from(start)
    assert any("untracked" in d for d in drift)


def test_fingerprint_notices_a_commit(sandbox):
    root = sandbox.root
    start = RepoFingerprint.capture(root)
    (root / "note.txt").write_text("x")
    subprocess.run(["git", "add", "-A"], cwd=root, check=True)
    subprocess.run(
        ["git", "-c", "user.email=a@b.c", "-c", "user.name=t", "commit", "-qm", "x"],
        cwd=root, check=True,
    )
    assert any("HEAD moved" in d for d in RepoFingerprint.capture(root).differs_from(start))


# --------------------------------------------------------------- journal


def test_journal_reverts_only_its_own_writes(tmp_path):
    target = tmp_path / "a.txt"
    target.write_text("original")
    journal = MutationJournal()
    target.write_text("agent wrote this")
    journal.record_write(target, "original", "agent wrote this", "apply_patch")

    outcome = journal.revert()
    assert outcome["reverted"] == [str(target)]
    assert target.read_text() == "original"


def test_journal_refuses_to_clobber_someone_elses_edit(tmp_path):
    target = tmp_path / "a.txt"
    target.write_text("original")
    journal = MutationJournal()
    target.write_text("agent wrote this")
    journal.record_write(target, "original", "agent wrote this", "apply_patch")

    # A human edits the file after the agent did.
    target.write_text("a person edited this afterwards")

    outcome = journal.revert()
    assert outcome["reverted"] == []
    assert "changed by someone else" in outcome["skipped"][0]
    assert target.read_text() == "a person edited this afterwards"


def test_journal_reverts_in_reverse_order(tmp_path):
    target = tmp_path / "a.txt"
    target.write_text("v0")
    journal = MutationJournal()
    target.write_text("v1")
    journal.record_write(target, "v0", "v1", "apply_patch")
    target.write_text("v2")
    journal.record_write(target, "v1", "v2", "apply_patch")

    journal.revert()
    assert target.read_text() == "v0"


def test_irreversible_operations_are_reported_not_hidden(tmp_path):
    journal = MutationJournal()
    journal.record_irreversible("commit created: fix the thing")
    outcome = journal.revert()
    assert outcome["irreversible"] == ["commit created: fix the thing"]


# ------------------------------------------------------------- integration


def test_a_bad_patch_is_reverted_before_the_strong_tier_sees_it(sandbox):
    """The whole point. The strong tier must not inherit a broken worktree."""
    sandbox.scenario("test_failure")
    source = sandbox.root / "src" / "ring_buffer.cpp"
    before = source.read_text()

    cheap = [
        ChatResponse(
            tool_calls=[tool_call("propose_patch", {
                "path": "src/ring_buffer.cpp",
                "find": "std::optional<int> RingBuffer::pop() {",
                "replace": "std::optional<int> RingBuffer::pop() {\n    // WRONG",
            }, "c1")],
            stats=_stats(),
        ),
        # Apply it, then loop until the repeat guard halts the run.
        ChatResponse(tool_calls=[tool_call("apply_patch", {"patch_id": "PLACEHOLDER"}, "c2")],
                     stats=_stats()),
    ] + [
        ChatResponse(tool_calls=[tool_call("run_test", {}, f"c{i}")], stats=_stats())
        for i in range(6)
    ]
    strong = [ChatResponse(content="The buffer reports full one slot early.",
                           stats=_stats())]

    def resolve_patch_id(messages):
        payload = json.loads(messages[-1]["content"])
        return ChatResponse(
            tool_calls=[tool_call("apply_patch",
                                  {"patch_id": payload["data"]["patch_id"]}, "c2")],
            stats=_stats(),
        )

    cheap[1] = resolve_patch_id
    orch, client, journal = _orch(sandbox.root, cheap, strong)
    result = orch.run("fix why ring_buffer fails", skill_name="fix-test-failure")

    assert result.routing.escalated is True
    assert source.read_text() == before, "the cheap tier's edit survived into the retry"
    assert any("reverted this agent's own edits" in w for w in result.state.warnings)


def test_evidence_is_replayed_without_the_cheap_tiers_reasoning(sandbox):
    sandbox.scenario("compile_error")
    cheap = [
        ChatResponse(tool_calls=[tool_call("build_target", {}, "c1")], stats=_stats()),
        # Then stall out so it escalates.
        ChatResponse(content="   ", stats=_stats()),
        ChatResponse(content="   ", stats=_stats()),
    ]
    strong = [
        ChatResponse(
            tool_calls=[tool_call("submit_answer",
                                  {"claim": "diagnosis",
                                   "summary": "ring_buffer.cpp:13 uses count.",
                                   "evidence_ids": []}, "s1")],
            stats=_stats(),
        )
    ]
    orch, client, _ = _orch(sandbox.root, cheap, strong)
    result = orch.run("why is the build broken", skill_name="diagnose-build-failure")

    assert result.routing.escalated is True
    sent = client.clients[STRONG].calls[0]
    evidence = [m for m in sent if "Evidence from a previous attempt" in str(m.get("content"))]
    assert evidence, "the strong tier was given no evidence packet"

    packet = evidence[0]["content"]
    assert "build_target" in packet
    assert "ring_buffer.cpp" in packet          # the real diagnostic survived
    assert "repo_state_at_start" in packet
    # And none of the cheap model's own words came across.
    assert "I think" not in packet


def test_evidence_packet_excludes_tools_that_never_ran():
    fingerprint = RepoFingerprint(
        head="abc", dirty_diff_sha="d", untracked_sha="u", untracked_count=0,
        config_sha="c",
    )
    history = [
        ToolCallRecord("build_target", {}, "auto", False, "build FAILED",
                       execution="ok", domain="fail",
                       evidence={"error_count": 2, "command": ["cmake", "--build"]}),
        ToolCallRecord("run_test", {}, "denied", False, "policy said no",
                       blocked=True, execution="blocked", reason="policy_denied"),
    ]
    packet = build_evidence_packet(history, fingerprint)

    assert len(packet["observations"]) == 1
    assert packet["observations"][0]["tool"] == "build_target"
    assert packet["observations"][0]["data"]["error_count"] == 2
    assert "no reasoning or conclusions" in packet["note"]


def test_no_journal_means_no_crash(sandbox):
    """Journalling is optional; the orchestrator must work without it."""
    repo = load_repo_config(sandbox.root)
    registry, _, _ = build_registry(repo)
    skills = SkillLibrary.discover(REPO / ".github" / "skills")
    client = TieredClient(
        {CHEAP: ScriptedClient([ChatResponse(content="  ", stats=_stats())]),
         STRONG: ScriptedClient([ChatResponse(content="Fine.", stats=_stats())])},
        default_tier=STRONG,
    )
    result = Orchestrator(repo, registry, client, skills).run(
        "where is RingBuffer", skill_name="repo-navigation"
    )
    assert result.routing.escalated is True
    assert result.answer == "Fine."


# ------------------------------------------------- commits outside the loop


def test_a_speculative_attempt_cannot_stage_or_commit(sandbox):
    """A commit inside a retryable attempt is a mutation nothing can undo."""
    config = sandbox.root / ".local-agent.toml"
    config.write_text(config.read_text().replace("allow_commit = false",
                                                 "allow_commit = true"))
    cheap = [
        ChatResponse(tool_calls=[tool_call("git_stage",
                                           {"paths": ["src/ring_buffer.cpp"]}, "c1")],
                     stats=_stats()),
        ChatResponse(content="I was not allowed to stage anything.", stats=_stats()),
    ]
    strong = [ChatResponse(content="unused", stats=_stats())]
    orch, client, _ = _orch(sandbox.root, cheap, strong)
    result = orch.run("review my changes", skill_name="git-review")

    staged = result.state.history[0]
    # Two independent walls, and the outer one fires first now: git-review does
    # not offer git_stage, so the call is TOOL_NOT_ALLOWED before policy is asked.
    # The policy wall is still there for any toolset that does offer it, and is
    # pinned directly below.
    assert staged.reason == "tool_not_allowed"
    assert staged.execution == "error"
    assert result.state.mutation_epoch == 0

    from local_agent.agent.policy import PolicyEngine, Verdict
    from local_agent.tools import build_registry
    from local_agent.config import load_repo_config
    repo = load_repo_config(sandbox.root)
    registry, _, _ = build_registry(repo)
    decision = PolicyEngine(repo.policy).check(
        registry.get("git_stage"), {"paths": ["src/ring_buffer.cpp"]}, speculative=True
    )
    assert decision.verdict is Verdict.DENY
    assert "can still be escalated" in decision.reason


def test_the_finalise_skill_may_commit_because_it_never_escalates(sandbox):
    config = sandbox.root / ".local-agent.toml"
    config.write_text(config.read_text().replace("allow_commit = false",
                                                 "allow_commit = true"))
    (sandbox.root / "src" / "ring_buffer.cpp").write_text("// changed\n")

    cheap = [
        ChatResponse(tool_calls=[tool_call("git_stage",
                                           {"paths": ["src/ring_buffer.cpp"]}, "c1")],
                     stats=_stats()),
        ChatResponse(content="Staged one file.", stats=_stats()),
    ]
    orch, client, _ = _orch(sandbox.root, cheap, [ChatResponse(content="unused")])
    result = orch.run("get this ready to commit", skill_name="prepare-commit")

    assert result.state.history[0].verdict == "approved"
    assert result.state.history[0].ok is True
    assert result.routing.escalated is False


def test_the_escalated_attempt_is_terminal_and_may_commit(sandbox):
    """After escalation there is nowhere left to retry, so the gate lifts."""
    from local_agent.agent.policy import PolicyEngine
    from local_agent.config import Policy
    from local_agent.tools.base import Risk, Tool

    engine = PolicyEngine(Policy(allow_commit=True))
    commit = Tool("git_commit", "", {}, lambda: None, Risk.DANGEROUS)
    assert engine.check(commit, {}, speculative=True).verdict.value == "deny"
    assert engine.check(commit, {}, speculative=False).requires_approval is True
