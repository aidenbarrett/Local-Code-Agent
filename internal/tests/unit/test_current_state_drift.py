from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from internal.devtools.check_current_state_drift import check, recorded_sha


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
    (tmp_path / "CURRENT_STATE.md").write_text(
        f"# Current state\n\nReconciled against GitHub `main` at `{first}`\n",
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
    subprocess.check_call(["git", "checkout", "--orphan", "other"], cwd=repo)
    subprocess.check_call(["git", "rm", "-rf", "."], cwd=repo)
    foreign = _commit(repo, "foreign")
    text = (repo / "CURRENT_STATE.md").read_text(encoding="utf-8")
    old = recorded_sha(text)
    (repo / "CURRENT_STATE.md").write_text(text.replace(old, foreign), encoding="utf-8")
    with pytest.raises(RuntimeError, match="not an ancestor"):
        check(repo, target=target, max_behind=3)


def test_requires_full_reconciliation_sha():
    with pytest.raises(ValueError, match="full reconciliation SHA"):
        recorded_sha("# Current state\nReconciled against GitHub `main` at `abc`\n")
