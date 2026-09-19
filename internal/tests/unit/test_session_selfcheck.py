import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from local_agent.config import BuildProfile, Policy, RepoConfig
from local_agent.session import self_check as selfcheck
from local_agent.session.event_buffer import EventBuffer


def report(path, tests=1, failures=0, skipped=0):
    cases = ''.join('<testcase name="case%d"/>' % i for i in range(tests))
    path.write_text(f'<testsuites><testsuite tests="{tests}" errors="0" failures="{failures}" skipped="{skipped}">{cases}</testsuite></testsuites>')


@pytest.mark.parametrize("body", [
    '<testsuites/>', '<wrong/>', '<testsuite tests="-1" errors="0" failures="0" skipped="0"/>',
    '<testsuite tests="1" errors="0" failures="0" skipped="0"/>',
])
def test_unusable_report_rejected(tmp_path, body):
    path = tmp_path / "report.xml"
    path.write_text(body)
    with pytest.raises(ValueError):
        selfcheck.junit_counts(path)


@pytest.mark.parametrize("exit_code,tests,failures,skipped,changed,verified", [
    (0, 2, 0, 0, False, True), (1, 2, 1, 0, False, False),
    (0, 0, 0, 0, False, False), (0, 2, 0, 2, False, False),
    (0, 2, 0, 0, True, False), (1, 2, 0, 0, False, False),
])
def test_selfcheck_requires_exit_counts_and_same_tree(tmp_path, monkeypatch, exit_code, tests, failures, skipped, changed, verified):
    (tmp_path / "internal/tests").mkdir(parents=True)
    repo = RepoConfig(tmp_path, "local-code-agent", "build", ".local-agent/runs",
                      {"python": BuildProfile("python")}, "python", Policy())
    digests = iter(["before", "changed" if changed else "before"])
    monkeypatch.setattr(selfcheck, "tree_digest", lambda root: next(digests))
    calls = []
    def execute(argv, root, artifacts, timeout, *env):
        calls.append(argv)
        is_test = "pytest" in argv
        if is_test:
            path = Path(next(a.split("=", 1)[1] for a in argv if a.startswith("--junitxml=")))
            report(path, tests, failures, skipped)
        return SimpleNamespace(ok=not is_test or exit_code == 0,
                               exit_code=exit_code if is_test else 0,
                               elapsed_s=0.1, combined_path=artifacts / "log")
    monkeypatch.setattr(selfcheck, "run_command", execute)
    result = selfcheck.run_self_check(repo, "t1", EventBuffer("s"))
    assert result.verified_at_completion is verified
    assert (result.outcome == "pass") is verified
    assert len(calls) == 2
    assert "internal/tests" in calls[1]
    assert "run_test_suite.py" not in str(calls)


def test_snapshot_detects_untracked_content_change_and_tracked_deletion(sandbox):
    root = sandbox.root
    extra = root / "new.py"
    extra.write_text("one")
    first = selfcheck.tree_digest(root)
    extra.write_text("two")
    second = selfcheck.tree_digest(root)
    assert second != first
    (root / "CMakeLists.txt").unlink()
    assert selfcheck.tree_digest(root) != second


def test_snapshot_refuses_non_git_directory(tmp_path):
    with pytest.raises(subprocess.CalledProcessError):
        selfcheck.tree_digest(tmp_path)