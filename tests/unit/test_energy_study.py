import pytest

from measurement.energy_study import (
    CLAIM_ELIGIBLE_SENSOR_DOMAIN,
    EXPECTED_CASES,
    EXPECTED_CONDITIONS,
    counterbalanced_device_order,
    project_fixed_window,
    sensor_domain_allows_efficiency_claim,
    summarize_block,
)


DOMAIN_EVIDENCE = "intel-power-domain-doc#package"


def _energy(joules=10.0):
    return {
        "energy_joules": joules,
        "comparison_eligible": True,
        "power_source": "mains",
        "sensor_domain_state": CLAIM_ELIGIBLE_SENSOR_DOMAIN,
        "sensor_domain_evidence": DOMAIN_EVIDENCE,
    }


def _complete_block():
    rows = []
    declared = {}
    for case in EXPECTED_CASES:
        for condition in EXPECTED_CONDITIONS:
            declared[f"{case}|{condition}"] = 3
            for attempt in range(3):
                rows.append(
                    {
                        "case": case,
                        "condition": condition,
                        "attempt": attempt,
                        "counted": True,
                        "validity": "valid",
                        "oracle_tampered": False,
                        "verified_completion": attempt != 2,
                        "energy": _energy(),
                    }
                )
    return rows, declared


def test_block_complete_requires_every_preregistered_cell():
    rows, declared = _complete_block()
    result = summarize_block(rows, attempts_declared=declared, termination_reason="completed")
    assert result["block_complete"] is True
    assert result["device_efficiency_comparison_eligible"] is True

    rows = [
        row
        for row in rows
        if not (row["case"] == EXPECTED_CASES[0] and row["condition"] == "control")
    ]
    result = summarize_block(rows, attempts_declared=declared, termination_reason="completed")
    assert result["block_complete"] is False
    assert result["device_efficiency_comparison_eligible"] is False


def test_block_complete_rejects_attempt_after_third_decision_draw():
    rows, declared = _complete_block()
    case = EXPECTED_CASES[0]
    declared[f"{case}|control"] = 4
    rows.append(
        {
            "case": case,
            "condition": "control",
            "attempt": 3,
            "counted": False,
            "validity": "invalid_server_error",
            "oracle_tampered": False,
            "verified_completion": False,
            "energy": _energy(1.0),
        }
    )
    result = summarize_block(rows, attempts_declared=declared, termination_reason="completed")
    assert result["block_complete"] is False
    assert any(
        "continued after the third decision draw" in reason
        for cell in result["cell_summaries"].values()
        for reason in cell["reasons"]
    )


def test_five_attempt_exhaustion_is_protocol_complete_but_not_fixed_decision_set():
    rows, declared = _complete_block()
    case = EXPECTED_CASES[0]
    rows = [
        row
        for row in rows
        if not (row["case"] == case and row["condition"] == "control")
    ]
    declared[f"{case}|control"] = 5
    for attempt in range(5):
        rows.append(
            {
                "case": case,
                "condition": "control",
                "attempt": attempt,
                "counted": attempt in (1, 3),
                "validity": "valid" if attempt in (1, 3) else "invalid_server_error",
                "oracle_tampered": False,
                "verified_completion": attempt == 1,
                "energy": _energy(2.0),
            }
        )
    result = summarize_block(rows, attempts_declared=declared, termination_reason="completed")
    assert result["block_complete"] is True
    assert result["fixed_decision_set_complete"] is False
    assert result["fixed_decision_set_energy_joules"] is None
    assert result["protocol_workload_energy_joules"] is not None


def test_missing_telemetry_never_becomes_comparable_energy():
    rows, declared = _complete_block()
    rows[0]["energy"]["energy_joules"] = None
    rows[0]["energy"]["comparison_eligible"] = False
    result = summarize_block(rows, attempts_declared=declared, termination_reason="completed")
    assert result["block_complete"] is True
    assert result["telemetry_complete"] is False
    assert result["protocol_workload_energy_joules"] is None
    assert result["device_efficiency_comparison_eligible"] is False


def test_sensor_responsiveness_is_not_domain_containment():
    assert sensor_domain_allows_efficiency_claim("target_responsive", "load-test") is False
    assert sensor_domain_allows_efficiency_claim("proxy_only", "intel-doc") is False
    assert sensor_domain_allows_efficiency_claim("documented_excludes_target", "intel-doc") is False
    assert sensor_domain_allows_efficiency_claim("documented_contains_target", None) is False
    assert sensor_domain_allows_efficiency_claim("documented_contains_target", "intel-doc") is True


def test_counterbalanced_rotation_is_deterministic():
    assert counterbalanced_device_order(0) == ("CPU", "GPU", "NPU")
    assert counterbalanced_device_order(1) == ("GPU", "NPU", "CPU")
    assert counterbalanced_device_order(2) == ("NPU", "CPU", "GPU")
    assert counterbalanced_device_order(3) == ("CPU", "GPU", "NPU")


def _active(session):
    return {
        "session_id": session,
        "energy_joules": 100.0,
        "duration_s": 20.0,
        "tasks_attempted": 2,
    }


def _idle(session):
    return {"session_id": session, "mean_watts": 5.0, "duration_s": 3600.0}


def test_projection_refuses_pseudoreplication():
    with pytest.raises(ValueError, match="repeated session_id"):
        project_fixed_window(
            [_active("same"), _active("same"), _active("same")],
            [_idle("i1"), _idle("i2"), _idle("i3")],
            window_seconds=3600,
            observed_tasks_per_second=0.01,
            arrival_multiplier=1.0,
        )


def test_projection_refuses_too_few_independent_runs():
    with pytest.raises(ValueError, match="at least 3 independent runs"):
        project_fixed_window(
            [_active("a1"), _active("a2")],
            [_idle("i1"), _idle("i2"), _idle("i3")],
            window_seconds=3600,
            observed_tasks_per_second=0.01,
            arrival_multiplier=1.0,
        )


def test_projection_is_explicitly_derived_and_ranged():
    result = project_fixed_window(
        [_active("a1"), _active("a2"), _active("a3")],
        [_idle("i1"), _idle("i2"), _idle("i3")],
        window_seconds=3600,
        observed_tasks_per_second=0.01,
        arrival_multiplier=0.25,
        bootstrap_draws=500,
    )
    assert result["derived"] is True
    assert result["active_independent_runs"] == 3
    assert result["idle_independent_runs"] == 3
    assert result["empirical_interval_joules"]["low_p05"] <= result["median_joules"]
    assert result["median_joules"] <= result["empirical_interval_joules"]["high_p95"]
