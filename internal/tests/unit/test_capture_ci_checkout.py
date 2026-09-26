from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest


MODULE_PATH = Path(__file__).resolve().parents[2] / "devtools" / "capture_ci_checkout.py"
SPEC = importlib.util.spec_from_file_location("lca_capture_ci_checkout", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
capture = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(capture)


def _env() -> dict[str, str]:
    return {
        "GITHUB_REPOSITORY": "aidenbarrett/Local-Code-Agent",
        "GITHUB_REPOSITORY_ID": "123456",
        "GITHUB_SERVER_URL": "https://github.com",
        "GITHUB_API_URL": "https://api.github.com",
        "GITHUB_WORKFLOW": "tests",
        "GITHUB_WORKFLOW_REF": (
            "aidenbarrett/Local-Code-Agent/.github/workflows/tests.yml@refs/pull/81/merge"
        ),
        "GITHUB_WORKFLOW_SHA": "e" * 40,
        "GITHUB_RUN_ID": "987654",
        "GITHUB_RUN_ATTEMPT": "2",
        "GITHUB_EVENT_NAME": "pull_request",
        "GITHUB_REF": "refs/pull/81/merge",
        "GITHUB_SHA": "a" * 40,
        "GITHUB_JOB": "pytest",
        "RUNNER_OS": "Windows",
        "RUNNER_ARCH": "X64",
        "LCA_ARTIFACT_NAME": "ci-job-tests-pytest-Windows-X64-attempt-2",
        "LCA_COMMAND_SCOPE": "authoritative-pytest",
        "LCA_PR_HEAD_SHA": "c" * 40,
        "LCA_PR_BASE_SHA": "d" * 40,
    }


def _git_identity(monkeypatch):
    values = iter(["a" * 40, "b" * 40])
    monkeypatch.setattr(capture, "git", lambda *_args: next(values))


def test_record_binds_run_job_platform_and_synthetic_merge_identity(monkeypatch):
    _git_identity(monkeypatch)
    value = capture.record(_env())
    assert value["schema"] == "lca.ci-job-evidence/2"
    assert value["repository_id"] == "123456"
    assert value["workflow_sha"] == "e" * 40
    assert value["run_id"] == "987654"
    assert value["run_attempt"] == "2"
    assert value["job_key"] == "pytest"
    assert value["runner_os"] == "Windows"
    assert value["declared_command_scope"] == "authoritative-pytest"
    assert value["checkout_commit_sha"] == "a" * 40
    assert value["checkout_tree_sha"] == "b" * 40
    assert value["pr_head_sha"] == "c" * 40
    assert value["pr_base_sha"] == "d" * 40
    assert value["checkout_commit_sha"] != value["pr_head_sha"]


def test_record_refuses_github_sha_mismatch(monkeypatch):
    _git_identity(monkeypatch)
    env = _env()
    env["GITHUB_SHA"] = "f" * 40
    with pytest.raises(RuntimeError, match="does not match"):
        capture.record(env)


@pytest.mark.parametrize(
    "name,value,message",
    [
        ("GITHUB_REPOSITORY", "other/repository", None),
        ("GITHUB_RUN_ATTEMPT", "0", "positive integer"),
        ("GITHUB_WORKFLOW_SHA", "short", "full lowercase commit SHA"),
        ("LCA_COMMAND_SCOPE", "", "required"),
    ],
)
def test_record_preserves_observed_values_but_refuses_malformed_fields(
    monkeypatch, name, value, message
):
    _git_identity(monkeypatch)
    env = _env()
    env[name] = value
    if message is None:
        assert capture.record(env)["repository"] == value
    else:
        with pytest.raises(RuntimeError, match=message):
            capture.record(env)


def test_pull_request_requires_both_head_and_base(monkeypatch):
    _git_identity(monkeypatch)
    env = _env()
    env["LCA_PR_BASE_SHA"] = ""
    with pytest.raises(RuntimeError, match="requires PR head and base"):
        capture.record(env)


def test_non_pr_event_allows_absent_pr_identity(monkeypatch):
    _git_identity(monkeypatch)
    env = _env()
    env.update({
        "GITHUB_EVENT_NAME": "push",
        "GITHUB_REF": "refs/heads/main",
        "LCA_PR_HEAD_SHA": "",
        "LCA_PR_BASE_SHA": "",
    })
    value = capture.record(env)
    assert value["pr_head_sha"] is None
    assert value["pr_base_sha"] is None


def test_every_workflow_job_defines_unique_matrix_evidence_and_scope():
    root = MODULE_PATH.parents[2]
    workflows = {
        root / ".github/workflows/tests.yml": {
            "ci-job-tests-gates-${{ runner.os }}-${{ runner.arch }}-"
            "attempt-${{ github.run_attempt }}": "fast-gates",
            "ci-job-tests-pytest-${{ runner.os }}-${{ runner.arch }}-"
            "attempt-${{ github.run_attempt }}": "authoritative-pytest",
            "ci-job-tests-coverage-${{ runner.os }}-${{ runner.arch }}-"
            "attempt-${{ github.run_attempt }}": "coverage-report",
        },
        root / ".github/workflows/serving.yml": {
            "ci-job-serving-serving-${{ runner.os }}-${{ runner.arch }}-"
            "attempt-${{ github.run_attempt }}": "serving",
        },
    }
    for path, jobs in workflows.items():
        text = path.read_text(encoding="utf-8")
        capture_command = "run: python internal/devtools/capture_ci_checkout.py"
        assert text.count(capture_command) == len(jobs)
        for artifact_name, command_scope in jobs.items():
            assert text.count(artifact_name) == 2
            assert text.count(f"LCA_COMMAND_SCOPE: {command_scope}") == 1
