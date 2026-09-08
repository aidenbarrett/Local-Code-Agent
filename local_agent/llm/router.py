"""Tiered model routing.

Most of a working day is not "design a new C++ subsystem from vague
requirements". It is: what changed in my branch, build this target, run that
test, why did it fail, where does this symbol live. Those are jobs where the
skill remembers the hard part and the model only has to pick a tool, fill in
its arguments, read the result and decide what comes next. An 8B on an NPU can
do that at single-digit watts.

So the orchestrator sends each skill to the cheapest tier that can do its job
and escalates only when the cheap one demonstrably failed. That produces the
number this whole exercise exists to find:

    what fraction of real work can the low-power engine carry on its own

Escalation is a full re-run on the stronger tier with a fresh context, not a
mid-conversation model swap. Swapping models halfway through a transcript means
the strong model inherits the weak one's confusions and its KV prefix is
useless anyway, so there is nothing to save and a lot to get wrong.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..config import ModelConfig
from .client import LLMClient, OpenAICompatibleClient
from .models import ChatResponse

CHEAP = "cheap"
STRONG = "strong"

# Which tier each skill is sent to first. A skill that only reads and reports
# goes to the cheap tier. A skill that has to hold a chain of reasoning about
# unfamiliar C++ starts strong, because a wrong diagnosis costs more than the
# tokens saved.
DEFAULT_SKILL_TIERS: dict[str, str] = {
    "repo-navigation": CHEAP,
    "git-review": CHEAP,
    "prepare-commit": CHEAP,
    "build-and-test": CHEAP,
    "diagnose-build-failure": STRONG,
    "diagnose-test-failure": STRONG,
}


@dataclass
class TierStats:
    calls: int = 0
    seconds: float = 0.0
    prompt_tokens: int = 0
    completion_tokens: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "calls": self.calls,
            "seconds": round(self.seconds, 2),
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
        }


class TieredClient:
    """Presents one LLMClient interface over several endpoints.

    `select(tier)` picks which one serves the next call. Anything that holds a
    reference to this object keeps working when the tier changes, so the
    orchestrator does not need to know that tiers exist at all beyond one call.
    """

    def __init__(
        self,
        clients: dict[str, LLMClient],
        configs: dict[str, ModelConfig] | None = None,
        default_tier: str = STRONG,
    ) -> None:
        if not clients:
            raise ValueError("a TieredClient needs at least one endpoint")
        self.clients = clients
        self.configs = configs or {}
        self.default_tier = default_tier if default_tier in clients else next(iter(clients))
        self.active_tier = self.default_tier
        self.stats: dict[str, TierStats] = {tier: TierStats() for tier in clients}

    # ------------------------------------------------------------ selection

    def has(self, tier: str) -> bool:
        return tier in self.clients

    def select(self, tier: str | None) -> str:
        """Switch tiers, falling back to what actually exists."""
        if tier and tier in self.clients:
            self.active_tier = tier
        else:
            self.active_tier = self.default_tier
        return self.active_tier

    def config_for(self, tier: str | None = None) -> ModelConfig | None:
        return self.configs.get(tier or self.active_tier)

    def context_budget(self, tier: str | None = None) -> int | None:
        config = self.config_for(tier)
        return config.context_budget_tokens if config else None

    def describe(self) -> dict[str, str]:
        return {
            tier: (
                f"{config.model} on {config.device_note} via {config.runtime}"
                if (config := self.configs.get(tier))
                else "unknown endpoint"
            )
            for tier in self.clients
        }

    def identity(self) -> dict[str, Any]:
        """Full configuration identity per tier, for the report."""
        return {
            tier: dict(config.identity())
            for tier, config in self.configs.items()
        }

    # ----------------------------------------------------------------- chat

    def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        max_tokens: int | None = None,
    ) -> ChatResponse:
        client = self.clients[self.active_tier]
        response = client.chat(messages, tools=tools, max_tokens=max_tokens)

        stats = self.stats[self.active_tier]
        stats.calls += 1
        stats.seconds += response.stats.total_s
        stats.prompt_tokens += response.stats.prompt_tokens
        stats.completion_tokens += response.stats.completion_tokens
        return response

    def mark_discarded(self) -> int:
        """Everything the cheap tier has spent so far is about to be thrown away.

        Recorded rather than deducted. Gross calls are what the hardware
        actually did, and that is the number energy has to be divided by;
        productive calls are what survived into an answer. Two different
        questions, two different numbers, neither pretending to be the other.
        """
        self._discarded_cheap = self.stats[CHEAP].calls if CHEAP in self.stats else 0
        return self._discarded_cheap

    @property
    def discarded_cheap_calls(self) -> int:
        return getattr(self, "_discarded_cheap", 0)

    def stats_as_dict(self) -> dict[str, Any]:
        served = {tier: s.calls for tier, s in self.stats.items()}
        total = sum(served.values())
        gross_cheap = served.get(CHEAP, 0)
        discarded = self.discarded_cheap_calls
        return {
            "endpoints": self.describe(),
            "identity": self.identity(),
            "by_tier": {tier: s.as_dict() for tier, s in self.stats.items()},
            "calls_total": total,
            "cheap_calls_gross": gross_cheap,
            "cheap_calls_discarded": discarded,
            "cheap_calls_productive": max(0, gross_cheap - discarded),
            # Gross share: what the hardware did, which is what energy divides by.
            "cheap_call_share": round(gross_cheap / total, 3) if total else None,
            # Productive share: what survived into an answer.
            "cheap_productive_share": (
                round(max(0, gross_cheap - discarded) / total, 3) if total else None
            ),
        }

    def reset_stats(self) -> None:
        self.stats = {tier: TierStats() for tier in self.clients}


@dataclass
class RoutingPlan:
    """How a task was routed, and why."""

    skill: str | None
    first_tier: str
    final_tier: str
    escalated: bool = False
    escalation_reason: str | None = None
    available: list[str] = field(default_factory=list)
    # Router confidence, so a wrong procedure is not silently blamed on the model.
    skill_score: float = 0.0
    skill_confident: bool = True
    discarded_cheap_calls: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "skill": self.skill,
            "first_tier": self.first_tier,
            "final_tier": self.final_tier,
            "escalated": self.escalated,
            "escalation_reason": self.escalation_reason,
            "available_tiers": self.available,
            "skill_score": round(self.skill_score, 3),
            "skill_confident": self.skill_confident,
            "discarded_cheap_calls": self.discarded_cheap_calls,
        }


def tier_for_skill(
    skill_name: str | None,
    declared: str | None = None,
    overrides: dict[str, str] | None = None,
) -> str:
    """A skill's own frontmatter wins, then any override map, then the default."""
    if declared in (CHEAP, STRONG):
        return declared
    table = {**DEFAULT_SKILL_TIERS, **(overrides or {})}
    if skill_name and skill_name in table:
        return table[skill_name]
    return STRONG


def build_tiered_client(
    configs: dict[str, ModelConfig],
    default_tier: str = STRONG,
) -> TieredClient:
    clients: dict[str, LLMClient] = {
        tier: OpenAICompatibleClient(config) for tier, config in configs.items()
    }
    return TieredClient(clients, configs=configs, default_tier=default_tier)
