from datetime import datetime
import json
import math
import sys

import pytest
from measurement import energy


def test_trapezoids_clip_window_and_report_hz():
    joules, hz = energy.integrate([(0, 10), (2, 30), (4, 10)], 1, 3)
    assert joules == 50
    assert hz == 0.5


def test_missing_edges_gaps_bad_values_and_reversed_samples_refuse():
    bad = [
        [(1, 10), (2, 10)],
        [(0, 10), (10, 10)],
        [(0, 10), (2, math.nan)],
        [(2, 10), (0, 10)],
        [(0, 10), (0, 10)],
        [(0, -1), (2, 10)],
    ]
    for samples in bad:
        with pytest.raises(ValueError):
            energy.integrate(samples, 0, 2)


def test_hwinfo_csv_requires_explicit_sensor_domain_qualification(tmp_path):
    p = tmp_path / "power.csv"
    p.write_text(
        "Date,Time,CPU Package Power [W]\n"
        "13.09.2026,12:00:00.000,10\n"
        "13.09.2026,12:00:02.000,30\n"
        "13.09.2026,12:00:04.000,10\n"
    )
    t = datetime(2026, 9, 13, 12).timestamp()
    measured = energy.measure(
        p,
        "CPU Package Power [W]",
        "%d.%m.%Y %H:%M:%S.%f",
        t + 1,
        t + 3,
    )
    assert measured["energy_joules"] == 50
    assert measured["measurement_quality"] == "sampled"
    assert measured["claim_eligible"] is False
    qualified = energy.measure(
        p,
        "CPU Package Power [W]",
        "%d.%m.%Y %H:%M:%S.%f",
        t + 1,
        t + 3,
        sensor_domain_status="confirmed_contains_target",
    )
    assert qualified["claim_eligible"] is True
    proxy = energy.measure(
        p,
        "CPU Package Power [W]",
        "%d.%m.%Y %H:%M:%S.%f",
        t + 1,
        t + 3,
        sensor_domain_status="proxy_only",
    )
    assert proxy["energy_joules"] == 50
    assert proxy["claim_eligible"] is False


def test_no_sampler_command_runs_and_existing_manifest_is_unchanged(tmp_path):
    original = tmp_path / "pre-run.json"
    original.write_text('{"identity":"original"}')
    output = tmp_path / "complete.json"
    rc = energy.main([
        "--out",
        str(output),
        "--run-manifest",
        str(original),
        "--",
        sys.executable,
        "-c",
        "print(1+1)",
    ])
    assert rc == 0
    data = json.loads(output.read_text())
    assert data["energy_joules"] is None
    assert data["measurement_quality"] == "unobserved"
    assert data["links"]["run_manifest"]["sha256"]
    assert data["links"]["run_manifest"]["name"] == "pre-run.json"
    assert "path" not in data["links"]["run_manifest"]
    assert original.read_text() == '{"identity":"original"}'
    with pytest.raises(FileExistsError):
        energy.main(["--out", str(output), "--", sys.executable, "-c", "print(2)"])


def test_duplicate_sensor_name_refuses(tmp_path):
    p = tmp_path / "ambiguous.csv"
    p.write_text("Date,Time,CPU Package Power [W],CPU Package Power [W]\n")
    data = energy.measure(
        p, "CPU Package Power [W]", "%d.%m.%Y %H:%M:%S.%f", 0, 1
    )
    assert data["measurement_quality"] == "unobserved"
    assert "found 2" in data["reason"]


def test_task_energy_derivation_is_energy_first():
    assert energy.joules_per_completed_task(120.0, 3) == 40.0
    assert energy.joules_per_completed_task(120.0, 0) is None
    with pytest.raises(ValueError):
        energy.joules_per_completed_task(120.0, -1)


def test_battery_drain_is_separate_from_package_energy():
    battery = energy.battery_delta(
        50000,
        47000,
        80,
        75,
        source="HWiNFO battery sensor",
        power_state="battery",
    )
    assert battery["drain_mwh"] == 3000
    assert battery["drain_percent"] == 5
    assert battery["claim_eligible"] is True
    with pytest.raises(ValueError):
        energy.battery_delta(47000, 50000, source="sensor", power_state="battery")
    unobserved = energy.battery_delta()
    assert unobserved["drain_mwh"] is None
    assert unobserved["claim_eligible"] is False


def test_command_manifest_with_synthetic_csv_sampler(tmp_path):
    import threading

    csv_path = tmp_path / "live.csv"
    done = threading.Event()
    ready = threading.Event()

    def logger():
        with csv_path.open("w") as fh:
            fh.write("Date,Time,CPU Package Power [W]\n")
            while not done.is_set():
                stamp = datetime.now().strftime("%d.%m.%Y,%H:%M:%S.%f")
                fh.write(stamp + ",10\n")
                fh.flush()
                ready.set()
                done.wait(0.02)

    thread = threading.Thread(target=logger)
    thread.start()
    ready.wait(timeout=3)
    try:
        out = tmp_path / "measured.json"
        assert energy.main([
            "--out",
            str(out),
            "--hwinfo-csv",
            str(csv_path),
            "--sensor-domain-status",
            "confirmed_contains_target",
            "--completed-tasks",
            "2",
            "--",
            sys.executable,
            "-c",
            "sum(i*i for i in range(1000000))",
        ]) == 0
        data = json.loads(out.read_text())
        assert math.isclose(
            data["energy_joules"], 10 * data["wall_seconds"], rel_tol=1e-6
        )
        assert math.isclose(
            data["joules_per_completed_task"], data["energy_joules"] / 2, rel_tol=1e-9
        )
        assert data["claim_eligible"] is True
        assert data["csv_name"] == "live.csv"
        assert "csv_path" not in data
    finally:
        done.set()
        thread.join(timeout=3)
