"""PASS / ESCALATED_PASS / ESCALATED_FAIL / BLOCKED / FAIL.

The category that matters most is BLOCKED. If cmake is missing or policy
forbids running tests, the cheap tier did not fail at engineering, and
escalating just walks the strong tier into the same wall and then blames it for
the result.
"""

from __future__ import annotations

from pathlib import Path

from local_agent.agent import Orchestrator, SkillLibrary, format_report
from local_agent.agent.outcome import Outcome, classify
from local_agent.tools.base import DomainStatus, ExecutionStatus, Reason, ToolResult
from local_agent.config import load_repo_config
from local_agent.llm.client import ScriptedClient, tool_call
from local_agent.llm.models import CallStats, ChatResponse
from local_agent.llm.router import CHEAP, STRONG, TieredClient
from local_agent.tools import build_registry

REPO = Path(__file__).resolve().parent.parent.parent


def _stats() -> CallStats:
    return CallStats(total_s=1.0, ttft_s=0.5, prompt_tokens=800,
                     completion_tokens=16, streamed=True)


def _tiered(cheap_turns, strong_turns) -> TieredClient:
    return TieredClient(
        {CHEAP: ScriptedClient(cheap_turns), STRONG: ScriptedClient(strong_turns)},
        default_tier=STRONG,
    )


def _orch(root: Path, client, **kwargs):
    repo = load_repo_config(root)
    registry, _, _ = build_registry(repo)
    skills = SkillLibrary.discover(REPO / ".github" / "skills")
    return Orchestrator(repo, registry, client, skills, **kwargs)


def _no_build_repo(root: Path) -> None:
    """Turn off building and testing, the way a locked-down environment would."""
    config = root / ".local-agent.toml"
    text = config.read_text()
    text = text.replace("allow_build = true", "allow_build = false")
    text = text.replace("allow_test = true", "allow_test = false")
    config.write_text(text)


# ------------------------------------------------------------ classification


def test_classify_covers_every_path():
    assert classify(succeeded=True, escalated=False, escalation_available=True,
                    blocked_reason=None) is Outcome.PASS
    assert classify(succeeded=True, escalated=True, escalation_available=True,
                    blocked_reason=None) is Outcome.ESCALATED_PASS
    assert classify(succeeded=False, escalated=True, escalation_available=True,
                    blocked_reason=None) is Outcome.ESCALATED_FAIL
    assert classify(succeeded=False, escalated=False, escalation_available=False,
                    blocked_reason=None) is Outcome.FAIL
    assert classify(succeeded=False, escalated=False, escalation_available=True,
                    blocked_reason="cmake missing") is Outcome.BLOCKED


def test_a_block_never_counts_as_a_model_success_or_failure():
    assert Outcome.BLOCKED.succeeded is False
    assert Outcome.BLOCKED.completed_locally is False
    assert Outcome.BLOCKED.needed_the_strong_tier is False
    assert Outcome.ESCALATED_PASS.completed_locally is True
    assert Outcome.PASS.needed_the_strong_tier is False


def test_the_two_axes_are_independent():
    """Did the tool run, and did the thing under test pass, are separate facts."""
    compile_error = ToolResult(ok=False, summary="build FAILED with 2 compiler error(s)")
    assert compile_error.execution_status is ExecutionStatus.OK
    assert compile_error.domain_status is DomainStatus.FAIL
    assert compile_error.ran is True and compile_error.blocked is False

    missing = ToolResult.blocked_by(Reason.MISSING_EXECUTABLE, "'cmake' is not on PATH")
    assert missing.domain_status is DomainStatus.UNKNOWN
    assert missing.ran is False and missing.blocked is True

    bad_args = ToolResult.errored(Reason.BAD_ARGUMENTS, "invalid arguments")
    assert bad_args.blocked is False and bad_args.ran is False
    assert bad_args.domain_status is DomainStatus.UNKNOWN


def test_our_timeout_and_the_tools_timeout_are_different_facts():
    """ctest saying a test timed out is evidence. Our runner giving up is not."""
    ctest_timeout = ToolResult(
        ok=False, summary="1 test(s) FAILED: slow",
        data={"ctest_reported_timeouts": ["slow"]},
    )
    assert ctest_timeout.ran is True
    assert ctest_timeout.domain_status is DomainStatus.FAIL

    ours = ToolResult(
        ok=False, summary="the orchestrator killed the test run",
        execution_status=ExecutionStatus.ERROR,
        domain_status=DomainStatus.UNKNOWN,
        reason=Reason.ORCHESTRATOR_TIMEOUT,
    )
    assert ours.ran is False
    assert ours.reason is Reason.ORCHESTRATOR_TIMEOUT


# ------------------------------------------------------------ orchestration


