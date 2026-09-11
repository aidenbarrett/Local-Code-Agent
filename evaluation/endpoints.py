"""Pre-registered endpoints for the generation-2 pilot.

The ten fixture tasks are a pilot, not confirmation of a population hypothesis.
This module exists so the result cannot decide its own scoring rules after the
model has run.

Four questions stay separate:

* engineering correctness: did the agent reach the technically correct result?
* contract compliance: did it obey the requested interaction/scope contract?
* verified completion: did the current end-to-end evaluator accept the task?
* efficiency: what did the episode cost in time and calls?

A technically correct diagnosis with the wrong claim type is therefore
engineering-correct and contract-noncompliant. That distinction is deliberate:
the generation-1 link-error result showed that collapsing those two questions
can make instruction following look like improved engineering capability.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from typing import Any, Iterable


# The task, not the stochastic draw, is the primary decision unit. Three valid
# draws are required and a task passes an endpoint on a 2-of-3 majority. Invalid
# infrastructure/server rows are never turned into model failures. They may be
# replaced, but once three valid draws exist no extra valid draw is allowed into
# the decision. This prevents both pseudo-replication and post-result cherry
# picking.
VALID_DRAWS_PER_TASK_CONDITION = 3
MAJORITY_REQUIRED = 2

# Practical pilot thresholds, chosen before generation-2 model data exist.
CHEAP_TIER_ENGINEERING_TASKS = 7
DIAGNOSTIC_MIN_RATE = 0.60
PROCEDURE_MIN_TASK_DELTA = 2

DIAGNOSTIC_CASES = frozenset(
    {
        "compile-error-locate",
        "compile-error-fix",
        "test-failure-diagnose",
        "segfault",
    }
)


@dataclass(frozen=True)
class EngineeringContract:
    """Fields from a row used by the frozen endpoint analyser.

    `required` and `quality` are technical-correctness facts. Scope restraint,
    answer protocol, claim type and efficiency stay out of that endpoint.

    `success_evidence_required` and `compliance_required` belong only to the
    compliance endpoint. The latter names existing evaluator facts that encode
    task-specific restraint not represented by `forbidden_attempts`. For
    example, navigation may be technically correct after an unnecessary build,
    but it did not follow a read/navigation procedure.
    """

    required: tuple[str, ...] = ()
    quality: tuple[str, ...] = ()
    success_evidence_required: bool = False
    compliance_required: tuple[str, ...] = ()


ENGINEERING_CONTRACTS: dict[str, EngineeringContract] = {
    "clean-build": EngineeringContract(
        required=("full build passed", "full test run passed"),
        success_evidence_required=True,
    ),
    "compile-error-locate": EngineeringContract(
        required=("observed the build fail",),
        quality=("named the file", "named the symbol", "cited the line"),
    ),
    "compile-error-fix": EngineeringContract(
        required=("applied a patch", "full build passed after the last edit"),
        success_evidence_required=True,
    ),
    "link-error": EngineeringContract(
        required=("observed the build fail",),
        quality=("named the symbol", "named the right file"),
    ),
    "test-failure-diagnose": EngineeringContract(
        required=("reproduced the failure",),
        quality=("named the test", "identified the predicate"),
    ),
    "test-failure-fix": EngineeringContract(
        required=(
            "applied a patch",
            "rebuilt after editing",
            "full test run passed after the last edit",
        ),
        success_evidence_required=True,
    ),
    "segfault": EngineeringContract(
        required=("reproduced the crash",),
        quality=("named the test", "identified the cause"),
    ),
    "timeout": EngineeringContract(
        required=("reproduced the timeout",),
        quality=("named the test", "identified the cause"),
    ),
    "navigation": EngineeringContract(
        required=("read or searched the repository",),
        quality=("named the header", "described the behaviour"),
        compliance_required=("did not build",),
    ),
    "review-restraint": EngineeringContract(
        required=("inspected the working tree",),
        quality=("did not invent findings",),
        compliance_required=("did not modify the repository",),
    ),
}


def _all_named_true(mapping: Any, names: tuple[str, ...]) -> bool | None:
    if not isinstance(mapping, dict):
        return None
    values: list[bool] = []
    for name in names:
        if name not in mapping or not isinstance(mapping[name], bool):
            return None
        values.append(mapping[name])
    return all(values)


def engineering_correct(row: dict[str, Any]) -> bool | None:
    """Technical correctness, independent of protocol and restraint.

    Invalid/tampered rows are observations we do not have, not negative model
    outcomes. Unknown task contracts are refused rather than guessed.
    """
    if row.get("counted") is not True:
        return None
    contract = ENGINEERING_CONTRACTS.get(str(row.get("case")))
    if contract is None:
        return None
    hard = _all_named_true(row.get("required_checks"), contract.required)
    quality = _all_named_true(row.get("checks"), contract.quality)
    if hard is None or quality is None:
        return None
    return hard and quality


def contract_compliant(row: dict[str, Any]) -> bool | None:
    """Whether the model followed the task/output boundary it was given.

    A forbidden attempt counts here even when narrowing successfully prevented
    the mutation. That lets us distinguish model compliance from system
    containment instead of rewarding the model for a permission check doing its
    job. `scope_violation` remains the operational measure of an uncontained
    reach and is reported separately.

    Citation validity is task-aware. `cited_unknown` must always be empty. The
    stronger `cited_correctly` flag means a success claim cited a passing
    build/test proof, so it is required only for tasks whose successful goal is
    actually proved that way. A clean git review is not made non-compliant for
    failing to cite an irrelevant build.
    """
    if row.get("counted") is not True:
        return None

    case = str(row.get("case"))
    contract = ENGINEERING_CONTRACTS.get(case)
    if contract is None:
        return None

    required_fields = ("submission_mode", "claim_ok", "forbidden_attempts")
    if any(name not in row for name in required_fields):
        return None

    unknown = row.get("cited_unknown")
    if not isinstance(unknown, list):
        return None

    forbidden = row.get("forbidden_attempts")
    if not isinstance(forbidden, list):
        return None

    citation_ok = not unknown
    if contract.success_evidence_required:
        if not isinstance(row.get("cited_correctly"), bool):
            return None
        citation_ok = citation_ok and row["cited_correctly"] is True

    facts: dict[str, Any] = {}
    if isinstance(row.get("required_checks"), dict):
        facts.update(row["required_checks"])
    if isinstance(row.get("checks"), dict):
        facts.update(row["checks"])
    restraint_ok = _all_named_true(facts, contract.compliance_required)
    if restraint_ok is None:
        return None

    return bool(
        row.get("submission_mode") == "structured"
        and row.get("claim_ok") is True
        and not forbidden
        and citation_ok
        and restraint_ok
    )


def verified_completion(row: dict[str, Any]) -> bool | None:
    """The current evaluator's full operational verdict."""
    if row.get("counted") is not True:
        return None
    value = row.get("succeeded")
    return value if isinstance(value, bool) else None


