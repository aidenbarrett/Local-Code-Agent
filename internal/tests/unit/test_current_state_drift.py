from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from devtools.check_current_state_drift import check, recorded_sha, validate_ledger


LEDGER = """\
## Capability ledger

| ID | Public request | Implemented behaviour | Evidence | Qualification | Known limit | Next action | Owner |
|---|---|---|---|---|---|---|---|
| CAP-build | Build it | Configured build | J01-build-pass, PR#233 | deterministic-ci | Repository-specific | Keep qualified | controller |
"""


def _git(root: Path, *args: str) -> str:
    return subprocess.check_output(["git", *args], cwd=root, text=True).strip()


def _commit(root: Path, message: str) -> str:
    (root / "marker.txt").write_text(message, encoding="utf-8")
    subprocess.check_call(["git", "add", "."], cwd=root)
    subprocess.check_call(["git", "commit", "-m", message], cwd=root)
    return _git(root, "rev-parse", "HEAD")


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    subprocess.check_call(["git", "init", "-q"], cwd=tmp_path)
    subprocess.check_call(["git", "config", "user.email", "ci@example.invalid"], cwd=tmp_path)
    subprocess.check_call(["git", "config", "user.name", "CI"], cwd=tmp_path)
    first = _commit(tmp_path, "first")
    scripts = tmp_path / "internal/scripts"
    scripts.mkdir(parents=True)
    (scripts / "acceptance-journeys.py").write_text(
        "JOURNEYS = [('J01-build-pass',)]\nREPO_JOURNEYS = [('R01-questions',)]\n",
        encoding="utf-8",
    )
    (tmp_path / "CURRENT_STATE.md").write_text(
        f"# Current state\n\nReconciled against GitHub `main` at `{first}`\n\n{LEDGER}",
        encoding="utf-8",
    )
    subprocess.check_call(["git", "add", "CURRENT_STATE.md"], cwd=tmp_path)
    subprocess.check_call(["git", "commit", "-m", "state"], cwd=tmp_path)
    return tmp_path


def test_accepts_bounded_first_parent_drift(repo: Path):
    target = _commit(repo, "one")
    assert check(repo, target=target, max_behind=3) == 2


def test_rejects_drift_beyond_budget(repo: Path):
    for i in range(4):
        target = _commit(repo, f"change-{i}")
    with pytest.raises(RuntimeError, match="allowed drift is 3"):
        check(repo, target=target, max_behind=3)


def test_rejects_non_ancestor_reconciliation(repo: Path):
    target = _git(repo, "rev-parse", "HEAD")
    text = (repo / "CURRENT_STATE.md").read_text(encoding="utf-8")
    old = recorded_sha(text)
    subprocess.check_call(["git", "checkout", "--orphan", "other"], cwd=repo)
    subprocess.check_call(["git", "rm", "-rf", "."], cwd=repo)
    foreign = _commit(repo, "foreign")
    (repo / "CURRENT_STATE.md").write_text(text.replace(old, foreign), encoding="utf-8")
    with pytest.raises(RuntimeError, match="not an ancestor"):
        check(repo, target=target, max_behind=3)


def test_requires_full_reconciliation_sha():
    with pytest.raises(ValueError, match="full reconciliation SHA"):
        recorded_sha("# Current state\nReconciled against GitHub `main` at `abc`\n")


def test_valid_ledger_passes(repo: Path):
    validate_ledger(repo, (repo / "CURRENT_STATE.md").read_text(encoding="utf-8"))


@pytest.mark.parametrize(
    ("replacement", "message"),
    [
        ("| CAP-build | Build it | Configured build | J01-build-pass, PR#233 | deterministic-ci | Repository-specific | Keep qualified | controller |\n" * 2, "duplicate"),
        ("| CAP-build | Build it | Configured build | J01-build-pass | magical | Repository-specific | Keep qualified | controller |", "invalid Qualification"),
        ("| CAP-build | Build it | Configured build | J99-missing | deterministic-ci | Repository-specific | Keep qualified | controller |", "unknown acceptance journey"),
        ("| CAP-build | Build it | Configured build | internal/tests/unit/missing.py::test_nope | deterministic-ci | Repository-specific | Keep qualified | controller |", "test file does not exist"),
        ("| CAP-build | Build it | Configured build | J01-build-pass | real-model-ci | Repository-specific | Keep qualified | controller |", "requires PR# or run:"),
    ],
)
def test_ledger_rules_fail_closed(repo: Path, replacement: str, message: str):
    text = (repo / "CURRENT_STATE.md").read_text(encoding="utf-8")
    original = "| CAP-build | Build it | Configured build | J01-build-pass, PR#233 | deterministic-ci | Repository-specific | Keep qualified | controller |"
    with pytest.raises(ValueError, match=message):
        validate_ledger(repo, text.replace(original, replacement))


def test_rejects_missing_test_function(repo: Path):
    test_file = repo / "internal/tests/unit/test_example.py"
    test_file.parent.mkdir(parents=True, exist_ok=True)
    test_file.write_text("def test_present():\n    pass\n", encoding="utf-8")
    text = (repo / "CURRENT_STATE.md").read_text(encoding="utf-8").replace(
        "J01-build-pass, PR#233", "internal/tests/unit/test_example.py::test_absent"
    )
    with pytest.raises(ValueError, match="test function does not exist"):
        validate_ledger(repo, text)
