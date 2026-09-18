from pathlib import Path
import sys


INTERNAL = Path(__file__).resolve().parents[2]
EVALUATION = INTERNAL / "evaluation"
if str(EVALUATION) not in sys.path:
    sys.path.insert(0, str(EVALUATION))

from run_evaluation import build_ledger, render_ledger  # noqa: E402


def _row(case: str, outcome: str) -> dict:
    return {
        "case": case,
        "counted": True,
        "validity": "valid",
        "outcome": outcome,
        "succeeded": outcome in {"pass", "escalated_pass"},
        "elapsed_s": 1.0,
        "routing": {},
        "metrics": {"llm_calls": 0},
    }


def test_ledger_partitions_every_counted_outcome_and_surfaces_unknown_values():
    rows = [
        _row("p", "pass"),
        _row("ep", "escalated_pass"),
        _row("ef", "escalated_fail"),
        _row("f", "fail"),
        _row("b", "blocked"),
        _row("u", "no_verdict"),
    ]
    ledger = build_ledger(rows)

    partition = (
        ledger["blocked"]
        + ledger["escalated_tasks"]
        + ledger["cheap_only_tasks"]
        + ledger["single_tier_tasks"]
        + ledger["unclassified"]
    )
    assert partition == ledger["tasks"] == len(rows)
    assert ledger["unclassified"] == 1
    assert ledger["unclassified_cases"] == ["u"]
    assert "Unclassified outcomes:  1  u" in render_ledger(ledger)
