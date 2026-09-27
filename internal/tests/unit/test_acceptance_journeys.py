"""The acceptance journey runner: the deterministic journeys, and that it fails closed.

These run the real Session Hub composition against real CMake builds on both CI
platforms, so the runner Aiden starts on the target machine has already been
exercised end to end on Linux and Windows. Model journeys need an endpoint and
must report UNKNOWN, with the reason, when there is none.
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "acceptance-journeys.py"
SPEC = importlib.util.spec_from_file_location("lca_acceptance_journeys", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
journeys = importlib.util.module_from_spec(SPEC)
sys.modules["lca_acceptance_journeys"] = journeys
SPEC.loader.exec_module(journeys)

DETERMINISTIC = ["J01-build-pass", "J02-build-fail", "J03-tests-fail", "J04-ambiguous", "J05-stop-build"]


def _report(output: Path) -> dict[str, dict[str, object]]:
    report = json.loads((output / "journeys.json").read_text(encoding="utf-8"))
    assert report["schema"] == "lca.acceptance-journeys/1"
    return {j["id"]: j for j in report["journeys"]}


def test_the_deterministic_journeys_pass_with_logs_and_no_model(tmp_path):
    out = tmp_path / "acc"
    # Exactly what the user runs without a model: every journey is attempted.
    assert journeys.main(["--output", str(out)]) == 0

    by_id = _report(out)
    for jid in DETERMINISTIC:
        assert by_id[jid]["status"] == "PASS", (jid, by_id[jid]["reason"], by_id[jid]["notes"])
        assert (out / "journeys" / jid / "transcript.txt").read_text(encoding="utf-8").strip()
        assert (out / "journeys" / jid / "events.jsonl").read_text(encoding="utf-8").strip()
    stop = by_id["J05-stop-build"]["measured"]
    assert stop["processes_before_stop"] >= 1 and stop["surviving_processes"] == []
    # Without --allow-model nothing that needs a model runs, and it says why.
    assert by_id["J08-fix-build"]["status"] == "UNKNOWN"
    assert "--allow-model" in by_id["J08-fix-build"]["reason"]
    summary = (out / "summary.txt").read_text(encoding="utf-8")
    assert "Product   PASS 5 / FAIL 0" in summary


def test_a_journey_whose_claim_does_not_hold_fails_the_run(tmp_path, monkeypatch):
    # The clean-build journey pointed at a compile error must FAIL, not pass or skip.
    monkeypatch.setattr(journeys, "JOURNEYS", [
        ("JX-wrong", "clean-build check on a broken tree", "product", "compile_error", False, {},
         journeys.j_build_pass),
    ])
    out = tmp_path / "acc"
    assert journeys.main(["--output", str(out)]) == 1
    wrong = _report(out)["JX-wrong"]
    assert wrong["status"] == "FAIL"
    assert "clean tree build was fail/verification_failed" in wrong["reason"]