def test_policy_lockout_blocks_rather_than_escalates(sandbox):
    _no_build_repo(sandbox.root)
    cheap = [
        ChatResponse(tool_calls=[tool_call("build_target", {}, "c1")], stats=_stats()),
        ChatResponse(content="I could not build, so I cannot say.", stats=_stats()),
    ]
    strong = [ChatResponse(content="the strong tier must not be used here")]
    client = _tiered(cheap, strong)
    result = _orch(sandbox.root, client).run("build it", skill_name="build-and-test")

    assert result.outcome is Outcome.BLOCKED
    assert result.routing.escalated is False
    assert client.stats[STRONG].calls == 0, "a blocked run must not burn the strong tier"
    assert any("blocked, not failed" in w for w in result.state.warnings)
    assert "outcome: blocked" in format_report(result)


def test_a_real_compiler_failure_is_not_blocked(sandbox):
    """A build that fails to compile is a result. The model can work with it."""
    sandbox.scenario("compile_error")
    cheap = [
        ChatResponse(tool_calls=[tool_call("build_target", {}, "c1")], stats=_stats()),
        ChatResponse(
            tool_calls=[tool_call("submit_answer",
                                  {"claim": "diagnosis",
                                   "summary": "ring_buffer.cpp line 13 uses count, "
                                              "not count_.",
                                   "evidence_ids": ["build_target:0"]}, "c2")],
            stats=_stats(),
        ),
    ]
    client = _tiered(cheap, [ChatResponse(content="unused")])
    result = _orch(sandbox.root, client).run(
        "why is the build broken", skill_name="diagnose-build-failure"
    )

    assert result.outcome is Outcome.PASS
    # The build ran and returned a real diagnostic, so the skill's verification
    # requirement is satisfied even though the build did not succeed.
    assert result.state.verification_attempted is True
    assert result.state.verified is False
    assert "count" in result.answer


def test_declined_approval_blocks_a_verification_skill(sandbox):
    sandbox.scenario("test_failure")
    cheap = [
        ChatResponse(
            tool_calls=[tool_call("propose_patch", {
                "path": "src/ring_buffer.cpp",
                "find": "count_ + 1 == slots_.size()",
                "replace": "count_ == slots_.size()",
            }, "c1")],
            stats=_stats(),
        ),
        ChatResponse(tool_calls=[tool_call("apply_patch", {"patch_id": "x"}, "c2")],
                     stats=_stats()),
        ChatResponse(content="I proposed a fix but was not allowed to apply it.",
                     stats=_stats()),
    ]
    client = _tiered(cheap, [ChatResponse(content="unused")])
    result = _orch(sandbox.root, client, approval=lambda *a: False).run(
        "fix the ring buffer", skill_name="fix-test-failure"
    )

    assert result.outcome is Outcome.BLOCKED
    assert client.stats[STRONG].calls == 0


def test_verification_required_forces_escalation_when_nothing_ran(sandbox):
    """Answering a build question without building is a model failure, not a block."""
    cheap = [ChatResponse(content="It probably builds fine.", stats=_stats())]
    strong = [
        ChatResponse(tool_calls=[tool_call("build_target", {}, "s1")], stats=_stats()),
        ChatResponse(content="The build succeeded, exit code 0.", stats=_stats()),
    ]
    client = _tiered(cheap, strong)
    result = _orch(sandbox.root, client).run("does it build",
                                             skill_name="build-and-test")

    assert result.routing.escalated is True
    assert "requires a tool-backed result" in result.routing.escalation_reason
    assert result.outcome is Outcome.ESCALATED_PASS
    assert result.state.verified is True


def test_escalated_fail_when_neither_tier_gets_there(sandbox):
    looping = [
        ChatResponse(tool_calls=[tool_call("git_status", {}, f"c{i}")], stats=_stats())
        for i in range(8)
    ]
    client = _tiered(list(looping), list(looping))
    result = _orch(sandbox.root, client).run("what changed", skill_name="git-review")

    assert result.routing.escalated is True
    assert result.outcome is Outcome.ESCALATED_FAIL


def test_single_tier_failure_is_fail_not_escalated_fail(sandbox):
    client = ScriptedClient([
        ChatResponse(tool_calls=[tool_call("git_status", {}, f"c{i}")], stats=_stats())
        for i in range(8)
    ])
    result = _orch(sandbox.root, client).run("what changed", skill_name="git-review")
    assert result.outcome is Outcome.FAIL


def test_skill_may_refuse_escalation(sandbox):
    client = _tiered(
        [ChatResponse(content="   ", stats=_stats())],
        [ChatResponse(content="unused")],
    )
    orch = _orch(sandbox.root, client)
    orch.skills.get("repo-navigation").escalation = "never"
    result = orch.run("where is RingBuffer", skill_name="repo-navigation")

    assert result.routing.escalated is False
    assert client.stats[STRONG].calls == 0
    assert result.outcome is Outcome.FAIL
