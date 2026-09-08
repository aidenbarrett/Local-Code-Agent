"""The orchestration layers must never learn what a runtime is.

The agent sees one OpenAI-compatible contract. Whether it is llama-server on
CPU, OVMS on an NPU, or a cloud provider is a property of the profile, and the
router selects profiles by tier, never by implementation.
"""

from __future__ import annotations

import ast
from pathlib import Path

from local_agent.agent import Orchestrator, SkillLibrary
from local_agent.config import MODEL_PRESETS, ModelConfig, load_repo_config
from local_agent.llm.client import ScriptedClient
from local_agent.llm.models import CallStats, ChatResponse
from local_agent.llm.router import CHEAP, STRONG, TieredClient, build_tiered_client
from local_agent.tools import build_registry

REPO = Path(__file__).resolve().parent.parent.parent
RUNTIME_WORDS = ("llama", "ovms", "openvino", "gguf", "llamacpp")

# Where a runtime name is allowed to appear at all: the composition root, the
# profile table, and the protocol edge that normalises server quirks.
ALLOWED = {
    "cli.py",
    "config.py",
    "client.py",
    "models.py",
}


def _stats() -> CallStats:
    return CallStats(total_s=1.0, ttft_s=0.4, prompt_tokens=600,
                     completion_tokens=10, streamed=True)


def _orch(root: Path, client, **kwargs):
    repo = load_repo_config(root)
    registry, _, _ = build_registry(repo)
    skills = SkillLibrary.discover(REPO / ".github" / "skills")
    return Orchestrator(repo, registry, client, skills, **kwargs)


# ---------------------------------------------------------- the hard contract


def test_no_orchestration_layer_mentions_a_runtime():
    """Static proof, so this cannot rot the next time somebody is in a hurry."""
    offenders = []
    for path in sorted((REPO / "src" / "local_agent").rglob("*.py")):
        if path.name in ALLOWED or "__pycache__" in str(path):
            continue
        lowered = path.read_text(encoding="utf-8").lower()
        for word in RUNTIME_WORDS:
            if word in lowered:
                offenders.append(f"{path.relative_to(REPO)} mentions {word!r}")
    assert offenders == [], offenders


def test_no_module_branches_on_a_runtime_value():
    """Even in the allowed files, nothing may switch behaviour on the runtime."""
    for name in ("client.py", "models.py"):
        source = (REPO / "src" / "local_agent" / "llm" / name).read_text()
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Compare):
                continue
            rendered = ast.dump(node).lower()
            if "runtime" in rendered and any(w in rendered for w in RUNTIME_WORDS):
                raise AssertionError(f"{name} branches on a runtime value")


# ------------------------------------------------------------------ routing


def test_routing_is_identical_whatever_the_runtime_is_called(sandbox):
    """Same skill, same tier, same tool exposure, three different runtimes."""
    seen = []
    for runtime, device in (("llamacpp", "CPU"), ("ovms", "NPU"), ("cloud", "remote")):
        cheap = ModelConfig(base_url="http://a/v1", model="m", runtime=runtime,
                            device_note=device, tier="cheap")
        strong = ModelConfig(base_url="http://b/v1", model="M", runtime=runtime,
                             device_note=device, tier="strong")
        client = TieredClient(
            {CHEAP: ScriptedClient([ChatResponse(content="ok", stats=_stats())]),
             STRONG: ScriptedClient([ChatResponse(content="unused")])},
            configs={CHEAP: cheap, STRONG: strong},
            default_tier=STRONG,
        )
        result = _orch(sandbox.root, client).run("review my changes",
                                                 skill_name="git-review")
        seen.append(
            (result.routing.first_tier, result.routing.escalated,
             result.state.active_skill, result.state.tool_calls)
        )
    assert len(set(seen)) == 1, seen


def test_two_profiles_on_different_urls_occupy_the_two_tiers():
    client = build_tiered_client(
        {CHEAP: MODEL_PRESETS["nuc-llama-8b"], STRONG: MODEL_PRESETS["ptl-gpu-30b"]}
    )
    assert client.has(CHEAP) and client.has(STRONG)
    assert client.config_for(CHEAP).base_url != client.config_for(STRONG).base_url
    # A deliberately mixed pair: llama.cpp on CPU under OVMS on a GPU.
    assert client.config_for(CHEAP).runtime == "llamacpp"
    assert client.config_for(STRONG).runtime == "ovms"
    # Budgets are read per tier, whatever they happen to be set to.
    client.select(CHEAP)
    assert client.context_budget() == MODEL_PRESETS["nuc-llama-8b"].context_budget_tokens
    client.select(STRONG)
    assert client.context_budget() == MODEL_PRESETS["ptl-gpu-30b"].context_budget_tokens


def test_identity_reaches_run_metadata(sandbox):
    cheap = MODEL_PRESETS["nuc-llama-8b"]
    strong = MODEL_PRESETS["ptl-npu-8b"]
    client = TieredClient(
        {CHEAP: ScriptedClient([ChatResponse(content="ok", stats=_stats())]),
         STRONG: ScriptedClient([ChatResponse(content="unused")])},
        configs={CHEAP: cheap, STRONG: strong},
        default_tier=STRONG,
    )
    result = _orch(sandbox.root, client).run("review changes", skill_name="git-review")

    identity = result.state.metrics.as_dict()["tiers"]["identity"]
    assert identity[CHEAP]["runtime"] == "llamacpp"
    assert identity[CHEAP]["runtime_version"] == "b10816-427291b5b"
    assert identity[CHEAP]["thinking"] is False
    assert identity[STRONG]["device"] == "NPU (8K)"


def test_thinking_mode_is_carried_into_run_metadata(sandbox):
    """Never report "8B vs 30B" when it was "thinking on vs thinking off"."""
    class _Describing(ScriptedClient):
        def identity(self):
            return dict(MODEL_PRESETS["nuc-llama-8b"].identity())

    client = _Describing([ChatResponse(content="ok", stats=_stats())])
    result = _orch(sandbox.root, client).run("review", skill_name="git-review")
    recorded = result.state.metrics.as_dict()["model_identity"]
    assert recorded["thinking"] is False
    assert recorded["runtime"] == "llamacpp"
    assert recorded["quant"] == "Q4_K_M"


# ---------------------------------------------------------------- telemetry


def test_absent_usage_fields_are_never_fabricated():
    stats = CallStats(total_s=2.0, ttft_s=0.5, prompt_tokens=100,
                      completion_tokens=20, cached_tokens=None)
    assert stats.cached_tokens is None
    assert stats.cache_reported is False
    assert stats.cache_hit_ratio is None
    assert stats.as_dict()["cache_hit_ratio"] is None

    from local_agent.agent.state import RunMetrics

    metrics = RunMetrics()
    metrics.observe_call(stats)
    out = metrics.as_dict()
    assert out["cached_prompt_tokens"] is None
    assert out["cache_reported_by_server"] is False
    assert out["cache_hit_ratio"] is None


def test_a_plain_single_client_still_works_unchanged(sandbox):
    """Nothing above is mandatory. One endpoint, no tiers, no identity."""
    client = ScriptedClient([ChatResponse(content="Clean tree.", stats=_stats())])
    result = _orch(sandbox.root, client).run("review", skill_name="git-review")
    assert result.answer == "Clean tree."
    assert result.routing.available == []
    assert result.state.metrics.tier_stats == {}
    assert result.state.metrics.model_identity == {}
