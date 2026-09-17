#!/usr/bin/env python3
"""Fail-closed aggregation and projection rules for the energy sidecar study.

This module does not change E1/E2/E3. It decides only whether energy evidence is
complete enough to compare and whether a derived deployment projection is
allowed to exist.
"""
from __future__ import annotations

from collections import defaultdict
import json
import math
from pathlib import Path
import random
import statistics
import sys
from typing import Any, Iterable, Mapping, Sequence

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from evaluation.endpoints import (  # noqa: E402
    MAX_ATTEMPTS_PER_TASK_CONDITION,
    VALID_DRAWS_PER_TASK_CONDITION,
    verified_completion,
)
from evaluation.task_contracts import CASES  # noqa: E402

EXPECTED_CASES = tuple(case.name for case in CASES)
EXPECTED_CONDITIONS = ("control", "narrow", "skill")
SENSOR_DOMAIN_STATES = frozenset(
    {
        "unverified",
        "target_responsive",
        "documented_contains_target",
        "proxy_only",
        "documented_excludes_target",
    }
)
CLAIM_ELIGIBLE_SENSOR_DOMAIN = "documented_contains_target"
MIN_INDEPENDENT_PROJECTION_RUNS = 3
MIN_IDLE_DURATION_SECONDS = 3600.0
COUNTERBALANCED_DEVICE_ORDERS = (
    ("CPU", "GPU", "NPU"),
    ("GPU", "NPU", "CPU"),
    ("NPU", "CPU", "GPU"),
)


def _finite_nonnegative(value: Any) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(float(value))
        and float(value) >= 0
    )


def sensor_domain_allows_efficiency_claim(
    state: str | None, evidence: str | None = None
) -> bool:
    return (
        state == CLAIM_ELIGIBLE_SENSOR_DOMAIN
        and isinstance(evidence, str)
        and bool(evidence.strip())
    )


def counterbalanced_device_order(repetition_index: int) -> tuple[str, str, str]:
    if (
        not isinstance(repetition_index, int)
        or isinstance(repetition_index, bool)
        or repetition_index < 0
    ):
        raise ValueError("repetition index must be a non-negative integer")
    return COUNTERBALANCED_DEVICE_ORDERS[
        repetition_index % len(COUNTERBALANCED_DEVICE_ORDERS)
    ]


def _declared_attempts(
    mapping: Mapping[Any, Any] | None, case: str, condition: str
) -> int | None:
    if mapping is None:
        return None
    for key in ((case, condition), f"{case}|{condition}", f"{case}:{condition}"):
        if key in mapping:
            value = mapping[key]
            return value if isinstance(value, int) and not isinstance(value, bool) else None
    return None


def _row_e3(row: dict[str, Any]) -> bool | None:
    direct = row.get("verified_completion")
    if isinstance(direct, bool):
        return direct
    endpoints = row.get("endpoints")
    if isinstance(endpoints, dict) and isinstance(
        endpoints.get("verified_completion"), bool
    ):
        return endpoints["verified_completion"]
    try:
        return verified_completion(row)
    except Exception:
        return None


def _energy_observation(row: dict[str, Any]) -> dict[str, Any] | None:
    value = row.get("energy")
    if isinstance(value, dict):
        return value
    # Explicit compatibility path for a joined sidecar represented flatly.
    if "energy_joules" in row:
        return {
            name: row.get(name)
            for name in (
                "energy_joules",
                "comparison_eligible",
                "power_source",
                "sensor_domain_state",
                "sensor_domain_evidence",
                "measurement_quality",
                "sample_hz",
                "min_sample_hz",
            )
        }
    return None


