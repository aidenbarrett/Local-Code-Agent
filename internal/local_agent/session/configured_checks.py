"""Deterministic build and test routes: the controller runs the configured check.

"build it" and "run the tests" need no judgement. Letting a model decide whether to
call the build tool made the result depend on the model: a weak one could answer
"failed, missing evidence" without CMake ever running. For these routes the worker
is a fixed plan instead of a model.

The plan drives the same Orchestrator, tools and durable activity as any skill, so
verification, proof binding and the durable ``tool.finished`` facts that ``fix it``
reads keep exactly one owner. The plan only chooses which configured tool runs next
and what it cites; it never decides whether anything passed.
"""
from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final

from ..llm.client import tool_call
from ..llm.protocol import ChatResponse

# The procedure (tool allowlist, repository policy) the checks run under.
CONFIGURED_CHECK_SKILL: Final = "build-and-test"
RUN_BUILD_CHECK: Final = "run-build"
RUN_TEST_CHECK: Final = "run-tests"
# Tests run against a current build: run_test refuses stale or unbuilt binaries.
CONFIGURED_CHECK_STEPS: Final[Mapping[str, tuple[str, ...]]] = {
    RUN_BUILD_CHECK: ("build_target",),
    RUN_TEST_CHECK: ("build_target", "run_test"),
}
CONFIGURED_CHECKS: Final = frozenset(CONFIGURED_CHECK_STEPS)
_PLAN_VERSION: Final = 1


def configured_check_sha256(name: str, skill_sha256: str) -> str:
    """Admission fingerprint: the plan's identity bound to the skill bytes it runs under."""
    if name not in CONFIGURED_CHECK_STEPS:
        raise ValueError(f"not a configured check: {name}")
    identity = json.dumps(
        {"check": name, "plan": _PLAN_VERSION, "steps": list(CONFIGURED_CHECK_STEPS[name]),
         "skill_sha256": skill_sha256},
        sort_keys=True, separators=(",", ":"),
    )
    return hashlib.sha256(f"lca-configured-check:{identity}".encode()).hexdigest()


@dataclass(frozen=True, slots=True)
class _StepResult:
    name: str
    execution: str
    result: str
    summary: str

    @property
    def passed(self) -> bool:
        return self.execution == "ok" and self.result == "pass"

    @property
    def failed(self) -> bool:
        return self.execution == "ok" and self.result == "fail"


def _step_results(messages: Sequence[Mapping[str, Any]]) -> list[_StepResult]:
    results: list[_StepResult] = []
    for message in messages:
        if message.get("role") != "tool":
            continue
        try:
            payload = json.loads(str(message.get("content") or "{}"))
        except json.JSONDecodeError:
            payload = {}
        if not isinstance(payload, dict):
            payload = {}
        results.append(_StepResult(
            name=str(message.get("name") or ""),
            execution=str(payload.get("execution", "unknown")),
            result=str(payload.get("result", "unknown")),
            summary=str(payload.get("summary", "")),
        ))
    return results


class ConfiguredCheckPlan:
    """The worker for a configured check: runs each step in order, then reports.

    It stops at the first step that did not pass, because nothing after a failed
    build means anything. The claim it submits is what the tool results already say;
    the orchestrator still decides what they prove.
    """

    def __init__(self, check: str) -> None:
        if check not in CONFIGURED_CHECK_STEPS:
            raise ValueError(f"not a configured check: {check}")
        self._steps = CONFIGURED_CHECK_STEPS[check]

    def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,  # noqa: ARG002 - LLMClient shape
        max_tokens: int | None = None,  # noqa: ARG002 - LLMClient shape
    ) -> ChatResponse:
        done = _step_results(messages)
        if len(done) < len(self._steps) and all(step.passed for step in done):
            name = self._steps[len(done)]
            return ChatResponse(tool_calls=[tool_call(name, {}, f"check-{len(done)}")])
        evidence = [f"{step.name}:{index}" for index, step in enumerate(done)]
        last = done[-1] if done else None
        if last is not None and all(step.passed for step in done):
            claim = "success"
        elif last is not None and last.failed:
            claim = "failure"
        else:
            # The check could not be run (blocked, errored, timed out): no verdict.
            claim = "diagnosis"
        summary = "; ".join(f"{step.name}: {step.summary}" for step in done) or "nothing ran"
        return ChatResponse(tool_calls=[tool_call("submit_answer", {
            "claim": claim, "summary": summary, "evidence_ids": evidence,
        }, f"check-{len(done)}")])


__all__ = [
    "CONFIGURED_CHECKS",
    "CONFIGURED_CHECK_SKILL",
    "CONFIGURED_CHECK_STEPS",
    "RUN_BUILD_CHECK",
    "RUN_TEST_CHECK",
    "ConfiguredCheckPlan",
    "configured_check_sha256",
]
