from datetime import datetime, timedelta

from measurement.energy import DEFAULT_MIN_SAMPLE_HZ, measure


def _csv(path, step_seconds: float, count: int = 20, watts: float = 10.0):
    start = datetime(2026, 1, 2, 12, 0, 0)
    lines = ["Date,Time,CPU Package Power [W]"]
    for i in range(count):
        stamp = start + timedelta(seconds=i * step_seconds)
        lines.append(f"{stamp:%d.%m.%Y},{stamp:%H:%M:%S.%f},{watts}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return start


def test_sparse_sampling_is_rejected_not_annotated(tmp_path):
    csv = tmp_path / "slow.csv"
    start = _csv(csv, 2.0)
    result = measure(
        csv,
        "CPU Package Power [W]",
        "%d.%m.%Y %H:%M:%S.%f",
        start.timestamp(),
        (start + timedelta(seconds=20)).timestamp(),
        min_sample_hz=DEFAULT_MIN_SAMPLE_HZ,
        power_source="mains",
        sensor_domain_state="target_responsive",
    )
    assert result["energy_joules"] is None
    assert result["comparison_eligible"] is False
    assert result["measurement_quality"] == "rejected_sampling_rate"
    assert result["sample_hz"] == 0.5


def test_adequate_sampling_emits_energy(tmp_path):
    csv = tmp_path / "fast.csv"
    start = _csv(csv, 0.25, count=100)
    result = measure(
        csv,
        "CPU Package Power [W]",
        "%d.%m.%Y %H:%M:%S.%f",
        start.timestamp(),
        (start + timedelta(seconds=20)).timestamp(),
        min_sample_hz=DEFAULT_MIN_SAMPLE_HZ,
        power_source="mains",
        sensor_domain_state="documented_contains_target",
    )
    assert result["comparison_eligible"] is True
    assert result["measurement_quality"] == "sampled"
    assert result["sample_hz"] == 4.0
    assert abs(result["energy_joules"] - 200.0) < 1e-9

def test_preregistered_sample_floor_cannot_be_lowered(tmp_path):
    csv = tmp_path / "slow-policy-bypass.csv"
    start = _csv(csv, 2.0)
    result = measure(
        csv,
        "CPU Package Power [W]",
        "%d.%m.%Y %H:%M:%S.%f",
        start.timestamp(),
        (start + timedelta(seconds=20)).timestamp(),
        min_sample_hz=0.5,
        power_source="mains",
        sensor_domain_state="target_responsive",
    )
    assert result["energy_joules"] is None
    assert result["comparison_eligible"] is False
    assert result["measurement_quality"] == "invalid_sampling_policy"
    assert "preregistered" in result["reason"]
