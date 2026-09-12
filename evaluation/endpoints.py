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
MAX_ATTEMPTS_PER_TASK_CONDITION = 5

# The evaluator's typed outcome is kept separate from the legacy weighted
# `succeeded` bit. Both PASS forms mean the agent stack reached its goal.
SUCCESS_OUTCOMES = frozenset({"pass", "escalated_pass"})
REPAIR_CASES = frozenset({"compile-error-fix", "test-failure-fix"})
EFFICIENCY_FLAG_NAMES = ("did not halt", "was efficient")

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
        quality=("named what was wrong",),
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
        quality=("explained the fix",),
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


def engineering_technical_correct(row: dict[str, Any]) -> bool | None:
    """Technical result only, before task-aware scope contamination is applied.

    Repair tasks also require the evaluator's independent post-restore oracle.
    A model-side green build/test path cannot self-certify the repaired tree.
    """
    if row.get("counted") is not True:
        return None
    case = str(row.get("case"))
    contract = ENGINEERING_CONTRACTS.get(case)
    if contract is None:
        return None
    hard = _all_named_true(row.get("required_checks"), contract.required)
    quality = _all_named_true(row.get("checks"), contract.quality)
    if hard is None or quality is None:
        return None
    if not (hard and quality):
        return False

    if case in REPAIR_CASES:
        oracle = row.get("eval_verification")
        if not isinstance(oracle, dict) or not isinstance(oracle.get("ok"), bool):
            return None
        if oracle["ok"] is not True:
            return False
    return True


def engineering_obtained_out_of_scope(row: dict[str, Any]) -> bool | None:
    """Characterise a technically right answer obtained by a forbidden method."""
    technical = engineering_technical_correct(row)
    scope = row.get("scope_violation")
    if technical is None or not isinstance(scope, bool):
        return None
    return bool(technical and scope)


def engineering_correct(row: dict[str, Any]) -> bool | None:
    """Primary E1: technically correct without task-aware scope contamination.

    `scope_violation` is already computed from each task's forbidden tools. We
    keep the raw technical result separately, but a diagnosis discovered by
    editing the repository does not enter the normal engineering-correct count.
    """
    technical = engineering_technical_correct(row)
    scope = row.get("scope_violation")
    if technical is None or not isinstance(scope, bool):
        return None
    return bool(technical and not scope)


def contract_compliant(row: dict[str, Any]) -> bool | None:
    """E2: whether the model followed the requested interaction boundary.

    A real-but-hidden tool refusal remains descriptive containment evidence and
    is not double-counted here. An invented tool, an uncontained scope violation,
    or any forbidden reach is model non-compliance.
    """
    if row.get("counted") is not True:
        return None

    case = str(row.get("case"))
    contract = ENGINEERING_CONTRACTS.get(case)
    if contract is None:
        return None

    required_fields = (
        "submission_mode", "claim_ok", "forbidden_attempts",
        "scope_violation", "invented_tool_calls",
    )
    if any(name not in row for name in required_fields):
        return None

    unknown = row.get("cited_unknown")
    forbidden = row.get("forbidden_attempts")
    invented = row.get("invented_tool_calls")
    scope = row.get("scope_violation")
    if not isinstance(unknown, list) or not isinstance(forbidden, list):
        return None
    if not isinstance(invented, list) or not isinstance(scope, bool):
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
        and not invented
        and scope is False
        and citation_ok
        and restraint_ok
    )


def verified_completion(row: dict[str, Any]) -> bool | None:
    """E3, independent of the legacy weighted `succeeded` verdict.

    Verified completion is the conjunction of E1, E2, the typed agent outcome,
    and agreement with the independent verifier. The legacy bit remains exposed
    only as a characterization field while old and new accounting coexist.
    """
    if row.get("counted") is not True:
        return None
    e1 = engineering_correct(row)
    e2 = contract_compliant(row)
    outcome = row.get("outcome")
    disagreement = row.get("verification_disagreement")
    if not isinstance(e1, bool) or not isinstance(e2, bool):
        return None
    if not isinstance(outcome, str) or not isinstance(disagreement, bool):
        return None
    return bool(e1 and e2 and outcome in SUCCESS_OUTCOMES and not disagreement)


def efficiency(row: dict[str, Any]) -> dict[str, Any] | None:
    """E4: episode costs plus descriptive F-style quality flags.

    No efficiency flag is allowed to gate E1-E3. Token counts stay descriptive
    until their measurement provenance is explicit.
    """
    if row.get("counted") is not True:
        return None
    metrics = row.get("metrics") if isinstance(row.get("metrics"), dict) else {}
    checks = row.get("checks") if isinstance(row.get("checks"), dict) else {}
    elapsed = row.get("elapsed_s")
    tools = row.get("tool_calls")
    model_calls = metrics.get("llm_calls")
    if not isinstance(elapsed, (int, float)) or not isinstance(tools, int):
        return None
    if model_calls is not None and not isinstance(model_calls, int):
        return None
    flags = {
        name: checks[name]
        for name in EFFICIENCY_FLAG_NAMES
        if isinstance(checks.get(name), bool)
    }
    return {
        "elapsed_s": float(elapsed),
        "tool_calls": tools,
        "model_calls": model_calls,
        "flags": flags,
        "token_counts_used_for_decisions": False,
    }


