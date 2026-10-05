from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

import pytest

from devtools.check_static_standards import (
    BASELINE_SCHEMA,
    STATIC_ROOTS,
    compare,
    dump_baseline,
    load_baseline,
    lowered,
    main,
    measure,
    parse_mypy_json,
    parse_ruff_json,
)

A = ("mypy-linux", "internal/local_agent/a.py", "union-attr")
B = ("ruff", "internal/local_agent/b.py", "S603")
NEW_FILE = ("ruff", "internal/local_agent/new.py", "E501")


def test_unchanged_counts_are_clean() -> None:
    assert compare({A: 2, B: 1}, {A: 2, B: 1}).clean


def test_a_new_finding_in_a_recorded_file_is_a_regression() -> None:
    drift = compare({A: 3, B: 1}, {A: 2, B: 1})
    assert drift.regressions == ((A, 2, 3),)
    assert not drift.clean


def test_a_finding_in_a_file_absent_from_the_baseline_is_a_regression() -> None:
    # New files start clean: there is no allowance to spend.
    assert compare({A: 2, NEW_FILE: 1}, {A: 2}).regressions == ((NEW_FILE, 0, 1),)


def test_trading_one_code_for_another_in_the_same_file_is_still_a_regression() -> None:
    other_code = (A[0], A[1], "no-untyped-def")
    drift = compare({other_code: 1}, {A: 1})
    assert drift.regressions == ((other_code, 0, 1),)
    assert drift.improvements == ((A, 1, 0),)


def test_fixed_debt_must_be_locked_in() -> None:
    drift = compare({A: 1}, {A: 2, B: 1})
    assert drift.regressions == ()
    assert drift.improvements == ((A, 2, 1), (B, 1, 0))
    assert not drift.clean


def test_update_only_lowers() -> None:
    assert lowered({A: 1}, {A: 2, B: 1}) == {A: 1}


def test_update_refuses_to_absorb_a_regression() -> None:
    with pytest.raises(ValueError, match="refusing to raise"):
        lowered({A: 3}, {A: 2})
    with pytest.raises(ValueError, match="refusing to raise"):
        lowered({NEW_FILE: 1}, {})


def test_baseline_round_trips(tmp_path: Path) -> None:
    path = tmp_path / "baseline.json"
    path.write_text(dump_baseline({A: 2, B: 1}), encoding="utf-8")
    assert load_baseline(path) == {A: 2, B: 1}
    assert json.loads(path.read_text(encoding="utf-8"))["total"] == 3


@pytest.mark.parametrize("findings", [
    {"ruff": {"x.py": {"E501": 0}}},
    {"ruff": {"x.py": {"E501": -1}}},
    {"ruff": {"x.py": {"E501": True}}},
    {"ruff": {"x.py": {"E501": "3"}}},
    {"ruff": {"x.py": ["E501"]}},
    {"ruff": ["x.py"]},
])
def test_malformed_baselines_are_refused(tmp_path: Path, findings: object) -> None:
    path = tmp_path / "baseline.json"
    path.write_text(json.dumps({"schema": BASELINE_SCHEMA, "findings": findings}), encoding="utf-8")
    with pytest.raises(ValueError):
        load_baseline(path)


def test_a_baseline_of_another_schema_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "baseline.json"
    path.write_text(json.dumps({"schema": "something/1", "findings": {}}), encoding="utf-8")
    with pytest.raises(ValueError):
        load_baseline(path)


def test_mypy_json_counts_errors_per_platform_and_ignores_notes() -> None:
    lines = [
        json.dumps({"file": "internal\\local_agent\\a.py", "code": "union-attr", "severity": "error"}),
        json.dumps({"file": "internal/local_agent/a.py", "code": "union-attr", "severity": "error"}),
        json.dumps({"file": "internal/local_agent/a.py", "code": None, "severity": "note"}),
        "",
    ]
    counts = parse_mypy_json(lines, "win32")
    assert counts == {("mypy-win32", "internal/local_agent/a.py", "union-attr"): 2}


def test_mypy_json_with_an_unexpected_error_shape_is_refused() -> None:
    with pytest.raises(ValueError):
        parse_mypy_json([json.dumps({"file": "a.py", "code": None, "severity": "error"})], "linux")


def test_ruff_json_counts_findings_per_code() -> None:
    text = json.dumps([
        {"filename": "internal/local_agent/b.py", "code": "S603"},
        {"filename": "internal/local_agent/b.py", "code": "S603"},
        {"filename": "internal/local_agent/b.py", "code": "E501"},
    ])
    assert parse_ruff_json(text) == {B: 2, ("ruff", B[1], "E501"): 1}


def test_ruff_json_that_is_not_a_list_is_refused() -> None:
    with pytest.raises(ValueError):
        parse_ruff_json("{}")


def test_the_recorded_baseline_is_well_formed() -> None:
    root = Path(__file__).resolve().parents[3]
    counts = load_baseline(root / "internal" / "static-standards-baseline.json")
    assert all(checker in {"mypy-linux", "mypy-win32", "ruff"} for checker, _, _ in counts)
    # The gate itself is held to the standard it enforces.
    assert not any(file.startswith("internal/devtools/check_static_standards") for _, file, _ in counts)


@pytest.mark.parametrize("root", STATIC_ROOTS)
def test_every_static_root_is_explicitly_checked(root: str) -> None:
    """Serving and launcher files cannot evade the gate through include globs."""
    assert root in {
        "internal/local_agent",
        "internal/devtools",
        "internal/serving",
        "internal/scripts",
        "internal/terminal_ui.py",
    }


def test_known_finding_in_each_static_root_reaches_the_gate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A live root cannot disappear behind checker-default discovery or include globs."""
    repository = Path(__file__).resolve().parents[3]
    shutil.copy2(repository / "pyproject.toml", tmp_path / "pyproject.toml")
    expected: set[str] = set()
    for root in STATIC_ROOTS:
        path = tmp_path / root
        if path.suffix == ".py":
            path.parent.mkdir(parents=True, exist_ok=True)
        else:
            path.mkdir(parents=True, exist_ok=True)
            path /= "injected_static_finding.py"
        path.write_text("import os\n", encoding="utf-8")
        expected.add(path.relative_to(tmp_path).as_posix())

    monkeypatch.chdir(tmp_path)
    counts = measure(
        tmp_path,
        [sys.executable, "-m", "mypy"],
        [sys.executable, "-m", "ruff"],
    )

    found = {file for checker, file, code in counts if checker == "ruff" and code == "F401"}
    assert found == expected

    baseline = tmp_path / "internal" / "static-standards-baseline.json"
    baseline.parent.mkdir(parents=True, exist_ok=True)
    baseline.write_text(dump_baseline({}), encoding="utf-8")
    assert main([
        "--root", str(tmp_path),
    ]) == 1
    output = capsys.readouterr()
    assert "New static-standard findings" in output.err
    assert all(path in output.err for path in expected)
