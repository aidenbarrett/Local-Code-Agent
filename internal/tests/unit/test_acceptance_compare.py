"""Comparison reads historical evidence without starting an acceptance run."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts.acceptance_compare import ComparisonError, comparison_text

SCHEMA = "lca.acceptance-journeys/1"


def _write(root: Path, name: str, outcomes: list[str], *, peak: int | None = 100,
           product: str = "PASS") -> Path:
    directory = root / name
    directory.mkdir()
    rows = [{"id": f"J10-fix-tests.r{i}", "kind": "model", "status": status,
             "tasks": [{"context_peak_tokens": peak}]} for i, status in enumerate(outcomes, 1)]
    rows.append({"id": "J01-build-pass", "kind": "product", "status": product})
    (directory / "journeys.json").write_text(json.dumps({
        "schema": SCHEMA, "model": name, "journeys": rows}), encoding="utf-8")
    return directory


def test_improvement_preserves_denominators_and_context(tmp_path: Path) -> None:
    old = _write(tmp_path, "old", ["MEASURED:fail"] * 3, peak=200)
    new = _write(tmp_path, "new", ["MEASURED:fixed"] * 2, peak=150)
    text = comparison_text(old, new, SCHEMA)
    assert "J10-fix-tests | fail 3/3 | fixed 2/2 | 200 | 150" in text
    assert "Old: PASS 1 / FAIL 0 / UNKNOWN 0" in text
    assert "New: PASS 1 / FAIL 0 / UNKNOWN 0" in text


def test_regression_and_unknown_are_reported_separately(tmp_path: Path) -> None:
    old = _write(tmp_path, "old", ["MEASURED:fixed"] * 3)
    new = _write(tmp_path, "new", ["MEASURED:fixed", "MEASURED:fail", "UNKNOWN"],
                 peak=None, product="FAIL")
    text = comparison_text(old, new, SCHEMA)
    assert "fixed 3/3 | UNKNOWN 1/3, fail 1/3, fixed 1/3 | 100 | unknown" in text
    assert "New: PASS 0 / FAIL 1 / UNKNOWN 0" in text


def test_missing_journey_is_explicit_not_a_zero_success_rate(tmp_path: Path) -> None:
    old = _write(tmp_path, "old", ["MEASURED:fixed"])
    new = _write(tmp_path, "new", [])
    assert "J10-fix-tests | fixed 1/1 | MISSING | 100 | unknown" in comparison_text(old, new, SCHEMA)


def test_bad_directory_names_the_missing_report(tmp_path: Path) -> None:
    with pytest.raises(ComparisonError, match="journeys.json"):
        comparison_text(tmp_path / "missing", tmp_path / "other", SCHEMA)


@pytest.mark.parametrize("change", ["schema", "duplicate", "peak", "status", "tasks", "json"])
def test_incompatible_or_malformed_evidence_is_refused(tmp_path: Path, change: str) -> None:
    old = _write(tmp_path, "old", ["MEASURED:fixed"])
    new = _write(tmp_path, "new", ["MEASURED:fixed"])
    path = new / "journeys.json"
    data = json.loads(path.read_text())
    if change == "schema":
        data["schema"] = "lca.acceptance-journeys/2"
    elif change == "duplicate":
        data["journeys"].append(data["journeys"][0])
    elif change == "peak":
        data["journeys"][0]["tasks"][0]["context_peak_tokens"] = True
    elif change == "status":
        data["journeys"][0]["status"] = "success"
    elif change == "tasks":
        data["journeys"][0]["tasks"] = ["bad"]
    path.write_text("{" if change == "json" else json.dumps(data))
    with pytest.raises(ComparisonError):
        comparison_text(old, new, SCHEMA)


def test_partial_context_is_labelled(tmp_path: Path) -> None:
    old = _write(tmp_path, "old", ["MEASURED:fixed", "MEASURED:fail"])
    new = _write(tmp_path, "new", ["MEASURED:fixed"])
    path = old / "journeys.json"
    data = json.loads(path.read_text())
    data["journeys"][1]["tasks"] = []
    path.write_text(json.dumps(data))
    assert "100 (partial) | 100" in comparison_text(old, new, SCHEMA)
