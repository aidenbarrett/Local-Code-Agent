from __future__ import annotations

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


def test_checkpoint_records_attempt_count_for_each_case(tmp_path):
    import json
    case = SimpleNamespace(name="case-a")
    out = tmp_path / "rows.json"
    run_all(
        [case], 3, lambda c, attempt: _row(c, attempt, True),
        out, {"condition": "narrow"}, echo=lambda *_: None, max_attempts=5,
    )
    payload = json.loads(out.read_text(encoding="utf-8"))
    assert payload["attempts_declared"] == {"case-a": 3}
