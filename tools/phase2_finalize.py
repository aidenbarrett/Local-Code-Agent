#!/usr/bin/env python3
"""One-shot driver for reconciling PR #7 before any generation-2 model row exists.

Lives only on the temporary driver branch. It edits the real phase-2 branch,
then the workflow runs the authoritative suite before anything is pushed.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path


def replace_once(text: str, old: str, new: str, label: str) -> str:
    count = text.count(old)
    if count != 1:
        raise RuntimeError(f"{label}: expected one match, found {count}")
    return text.replace(old, new, 1)


def replace_block(text: str, start: str, end: str, new: str, label: str) -> str:
    a = text.find(start)
    if a < 0:
        raise RuntimeError(f"{label}: start marker missing")
    b = text.find(end, a)
    if b < 0:
        raise RuntimeError(f"{label}: end marker missing")
    return text[:a] + new.rstrip() + "\n\n\n" + text[b:]


def edit_endpoints(repo: Path) -> None:
    path = repo / "evaluation/endpoints.py"
    text = path.read_text(encoding="utf-8")

    text = replace_once(
        text,
        "VALID_DRAWS_PER_TASK_CONDITION = 3\nMAJORITY_REQUIRED = 2\n",
        "VALID_DRAWS_PER_TASK_CONDITION = 3\n"
        "MAJORITY_REQUIRED = 2\n"
        "MAX_ATTEMPTS_PER_TASK_CONDITION = 5\n\n"
        "# The evaluator's typed outcome is kept separate from the legacy weighted\n"
        "# `succeeded` bit. Both PASS forms mean the agent stack reached its goal.\n"
        "SUCCESS_OUTCOMES = frozenset({\"pass\", \"escalated_pass\"})\n"
        "REPAIR_CASES = frozenset({\"compile-error-fix\", \"test-failure-fix\"})\n"
        "EFFICIENCY_FLAG_NAMES = (\"did not halt\", \"was efficient\")\n",
        "repeat constants",
    )

    text = replace_once(
        text,
        '    "compile-error-fix": EngineeringContract(\n'
        '        required=("applied a patch", "full build passed after the last edit"),\n'
        '        success_evidence_required=True,\n'
        '    ),',
        '    "compile-error-fix": EngineeringContract(\n'
        '        required=("applied a patch", "full build passed after the last edit"),\n'
        '        quality=("named what was wrong",),\n'
        '        success_evidence_required=True,\n'
        '    ),',
        "compile fix causal fact",
    )
    text = replace_once(
        text,
        '    "test-failure-fix": EngineeringContract(\n'
        '        required=(\n'
        '            "applied a patch",\n'
        '            "rebuilt after editing",\n'
        '            "full test run passed after the last edit",\n'
        '        ),\n'
        '        success_evidence_required=True,\n'
        '    ),',
        '    "test-failure-fix": EngineeringContract(\n'
        '        required=(\n'
        '            "applied a patch",\n'
        '            "rebuilt after editing",\n'
        '            "full test run passed after the last edit",\n'
        '        ),\n'
        '        quality=("explained the fix",),\n'
        '        success_evidence_required=True,\n'
        '    ),',
        "test fix causal fact",
    )

    engineering = '''def engineering_technical_correct(row: dict[str, Any]) -> bool | None:
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
'''
    text = replace_block(text, "def engineering_correct(", "def contract_compliant(", engineering,
                         "engineering endpoint")

    compliant = '''def contract_compliant(row: dict[str, Any]) -> bool | None:
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
'''
    text = replace_block(text, "def contract_compliant(", "def verified_completion(", compliant,
                         "compliance endpoint")

    verified = '''def verified_completion(row: dict[str, Any]) -> bool | None:
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
'''
    text = replace_block(text, "def verified_completion(", "def efficiency(", verified,
                         "verified endpoint")

    efficiency = '''def efficiency(row: dict[str, Any]) -> dict[str, Any] | None:
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
'''
    text = replace_block(text, "def efficiency(", "def row_endpoints(", efficiency,
                         "efficiency endpoint")

    row_and_repeat = '''def row_endpoints(row: dict[str, Any]) -> dict[str, Any]:
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
'''
    text = replace_block(text, "def row_endpoints(", "def analyse(", row_and_repeat,
                         "row endpoints and repeat selection")

    analyse = '''def analyse(rows: Iterable[dict[str, Any]]) -> dict[str, Any]:
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
'''
    start = text.find("def analyse(")
    if start < 0:
        raise RuntimeError("analyse: marker missing")
    text = text[:start] + analyse.rstrip() + "\n"
    path.write_text(text, encoding="utf-8", newline="\n")


def edit_run_evaluation(repo: Path) -> None:
    path = repo / "evaluation/run_evaluation.py"
    text = path.read_text(encoding="utf-8")

    text = replace_once(
        text,
        "    identity: dict,\n    echo: Any = print,\n) -> tuple[list[dict], bool]:",
        "    identity: dict,\n    echo: Any = print,\n    max_attempts: int | None = None,\n) -> tuple[list[dict], bool]:",
        "run_all signature",
    )
    text = replace_once(
        text,
        "    rows: list[dict] = []\n    consecutive_harness = 0\n",
        "    if repeat < 1:\n"
        "        raise ValueError(\"repeat must be at least 1\")\n"
        "    if max_attempts is not None and max_attempts < repeat:\n"
        "        raise ValueError(\"max_attempts must be >= repeat\")\n"
        "    attempt_limit = repeat if max_attempts is None else max_attempts\n\n"
        "    rows: list[dict] = []\n"
        "    consecutive_harness = 0\n",
        "run_all validation",
    )
    text = replace_once(
        text,
        "    for case in selected:\n        for attempt in range(repeat):\n            row = run_one(case, attempt)\n",
        "    for case in selected:\n"
        "        valid_draws = 0\n"
        "        for attempt in range(attempt_limit):\n"
        "            row = run_one(case, attempt)\n",
        "run_all bounded loop",
    )
    text = replace_once(
        text,
        "            rows.append(row)\n            checkpoint(final=False)\n\n            if row.get(\"error\"):",
        "            rows.append(row)\n"
        "            if row.get(\"counted\") is True:\n"
        "                valid_draws += 1\n"
        "            checkpoint(final=False)\n\n"
        "            if row.get(\"error\"):",
        "run_all valid count",
    )
    text = replace_once(
        text,
        "            else:\n                consecutive_harness = 0\n    return rows, False\n",
        "            else:\n"
        "                consecutive_harness = 0\n\n"
        "            if max_attempts is not None and valid_draws >= repeat:\n"
        "                break\n"
        "    return rows, False\n",
        "run_all early stop",
    )
    text = replace_once(
        text,
        '    parser.add_argument("--repeat", type=int, default=1)\n',
        '    parser.add_argument("--repeat", type=int, default=1)\n'
        '    parser.add_argument(\n'
        '        "--max-attempts", type=int,\n'
        '        help="bounded replacement attempts per case; stop as soon as --repeat valid rows exist",\n'
        '    )\n',
        "max attempts parser",
    )
    text = replace_once(
        text,
        "    args = parser.parse_args()\n\n    model =",
        "    args = parser.parse_args()\n"
        "    if args.max_attempts is not None and args.max_attempts < args.repeat:\n"
        "        parser.error(\"--max-attempts must be >= --repeat\")\n\n"
        "    model =",
        "max attempts cli validation",
    )
    text = replace_once(
        text,
        '        "kill_threshold": KILL_THRESHOLD,\n',
        '        "kill_threshold": KILL_THRESHOLD,\n'
        '        "valid_draw_target": args.repeat,\n'
        '        "max_attempts_per_case": (args.max_attempts if args.max_attempts is not None else args.repeat),\n',
        "repeat identity",
    )
    text = replace_once(
        text,
        "        out_path, identity,\n    )\n",
        "        out_path, identity,\n"
        "        max_attempts=args.max_attempts,\n"
        "    )\n",
        "run_all call",
    )
    path.write_text(text, encoding="utf-8", newline="\n")


def edit_provenance(repo: Path) -> None:
    path = repo / "local_agent/provenance.py"
    text = path.read_text(encoding="utf-8")
    text = replace_once(
        text,
        '    run_case_source = _function_source(evaluator, "run_case")\n'
        '    required = (task_contracts, oracle, endpoints)\n'
        '    if run_case_source is None or not all(path.is_file() for path in required):\n',
        '    run_case_source = _function_source(evaluator, "run_case")\n'
        '    run_all_source = _function_source(evaluator, "run_all")\n'
        '    required = (task_contracts, oracle, endpoints)\n'
        '    if (run_case_source is None or run_all_source is None\n'
        '            or not all(path.is_file() for path in required)):\n',
        "outcome hash run_all source",
    )
    text = replace_once(
        text,
        '        ("evaluation/endpoints.py", endpoints.read_text(encoding="utf-8")),\n'
        '        ("evaluation.run_evaluation.run_case", run_case_source),\n',
        '        ("evaluation/endpoints.py", endpoints.read_text(encoding="utf-8")),\n'
        '        ("evaluation.run_evaluation.run_case", run_case_source),\n'
        '        ("evaluation.run_evaluation.run_all", run_all_source),\n',
        "outcome hash parts",
    )
    path.write_text(text, encoding="utf-8", newline="\n")


def edit_manifest(repo: Path) -> None:
    path = repo / "measurement/capture_run_manifest.py"
    text = path.read_text(encoding="utf-8")
    text = replace_once(
        text,
        '                "base_prompt_sha256": instrument_declared.get("base_prompt_sha256"),\n'
        '            },\n',
        '                "base_prompt_sha256": instrument_declared.get("base_prompt_sha256"),\n'
        '                "outcome_contract_sha256": instrument_declared.get("outcome_contract_sha256"),\n'
        '            },\n',
        "manifest declared outcome identity",
    )
    path.write_text(text, encoding="utf-8", newline="\n")


def edit_launcher(repo: Path) -> None:
    path = repo / "measurement/run_experiment.sh"
    text = path.read_text(encoding="utf-8")
    text = replace_once(
        text,
        '    step "7. END-TO-END RUN ($PROFILE, 10 pilot cases, once each)"\n',
        '    step "7. END-TO-END RUN ($PROFILE, 10 pilot cases, 3 valid draws each; max 5 attempts)"\n',
        "launcher description",
    )
    text = replace_once(
        text,
        '    python evaluation/run_evaluation.py --profile "$PROFILE" --label "$RUN" $COND_FLAG \\\n'
        '        --out "$OUT/$RUN.json" --workdir "$HOME/local-agent-evals" \\\n',
        '    python evaluation/run_evaluation.py --profile "$PROFILE" --label "$RUN" $COND_FLAG \\\n'
        '        --repeat 3 --max-attempts 5 \\\n'
        '        --out "$OUT/$RUN.json" --workdir "$HOME/local-agent-evals" \\\n',
        "launcher bounded repeats",
    )
    path.write_text(text, encoding="utf-8", newline="\n")


def edit_prereg(repo: Path) -> None:
    path = repo / "docs/next-experiment-preregistration.md"
    text = path.read_text(encoding="utf-8")
    old = '''The **task** is the primary decision unit, not an individual stochastic draw.
Each task/condition is run until it has exactly **three valid draws**. A binary
endpoint passes that task on a **2-of-3 majority**.

Invalid harness, server or precondition rows are missing observations, never
model failures. They remain archived and may be replaced so that the cell has
three valid draws. Once three valid draws exist, an additional valid draw is not
admitted to the pre-registered decision. The analyser marks fewer or more than
three valid endpoint observations as indeterminate rather than choosing a
convenient subset.
'''
    new = '''The **task** is the primary decision unit, not an individual stochastic draw.
Each task/condition needs exactly **three valid draws** and a binary endpoint
passes that task on a **2-of-3 majority**. Collection is bounded to at most
**five total attempts** per task/condition. Attempts are ordered by their
recorded attempt index; the first three valid rows are the decision set.

Invalid harness, server or precondition rows are missing observations, never
model failures. They remain archived and may be replaced only inside that
five-attempt bound. Collection stops immediately when the third valid draw is
obtained. Fewer than three valid draws after five attempts is **indeterminate**.
Any accidental extra attempt is archived and reported as a protocol violation,
and no later valid row is admitted to the decision. Invalid-attempt rate is
reported separately as reliability/deployment evidence.
'''
    text = replace_once(text, old, new, "prereg repeat policy")

    text = replace_once(
        text,
        '''For diagnosis/navigation work this requires the task's observed evidence plus
the task-specific technical facts already scored by the evaluator. For fix/build
work it requires the task's deterministic technical completion gates. It does
not use `was efficient`, terminal claim correctness or scope restraint to
manufacture a capability result.
''',
        '''For diagnosis/navigation work this requires the task's observed evidence plus
the task-specific technical facts already scored by the evaluator. For repair
work it also requires the independent post-restore oracle to pass. Raw technical
correctness is retained descriptively, but a technically right answer obtained
through a task-aware `scope_violation` is labelled
`engineering_obtained_out_of_scope` and is excluded from the normal engineering-
correct pass count. Efficiency and terminal claim correctness do not manufacture
a capability result.
''',
        "prereg engineering endpoint",
    )
    text = replace_once(
        text,
        '''Did it follow the requested interaction boundary: structured submission,
correct claim type, valid evidence citation and no forbidden action attempt?

A forbidden action that the permission boundary successfully blocks still
counts as model non-compliance here. It does **not** become an uncontained scope
violation. That distinction is intentional: model behaviour and system
containment are different questions.
''',
        '''Did it follow the requested interaction boundary: structured submission,
correct claim type, valid evidence citation, no forbidden action attempt, no
uncontained `scope_violation`, and no invented tool call?

A forbidden action that the permission boundary successfully blocks still
counts as model non-compliance here. It does **not** become an uncontained scope
violation. A real-but-not-offered tool remains descriptive containment evidence
rather than being double-counted. Model behaviour and system containment are
different questions.
''',
        "prereg compliance endpoint",
    )
    text = replace_once(
        text,
        '''Did the complete operational contract pass under the current evaluator? This is
the existing `succeeded` verdict, including deterministic evidence, independent
evaluator checks, scope/tamper rules and claim requirements.
''',
        '''Did the complete operational contract pass? E3 is computed independently as
**E1 AND E2 AND a successful typed agent outcome AND no verification
disagreement**. The legacy weighted `succeeded` bit is retained only as a
characterisation field and cannot leak answer-quality or efficiency weighting
back into this endpoint.
''',
        "prereg verified endpoint",
    )
    text = replace_once(
        text,
        '''Wall time, model calls and tool calls are the normative pilot cost measures.
Retries/escalations are reported where present. Token counts are not used for a
pilot decision until the streaming client records whether they came from real
server usage/tokenisation or from an estimate; stream chunks must never be
silently treated as measured tokens.
''',
        '''Wall time, model calls and tool calls are the normative pilot cost measures.
Retries/escalations and the existing `did not halt` / `was efficient` check flags
are carried descriptively. None gates correctness. Token counts are not used for
a pilot decision until the streaming client records whether they came from real
server usage/tokenisation or from an estimate; stream chunks must never be
silently treated as measured tokens.
''',
        "prereg efficiency endpoint",
    )
    path.write_text(text, encoding="utf-8", newline="\n")


def edit_endpoint_tests(repo: Path) -> None:
    path = repo / "tests/unit/test_endpoint_analysis.py"
    text = path.read_text(encoding="utf-8")
    text = text.replace(
        'def _row(case="link-error", condition="narrow", *, engineering=True,\n'
        '         claim_ok=True, forbidden=None, succeeded=True, counted=True,\n'
        '         attempt=0, scope=False, cited_correctly=True, cited_unknown=None):',
        'def _row(case="link-error", condition="narrow", *, engineering=True,\n'
        '         claim_ok=True, forbidden=None, succeeded=True, counted=True,\n'
        '         attempt=0, scope=False, cited_correctly=True, cited_unknown=None,\n'
        '         outcome="pass", oracle_ok=True, disagreement=False, invented=None):'
    )
    text = replace_once(
        text,
        '        "succeeded": succeeded,\n        "scope_violation": scope,\n',
        '        "succeeded": succeeded,\n'
        '        "outcome": outcome,\n'
        '        "eval_verification": {"ok": oracle_ok},\n'
        '        "verification_disagreement": disagreement,\n'
        '        "scope_violation": scope,\n'
        '        "invented_tool_calls": list(invented or []),\n',
        "endpoint test row fields",
    )

    text = replace_once(
        text,
        '    assert endpoints["verified_completion"] is True\n',
        '    assert endpoints["verified_completion"] is False\n',
        "forbidden reach E3 expectation",
    )
    text = replace_once(
        text,
        '''def test_review_can_be_correct_but_noncompliant_if_it_mutates():
    row = _row(case="review-restraint", engineering=True, scope=True, succeeded=False)
    endpoints = row_endpoints(row)
    assert endpoints["engineering_correct"] is True
    assert endpoints["contract_compliant"] is False
''',
        '''def test_scope_contamination_is_characterised_but_excluded_from_primary_e1():
    row = _row(case="review-restraint", engineering=True, scope=True, succeeded=False)
    endpoints = row_endpoints(row)
    assert endpoints["engineering_technical_correct"] is True
    assert endpoints["engineering_obtained_out_of_scope"] is True
    assert endpoints["engineering_correct"] is False
    assert endpoints["contract_compliant"] is False
''',
        "scope contamination test",
    )
    text = replace_once(
        text,
        '        "model_calls": 5,\n        "token_counts_used_for_decisions": False,\n',
        '        "model_calls": 5,\n'
        '        "flags": {"did not halt": True, "was efficient": False},\n'
        '        "token_counts_used_for_decisions": False,\n',
        "efficiency expected flags",
    )

    old_more = '''def test_less_or_more_than_three_valid_draws_is_indeterminate():
    rows = _three("link-error", "narrow", 2)[:2]
    result = analyse(rows)["by_condition"]["narrow"]["tasks"]["link-error"]
    assert result["engineering_correct"]["decision"] == "indeterminate"

    rows = _three("link-error", "narrow", 3) + [
        _row("link-error", "narrow", engineering=True, attempt=3)
    ]
    result = analyse(rows)["by_condition"]["narrow"]["tasks"]["link-error"]
    assert result["engineering_correct"]["decision"] == "indeterminate"
'''
    new_more = '''def test_fewer_than_three_valid_draws_is_indeterminate():
    rows = _three("link-error", "narrow", 2)[:2]
    result = analyse(rows)["by_condition"]["narrow"]["tasks"]["link-error"]
    assert result["engineering_correct"]["decision"] == "indeterminate"


def test_extra_attempt_is_archived_but_never_changes_the_first_three_valid_draws():
    rows = _three("link-error", "narrow", 3) + [
        _row("link-error", "narrow", engineering=False, attempt=3)
    ]
    result = analyse(rows)["by_condition"]["narrow"]["tasks"]["link-error"]
    assert result["engineering_correct"]["decision"] == "pass"
    assert result["engineering_correct"]["successes"] == 3
    assert result["attempts"]["protocol_violation"] is True
    assert "collection continued after the third valid draw" in result["attempts"]["protocol_violation_reasons"]
'''
    text = replace_once(text, old_more, new_more, "repeat tests")

    # Append the contract-blocker regressions. They are deliberately explicit:
    # each corresponds to a pre-run semantic decision that must not drift.
    text += '''\n\ndef test_legacy_weighted_succeeded_cannot_define_verified_completion():
    row = _row(succeeded=False, outcome="pass", engineering=True)
    endpoints = row_endpoints(row)
    assert endpoints["legacy_succeeded"] is False
    assert endpoints["verified_completion"] is True


def test_verification_disagreement_blocks_e3_without_rewriting_e1_or_e2():
    row = _row(disagreement=True)
    endpoints = row_endpoints(row)
    assert endpoints["engineering_correct"] is True
    assert endpoints["contract_compliant"] is True
    assert endpoints["verified_completion"] is False


def test_repair_e1_requires_the_independent_oracle():
    row = _row(case="compile-error-fix", oracle_ok=False)
    assert row_endpoints(row)["engineering_correct"] is False
    row = _row(case="test-failure-fix", oracle_ok=False)
    assert row_endpoints(row)["engineering_correct"] is False


def test_repair_e1_includes_the_causal_explanation_checks():
    row = _row(case="compile-error-fix")
    row["checks"]["named what was wrong"] = False
    assert row_endpoints(row)["engineering_correct"] is False
    row = _row(case="test-failure-fix")
    row["checks"]["explained the fix"] = False
    assert row_endpoints(row)["engineering_correct"] is False


def test_scope_violation_and_invented_tool_calls_both_fail_e2():
    assert row_endpoints(_row(scope=True))["contract_compliant"] is False
    assert row_endpoints(_row(invented=["magic_tool"]))["contract_compliant"] is False


def test_invalid_replacements_are_bounded_to_five_attempts():
    rows = [
        _row("link-error", "narrow", counted=False, attempt=0),
        _row("link-error", "narrow", engineering=True, attempt=1),
        _row("link-error", "narrow", counted=False, attempt=2),
        _row("link-error", "narrow", engineering=True, attempt=3),
        _row("link-error", "narrow", counted=False, attempt=4),
    ]
    cell = analyse(rows)["by_condition"]["narrow"]["tasks"]["link-error"]
    assert cell["engineering_correct"]["decision"] == "indeterminate"
    assert cell["attempts"]["attempts_considered"] == 5
    assert cell["attempts"]["invalid_attempts"] == 3


def test_first_three_valid_rows_within_five_are_the_only_decision_set():
    rows = [
        _row("link-error", "narrow", counted=False, attempt=0),
        _row("link-error", "narrow", engineering=True, attempt=1),
        _row("link-error", "narrow", engineering=False, attempt=2),
        _row("link-error", "narrow", counted=False, attempt=3),
        _row("link-error", "narrow", engineering=True, attempt=4),
    ]
    cell = analyse(rows)["by_condition"]["narrow"]["tasks"]["link-error"]
    assert cell["engineering_correct"]["decision"] == "pass"
    assert cell["engineering_correct"]["successes"] == 2
    assert cell["attempts"]["protocol_violation"] is False
'''
    path.write_text(text, encoding="utf-8", newline="\n")


def edit_provenance_tests(repo: Path) -> None:
    path = repo / "tests/unit/test_provenance.py"
    text = path.read_text(encoding="utf-8")
    if "import hashlib\n" not in text:
        text = text.replace("from __future__ import annotations\n\n", "from __future__ import annotations\n\nimport hashlib\n")
    old = '''    run_case = provenance._function_source(
        REPO / "evaluation" / "run_evaluation.py", "run_case"
    )
    assert run_case is not None
    assert "required_ok" in run_case
    assert "claim_ok" in run_case
    assert "scope_violation" in run_case
    assert "verification_disagreement" in run_case
    assert (REPO / "evaluation" / "task_contracts.py").is_file()
    assert (REPO / "evaluation" / "oracle.py").is_file()
    endpoints = (REPO / "evaluation" / "endpoints.py").read_text(encoding="utf-8")
    assert "ENGINEERING_CONTRACTS" in endpoints
    assert "VALID_DRAWS_PER_TASK_CONDITION = 3" in endpoints
    assert "MAJORITY_REQUIRED = 2" in endpoints
'''
    new = '''    run_case = provenance._function_source(
        REPO / "evaluation" / "run_evaluation.py", "run_case"
    )
    run_all = provenance._function_source(
        REPO / "evaluation" / "run_evaluation.py", "run_all"
    )
    assert run_case is not None and run_all is not None
    assert "required_ok" in run_case
    assert "claim_ok" in run_case
    assert "scope_violation" in run_case
    assert "verification_disagreement" in run_case
    task_contracts = (REPO / "evaluation" / "task_contracts.py").read_text(encoding="utf-8")
    oracle = (REPO / "evaluation" / "oracle.py").read_text(encoding="utf-8")
    endpoints = (REPO / "evaluation" / "endpoints.py").read_text(encoding="utf-8")
    assert "ENGINEERING_CONTRACTS" in endpoints
    assert "VALID_DRAWS_PER_TASK_CONDITION = 3" in endpoints
    assert "MAX_ATTEMPTS_PER_TASK_CONDITION = 5" in endpoints

    parts = [
        ("evaluation/task_contracts.py", task_contracts),
        ("evaluation/oracle.py", oracle),
        ("evaluation/endpoints.py", endpoints),
        ("evaluation.run_evaluation.run_case", run_case),
        ("evaluation.run_evaluation.run_all", run_all),
    ]

    def digest(items):
        out = hashlib.sha256()
        for label, body in items:
            out.update(label.encode())
            out.update(b"\\0")
            out.update(body.encode())
            out.update(b"\\0")
        return out.hexdigest()

    assert digest(parts) == source
    mutated = list(parts)
    mutated[2] = (mutated[2][0], mutated[2][1] + "\\n# endpoint-policy mutation\\n")
    assert digest(mutated) != source
'''
    text = replace_once(text, old, new, "provenance contract coverage test")
    path.write_text(text, encoding="utf-8", newline="\n")


def edit_manifest_tests(repo: Path) -> None:
    path = repo / "tests/unit/test_run_manifest.py"
    text = path.read_text(encoding="utf-8")
    anchor = '''    assert payload["measurement_quality"]["actual_device"] == "unobserved"
    assert payload["tool_schema_archive"]
'''
    replacement = '''    assert payload["measurement_quality"]["actual_device"] == "unobserved"
    assert payload["tool_schema_archive"]
    assert payload["instrument"]["declared"]["outcome_contract_sha256"]
    assert (
        payload["instrument"]["declared"]["outcome_contract_sha256"]
        == payload["instrument"]["observed"]["outcome_contract_sha256"]
    )
'''
    text = replace_once(text, anchor, replacement, "manifest outcome hash test")
    path.write_text(text, encoding="utf-8", newline="\n")


def add_repeat_collection_tests(repo: Path) -> None:
    path = repo / "tests/unit/test_repeat_collection.py"
    if path.exists():
        raise RuntimeError("repeat collection test file already exists")
    path.write_text('''from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

REPO = Path(__file__).resolve().parent.parent.parent
EVAL = REPO / "evaluation"
sys.path.insert(0, str(EVAL))

from run_evaluation import run_all


def _row(case, attempt, counted):
    return {
        "case": case.name,
        "attempt": attempt,
        "counted": counted,
        "score": 1.0,
        "elapsed_s": 0.1,
        "succeeded": counted,
        "outcome": "pass" if counted else None,
        "required_checks": {},
        "checks": {},
    }


def test_bounded_collection_stops_as_soon_as_three_valid_draws_exist(tmp_path):
    case = SimpleNamespace(name="case-a")
    validity = [False, True, True, True, True]
    seen = []

    def run_one(c, attempt):
        seen.append(attempt)
        return _row(c, attempt, validity[attempt])

    rows, stopped = run_all(
        [case], 3, run_one, tmp_path / "rows.json", {},
        echo=lambda *_: None, max_attempts=5,
    )
    assert stopped is False
    assert seen == [0, 1, 2, 3]
    assert [row["attempt"] for row in rows] == [0, 1, 2, 3]
    assert sum(row["counted"] is True for row in rows) == 3


def test_bounded_collection_stops_at_five_when_three_valid_draws_never_arrive(tmp_path):
    case = SimpleNamespace(name="case-a")
    validity = [False, True, False, True, False]

    rows, stopped = run_all(
        [case], 3,
        lambda c, attempt: _row(c, attempt, validity[attempt]),
        tmp_path / "rows.json", {}, echo=lambda *_: None, max_attempts=5,
    )
    assert stopped is False
    assert len(rows) == 5
    assert sum(row["counted"] is True for row in rows) == 2


def test_legacy_unbounded_mode_still_runs_exactly_repeat_attempts(tmp_path):
    case = SimpleNamespace(name="case-a")
    rows, _ = run_all(
        [case], 2, lambda c, attempt: _row(c, attempt, False),
        tmp_path / "rows.json", {}, echo=lambda *_: None,
    )
    assert [row["attempt"] for row in rows] == [0, 1]
''', encoding="utf-8", newline="\n")


def edit_all(repo: Path) -> None:
    edit_endpoints(repo)
    edit_run_evaluation(repo)
    edit_provenance(repo)
    edit_manifest(repo)
    edit_launcher(repo)
    edit_prereg(repo)
    edit_endpoint_tests(repo)
    edit_provenance_tests(repo)
    edit_manifest_tests(repo)
    add_repeat_collection_tests(repo)


def finalize_instrument(repo: Path) -> None:
    code = (
        "import json,sys;sys.path.insert(0,'.');"
        "from local_agent import provenance as p;"
        "print(json.dumps({'source_sha256':p.source_sha256(),"
        "'base_prompt_sha256':p.base_prompt_sha256(),"
        "'outcome_contract_sha256':p.outcome_contract_sha256()}))"
    )
    raw = subprocess.check_output([sys.executable, "-c", code], cwd=repo, text=True)
    hashes = json.loads(raw.strip())
    if hashes["base_prompt_sha256"] != "f1fb88236b18c2aa21d396a91cdc878274005cb47d687b1442811c6a740f1559":
        raise RuntimeError(f"unexpected model-facing contract move: {hashes['base_prompt_sha256']}")
    if hashes["outcome_contract_sha256"] == "unavailable-no-source":
        raise RuntimeError("outcome contract source unavailable")

    path = repo / "INSTRUMENT.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    data["_comment"] = [
        "The declared identity of the instrument on this branch. CI recomputes the",
        "source, model-facing and outcome-facing identities from the working tree",
        "and fails when any differs from the values below.",
        "",
        "This file is deliberately outside the hashed surface so updating a",
        "declaration does not invalidate itself.",
        "",
        "Generation 2 has no collected model rows yet. outcome_contract_sha256 and",
        "the bounded repeat policy were frozen while generation 2 was still being",
        "prepared, so this does not create a generation-3 boundary. The first",
        "generation-2 model row must match all three identities.",
    ]
    data["generation"] = 2
    data["source_sha256"] = hashes["source_sha256"]
    data["base_prompt_sha256"] = hashes["base_prompt_sha256"]
    data["outcome_contract_sha256"] = hashes["outcome_contract_sha256"]
    data["how_to_recompute"] = (
        "python -c \"import sys; sys.path.insert(0,'.'); from local_agent import provenance as p; "
        "print(p.source_sha256()); print(p.base_prompt_sha256()); print(p.outcome_contract_sha256())\""
    )
    data["when_this_changes"] = [
        "source_sha256 moves for any change to hashed source. Update this file in the same commit.",
        "A source-only move does not automatically end a generation; archived rows are re-scored only when retained evidence supports it.",
        "",
        "base_prompt_sha256 moves when the model-facing contract changes.",
        "outcome_contract_sha256 moves when task contracts, oracle/evaluator semantics, endpoint definitions or bounded repeat policy change.",
        "Once rows exist under a generation, either contract-axis move ends that generation for confirmatory comparison.",
    ]
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8", newline="\n")
    print(json.dumps(hashes, indent=2))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("repo", type=Path)
    parser.add_argument("--finalize-instrument", action="store_true")
    args = parser.parse_args()
    repo = args.repo.resolve()
    if args.finalize_instrument:
        finalize_instrument(repo)
    else:
        edit_all(repo)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
