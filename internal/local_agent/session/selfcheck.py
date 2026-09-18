"""LCA's Python check adapter. Does not change CTest or experiment semantics."""
from __future__ import annotations

import hashlib
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

from ..tools.base import resolve_in_repo
from ..tools.runner import run_command
from .contracts import TaskResult


def tree_digest(root: Path) -> str:
    """Hash HEAD, index entries and nonignored file bytes, including untracked edits.

    Failure to read Git or a file blocks verification. This is a product snapshot,
    not the experiment source hash. Ignored outputs are outside this contract.
    """
    def git(*args):
        return subprocess.run(["git", *args], cwd=root, check=True,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30).stdout

    digest = hashlib.sha256(git("rev-parse", "HEAD") + git("ls-files", "--stage", "-z"))
    names = set(git("ls-files", "-z", "--cached", "--others", "--exclude-standard").split(b"\0"))
    for name in sorted(names - {b""}):
        path = resolve_in_repo(root, name.decode("utf-8"))
        digest.update(name + b"\0")
        if not path.exists():
            digest.update(b"deleted\0")
        elif path.is_file():
            digest.update(path.read_bytes())
            digest.update(b"\0")
        else:
            raise ValueError("unsupported repository entry")
    return digest.hexdigest()


def junit_counts(path: Path) -> dict[str, int]:
    if path.stat().st_size > 16_000_000:
        raise ValueError("test report too large")
    root = ET.fromstring(path.read_bytes())
    suites = [root] if root.tag == "testsuite" else list(root.findall("testsuite"))
    if root.tag not in ("testsuite", "testsuites") or not suites:
        raise ValueError("missing test suite report")
    counts = {key: 0 for key in ("tests", "failures", "errors", "skipped")}
    for suite in suites:
        values = {key: int(suite.attrib[key]) for key in counts}
        if any(value < 0 for value in values.values()):
            raise ValueError("negative test count")
        if sum(values[k] for k in ("failures", "errors", "skipped")) > values["tests"]:
            raise ValueError("inconsistent test counts")
        if len(suite.findall("testcase")) != values["tests"]:
            raise ValueError("test count does not match report cases")
        for key, value in values.items():
            counts[key] += value
    return counts


def run_self_check(repo, task_id, events) -> TaskResult:
    if not (repo.policy.allow_build and repo.policy.allow_test):
        return TaskResult(task_id, "blocked", "Self-check requires execution enabled and repository build/test permission.")
    if repo.name != "local-code-agent" or not (repo.root / "internal/tests").is_dir():
        return TaskResult(task_id, "blocked", "Self-check only supports the Local Code Agent checkout.")
    before = tree_digest(repo.root)
    artifacts = resolve_in_repo(repo.root, f".local-agent/session-checks/{task_id}")
    artifacts.mkdir(parents=True, exist_ok=False)
    report = artifacts / "pytest.xml"
    compile_argv = [sys.executable, "-m", "compileall", "-q", "internal/local_agent"]
    events.emit("check.compile", {}, task_id)
    compiled = run_command(compile_argv, repo.root, artifacts, repo.policy.command_timeout_seconds)
    if not compiled.ok:
        return TaskResult(task_id, "fail", f"Python compilation failed. Log: {compiled.combined_path}")
    events.emit("check.pytest", {}, task_id)
    tested = run_command(
        [sys.executable, "-m", "pytest", "-q", "-o", "addopts=", "internal/tests", f"--junitxml={report}"],
        repo.root, artifacts, repo.policy.command_timeout_seconds,
        {"PYTEST_ADDOPTS": "", "PYTEST_PLUGINS": ""},
    )
    unchanged = before == tree_digest(repo.root)
    try:
        counts = junit_counts(report)
    except (OSError, ValueError, KeyError, ET.ParseError):
        return TaskResult(task_id, "fail", f"No usable pytest report. Log: {tested.combined_path}")
    proved = (tested.ok and counts["tests"] > counts["skipped"]
              and not counts["failures"] and not counts["errors"] and unchanged)
    summary = (f"Python compilation passed. Pytest: {counts['tests']} cases, "
               f"{counts['failures']} failures, {counts['errors']} errors, {counts['skipped']} skipped. "
               f"Exit: {tested.exit_code}. Repository unchanged during check: {unchanged}. "
               f"Log: {tested.combined_path}")
    return TaskResult(task_id, "pass" if proved else "fail", summary, proved,
                      ("compile:0", "pytest:1"),
                      {"compile_s": compiled.elapsed_s, "pytest_s": tested.elapsed_s,
                       "tree_sha256": before, "tests": counts})
