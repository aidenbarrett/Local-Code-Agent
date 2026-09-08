"""Contract-hardening layer for the orchestrator.

Keeps policy boundaries explicit: skill toolsets fail closed, citations use
canonical ids that are actually shown to the model, on-demand skill references
are retrievable, and uncertain automatic routing abstains instead of applying a
possibly wrong procedure.
"""

from __future__ import annotations

from typing import Any

from ..tools import TOOLSETS, UNIVERSAL_TOOLS
from ..tools.base import Reason, Risk, Tool, ToolError, ToolResult
from ..verification import ProofKind
from .orchestrator import Orchestrator as _BaseOrchestrator


REFERENCE_TOOL = "read_skill_reference"


class Orchestrator(_BaseOrchestrator):
    """Orchestrator with fail-closed skill and evidence contracts."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._reference_skill = None
        if REFERENCE_TOOL not in self.registry:
            self.registry.register(
                Tool(
                    name=REFERENCE_TOOL,
                    description=(
                        "Read one named reference belonging to the active skill. "
                        "Only references advertised by that skill are available."
                    ),
                    parameters={
                        "type": "object",
                        "properties": {"name": {"type": "string"}},
                        "required": ["name"],
                        "additionalProperties": False,
                    },
                    handler=self._read_skill_reference,
                    risk=Risk.READ,
                )
            )

    def _read_skill_reference(self, name: str) -> ToolResult:
        skill = self._reference_skill
        if skill is None:
            raise ToolError(
                "no active skill has references available",
                Reason.TOOL_NOT_ALLOWED,
            )
        if name not in skill.references:
            raise ToolError(
                f"reference {name!r} is not advertised by active skill {skill.name!r}; "
                f"available: {skill.references}",
                Reason.NOT_FOUND,
            )
        text = skill.reference(name)
        return ToolResult(
            ok=True,
            summary=f"loaded skill reference {name!r}",
            data={"name": name, "content": text},
        )

    def run(
        self,
        task: str,
        skill_name: str | None = None,
        no_skill: bool = False,
        verification_required: bool | None = None,
        condition: str = "skill",
        catalogue: bool = True,
    ):
        # Explicitly pinned experiment cells keep their exact semantics.
        if no_skill or condition == "control" or skill_name:
            return super().run(
                task,
                skill_name=skill_name,
                no_skill=no_skill,
                verification_required=verification_required,
                condition=condition,
                catalogue=catalogue,
            )

        chosen, score, confident = self.skills.route_with_confidence(task)
        if confident:
            return super().run(
                task,
                skill_name=chosen,
                verification_required=verification_required,
                condition=condition,
                catalogue=catalogue,
            )

        # Abstain from narrowing/procedure activation when the deterministic
        # router is guessing. Full registered tools are safer than confidently
        # selecting the wrong restricted world. Record the abstention so it
        # cannot be mistaken for a confident control route.
        result = super().run(
            task,
            condition="control",
            catalogue=catalogue,
            verification_required=verification_required,
        )
        if result.routing is not None:
            result.routing.skill_score = score
            result.routing.skill_confident = False
        result.state.warnings.append(
            f"automatic skill routing abstained: best candidate {chosen!r} "
            f"scored {score:.2f}; no procedure or skill narrowing was applied"
        )
        self.observer(
            "router_uncertain",
            {"skill": chosen, "score": round(score, 3), "ranking": self.skills.rank(task, top=3)},
        )
        return result

    def _run_once(self, task: str, skill: Any, *args: Any, **kwargs: Any):
        previous = self._reference_skill
        self._reference_skill = skill
        try:
            return super()._run_once(task, skill, *args, **kwargs)
        finally:
            self._reference_skill = previous

    def _toolset_for(
        self, name: str | None, skill: Any, no_skill: bool = False
    ) -> list[str]:
        if no_skill:
            return sorted(self.registry.names())

        if skill is not None:
            # A skill declaration is a security boundary. Unknown names are a
            # configuration error, never a reason to fall back to a broader
            # legacy toolset. An explicit empty list means exactly that.
            unknown = [tool for tool in skill.tools if tool not in self.registry]
            if unknown:
                raise ValueError(
                    f"skill {skill.name!r} declares unknown tools: {sorted(unknown)}"
                )
            chosen = list(skill.tools)
            if skill.references and REFERENCE_TOOL not in chosen:
                chosen.append(REFERENCE_TOOL)
        else:
            chosen = list(TOOLSETS.get(name or "", TOOLSETS["_default"]))

        for extra in UNIVERSAL_TOOLS:
            if extra in self.registry and extra not in chosen:
                chosen.append(extra)
        return chosen

    def _execute(self, call, state, speculative: bool = False):
        result, stop = super()._execute(call, state, speculative=speculative)
        if state.history:
            record = state.history[-1]
            if record.name == call.name:
                # Canonical, zero-based history index. Put the id in the payload
                # before the caller serialises the ToolResult back to the model.
                result.data = dict(result.data)
                result.data["evidence_id"] = f"{record.name}:{len(state.history) - 1}"
        return result, stop

    def _accept_answer(self, call, state):
        claim = str(call.arguments.get("claim", "diagnosis"))
        summary = str(call.arguments.get("summary", "")).strip()
        cited = [str(x) for x in (call.arguments.get("evidence_ids") or [])]

        table = {f"{h.name}:{i}": h for i, h in enumerate(state.history)}
        resolved = [table[token] for token in cited if token in table]
        unknown = [token for token in cited if token not in table]

        passing = {
            ProofKind.FULL_BUILD_PASS,
            ProofKind.FULL_TEST_PASS,
            ProofKind.TARGETED_BUILD_PASS,
            ProofKind.TARGETED_TEST_PASS,
        }
        passing_verification = [
            h
            for h in resolved
            if ProofKind(getattr(h, "proof", "no_current_proof")) in passing
        ]

        state.claim = claim
        state.cited_evidence = cited
        state.cited_unknown = unknown
        state.citation_schemes = {
            "canonical": len(resolved),
            "unknown": len(unknown),
            "chosen": "canonical",
        }
        state.cited_correctly = (
            not unknown and (claim != "success" or bool(passing_verification))
        )
        if claim == "success" and not passing_verification:
            state.warnings.append("claim of success cited no passing build or test call")
        if unknown:
            state.warnings.append(
                "cited evidence ids that do not exist in this run: " + ", ".join(unknown)
            )

        self.observer(
            "answer",
            {"claim": claim, "cited": cited, "verified": state.verified},
        )
        return summary, True