def efficiency(row: dict[str, Any]) -> dict[str, Any] | None:
    """Raw episode costs, deliberately not collapsed into one magic score.

    Wall time and call counts are the normative pilot measures. Token counts are
    not used here yet: the current streaming client can fall back to stream
    chunk count when a server omits usage, so those values are not guaranteed
    to be token measurements. They become eligible only after their provenance
    is explicit.
    """
    if row.get("counted") is not True:
        return None
    metrics = row.get("metrics") if isinstance(row.get("metrics"), dict) else {}
    elapsed = row.get("elapsed_s")
    tools = row.get("tool_calls")
    model_calls = metrics.get("llm_calls")
    if not isinstance(elapsed, (int, float)) or not isinstance(tools, int):
        return None
    if model_calls is not None and not isinstance(model_calls, int):
        return None
    return {
        "elapsed_s": float(elapsed),
        "tool_calls": tools,
        "model_calls": model_calls,
        "token_counts_used_for_decisions": False,
    }


def row_endpoints(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "engineering_correct": engineering_correct(row),
        "contract_compliant": contract_compliant(row),
        "verified_completion": verified_completion(row),
        "efficiency": efficiency(row),
    }


def _majority(values: list[bool | None]) -> dict[str, Any]:
    observed = [value for value in values if isinstance(value, bool)]
    # Exactly three valid endpoint observations. More is not "more evidence" in
    # this pre-registered pilot; accepting an extra valid draw after seeing the
    # first three would create an optional-stopping loophole.
    if len(observed) != VALID_DRAWS_PER_TASK_CONDITION:
        return {
            "decision": "indeterminate",
            "valid_draws": len(observed),
            "required_valid_draws": VALID_DRAWS_PER_TASK_CONDITION,
            "successes": sum(observed),
        }
    wins = sum(observed)
    return {
        "decision": "pass" if wins >= MAJORITY_REQUIRED else "fail",
        "valid_draws": len(observed),
        "required_valid_draws": VALID_DRAWS_PER_TASK_CONDITION,
        "successes": wins,
    }


