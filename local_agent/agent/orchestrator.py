"""The orchestrator.

UNDERSTAND -> ROUTE -> ACTIVATE SKILL -> GATHER -> DECIDE -> POLICY -> TOOL ->
OBSERVE -> (loop) -> REPORT.

One agent, one loop, one state object. No swarm of small models arguing with
itself and burning context doing it.

Everything the loop does is timed and attributed, because "it feels slow" is
not a finding and "median time to first token was 6.2 seconds across 14 calls
with a 71 percent prompt cache hit rate" is.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from ..config import RepoConfig
from ..llm.client import LLMClient
from ..llm.models import LLMTransportError, ToolCall
from ..llm.router import CHEAP, STRONG, RoutingPlan, TieredClient, tier_for_skill
from ..tools import TOOLSETS, UNIVERSAL_TOOLS, ToolRegistry
from ..tools.base import BlockedError, Reason, Risk, ToolError, ToolResult
from ..verification import (
    CURRENT_TREE_PROOFS,
    VERIFYING_TOOLS,
    ProofKind,
    classify_proof,
)
from . import context as ctxmod
from .context import ContextManager
from .journal import (
    MutationJournal,
    RepoFingerprint,
    build_evidence_packet,
    render_evidence_message,
)
from .outcome import Outcome, classify
from .policy import ApprovalFn, Decision, PolicyEngine, Verdict, deny_all_approvals
from .skills import SkillLibrary
from .state import AgentState, HaltCause, Phase, ToolCallRecord

log = logging.getLogger("local_agent.orchestrator")

# Tools whose passing result can satisfy a verification contract. Configuring
# is a prerequisite, not a verification: a project that configures may not
# build, and one that builds may not pass. So configure_project is not here.
#
# Defined once, in local_agent.verification, and re-exported here under the old
# name so the orchestrator and the evaluator cannot end up with two lists.
_VERIFYING_TOOLS = VERIFYING_TOOLS


def _resolve_citations(cited: list[str], history: list[Any]):
    """Match cited evidence ids against the calls actually made.

    The model is not told what an evidence id looks like, so it invents one.
    The 30B used per-tool ordinals, "the first run_test", "the third
    read_file", which is the only scheme a model can compute from what it can
    see and is what a person would write. The orchestrator had only ever
    matched a global history index, so most citations were reported unknown
    and two matched by coincidence: `read_file:4` and `read_file:5` resolved
    to the fourth and fifth CALLS, which happened to be read_file, rather than
    to the fourth and fifth READS the model meant. A false positive is worse
    than a miss, and `read_file:5` is a real citation error the old matching
    would have scored as correct.

    The convention belongs to the model, not to the token, so the scheme is
    decided once per run: whichever spelling explains more of the list wins,
    and the rest are unknown under that scheme. On a tie only the tokens both
    schemes agree on resolve, because guessing which one was meant would be
    inventing data. Which scheme won is recorded, since "the model cannot cite
    its evidence" and "we never documented the id scheme" are different
    findings and only one of them is about the model.
    """
    by_index: dict[str, int] = {}
    by_ordinal: dict[str, int] = {}
    seen: dict[str, int] = {}
    for i, h in enumerate(history):
        by_index[f"{h.name}:{i}"] = i
        seen[h.name] = seen.get(h.name, 0) + 1
        for sep in (":", "#"):
            by_ordinal[f"{h.name}{sep}{seen[h.name]}"] = i

    index_hits = [t for t in cited if t in by_index]
    ordinal_hits = [t for t in cited if t in by_ordinal]
    schemes = {
        "index": len(index_hits),
        "ordinal": len(ordinal_hits),
        "ambiguous": 0,
        "unknown": 0,
        "chosen": None,
    }

    if len(ordinal_hits) > len(index_hits):
        table, schemes["chosen"] = by_ordinal, "ordinal"
    elif len(index_hits) > len(ordinal_hits):
        table, schemes["chosen"] = by_index, "index"
    else:
        # Neither convention explains the list better. Only agreement counts.
        table = {t: by_index[t] for t in index_hits if by_ordinal.get(t) == by_index[t]}
        schemes["chosen"] = "agreed" if table else "none"
        schemes["ambiguous"] = len(set(index_hits) | set(ordinal_hits)) - len(table)

    resolved: list[Any] = []
    unknown: list[str] = []
    for token in cited:
        hit = table.get(token)
        if hit is None:
            unknown.append(token)
        else:
            resolved.append(history[hit])
    schemes["unknown"] = len(unknown) - schemes["ambiguous"]
    return resolved, unknown, schemes


def _satisfies_verification(
    name: str,
    arguments: dict[str, Any],
    execution: str = "ok",
    domain: str = "pass",
    evidence: dict[str, Any] | None = None,
) -> bool:
    """Does this call prove what the contract asks?

    The contract is "the project builds" or "the tests pass". A build of one
    named target proves that target and nothing else: under the link_error
    scenario `build_target(target="sandbox")` passes, because a static library
    does not link, while the project does not build.

    The rules used to live here AND in the evaluator, in two copies that agreed
    until they did not: this one accepted `run_test(rerun_failed=True)` and a
    stale PASS as proof while the evaluator rejected both, so a run could be
    recorded verified and then scored unproven. There is now one copy, in
    `local_agent.verification`, and this is a thin adapter onto it.
    """
    return classify_proof(
        name=name,
        arguments=arguments,
        execution=execution,
        domain=domain,
        evidence=evidence or {},
    ) in CURRENT_TREE_PROOFS

Observer = Callable[[str, dict[str, Any]], None]


@dataclass
class RunResult:
    answer: str
    state: AgentState
    messages: list[dict[str, Any]] = field(default_factory=list)
    routing: RoutingPlan | None = None
    outcome: Outcome = Outcome.PASS

    @property
    def ok(self) -> bool:
        return self.state.halt_reason is None


class Orchestrator:
    def __init__(
        self,
        repo: RepoConfig,
        registry: ToolRegistry,
        client: LLMClient,
        skills: SkillLibrary,
        approval: ApprovalFn | None = None,
        observer: Observer | None = None,
        context_budget_tokens: int = 12_000,
        skill_tiers: dict[str, str] | None = None,
        allow_escalation: bool = True,
        journal: MutationJournal | None = None,
        replay_evidence: bool = True,
    ) -> None:
        self.repo = repo
        self.registry = registry
        self.client = client
        self.skills = skills
        self.policy = PolicyEngine(repo.policy)
        self.approval = approval or deny_all_approvals
        self.observer = observer or (lambda event, payload: None)
        self.context_budget_tokens = context_budget_tokens
        self.skill_tiers = skill_tiers or {}
        self.allow_escalation = allow_escalation
        # Shared with the tool layer so applied patches can be undone before an
        # escalation hands the strong tier a tree the cheap tier already broke.
        self.journal = journal
        self.replay_evidence = replay_evidence

    # ------------------------------------------------------------------ run

    def run(
        self,
        task: str,
        skill_name: str | None = None,
        no_skill: bool = False,
        verification_required: bool | None = None,
        condition: str = "skill",
        catalogue: bool = True,
    ) -> RunResult:
        """Route to the cheapest tier that can do the job, escalate if it fails.

        A skill supplies three separable things, and `condition` says which of
        them this run gets:

          control   no procedure, every registered tool
          narrow    no procedure, the skill's toolset
          skill     the skill body, the skill's toolset

        `control` versus `narrow` is the effect of taking tools away. `narrow`
        versus `skill` is what the written procedure adds on top of that. The
        two-cell version could only say whether the whole package helps, and
        every scope violation in the first control run was a reach for a tool
        the treatment withholds, which is a strong hint that the two components
        do not contribute equally.

        `catalogue` is independent of all three. It is the list of skill names
        in the system prompt, which is about discovery and routing rather than
        about doing the work, and the eval harness pins the skill anyway. For
        the mechanism experiment it is off in every condition, so that
        `skill` minus `narrow` is procedure and not procedure plus catalogue.

        Everything else is held identical across conditions: the same tools with
        the same contracts and the same typed feedback, the same policy, the
        same budgets, the same orchestrator-owned verification, and the same
        outcome rules. `verification_required` is passed in rather than read off
        a skill, because a control whose success bar is lower than the
        treatment's is not a control.

        `no_skill=True` is the older spelling of `condition="control"`.
        """
        if no_skill:
            condition = "control"
        if condition not in ("control", "narrow", "skill"):
            raise ValueError(f"unknown condition {condition!r}")
        if condition == "control":
            chosen, score, confident, skill_name = None, 0.0, True, None
        if skill_name:
            chosen, score, confident = skill_name, 1.0, True
        elif condition == "control":
            pass
        else:
            chosen, score, confident = self.skills.route_with_confidence(task)
        # `narrow` keeps the skill only to know which tools it offers. Its body
        # never reaches the model.
        skill = self.skills.get(chosen) if chosen else None

        tiered = self.client if isinstance(self.client, TieredClient) else None
        wanted = tier_for_skill(
            skill.name if skill else None,
            getattr(skill, "tier", None),
            self.skill_tiers,
        )
        # Fingerprint before anything runs, so we can prove the tree we hand to
        # the strong tier is the tree we started with.
        start_state = RepoFingerprint.capture(
            self.repo.root, getattr(skill, "body", None)
        )

        first_tier = tiered.select(wanted) if tiered else wanted
        plan = RoutingPlan(
            skill=skill.name if skill else None,
            first_tier=first_tier,
            final_tier=first_tier,
            available=sorted(tiered.clients) if tiered else [],
            skill_score=score,
            skill_confident=confident,
        )
        if not confident:
            self.observer(
                "router_uncertain",
                {"skill": chosen, "score": round(score, 3),
                 "ranking": self.skills.rank(task, top=3)},
            )
        self.observer("route", {"skill": plan.skill, "tier": first_tier,
                                "ranking": self.skills.rank(task)})

        if verification_required is None:
            verification_required = bool(getattr(skill, "verification_required", False))
        escalation_allowed = (
            self.allow_escalation
            and getattr(skill, "escalation", "allowed") != "never"
        )
        escalation_available = (
            escalation_allowed and tiered is not None and tiered.has(STRONG)
            and first_tier == CHEAP
        )

        result = self._run_once(task, skill, tiered, speculative=escalation_available,
                                condition=condition, catalogue=catalogue,
                                verification_required=verification_required)
        result.routing = plan
        if not confident:
            result.state.warnings.append(
                f"skill routing was not confident ({chosen} scored {score:.2f}); "
                "a wrong procedure here looks like a model failure but is not"
            )

        # An environment that would not let the tool run is not a model failure.
        # Escalating it just walks the strong tier into the same wall.
        blocked = self._blocked_reason(result, verification_required)
        if blocked:
            result.outcome = Outcome.BLOCKED
            result.state.warnings.append(f"blocked, not failed: {blocked}")
            self.observer("blocked", {"reason": blocked, "skill": plan.skill})
            return result

        reason = self._escalation_reason(result, verification_required)
        if escalation_available and reason is not None:
            self.observer("escalate", {"from": CHEAP, "to": STRONG, "reason": reason})
            plan.discarded_cheap_calls = tiered.mark_discarded()

            # 1. Undo only our own mutations. Never anybody else's.
            restoration = self._restore(start_state, result)

            # 2. Carry forward deterministic observations, not the cheap tier's
            #    reasoning. Real compiler and test output cost seconds to
            #    regenerate; a wrong diagnosis costs the whole run.
            packet = None
            if self.replay_evidence and result.state.history:
                packet = build_evidence_packet(result.state.history, start_state)

            tiered.select(STRONG)
            retry = self._run_once(task, skill, tiered, evidence=packet, speculative=False,
                                   condition=condition, catalogue=catalogue,
                                   verification_required=verification_required)
            retry.state.warnings.extend(restoration)
            plan.final_tier = STRONG
            plan.escalated = True
            plan.escalation_reason = reason
            retry.routing = plan
            retry.state.warnings.append(
                f"escalated from the cheap tier to the strong tier: {reason}"
            )
            retry_blocked = self._blocked_reason(retry, verification_required)
            if retry_blocked:
                retry.outcome = Outcome.BLOCKED
            else:
                retry.outcome = classify(
                    succeeded=self._escalation_reason(retry, verification_required) is None,
                    escalated=True,
                    escalation_available=True,
                    blocked_reason=None,
                )
            return retry

        result.outcome = classify(
            succeeded=reason is None,
            escalated=False,
            escalation_available=escalation_available,
            blocked_reason=None,
        )
        return result

    def _accept_answer(self, call: ToolCall, state: AgentState) -> tuple[str, bool]:
        """The model cites evidence; the state machine decides what it proves.

        This is why there is no English phrase matching anywhere: a claim of
        success is only accepted when the cited tool calls actually ran and
        actually passed. "the suite is now green" proves nothing on its own, and
        neither does "I cannot confirm the tests pass".
        """
        claim = str(call.arguments.get("claim", "diagnosis"))
        summary = str(call.arguments.get("summary", "")).strip()
        cited = [str(x) for x in (call.arguments.get("evidence_ids") or [])]

        resolved, unknown, schemes = _resolve_citations(cited, state.history)

        # A build or test that passed, of any breadth. Deliberately wider than
        # `verified`: citing the targeted build you did is honest grounding
        # even though it is not proof of the whole tree, and the two questions
        # are reported separately. The kinds come from the shared classifier so
        # "passed" means one thing across the system.
        passing = {
            ProofKind.FULL_BUILD_PASS,
            ProofKind.FULL_TEST_PASS,
            ProofKind.TARGETED_BUILD_PASS,
            ProofKind.TARGETED_TEST_PASS,
        }
        passing_verification = [
            h for h in resolved if ProofKind(getattr(h, "proof", "no_current_proof")) in passing
        ]
        state.claim = claim
        state.cited_evidence = cited
        state.cited_unknown = unknown
        state.citation_schemes = schemes
        # Citation quality is a fact about the model's grounding and is
        # reported as such. It does not decide `verified`: the orchestrator
        # already knows whether the current tree passed, from its own history,
        # and a correct fix with a wrong record id is still a correct fix.
        #
        # Grounding is "every id you cited is a call you actually made". A
        # claim of success additionally has to cite the passing build or test,
        # because that is what makes it a claim rather than an opinion. A
        # diagnosis has no passing verification to cite, by construction: the
        # test under diagnosis fails. Requiring one made this metric
        # unearnable on every diagnosis case in the suite.
        state.cited_correctly = (
            not unknown
            and (claim != "success" or bool(passing_verification))
        )
        if claim == "success" and not passing_verification:
            state.warnings.append(
                "claim of success cited no passing build or test call"
            )
        if unknown:
            state.warnings.append(
                "cited evidence ids that do not exist in this run: " + ", ".join(unknown)
            )

        self.observer(
            "answer",
            {"claim": claim, "cited": cited, "verified": state.verified},
        )
        return summary, True

    @staticmethod
    def _blocked_reason(result: RunResult, verification_required: bool) -> str | None:
        """Did the environment, rather than the model, stop this run?

        Decided entirely from the typed execution status recorded against each
        tool call. Nothing here reads English out of a summary.
        """
        if result.state.halt_cause is HaltCause.SERVER_UNAVAILABLE:
            return f"inference server unavailable ({Reason.SERVER_UNAVAILABLE.value})"
        if result.state.halt_cause is HaltCause.INFERENCE_STALLED:
            return f"inference stalled ({Reason.SERVER_STALLED.value})"
        blocked = [h for h in result.state.history if h.blocked]
        if not blocked:
            return None
        if verification_required and not result.state.verification_attempted:
            first = blocked[0]
            return f"{first.name} could not run ({first.reason or 'environment'})"
        return None

    @staticmethod
    def _escalation_reason(result: RunResult, verification_required: bool = False) -> str | None:
        state = result.state
        if state.halt_reason:
            return state.halt_reason
        if not result.answer.strip():
            return "no answer was produced"
        if verification_required and state.claim == "success" and not state.verified:
            return "success was claimed without a passing build or test to back it"
        if verification_required and not state.verification_attempted:
            return "the skill requires a tool-backed result and no tool was run"
        return None

    def _restore(self, start_state: RepoFingerprint, result: RunResult) -> list[str]:
        """Put the worktree back before escalating. Report honestly if we cannot."""
        notes: list[str] = []
        if self.journal is not None and (self.journal.entries or self.journal.irreversible):
            outcome = self.journal.revert()
            if outcome["reverted"]:
                notes.append(
                    "reverted this agent's own edits before escalating: "
                    + ", ".join(outcome["reverted"])
                )
            for skipped in outcome["skipped"]:
                notes.append(f"did NOT revert {skipped}; left exactly as found")
            for irreversible in outcome["irreversible"]:
                notes.append(f"could not be undone before escalating: {irreversible}")

        after = RepoFingerprint.capture(self.repo.root)
        drift = after.differs_from(start_state)
        if drift:
            notes.append(
                "the strong tier is starting from a tree that differs from the "
                "original: " + "; ".join(drift)
            )
            self.observer("state_drift", {"differences": drift})
        for note in notes:
            self.observer("restore", {"note": note})
        return notes

    def _run_once(
        self,
        task: str,
        skill: Any,
        tiered: TieredClient | None,
        evidence: dict[str, Any] | None = None,
        speculative: bool = False,
        condition: str = "skill",
        catalogue: bool = True,
        verification_required: bool | None = None,
    ) -> RunResult:
        if verification_required is None:
            verification_required = bool(getattr(skill, "verification_required", False))
        state = AgentState(task=task, repo_root=self.repo.root)
        run_started = time.monotonic()

        # What build string the profile claims, so every response's fingerprint
        # can be checked against it. Duck-typed: a client with no identity
        # contributes nothing and nothing is checked.
        describe = getattr(self.client, "identity", None)
        if callable(describe):
            try:
                claimed = dict(describe()).get("runtime_version")
                if claimed and claimed != "not recorded":
                    state.metrics.expected_runtime_version = str(claimed)
            except Exception:  # pragma: no cover - telemetry is never load-bearing
                pass

        # In `narrow` the skill shaped the toolset but never spoke. Record what
        # actually reached the model, not what was consulted to build it.
        state.active_skill = skill.name if (skill and condition == "skill") else None
        state.narrowed_by = skill.name if skill else None
        state.phase = Phase.GATHER

        budget = self.context_budget_tokens
        if tiered is not None:
            budget = tiered.context_budget() or budget
        ctx = ContextManager(budget_tokens=budget)
        ctx.append(ctxmod.build_system_message(
            self.repo, self.skills.catalogue() if catalogue else None))
        if skill and condition == "skill":
            ctx.append(ctxmod.build_skill_message(skill))
        if evidence is not None:
            ctx.append(render_evidence_message(evidence))
        ctx.append({"role": "user", "content": task})

        toolset = self._toolset_for(
            skill.name if skill else None, skill, no_skill=(condition == "control"))
        state.toolset = list(toolset)
        schemas = self.registry.schemas(toolset)
        self.observer("toolset", {"tools": toolset})

        answer = ""
        nudged = False

        while True:
            if state.tool_calls >= self.repo.policy.max_tool_calls:
                state.halt(
                    HaltCause.BUDGET_EXHAUSTED,
                    f"tool call budget exhausted ({self.repo.policy.max_tool_calls})",
                )
                break

            # Compact only when we are actually over budget, and account for it.
            # Every compaction rewrites history, which discards the server's KV
            # prefix and forces a full re-prefill on the next call.
            if ctx.needs_compaction():
                event = ctx.compact()
                if event:
                    state.metrics.compactions += 1
                    state.warnings.append(
                        f"context compacted at message {event.at_message_index}, "
                        f"reclaiming {event.tokens_reclaimed} tokens; the prompt "
                        "cache prefix was invalidated"
                    )
                    self.observer(
                        "compaction",
                        {
                            "at": event.at_message_index,
                            "reclaimed": event.tokens_reclaimed,
                            "collapsed": event.collapsed_results,
                        },
                    )

            state.metrics.context_peak_tokens = max(
                state.metrics.context_peak_tokens, ctx.tokens
            )

            try:
                response = self.client.chat(ctx.for_request(), tools=schemas)
            except LLMTransportError as exc:
                # No tool ran and no answer exists. The environment took the
                # model away, so this is BLOCKED, and the run is not evidence
                # about anything: state.validity says so. Two different facts:
                # a server we could not reach, and one that answered but made no
                # progress. They get different follow-up, so they are typed apart.
                state.llm_error = str(exc)
                if getattr(exc, "kind", "unavailable") == "stalled":
                    state.halt(HaltCause.INFERENCE_STALLED, f"inference stalled: {exc}")
                    self.observer("server_stalled", {"error": str(exc), "cause": exc.cause})
                else:
                    state.halt(
                        HaltCause.SERVER_UNAVAILABLE,
                        f"inference server unavailable: {exc}",
                    )
                    self.observer("server_unavailable", {"error": str(exc), "cause": exc.cause})
                break
            state.metrics.observe_call(response.stats)
            self.observer("llm", response.stats.as_dict())
            ctx.append(response.as_assistant_message())

            if not response.wants_tools:
                answer = response.content or ""
                # A model that answers in prose instead of calling submit_answer
                # gets one nudge to state its claim in a checkable form. Nothing
                # here inspects what the prose says: that was phrase matching and
                # it is gone.
                if state.claim is None and verification_required and not nudged:
                    nudged = True
                    state.warnings.append("finished without calling submit_answer")
                    ctx.append(
                        {
                            "role": "user",
                            "content": (
                                "Call submit_answer to finish. State your claim and "
                                "cite the tool call ids that support it. If you have "
                                "not run a build or test, do not claim success."
                            ),
                        }
                    )
                    state.phase = Phase.VERIFY
                    continue
                state.phase = Phase.REPORT
                break

            state.phase = Phase.ACT
            halt = False
            submitted = False
            for call in response.tool_calls:
                if call.name == "submit_answer":
                    answer, submitted = self._accept_answer(call, state)
                    break
                outcome, stop = self._execute(call, state, speculative=speculative)
                ctx.append(
                    ctxmod.tool_result_message(
                        call.id,
                        call.name,
                        outcome.to_json(self.repo.policy.max_tool_result_bytes),
                    )
                )
                if stop:
                    halt = True
                    break
            if submitted:
                state.phase = Phase.REPORT
                break
            if halt:
                state.phase = Phase.HALTED
                break

        state.metrics.wall_seconds = time.monotonic() - run_started
        state.metrics.context_peak_tokens = max(
            state.metrics.context_peak_tokens, ctx.tokens
        )
        if tiered is not None:
            state.metrics.tier_stats = tiered.stats_as_dict()
        # Duck-typed on purpose: any client that can describe itself does, and
        # the orchestrator never learns what a "runtime" is.
        describe = getattr(self.client, "identity", None)
        if callable(describe):
            try:
                state.metrics.model_identity = dict(describe())
            except Exception:  # pragma: no cover - telemetry is never load-bearing
                pass
        self.observer("done", state.metrics.as_dict())
        return RunResult(answer=answer, state=state, messages=ctx.messages)

    # -------------------------------------------------------------- helpers

    def _toolset_for(self, name: str | None, skill: Any,
                     no_skill: bool = False) -> list[str]:
        chosen: list[str] = []
        if no_skill:
            # No narrowing means no narrowing. Not the read-only `_default`
            # set, which is narrowing under another name and would hand the
            # control condition a smaller world than the treatment: a control
            # that cannot call build_target has not been denied a procedure,
            # it has been denied the job.
            return sorted(self.registry.names())
        if skill is not None and getattr(skill, "tools", None):
            chosen = [t for t in skill.tools if t in self.registry]
        if not chosen:
            chosen = list(TOOLSETS.get(name or "", TOOLSETS["_default"]))
        for extra in UNIVERSAL_TOOLS:
            if extra in self.registry and extra not in chosen:
                chosen.append(extra)
        return chosen

    def _execute(
        self, call: ToolCall, state: AgentState, speculative: bool = False
    ) -> tuple[ToolResult, bool]:
        """Returns (result, should_halt)."""
        try:
            # Registry first, then narrowing. The order is the distinction:
            # a name nobody registers is UNKNOWN_TOOL (the model invented it);
            # a registered tool the active skill does not offer is
            # TOOL_NOT_ALLOWED (the model picked a real but forbidden tool).
            # Asking the toolset first would report every hallucinated name as
            # a policy denial and the two failures could never be separated.
            tool = self.registry.get(call.name)
            if state.toolset and call.name not in state.toolset:
                # Enforced here, not only at schema time: a diagnosis skill
                # without patch tools must mean the tree cannot be edited under
                # it, not that the model was not told it could.
                raise ToolError(
                    f"{call.name!r} is not available to the active skill; "
                    f"available: {sorted(state.toolset)}",
                    Reason.TOOL_NOT_ALLOWED,
                )
        except ToolError as exc:
            reason = exc.reason
            result = ToolResult.errored(reason, str(exc))
            state.record(
                ToolCallRecord(
                    call.name, call.arguments, "unknown", False, str(exc),
                    execution=result.execution_status.value,
                    domain=result.domain_status.value,
                    reason=reason.value,
                )
            )
            state.errors.append(str(exc))
            if self._too_many_unknown(state):
                state.halt(
                    HaltCause.UNKNOWN_TOOLS,
                    "model repeatedly called tools it could not use",
                )
                return result, True
            return result, False

        record = ToolCallRecord(call.name, call.arguments, "pending", None, "",
                                epoch=state.mutation_epoch)
        repeats = state.repeat_count(record)
        if repeats >= self.repo.policy.max_repeat_calls:
            state.halt(
                HaltCause.REPEAT_LOOP,
                f"{call.name} called with identical arguments "
                f"{repeats + 1} times; stopping the loop",
            )
            state.record(record)
            return ToolResult(False, state.halt_reason or ""), True

        decision: Decision = self.policy.check(tool, call.arguments, speculative)
        self.observer(
            "policy",
            {"tool": call.name, "verdict": decision.verdict.value, "reason": decision.reason},
        )

        if decision.verdict is Verdict.DENY:
            record.verdict = "denied"
            record.ok = False
            record.blocked = True
            record.execution = "blocked"
            record.reason = "policy_denied"
            record.summary = decision.reason
            state.record(record)
            return (
                ToolResult.blocked_by(
                    Reason.POLICY_DENIED,
                    f"{call.name} denied: {decision.reason}",
                    data={"remedy": "Propose the change instead, or ask the operator "
                                    "to enable it in .local-agent.toml."},
                ),
                False,
            )

        if decision.requires_approval:
            granted = self.approval(tool, call.arguments, decision)
            if not granted:
                record.verdict = "not-approved"
                record.ok = False
                record.blocked = True
                record.execution = "blocked"
                record.reason = "approval_declined"
                record.summary = "operator declined"
                state.record(record)
                return (
                    ToolResult.blocked_by(
                        Reason.APPROVAL_DECLINED,
                        f"{call.name} was not approved by the operator",
                        data={"remedy": "Continue without it and report what you would "
                                        "have done."},
                    ),
                    False,
                )
            record.verdict = "approved"
        else:
            record.verdict = "auto"

        self.observer("tool", {"name": call.name, "arguments": call.arguments})
        tool_started = time.monotonic()
        try:
            result = tool.handler(**call.arguments)
        except TypeError as exc:
            result = ToolResult.errored(
                Reason.BAD_ARGUMENTS,
                f"invalid arguments for {call.name}: {exc}",
                data={"schema": tool.parameters},
            )
        except BlockedError as exc:
            result = ToolResult.blocked_by(exc.reason, str(exc))
        except ToolError as exc:
            result = ToolResult.errored(
                getattr(exc, "reason", Reason.BAD_ARGUMENTS), str(exc)
            )
        except Exception as exc:  # pragma: no cover - defensive
            log.exception("tool %s blew up", call.name)
            result = ToolResult.errored(
                Reason.INTERNAL_ERROR,
                f"{call.name} raised {type(exc).__name__}: {exc}",
            )
        finally:
            state.metrics.tool_seconds += time.monotonic() - tool_started

        record.ok = result.ok
        record.summary = result.summary
        record.blocked = result.blocked
        record.execution = result.execution_status.value
        record.domain = result.domain_status.value
        record.reason = result.reason.value if result.reason else None
        record.exit_code = result.exit_code
        record.artifacts = list(result.artifacts)
        record.evidence = dict(result.data)

        # Classified once, off the typed result, and stored on the row. The
        # evaluator and the re-scorer read this rather than re-deriving it, so
        # a row's proof kind is the same fact in the transcript, in the score
        # and in any later replay.
        proof = classify_proof(
            name=call.name,
            arguments=call.arguments,
            execution=result.execution_status.value,
            domain=result.domain_status.value,
            evidence=result.data,
        )
        record.proof = proof.value
        state.record(record)

        if call.name in _VERIFYING_TOOLS and result.observed:
            # `observed`, not `ran`. A run_test whose filter matched no test
            # executes cleanly and exercises nothing: it is not an attempt at
            # verification, it is a null result that happens to exit 0.
            state.verification_attempted = True
        if proof in CURRENT_TREE_PROOFS:
            # The orchestrator decides this, from its own history, for the
            # current repository state. The model's citations are reported
            # separately and never overrule it.
            #
            # `result.ok` is not tested separately any more. It was a second
            # gate on top of a predicate that already requires a PASS, and
            # keeping two conditions in step is exactly the drift this whole
            # change exists to remove.
            state.verified = True
            state.note_evidence(f"{call.name}: {result.summary}")
        if not result.ok:
            state.note_evidence(f"{call.name}: {result.summary}")
        if call.name == "apply_patch" and result.ok:
            path = str(result.data.get("path", ""))
            if path and path not in state.changed_files:
                state.changed_files.append(path)
        if result.ok and tool.risk in (Risk.WRITE, Risk.DANGEROUS):
            state.note_mutation()  # new epoch; old verification is stale

        self.observer("observe", {"name": call.name, "ok": result.ok,
                                  "summary": result.summary})
        return result, False

    @staticmethod
    def _too_many_unknown(state: AgentState) -> bool:
        return sum(1 for h in state.history if h.verdict == "unknown") >= 3


def format_report(result: RunResult) -> str:
    state = result.state
    lines = [result.answer.strip() or "(no answer produced)", ""]
    lines.append("--- run summary ---")
    lines.append(f"outcome: {result.outcome.value}")
    lines.extend(state.summary_lines())
    if result.routing and result.routing.available:
        plan = result.routing
        line = f"routing: {plan.skill or 'no skill'} -> {plan.first_tier}"
        if plan.escalated:
            line += f", escalated to {plan.final_tier} ({plan.escalation_reason})"
        lines.append(line)
    tiers = state.metrics.tier_stats
    if tiers and tiers.get("cheap_call_share") is not None:
        lines.append(
            f"cheap tier served {tiers['cheap_call_share']:.0%} of model calls"
        )
    if state.evidence:
        lines.append("evidence:")
        lines.extend(f"  - {e}" for e in state.evidence[-8:])
    if state.warnings:
        lines.append("warnings:")
        lines.extend(f"  - {w}" for w in state.warnings)
    return "\n".join(lines)


def dump_transcript(result: RunResult, path: Path) -> None:
    path.write_text(
        json.dumps(
            {"state": result.state.as_dict(), "messages": result.messages},
            indent=2,
            default=str,
        ),
        encoding="utf-8",
    )