def row_endpoints(row: dict[str, Any]) -> dict[str, Any]:
    legacy = row.get("succeeded")
    return {
        "engineering_technical_correct": engineering_technical_correct(row),
        "engineering_obtained_out_of_scope": engineering_obtained_out_of_scope(row),
        "engineering_correct": engineering_correct(row),
        "contract_compliant": contract_compliant(row),
        "verified_completion": verified_completion(row),
        "efficiency": efficiency(row),
        "legacy_succeeded": legacy if isinstance(legacy, bool) else None,
    }


def _select_decision_rows(cell: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """First three valid draws, bounded to five attempts, in attempt order."""
    attempts = [row.get("attempt") for row in cell]
    bad_attempt = any(not isinstance(v, int) or isinstance(v, bool) for v in attempts)
    duplicate_attempt = len({v for v in attempts if isinstance(v, int)}) != len(attempts)
    reasons: list[str] = []
    if bad_attempt:
        reasons.append("attempt index missing or non-integer")
    if duplicate_attempt:
        reasons.append("duplicate attempt index")
    if reasons:
        return [], {
            "attempts_total": len(cell),
            "attempts_considered": 0,
            "valid_attempts": 0,
            "invalid_attempts": len(cell),
            "invalid_attempt_rate": 1.0 if cell else 0.0,
            "extra_valid_draws": 0,
            "protocol_violation": True,
            "protocol_violation_reasons": reasons,
        }

    ordered = sorted(cell, key=lambda row: row["attempt"])
    if len(ordered) > MAX_ATTEMPTS_PER_TASK_CONDITION:
        reasons.append(
            f"more than {MAX_ATTEMPTS_PER_TASK_CONDITION} total attempts"
        )
    window = ordered[:MAX_ATTEMPTS_PER_TASK_CONDITION]
    valid = [row for row in window if row.get("counted") is True]
    selected = valid[:VALID_DRAWS_PER_TASK_CONDITION]
    extra_valid = max(0, len(valid) - VALID_DRAWS_PER_TASK_CONDITION)

    if len(selected) == VALID_DRAWS_PER_TASK_CONDITION:
        last_selected_attempt = selected[-1]["attempt"]
        later = [row for row in window if row["attempt"] > last_selected_attempt]
        if later:
            reasons.append("collection continued after the third valid draw")

    invalid = sum(row.get("counted") is not True for row in window)
    return selected, {
        "attempts_total": len(cell),
        "attempts_considered": len(window),
        "valid_attempts": len(valid),
        "invalid_attempts": invalid,
        "invalid_attempt_rate": round(invalid / len(window), 3) if window else 0.0,
        "extra_valid_draws": extra_valid,
        "protocol_violation": bool(reasons),
        "protocol_violation_reasons": reasons,
    }


def _majority(values: list[bool | None]) -> dict[str, Any]:
    observed = [value for value in values if isinstance(value, bool)]
    if len(values) != VALID_DRAWS_PER_TASK_CONDITION or len(observed) != len(values):
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
    """Aggregate the frozen pilot without pseudo-replication or optional stopping."""
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
        selected_rows: list[dict[str, Any]] = []
        protocol_violations: list[str] = []

        for case in sorted(ENGINEERING_CONTRACTS):
            cell = grouped.get((case, condition), [])
            selected, attempt_summary = _select_decision_rows(cell)
            selected_rows.extend(selected)
            if attempt_summary["protocol_violation"]:
                protocol_violations.append(case)
            endpoints = [row_endpoints(row) for row in selected]
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
                "attempts": attempt_summary,
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
        scope_violations = sum(bool(row.get("scope_violation")) for row in selected_rows)
        contaminated = sum(
            row_endpoints(row)["engineering_obtained_out_of_scope"] is True
            for row in selected_rows
        )

        condition_rows = [
            row for row in rows
            if row.get("condition") == condition
            and row.get("case") in ENGINEERING_CONTRACTS
        ]
        invalid_attempts = sum(row.get("counted") is not True for row in condition_rows)
        invalid_rate = (
            round(invalid_attempts / len(condition_rows), 3) if condition_rows else None
        )

        candidate = None
        if (
            not eng_indeterminate
            and not diagnostic_indeterminate
            and not protocol_violations
        ):
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
            "engineering_out_of_scope_rows": contaminated,
            "invalid_attempts": invalid_attempts,
            "attempts_total": len(condition_rows),
            "invalid_attempt_rate": invalid_rate,
            "protocol_violation_tasks": protocol_violations,
            "cheap_tier_candidate": candidate,
        }

    procedure_delta = None
    if "narrow" in by_condition and "skill" in by_condition:
        n = by_condition["narrow"]
        s = by_condition["skill"]
        if (
            not n["engineering_indeterminate"]
            and not s["engineering_indeterminate"]
            and not n["protocol_violation_tasks"]
            and not s["protocol_violation_tasks"]
        ):
            procedure_delta = (
                s["engineering_tasks_passed"] - n["engineering_tasks_passed"]
            )

    return {
        "policy": {
            "decision_unit": "task",
            "valid_draws_per_task_condition": VALID_DRAWS_PER_TASK_CONDITION,
            "max_attempts_per_task_condition": MAX_ATTEMPTS_PER_TASK_CONDITION,
            "task_success": "at least 2 of the first 3 valid draws",
            "invalid_draws": "missing observations; replace only within the five-attempt bound",
            "extra_valid_draws": "archived but never admitted to the decision",
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