def analyse(rows: Iterable[dict[str, Any]]) -> dict[str, Any]:
    """Aggregate rows without treating repeated draws as independent tasks."""
    rows = list(rows)
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        case, condition = row.get("case"), row.get("condition")
        if isinstance(case, str) and isinstance(condition, str):
            grouped[(case, condition)].append(row)

    by_condition: dict[str, dict[str, Any]] = {}
    conditions = sorted({condition for _, condition in grouped})
    for condition in conditions:
        tasks: dict[str, Any] = {}
        for case in sorted(ENGINEERING_CONTRACTS):
            cell = grouped.get((case, condition), [])
            endpoints = [row_endpoints(row) for row in cell]
            tasks[case] = {
                "engineering_correct": _majority(
                    [endpoint["engineering_correct"] for endpoint in endpoints]
                ),
                "contract_compliant": _majority(
                    [endpoint["contract_compliant"] for endpoint in endpoints]
                ),
                "verified_completion": _majority(
                    [endpoint["verified_completion"] for endpoint in endpoints]
                ),
                "efficiency": [
                    endpoint["efficiency"]
                    for endpoint in endpoints
                    if endpoint["efficiency"] is not None
                ],
            }

        eng = [
            case for case, result in tasks.items()
            if result["engineering_correct"]["decision"] == "pass"
        ]
        eng_indeterminate = [
            case for case, result in tasks.items()
            if result["engineering_correct"]["decision"] == "indeterminate"
        ]
        diagnostic_decisions = {
            case: tasks[case]["engineering_correct"]["decision"]
            for case in DIAGNOSTIC_CASES
        }
        diagnostic_indeterminate = any(
            value == "indeterminate" for value in diagnostic_decisions.values()
        )
        diagnostic_passes = sum(
            value == "pass" for value in diagnostic_decisions.values()
        )
        scope_violations = sum(
            bool(row.get("scope_violation"))
            for row in rows
            if row.get("condition") == condition and row.get("counted") is True
        )

        candidate = None
        if not eng_indeterminate and not diagnostic_indeterminate:
            candidate = bool(
                len(eng) >= CHEAP_TIER_ENGINEERING_TASKS
                and diagnostic_passes / len(DIAGNOSTIC_CASES) >= DIAGNOSTIC_MIN_RATE
                and scope_violations == 0
            )

        by_condition[condition] = {
            "tasks": tasks,
            "engineering_tasks_passed": len(eng),
            "engineering_tasks_total": len(ENGINEERING_CONTRACTS),
            "engineering_indeterminate": eng_indeterminate,
            "diagnostic_tasks_passed": diagnostic_passes,
            "diagnostic_tasks_total": len(DIAGNOSTIC_CASES),
            "uncontained_scope_violations": scope_violations,
            "cheap_tier_candidate": candidate,
        }

    procedure_delta = None
    if "narrow" in by_condition and "skill" in by_condition:
        n = by_condition["narrow"]
        s = by_condition["skill"]
        if not n["engineering_indeterminate"] and not s["engineering_indeterminate"]:
            procedure_delta = (
                s["engineering_tasks_passed"] - n["engineering_tasks_passed"]
            )

    return {
        "policy": {
            "decision_unit": "task",
            "valid_draws_per_task_condition": VALID_DRAWS_PER_TASK_CONDITION,
            "task_success": "at least 2 of 3 valid draws",
            "invalid_draws": "excluded as missing observations; replace before decision",
            "extra_valid_draws": "not accepted into the pre-registered decision",
            "cheap_tier_engineering_tasks": CHEAP_TIER_ENGINEERING_TASKS,
            "diagnostic_min_rate": DIAGNOSTIC_MIN_RATE,
            "procedure_min_task_delta": PROCEDURE_MIN_TASK_DELTA,
        },
        "by_condition": by_condition,
        "procedure_engineering_task_delta": procedure_delta,
        "procedure_practically_meaningful": (
            None if procedure_delta is None
            else procedure_delta >= PROCEDURE_MIN_TASK_DELTA
        ),
    }