def _cell_summary(
    rows: Sequence[dict[str, Any]], *, declared_attempts: int | None
) -> dict[str, Any]:
    reasons: list[str] = []
    attempts = [row.get("attempt") for row in rows]
    if any(not isinstance(v, int) or isinstance(v, bool) or v < 0 for v in attempts):
        return {
            "protocol_complete": False,
            "reasons": ["attempt index missing, negative or non-integer"],
            "decision_rows": [],
        }
    if len(set(attempts)) != len(attempts):
        reasons.append("duplicate attempt index")
    ordered = sorted(rows, key=lambda row: row["attempt"])
    observed = [row["attempt"] for row in ordered]
    if observed != list(range(len(ordered))):
        reasons.append(f"attempt sequence is not contiguous from zero: {observed}")
    if len(ordered) > MAX_ATTEMPTS_PER_TASK_CONDITION:
        reasons.append("attempt count exceeds bounded protocol")
    if declared_attempts is None:
        reasons.append("declared attempt count missing")
    elif declared_attempts != len(ordered):
        reasons.append(
            f"declared attempt count {declared_attempts} does not match observed {len(ordered)}"
        )

    counted = [row for row in ordered if row.get("counted") is True]
    decision_rows = counted[:VALID_DRAWS_PER_TASK_CONDITION]
    if len(counted) >= VALID_DRAWS_PER_TASK_CONDITION:
        third_attempt = decision_rows[-1]["attempt"]
        if ordered[-1]["attempt"] != third_attempt:
            reasons.append("collection continued after the third decision draw")
        if len(counted) > VALID_DRAWS_PER_TASK_CONDITION:
            reasons.append("more than three decision draws were collected")
        terminal = ordered[-1]["attempt"] == third_attempt
    else:
        terminal = len(ordered) == MAX_ATTEMPTS_PER_TASK_CONDITION
        if not terminal:
            reasons.append(
                "cell stopped before three decision draws or five bounded attempts"
            )

    invalid = [row for row in ordered if row.get("counted") is not True]
    valid = [row for row in ordered if row.get("validity") == "valid"]
    tampered = [row for row in ordered if row.get("oracle_tampered") is True]
    return {
        "protocol_complete": not reasons and terminal,
        "reasons": reasons,
        "attempts": len(ordered),
        "attempts_declared": declared_attempts,
        "decision_draws": len(decision_rows),
        "fixed_decision_set_available": (
            len(decision_rows) == VALID_DRAWS_PER_TASK_CONDITION
        ),
        "invalid_attempts": len(invalid),
        "valid_attempts": len(valid),
        "oracle_tampered_attempts": len(tampered),
        "decision_rows": decision_rows,
        "ordered_rows": ordered,
    }


def summarize_block(
    rows: Iterable[dict[str, Any]],
    *,
    attempts_declared: Mapping[Any, Any] | None,
    termination_reason: str,
) -> dict[str, Any]:
    """Summarise one preregistered workload block, refusing incomplete work.

    `block_complete` is a protocol statement, not an energy statement. A block can
    be protocol-complete while an energy observation is missing, and then the
    coding result remains valid while the energy comparison is unavailable.
    """
    rows = list(rows)
    reasons: list[str] = []
    if termination_reason != "completed":
        reasons.append(
            f"block termination reason is {termination_reason!r}, not 'completed'"
        )

    expected_cases = EXPECTED_CASES
    expected_conditions = EXPECTED_CONDITIONS
    expected = {
        (case, condition)
        for case in expected_cases
        for condition in expected_conditions
    }
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    malformed = 0
    for row in rows:
        case, condition = row.get("case"), row.get("condition")
        if not isinstance(case, str) or not isinstance(condition, str):
            malformed += 1
            continue
        grouped[(case, condition)].append(row)
    if malformed:
        reasons.append(f"{malformed} row(s) missing string case/condition")

    observed_cells = set(grouped)
    missing = sorted(expected - observed_cells)
    extra = sorted(observed_cells - expected)
    if missing:
        reasons.append(f"missing task-condition cells: {missing}")
    if extra:
        reasons.append(f"unexpected task-condition cells: {extra}")

    cell_summaries: dict[str, dict[str, Any]] = {}
    protocol_attempts = 0
    decision_rows: list[dict[str, Any]] = []
    invalid_attempts = 0
    tampered_attempts = 0
    all_cells_complete = True

    for case, condition in sorted(expected):
        key = f"{case}|{condition}"
        cell = _cell_summary(
            grouped.get((case, condition), []),
            declared_attempts=_declared_attempts(attempts_declared, case, condition),
        )
        serial = {
            k: v
            for k, v in cell.items()
            if k not in {"decision_rows", "ordered_rows"}
        }
        cell_summaries[key] = serial
        if not cell.get("protocol_complete"):
            all_cells_complete = False
        protocol_attempts += int(cell.get("attempts", 0))
        invalid_attempts += int(cell.get("invalid_attempts", 0))
        tampered_attempts += int(cell.get("oracle_tampered_attempts", 0))
        decision_rows.extend(cell.get("decision_rows", []))

    block_complete = not reasons and all_cells_complete

    energy_rows = [row for cell in grouped.values() for row in cell]
    observations = [_energy_observation(row) for row in energy_rows]
    telemetry_complete = bool(energy_rows) and all(
        isinstance(obs, dict)
        and _finite_nonnegative(obs.get("energy_joules"))
        and obs.get("comparison_eligible") is True
        for obs in observations
    )
    power_sources = sorted(
        {
            str(obs.get("power_source"))
            for obs in observations
            if isinstance(obs, dict) and obs.get("power_source")
        }
    )
    domain_states = sorted(
        {
            str(obs.get("sensor_domain_state"))
            for obs in observations
            if isinstance(obs, dict) and obs.get("sensor_domain_state")
        }
    )
    mains_only = power_sources == ["mains"]
    domain_claim_eligible = bool(observations) and all(
        isinstance(obs, dict)
        and sensor_domain_allows_efficiency_claim(
            obs.get("sensor_domain_state"), obs.get("sensor_domain_evidence")
        )
        for obs in observations
    )

    protocol_energy = None
    if telemetry_complete:
        protocol_energy = sum(
            float(obs["energy_joules"])
            for obs in observations
            if obs is not None
        )

    fixed_decision_set_complete = (
        len(decision_rows) == len(expected) * VALID_DRAWS_PER_TASK_CONDITION
    )
    decision_observations = [_energy_observation(row) for row in decision_rows]
    fixed_energy = None
    if fixed_decision_set_complete and all(
        isinstance(obs, dict)
        and _finite_nonnegative(obs.get("energy_joules"))
        and obs.get("comparison_eligible") is True
        for obs in decision_observations
    ):
        fixed_energy = sum(
            float(obs["energy_joules"])
            for obs in decision_observations
            if obs is not None
        )

    e3_values = [_row_e3(row) for row in decision_rows]
    e3_complete = bool(decision_rows) and all(
        isinstance(value, bool) for value in e3_values
    )
    e3_completions = (
        sum(value is True for value in e3_values) if e3_complete else None
    )
    e3_rate = (
        (e3_completions / len(e3_values))
        if e3_complete and e3_values
        else None
    )

    joules_per_attempt = (
        protocol_energy / protocol_attempts
        if protocol_energy is not None and protocol_attempts
        else None
    )
    joules_per_e3_completion = (
        fixed_energy / e3_completions
        if fixed_energy is not None
        and isinstance(e3_completions, int)
        and e3_completions > 0
        else None
    )

    return {
        "kind": "energy_workload_block_summary",
        "expected_tasks": len(set(expected_cases)),
        "attempted_tasks": len(
            {
                case
                for case, condition in observed_cells
                if (case, condition) in expected
            }
        ),
        "expected_task_condition_cells": len(expected),
        "observed_task_condition_cells": len(observed_cells & expected),
        "termination_reason": termination_reason,
        "block_complete": block_complete,
        "block_reasons": reasons,
        "protocol_attempts": protocol_attempts,
        "decision_draws": len(decision_rows),
        "infrastructure_invalid_attempts": invalid_attempts,
        "oracle_tampered_attempts": tampered_attempts,
        "fixed_decision_set_complete": fixed_decision_set_complete,
        "e3_draw_evidence_complete": e3_complete,
        "e3_draw_completions": e3_completions,
        "e3_draw_completion_rate": e3_rate,
        "telemetry_complete": telemetry_complete,
        "power_sources": power_sources,
        "sensor_domain_states": domain_states,
        "protocol_workload_energy_joules": protocol_energy,
        "fixed_decision_set_energy_joules": fixed_energy,
        "joules_per_attempt": joules_per_attempt,
        "joules_per_e3_completion": joules_per_e3_completion,
        "device_efficiency_comparison_eligible": bool(
            block_complete
            and telemetry_complete
            and mains_only
            and domain_claim_eligible
        ),
        "cell_summaries": cell_summaries,
    }


