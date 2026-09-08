"""Tests for the telemetry sampler and the one-command suite report."""

from __future__ import annotations

import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO / "devtools"))
sys.path.insert(0, str(REPO / "tests" / "evals"))


# ---------------------------------------------------------------- telemetry


def test_monitor_never_raises_and_always_reports():
    from telemetry import HostMonitor

    with HostMonitor(interval_s=0.05) as monitor:
        # Burn a little CPU so there is something to sample.
        end = time.monotonic() + 0.3
        total = 0
        while time.monotonic() < end:
            total += 1

    report = monitor.report.as_dict()
    assert report["duration_s"] > 0
    assert set(report["available"]) == {"psutil", "proc", "rapl"}
    # Every field is either a number or an explicit None. Nothing is invented.
    for key in ("mean_cpu_percent", "peak_cpu_percent", "peak_mem_used_gb",
                "package_energy_j", "average_watts"):
        assert key in report


def test_missing_counters_produce_a_note_not_a_crash(monkeypatch):
    import telemetry

    monkeypatch.setattr(telemetry, "_psutil", lambda: None)
    monkeypatch.setattr(telemetry, "_rapl_domains", lambda: [])
    monkeypatch.setattr(telemetry, "_proc_cpu_times", lambda: None)
    monkeypatch.setattr(telemetry, "_proc_mem_used_gb", lambda: None)

    with telemetry.HostMonitor(interval_s=0.05) as monitor:
        time.sleep(0.12)

    report = monitor.report
    assert report.package_energy_j is None
    assert report.average_watts() is None
    assert any("RAPL" in n for n in report.notes)
    assert any("psutil" in n for n in report.notes)


def test_energy_is_reported_when_counters_are_present(monkeypatch, tmp_path):
    import telemetry

    counter = tmp_path / "energy_uj"
    name = tmp_path / "name"
    name.write_text("package-0")
    counter.write_text("1000000")

    monkeypatch.setattr(telemetry, "_rapl_domains", lambda: [("package-0", counter)])
    with telemetry.HostMonitor(interval_s=0.05) as monitor:
        counter.write_text("4000000")  # 3 J consumed
        time.sleep(0.12)

    assert monitor.report.package_energy_j == 3.0
    assert monitor.report.average_watts() is not None


def test_wrapped_energy_counter_is_discarded(monkeypatch, tmp_path):
    import telemetry

    counter = tmp_path / "energy_uj"
    counter.write_text("9000000")
    monkeypatch.setattr(telemetry, "_rapl_domains", lambda: [("package-0", counter)])
    with telemetry.HostMonitor(interval_s=0.05) as monitor:
        counter.write_text("10")  # counter wrapped: the reading is unusable
        time.sleep(0.12)

    assert monitor.report.energy_j == {}
    assert monitor.report.package_energy_j is None


# -------------------------------------------------------------- suite report


def _payload(diag_score: float, ttft: float, energy: float | None = 900.0) -> dict:
    from eval_cases import CASES

    rows = [
        {
            "case": c.name,
            "score": diag_score,
            "tool_calls": 4,
            "elapsed_s": 42.0,
            "checks": {"named the file": diag_score > 0.5, "was efficient": False},
        }
        for c in CASES
    ]
    return {
        "config": {
            "label": "test config",
            "model": "m",
            "device": "CPU",
            "base_url": "fake://",
            "context_budget_tokens": 12000,
            "memory_note": "DDR4-3200 dual channel, 64 GB",
            "started_at": "now",
            "host": {"platform": "test", "processor": "test", "cpu_count": 8, "ram_gb": 64},
        },
        "benchmark": {
            "model": "m", "device": "CPU", "endpoint": "fake://",
            "host": {"platform": "test"},
            "prefill": [{"prompt_tokens": 4000, "median_ttft_s": ttft,
                         "median_prefill_tok_s": 300.0, "n": 3}],
            "median_decode_tok_s": 11.0,
            "cache": {"cold_ttft_s": ttft, "warm_ttft_s": 0.4, "speedup": 12.0,
                      "warm_cached_tokens": 3900, "mutated_ttft_s": ttft},
            "projection": None,
            "notes": ["warm-up call excluded"],
        },
        "evals": {"overall": diag_score, "diagnostic_score": diag_score, "rows": rows},
        "telemetry": {
            "package_energy_j": energy,
            "average_watts": 45.0 if energy else None,
            "peak_cpu_percent": 98.0,
            "peak_mem_used_gb": 31.4,
            "notes": [],
        },
        "task_seconds": {"median": 42.0, "p90": 51.0},
        "joules_per_task": round(energy / 9, 1) if energy else None,
        "diagnostic_cases": ["compile-error-fix", "compile-error-locate",
                             "segfault", "test-failure-diagnose"],
        "verdict": "placeholder",
    }


def test_verdict_kills_a_low_scorer():
    from run_suite import build_verdict

    payload = _payload(0.3, 2.0)
    verdict = build_verdict(payload["evals"], payload["benchmark"], 0.6, 10.0)
    assert "Not a daily driver" in verdict
    assert "prompt engineering will not rescue it" in verdict


def test_verdict_separates_accurate_but_slow():
    from run_suite import build_verdict

    payload = _payload(0.9, 25.0)
    verdict = build_verdict(payload["evals"], payload["benchmark"], 0.6, 10.0)
    assert "Batch tool" in verdict
    assert "overnight build triage" in verdict


def test_verdict_passes_a_fast_accurate_configuration():
    from run_suite import build_verdict

    payload = _payload(0.9, 2.0)
    verdict = build_verdict(payload["evals"], payload["benchmark"], 0.6, 10.0)
    assert "Usable in the loop" in verdict


def test_report_renders_with_energy():
    from run_suite import render_suite

    text = render_suite(_payload(0.9, 2.0))
    assert "energy per completed task" in text
    assert "DDR4-3200" in text
    assert "What it got wrong" in text
    assert "was efficient" in text
    assert "## Prefill" in text


def test_report_renders_without_energy_counters():
    from run_suite import render_suite

    text = render_suite(_payload(0.9, 2.0, energy=None))
    assert "energy per completed task" not in text
    assert "Headline" in text
