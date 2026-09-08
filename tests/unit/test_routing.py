"""Tiered routing: cheapest engine that can do the job, escalate when it cannot.

The number this whole exercise exists to produce is "what share of real work can
the low-power engine carry", so the accounting behind it gets tested properly.
"""

from __future__ import annotations

from pathlib import Path

from local_agent.agent import Orchestrator, SkillLibrary, format_report
from local_agent.config import load_repo_config
from local_agent.llm.client import ScriptedClient, tool_call
from local_agent.llm.models import CallStats, ChatResponse
from local_agent.llm.router import (
    CHEAP,
    STRONG,
    TieredClient,
    tier_for_skill,
)
from local_agent.tools import build_registry

REPO = Path(__file__).resolve().parent.parent.parent


def _stats(seconds: float = 1.0) -> CallStats:
    return CallStats(total_s=seconds, ttft_s=seconds / 2, prompt_tokens=1000,
                     completion_tokens=20, streamed=True)


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


# ------------------------------------------------------------- tier choice


def test_every_skill_starts_cheap_and_earns_the_strong_tier():
    """Nothing is hard-wired to the big model. Escalation is how it gets used.

    Hard-wiring `diagnose-build-failure` to the strong tier would assume the
    answer to the question the POC exists to ask.
    """
    skills = SkillLibrary.discover(REPO / ".github" / "skills")
    for name in skills.names():
        skill = skills.get(name)
        assert skill.tier == CHEAP, f"{name} is hard-wired to {skill.tier}"

    # One deliberate exception. prepare-commit is the finalise phase: staging
    # and committing are refused inside anything that could still be retried,
    # so the skill that does them has to be terminal by construction.
    non_escalating = {n for n in skills.names() if skills.get(n).escalation == "never"}
    assert non_escalating == {"prepare-commit"}


def test_skills_that_need_evidence_say_so():
    skills = SkillLibrary.discover(REPO / ".github" / "skills")
    needs = {n for n in skills.names() if skills.get(n).verification_required}
    assert needs == {
        "build-and-test", "diagnose-build-failure", "diagnose-test-failure",
        "fix-build-failure", "fix-test-failure",
    }


def test_declared_tier_beats_the_default_table():
    assert tier_for_skill("diagnose-build-failure", declared=CHEAP) == CHEAP
    assert tier_for_skill("repo-navigation", declared=None) == CHEAP
    assert tier_for_skill("something-new", declared=None) == STRONG
    assert tier_for_skill("something-new", None, {"something-new": CHEAP}) == CHEAP
    assert tier_for_skill("repo-navigation", declared="nonsense") == CHEAP


# ---------------------------------------------------------------- routing


def test_cheap_skill_is_served_by_the_cheap_endpoint(sandbox):
    cheap = [ChatResponse(content="Nothing is modified.", stats=_stats())]
    strong = [ChatResponse(content="should not be reached")]
    client = _tiered(cheap, strong)
    result = _orch(sandbox.root, client).run("review my changes", skill_name="git-review")

    assert result.routing.first_tier == CHEAP
    assert result.routing.escalated is False
    assert client.stats[CHEAP].calls == 1
    assert client.stats[STRONG].calls == 0
    assert "Nothing is modified" in result.answer


def test_a_skill_can_still_be_pinned_to_the_strong_tier(sandbox):
    """The override exists; it is just not the default any more."""
    sandbox.scenario("compile_error")
    cheap = [ChatResponse(content="should not be reached")]
    strong = [ChatResponse(content="ring_buffer.cpp line 13.", stats=_stats())]
    client = _tiered(cheap, strong)
    orch = _orch(sandbox.root, client,
                 skill_tiers={"diagnose-build-failure": STRONG})
    # A skill's own frontmatter wins over the override map, so clear it first.
    orch.skills.get("diagnose-build-failure").tier = None
    result = orch.run("why is the build broken", skill_name="diagnose-build-failure")

    assert result.routing.first_tier == STRONG
    assert client.stats[CHEAP].calls == 0


def test_escalation_when_the_cheap_tier_halts(sandbox):
    # The cheap model loops on one call until the repeat guard stops it.
    cheap = [
        ChatResponse(tool_calls=[tool_call("git_status", {}, f"c{i}")], stats=_stats())
        for i in range(8)
    ]
    strong = [ChatResponse(content="The working tree is clean.", stats=_stats())]
    client = _tiered(cheap, strong)
    result = _orch(sandbox.root, client).run("what changed", skill_name="git-review")

    assert result.routing.escalated is True
    assert result.routing.first_tier == CHEAP
    assert result.routing.final_tier == STRONG
    assert "identical arguments" in result.routing.escalation_reason
    assert "The working tree is clean." in result.answer
    assert client.stats[STRONG].calls == 1
    assert any("escalated" in w for w in result.state.warnings)