def _validate_component_runs(
    runs: Sequence[dict[str, Any]], *, kind: str
) -> None:
    if len(runs) < MIN_INDEPENDENT_PROJECTION_RUNS:
        raise ValueError(
            f"{kind} projection requires at least "
            f"{MIN_INDEPENDENT_PROJECTION_RUNS} independent runs"
        )
    session_ids = [run.get("session_id") for run in runs]
    if any(
        not isinstance(value, str) or not value.strip() for value in session_ids
    ):
        raise ValueError(f"{kind} runs require explicit non-empty session_id values")
    if len(set(session_ids)) != len(session_ids):
        raise ValueError(
            f"{kind} runs are not independent: repeated session_id detected"
        )


def project_fixed_window(
    active_runs: Sequence[dict[str, Any]],
    idle_runs: Sequence[dict[str, Any]],
    *,
    window_seconds: float,
    observed_tasks_per_second: float,
    arrival_multiplier: float | str,
    bootstrap_draws: int = 5000,
    seed: int = 0,
) -> dict[str, Any]:
    """Compose measured active + idle components into an explicitly derived range."""
    _validate_component_runs(active_runs, kind="active")
    _validate_component_runs(idle_runs, kind="idle")
    if not _finite_nonnegative(window_seconds) or window_seconds <= 0:
        raise ValueError("window_seconds must be positive")
    if (
        not _finite_nonnegative(observed_tasks_per_second)
        or observed_tasks_per_second <= 0
    ):
        raise ValueError("observed_tasks_per_second must be positive")
    if not isinstance(bootstrap_draws, int) or bootstrap_draws < 100:
        raise ValueError("bootstrap_draws must be at least 100")
    if arrival_multiplier != "saturation" and (
        not _finite_nonnegative(arrival_multiplier)
        or float(arrival_multiplier) <= 0
    ):
        raise ValueError("arrival_multiplier must be positive or 'saturation'")

    active_components = []
    for run in active_runs:
        energy = run.get("energy_joules")
        duration = run.get("duration_s")
        tasks = run.get("tasks_attempted")
        if (
            not _finite_nonnegative(energy)
            or not _finite_nonnegative(duration)
            or float(duration) <= 0
        ):
            raise ValueError(
                "active runs require finite non-negative energy and positive duration"
            )
        if not isinstance(tasks, int) or isinstance(tasks, bool) or tasks <= 0:
            raise ValueError("active runs require positive integer tasks_attempted")
        active_components.append(
            (float(energy) / tasks, float(duration) / tasks)
        )

    idle_watts = []
    idle_durations = []
    for run in idle_runs:
        watts = run.get("mean_watts")
        duration = run.get("duration_s")
        if (
            not _finite_nonnegative(watts)
            or not _finite_nonnegative(duration)
            or float(duration) <= 0
        ):
            raise ValueError(
                "idle runs require finite mean_watts and positive duration_s"
            )
        if float(duration) < MIN_IDLE_DURATION_SECONDS:
            raise ValueError(
                f"idle runs require at least "
                f"{MIN_IDLE_DURATION_SECONDS:.0f} seconds each"
            )
        idle_watts.append(float(watts))
        idle_durations.append(float(duration))

    rng = random.Random(seed)
    projected = []
    serviced_tasks = []
    demand_exceeded_capacity = False
    fixed_tasks_demanded = None
    if arrival_multiplier != "saturation":
        fixed_tasks_demanded = (
            observed_tasks_per_second
            * float(arrival_multiplier)
            * float(window_seconds)
        )

    for _ in range(bootstrap_draws):
        joules_per_task, seconds_per_task = rng.choice(active_components)
        idle_power = rng.choice(idle_watts)
        capacity_tasks = window_seconds / seconds_per_task

        if arrival_multiplier == "saturation":
            tasks_demanded = capacity_tasks
        else:
            tasks_demanded = fixed_tasks_demanded

        tasks_serviced = min(tasks_demanded, capacity_tasks)
        overload = tasks_demanded > capacity_tasks
        demand_exceeded_capacity = demand_exceeded_capacity or overload

        active_seconds = tasks_serviced * seconds_per_task
        if active_seconds >= window_seconds:
            active_energy = capacity_tasks * joules_per_task
            idle_seconds = 0.0
        else:
            active_energy = tasks_serviced * joules_per_task
            idle_seconds = window_seconds - active_seconds

        projected.append(active_energy + idle_power * idle_seconds)
        serviced_tasks.append(tasks_serviced)

    projected.sort()
    serviced_tasks.sort()

    def percentile(values: Sequence[float], p: float) -> float:
        index = int(round((len(values) - 1) * p))
        return values[index]

    return {
        "kind": "derived_fixed_window_energy_projection",
        "derived": True,
        "window_seconds": float(window_seconds),
        "arrival_multiplier": arrival_multiplier,
        "observed_tasks_per_second": float(observed_tasks_per_second),
        "active_independent_runs": len(active_runs),
        "idle_independent_runs": len(idle_runs),
        "idle_duration_seconds": idle_durations,
        "bootstrap_draws": bootstrap_draws,
        "tasks_demanded": (
            "saturation"
            if arrival_multiplier == "saturation"
            else float(fixed_tasks_demanded)
        ),
        "tasks_serviced": statistics.median(serviced_tasks),
        "tasks_serviced_predictive_interval": {
            "low_p05": percentile(serviced_tasks, 0.05),
            "high_p95": percentile(serviced_tasks, 0.95),
        },
        "demand_exceeded_capacity": demand_exceeded_capacity,
        "median_joules": statistics.median(projected),
        "interval_kind": "predictive_p05_p95",
        "predictive_interval_joules": {
            "low_p05": percentile(projected, 0.05),
            "high_p95": percentile(projected, 0.95),
        },
    }


def main(argv=None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    block = sub.add_parser("summarize-block")
    block.add_argument(
        "input", type=Path, help="JSON object containing rows/attempt declarations"
    )
    args = parser.parse_args(argv)

    if args.command == "summarize-block":
        payload = json.loads(args.input.read_text(encoding="utf-8"))
        forbidden = {"expected_cases", "expected_conditions"} & set(payload)
        if forbidden:
            parser.error(
                "input payload must not define "
                + ", ".join(sorted(forbidden))
                + "; workload expectations are preregistered"
            )
        result = summarize_block(
            payload.get("rows", []),
            attempts_declared=payload.get("attempts_declared"),
            termination_reason=payload.get("termination_reason", "missing"),
        )
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0 if result["block_complete"] else 2
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
