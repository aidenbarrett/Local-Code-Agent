from __future__ import annotations

import json
import shutil
import subprocess
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
    validate_root_inventory,
)
from local_agent.provenance import PRODUCT_PYTHON_FILES, PRODUCT_PYTHON_ROOTS

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
        "internal/perf",
        "internal/native_endpoint/ci",
        "internal/evaluation",
        "internal/benchmark_fixture",
        "internal/terminal_ui.py",
    }


def test_live_product_root_omission_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    """The provenance owner, not STATIC_ROOTS itself, discovers a missing live root."""
    monkeypatch.setattr(
        "devtools.check_static_standards.PRODUCT_PYTHON_ROOTS",
        ("internal/local_agent", "internal/serving", "internal/scripts", "internal/future"),
    )
    with pytest.raises(RuntimeError, match="internal/future"):
        validate_root_inventory()


# Test-only Python, owned by native pytest and the import-boundary gate: one
# directory and one exact file. A directory prefix and an exact file are different
# exemptions; matching the file name as a prefix let `conftest.py.escape.py` through.
_TEST_ONLY_DIRECTORY = "internal/tests/"
_TEST_ONLY_FILE = "conftest.py"


def _outside_every_owner(paths: list[str]) -> list[str]:
    """Tracked Python paths neither test-only nor inside a static root."""
    return [
        path for path in paths
        if path
        and not (path.startswith(_TEST_ONLY_DIRECTORY) or path == _TEST_ONLY_FILE)
        and not any(path == root or path.startswith(root + "/") for root in STATIC_ROOTS)
    ]


def test_all_live_python_in_the_repository_is_classified_and_checked() -> None:
    """Every tracked .py anywhere (not only under internal/) is test-only or gated."""
    repository = Path(__file__).resolve().parents[3]
    paths = subprocess.check_output(
        ["git", "-c", "core.quotepath=false", "ls-files", "-z", "--", "*.py"],
        cwd=repository, text=True,
    ).split("\0")
    unchecked = _outside_every_owner(paths)
    assert not unchecked, f"tracked Python outside every static root: {unchecked}"


@pytest.mark.parametrize("path", (
    "conftest.py",
    "internal/tests/unit/test_anything.py",
    "internal/tests/conftest.py",
    "internal/terminal_ui.py",
    "internal/local_agent/config.py",
))
def test_owned_python_is_classified(path: str) -> None:
    assert _outside_every_owner([path]) == []


@pytest.mark.parametrize("path", (
    "demo/escape_probe.py",
    "conftest.py.escape.py",        # a file-name prefix is not the exact file
    "conftest.py/helper.py",        # nor is a directory of that name
    "internal/testsuite/helper.py",  # nor a sibling of the test directory
    "internal/terminal_ui.py.bak.py",
    "internal/local_agent_extra/module.py",
))
def test_python_outside_every_owner_is_caught(path: str) -> None:
    assert _outside_every_owner([path]) == [path]


@pytest.mark.parametrize("omitted", (*PRODUCT_PYTHON_ROOTS, *PRODUCT_PYTHON_FILES))
def test_each_owned_product_surface_is_required(omitted: str) -> None:
    retained = tuple(
        root for root in STATIC_ROOTS
        if not (omitted == root or omitted.startswith(root + "/"))
    )
    with pytest.raises(RuntimeError, match=omitted):
        validate_root_inventory(retained)


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
        path.write_text("import os\nvalue: int = 'not an int'\n", encoding="utf-8")
        expected.add(path.relative_to(tmp_path).as_posix())

    monkeypatch.chdir(tmp_path)
    counts = measure(
        tmp_path,
        [sys.executable, "-m", "mypy"],
        [sys.executable, "-m", "ruff"],
    )

    found = {file for checker, file, code in counts if checker == "ruff" and code == "F401"}
    assert found == expected
    for platform in ("linux", "win32"):
        typed = {
            file for checker, file, code in counts
            if checker == f"mypy-{platform}" and code == "assignment"
        }
        assert typed == expected

    baseline = tmp_path / "internal" / "static-standards-baseline.json"
    baseline.parent.mkdir(parents=True, exist_ok=True)
    baseline.write_text(dump_baseline({}), encoding="utf-8")
    assert main([
        "--root", str(tmp_path),
    ]) == 1
    output = capsys.readouterr()
    assert "New static-standard findings" in output.err
    assert all(path in output.err for path in expected)