def test_escalation_when_the_cheap_tier_says_nothing(sandbox):
    client = _tiered(
        [ChatResponse(content="   ", stats=_stats())],
        [ChatResponse(content="Here is the actual answer.", stats=_stats())],
    )
    result = _orch(sandbox.root, client).run("where is RingBuffer",
                                             skill_name="repo-navigation")
    assert result.routing.escalated is True
    assert "no answer was produced" in result.routing.escalation_reason


def test_escalation_when_the_cheap_tier_claims_unverified_success(sandbox):
    """The claim is structured, so no English is inspected to catch this."""
    cheap = [
        ChatResponse(
            tool_calls=[tool_call("submit_answer",
                                  {"claim": "success", "summary": "All good.",
                                   "evidence_ids": []}, "c1")],
            stats=_stats(),
        ),
    ]
    strong = [ChatResponse(
        tool_calls=[tool_call("submit_answer",
                              {"claim": "needs_action",
                               "summary": "I ran nothing, so I cannot say."}, "s1")],
        stats=_stats(),
    )]
    client = _tiered(cheap, strong)
    result = _orch(sandbox.root, client).run("is it green", skill_name="build-and-test")

    assert result.routing.escalated is True
    assert "without a passing build or test" in result.routing.escalation_reason


def test_no_escalation_when_the_cheap_tier_did_the_job(sandbox):
    client = _tiered(
        [ChatResponse(content="RingBuffer lives in ring_buffer.hpp.", stats=_stats())],
        [ChatResponse(content="should not be reached")],
    )
    result = _orch(sandbox.root, client).run("where is RingBuffer",
                                             skill_name="repo-navigation")
    assert result.routing.escalated is False
    assert client.stats[STRONG].calls == 0


def test_escalation_can_be_switched_off(sandbox):
    cheap = [
        ChatResponse(tool_calls=[tool_call("git_status", {}, f"c{i}")], stats=_stats())
        for i in range(8)
    ]
    client = _tiered(cheap, [ChatResponse(content="unused")])
    result = _orch(sandbox.root, client, allow_escalation=False).run(
        "what changed", skill_name="git-review"
    )
    assert result.routing.escalated is False
    assert result.state.halt_reason is not None
    assert client.stats[STRONG].calls == 0


# -------------------------------------------------------------- accounting


def test_cheap_share_is_reported(sandbox):
    client = _tiered(
        [ChatResponse(content="Clean tree.", stats=_stats(2.0))],
        [ChatResponse(content="unused")],
    )
    result = _orch(sandbox.root, client).run("review changes", skill_name="git-review")

    tiers = result.state.metrics.tier_stats
    assert tiers["cheap_call_share"] == 1.0
    assert tiers["by_tier"][CHEAP]["calls"] == 1
    assert tiers["by_tier"][CHEAP]["seconds"] == 2.0
    assert tiers["by_tier"][STRONG]["calls"] == 0
    assert "cheap tier served 100% of model calls" in format_report(result)


def test_escalated_run_shows_both_tiers_in_the_accounting(sandbox):
    cheap = [
        ChatResponse(tool_calls=[tool_call("git_status", {}, f"c{i}")], stats=_stats())
        for i in range(8)
    ]
    strong = [ChatResponse(content="Clean.", stats=_stats())]
    client = _tiered(cheap, strong)
    result = _orch(sandbox.root, client).run("what changed", skill_name="git-review")

    tiers = result.state.metrics.tier_stats
    assert tiers["by_tier"][CHEAP]["calls"] >= 1
    assert tiers["by_tier"][STRONG]["calls"] == 1
    assert 0.0 < tiers["cheap_call_share"] < 1.0
    report = format_report(result)
    assert "escalated to strong" in report


def test_a_single_client_still_works_untouched(sandbox):
    """Nothing about tiering is mandatory."""
    client = ScriptedClient([ChatResponse(content="Fine.", stats=_stats())])
    result = _orch(sandbox.root, client).run("review", skill_name="git-review")
    assert result.answer == "Fine."
    assert result.routing.available == []
    assert result.state.metrics.tier_stats == {}


def test_tiered_client_falls_back_when_a_tier_is_missing():
    only_strong = TieredClient({STRONG: ScriptedClient([])}, default_tier=STRONG)
    assert only_strong.select(CHEAP) == STRONG
    assert only_strong.has(CHEAP) is False


def test_context_budget_follows_the_selected_tier():
    from local_agent.config import MODEL_PRESETS
    from local_agent.llm.router import build_tiered_client

    client = build_tiered_client(
        {CHEAP: MODEL_PRESETS["ptl-npu-8b"], STRONG: MODEL_PRESETS["ptl-gpu-30b"]}
    )
    client.select(CHEAP)
    assert client.context_budget() == MODEL_PRESETS["ptl-npu-8b"].context_budget_tokens
    client.select(STRONG)
    assert client.context_budget() == MODEL_PRESETS["ptl-gpu-30b"].context_budget_tokens
