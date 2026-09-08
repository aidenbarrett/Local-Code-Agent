"""Structured agent state.

Deliberately outside the model context. Once C++ source and build logs start
piling up, the thing that keeps the run coherent is this record, not the
transcript.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any


class Phase(str, Enum):
    UNDERSTAND = "understand"
    ROUTE = "route"
    GATHER = "gather"
    ACT = "act"
    VERIFY = "verify"
    REPORT = "report"
    HALTED = "halted"


class HaltCause(str, Enum):
    """Why the loop stopped early. Typed twin of the English in `halt_reason`.

    The English is for the human reading the report. This is for everything
    else: outcome classification, escalation decisions, grouping in evals.
    """

    BUDGET_EXHAUSTED = "budget_exhausted"      # tool-call budget spent
    REPEAT_LOOP = "repeat_loop"                # identical call, again and again
    UNKNOWN_TOOLS = "unknown_tools"            # kept asking for tools it could not use
                                               # (invented, or real but not offered)
    SERVER_UNAVAILABLE = "server_unavailable"  # the inference call itself failed
    INFERENCE_STALLED = "inference_stalled"    # server alive, request made no progress


class Validity(str, Enum):
    """Whether this run may enter a denominator. Orthogonal to Outcome.

    A run can be PASS and INVALID at the same time: the task succeeded, but
    under a configuration nobody chose, so the number belongs to no experiment.
    """

    VALID = "valid"
    INVALID_SERVER_UNAVAILABLE = "invalid_server_unavailable"
    INVALID_SERVER_STALLED = "invalid_server_stalled"
    INVALID_IDENTITY_MISMATCH = "invalid_identity_mismatch"
    INVALID_FALLBACK = "invalid_fallback"


@dataclass
class ToolCallRecord:
    name: str
    arguments: dict[str, Any]
    verdict: str
    ok: bool | None
    summary: str
    # Typed classification, so nothing downstream has to read English.
    blocked: bool = False
    execution: str = "ok"
    domain: str = "unknown"
    reason: str | None = None
    exit_code: int | None = None
    artifacts: list[str] = field(default_factory=list)
    # Structured payload kept for evidence replay on escalation. Never prose.
    evidence: dict[str, Any] = field(default_factory=dict)
    # Which repository state this call ran against. Two identical calls in
    # different epochs are not a repeat: the tree changed in between.
    epoch: int = 0
    # What this call proves, from local_agent.verification.ProofKind. Written
    # by the orchestrator when the result comes back, read by the evaluator and
    # by the re-scorer, so all three use one classification rather than three.
    proof: str = "no_current_proof"

    def signature(self) -> str:
        return self.name + "|" + json.dumps(self.arguments, sort_keys=True, default=str)


@dataclass
class RunMetrics:
    """Everything you need to argue about performance with numbers.

    Wall clock is split three ways because they have different causes and
    different fixes: model time is the hardware, tool time is the build system,
    and the remainder is the agent's own overhead.
    """

    llm_calls: int = 0
    llm_seconds: float = 0.0
    tool_seconds: float = 0.0
    wall_seconds: float = 0.0

    prompt_tokens: int = 0
    completion_tokens: int = 0
    # None until some server actually reports cached prompt tokens. Absent is
    # not the same as zero, and reporting it as zero would make a working prefix
    # cache look broken.
    cached_prompt_tokens: int | None = None

    ttft_samples: list[float] = field(default_factory=list)
    prefill_tok_s_samples: list[float] = field(default_factory=list)
    decode_tok_s_samples: list[float] = field(default_factory=list)

    compactions: int = 0
    context_peak_tokens: int = 0

    # Filled in when a TieredClient served the run: how many calls each engine
    # took, which is how you answer "what share of the work can the NPU carry".
    tier_stats: dict[str, Any] = field(default_factory=dict)

    # Which configuration produced these numbers: model, quant, runtime,
    # runtime version, device, parser, thinking mode, context limit. Recorded so
    # two runs can never be compared without their inference policy attached.
    model_identity: dict[str, Any] = field(default_factory=dict)

    # Requested, accepted and observed, kept apart. See CallStats.
    thinking_requested: bool | None = None
    thinking_control_accepted: bool | None = None
    thinking_observed: list[str] = field(default_factory=list)

    # Runtime provenance, per call. `expected_runtime_version` is what the
    # profile claims; `fingerprints_observed` is what every response said. A
    # server that reports a build string the profile did not name has been
    # swapped under us, and the run is not about the configuration it claims.
    expected_runtime_version: str | None = None
    fingerprints_observed: list[str] = field(default_factory=list)
    identity_mismatch: bool = False

    # Server-side timings when the backend reports them. Ground truth where our
    # client-side TTFT is an inference. None-able all the way down: a backend
    # that does not report them simply contributes nothing here.
    server_prompt_ms_total: float | None = None
    server_predicted_ms_total: float | None = None

    def observe_call(self, stats: Any) -> None:
        self.llm_calls += 1
        self.llm_seconds += stats.total_s
        self.prompt_tokens += stats.prompt_tokens
        self.completion_tokens += stats.completion_tokens
        if stats.cached_tokens is not None:
            self.cached_prompt_tokens = (self.cached_prompt_tokens or 0) + stats.cached_tokens
        if stats.ttft_s is not None:
            self.ttft_samples.append(stats.ttft_s)
        if stats.prefill_tok_s is not None:
            self.prefill_tok_s_samples.append(stats.prefill_tok_s)
        if stats.decode_tok_s is not None:
            self.decode_tok_s_samples.append(stats.decode_tok_s)

        if stats.thinking_requested is not None:
            self.thinking_requested = stats.thinking_requested
        if stats.thinking_control_accepted is False:
            # One rejection taints the whole run. Never upgrade back to True.
            self.thinking_control_accepted = False
        elif (stats.thinking_control_accepted is True
              and self.thinking_control_accepted is None):
            self.thinking_control_accepted = True
        self.thinking_observed.append(stats.thinking_effective)

        fingerprint = getattr(stats, "system_fingerprint", None)
        if fingerprint:
            self.fingerprints_observed.append(fingerprint)
            expected = self.expected_runtime_version
            if expected and expected != "not recorded" and fingerprint != expected:
                self.identity_mismatch = True

        prompt_ms = getattr(stats, "server_prompt_ms", None)
        if prompt_ms is not None:
            self.server_prompt_ms_total = (self.server_prompt_ms_total or 0.0) + prompt_ms
        predicted_ms = getattr(stats, "server_predicted_ms", None)
        if predicted_ms is not None:
            self.server_predicted_ms_total = (
                (self.server_predicted_ms_total or 0.0) + predicted_ms
            )

    @staticmethod
    def _median(values: list[float]) -> float | None:
        if not values:
            return None
        ordered = sorted(values)
        mid = len(ordered) // 2
        if len(ordered) % 2:
            return round(ordered[mid], 3)
        return round((ordered[mid - 1] + ordered[mid]) / 2, 3)

    @property
    def thinking_effective(self) -> str:
        """What actually happened across the run, not what was asked for."""
        if "on" in self.thinking_observed:
            return "on"
        if self.thinking_observed and all(v == "off" for v in self.thinking_observed):
            return "off"
        return "unknown"

    @property
    def thinking_policy_honoured(self) -> bool | None:
        """False when we asked for a policy and observably did not get it."""
        if self.thinking_requested is None:
            return None
        effective = self.thinking_effective
        if effective == "unknown":
            return None
        return effective == ("on" if self.thinking_requested else "off")

    @property
    def overhead_seconds(self) -> float:
        return max(0.0, self.wall_seconds - self.llm_seconds - self.tool_seconds)

    @property
    def cache_hit_ratio(self) -> float | None:
        """None means the server never reported it, not that nothing was cached."""
        if not self.prompt_tokens or self.cached_prompt_tokens is None:
            return None
        return round(self.cached_prompt_tokens / self.prompt_tokens, 3)

    def as_dict(self) -> dict[str, Any]:
        return {
            "llm_calls": self.llm_calls,
            "llm_seconds": round(self.llm_seconds, 2),
            "tool_seconds": round(self.tool_seconds, 2),
            "overhead_seconds": round(self.overhead_seconds, 2),
            "wall_seconds": round(self.wall_seconds, 2),
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "cached_prompt_tokens": self.cached_prompt_tokens,
            "cache_hit_ratio": self.cache_hit_ratio,
            "cache_reported_by_server": self.cached_prompt_tokens is not None,
            "median_ttft_s": self._median(self.ttft_samples),
            "max_ttft_s": round(max(self.ttft_samples), 3) if self.ttft_samples else None,
            "median_prefill_tok_s": self._median(self.prefill_tok_s_samples),
            "median_decode_tok_s": self._median(self.decode_tok_s_samples),
            "compactions": self.compactions,
            "context_peak_tokens": self.context_peak_tokens,
            "tiers": self.tier_stats or None,
            "model_identity": self.model_identity or None,
            "thinking_requested": self.thinking_requested,
            "thinking_control_accepted": self.thinking_control_accepted,
            "thinking_effective": self.thinking_effective,
            "thinking_policy_honoured": self.thinking_policy_honoured,
            "expected_runtime_version": self.expected_runtime_version,
            "fingerprints_observed": sorted(set(self.fingerprints_observed)) or None,
            "identity_mismatch": self.identity_mismatch,
            "server_prompt_ms_total": (
                round(self.server_prompt_ms_total, 1)
                if self.server_prompt_ms_total is not None else None
            ),
            "server_predicted_ms_total": (
                round(self.server_predicted_ms_total, 1)
                if self.server_predicted_ms_total is not None else None
            ),
        }


@dataclass
class AgentState:
    task: str
    repo_root: Path
    active_skill: str | None = None
    phase: Phase = Phase.UNDERSTAND

    tool_calls: int = 0
    history: list[ToolCallRecord] = field(default_factory=list)
    metrics: RunMetrics = field(default_factory=RunMetrics)

    changed_files: list[str] = field(default_factory=list)
    evidence: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    # Two different things, and conflating them was a bug:
    #   verified               a claim of success is backed by a tool that passed
    #   verification_attempted a verifying tool actually ran and returned a real
    #                          result, pass or fail
    # A build that fails to compile is evidence, not an absence of evidence, so
    # a diagnosis skill is satisfied by the second and never needs the first.
    verified: bool = False
    verification_attempted: bool = False
    halt_reason: str | None = None
    # Typed twin of halt_reason. Nothing downstream should need the English.
    halt_cause: HaltCause | None = None
    # The transport's own words when the inference call failed. Not a tool
    # record: no tool ran, and pretending one did would corrupt the evidence.
    llm_error: str | None = None

    # Set when the model finished through `submit_answer`. The claim is the
    # model's; whether the evidence supports it is decided by the orchestrator.
    claim: str | None = None
    cited_evidence: list[str] = field(default_factory=list)
    # Did the citations point at a real passing verification? Reported, never
    # used to decide `verified`: the orchestrator has the history and does not
    # need the model to hand it back. None until an answer was submitted.
    cited_correctly: bool | None = None
    cited_unknown: list[str] = field(default_factory=list)

    # Incremented on every successful mutation of the repository. `verified`
    # describes the current epoch only; a mutation resets it. The repeat guard
    # compares calls within one epoch only, so edit/build/edit/build is the fix
    # loop and not a stall, while build/build/build/build on an unchanged tree
    # still is. The first real run killed a correct fix one build short of
    # proving it, because the guard could not tell those two apart.
    mutation_epoch: int = 0

    # The tools this run may execute: the active skill's list plus the
    # universal ones. Set once by the orchestrator. Anything else the model
    # asks for is TOOL_NOT_ALLOWED even if the registry has it, which is typed
    # apart from UNKNOWN_TOOL: inventing a tool and picking a forbidden real
    # one are different failures. Narrowing that only controlled what the model
    # was shown was a suggestion, not a contract.
    toolset: list[str] = field(default_factory=list)

    # How the model spelled its evidence ids: global index, per-tool ordinal,
    # ambiguous, or unresolvable. "The model cannot cite its evidence" and "we
    # never documented the id scheme" are different findings and this is what
    # separates them.
    citation_schemes: dict[str, int] = field(default_factory=dict)

    # Which skill's toolset shaped what was offered, even when its body never
    # reached the model. In the `narrow` condition these differ, and that
    # difference is the independent variable.
    narrowed_by: str | None = None

    def record(self, rec: ToolCallRecord) -> None:
        self.history.append(rec)
        self.tool_calls += 1

    def halt(self, cause: HaltCause, reason: str) -> None:
        """Stop the loop with both the typed cause and the human sentence."""
        self.halt_cause = cause
        self.halt_reason = reason
        self.phase = Phase.HALTED

    @property
    def validity(self) -> Validity:
        """May this run enter a denominator? Worst applicable reason wins."""
        if self.halt_cause is HaltCause.SERVER_UNAVAILABLE:
            return Validity.INVALID_SERVER_UNAVAILABLE
        if self.halt_cause is HaltCause.INFERENCE_STALLED:
            return Validity.INVALID_SERVER_STALLED
        if self.metrics.identity_mismatch:
            return Validity.INVALID_IDENTITY_MISMATCH
        if self.metrics.thinking_control_accepted is False:
            return Validity.INVALID_FALLBACK
        return Validity.VALID

    def repeat_count(self, rec: ToolCallRecord) -> int:
        """Identical calls against the same repository state."""
        sig = rec.signature()
        return sum(
            1 for h in self.history
            if h.signature() == sig and h.epoch == rec.epoch
        )

    def note_mutation(self) -> None:
        """The tree changed. Old verification no longer describes it."""
        self.mutation_epoch += 1
        self.verified = False

    def note_evidence(self, text: str) -> None:
        if text and text not in self.evidence:
            self.evidence.append(text)

    def as_dict(self) -> dict[str, Any]:
        out = asdict(self)
        out["repo_root"] = str(self.repo_root)
        out["phase"] = self.phase.value
        out["halt_cause"] = self.halt_cause.value if self.halt_cause else None
        out["validity"] = self.validity.value
        out["mutation_epoch"] = self.mutation_epoch
        out["history"] = [asdict(h) for h in self.history]
        out["metrics"] = self.metrics.as_dict()
        return out

    def summary_lines(self) -> list[str]:
        m = self.metrics
        lines = [
            f"task: {self.task}",
            f"skill: {self.active_skill or 'none'}",
            f"phase: {self.phase.value}",
            f"tool calls: {self.tool_calls}",
        ]
        if m.wall_seconds:
            lines.append(
                f"wall: {m.wall_seconds:.1f}s "
                f"(model {m.llm_seconds:.1f}s over {m.llm_calls} call(s), "
                f"tools {m.tool_seconds:.1f}s, overhead {m.overhead_seconds:.1f}s)"
            )
            ttft = m._median(m.ttft_samples)
            decode = m._median(m.decode_tok_s_samples)
            if ttft is not None:
                lines.append(
                    f"median ttft: {ttft:.2f}s"
                    + (f", decode {decode:.1f} tok/s" if decode else "")
                    + (
                        f", prompt cache {m.cache_hit_ratio:.0%}"
                        if m.cache_hit_ratio is not None
                        else ""
                    )
                )
            if m.compactions:
                lines.append(
                    f"context compactions: {m.compactions} "
                    "(each one throws away the server's KV prefix)"
                )
        if self.changed_files:
            lines.append("changed: " + ", ".join(self.changed_files))
        if self.errors:
            lines.append(f"errors: {len(self.errors)}")
        if self.halt_reason:
            lines.append(f"halted: {self.halt_reason}")
        if self.validity is not Validity.VALID:
            lines.append(f"run validity: {self.validity.value} (excluded from denominators)")
        return lines
