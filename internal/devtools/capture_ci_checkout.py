#!/usr/bin/env python3
"""Record one GitHub Actions job's exact execution and checkout identity.

This artifact is evidence input, not proof that the job passed or that the file came
from GitHub. A readiness consumer must obtain it through the GitHub Actions artifact
API, match it to trusted run and job records, and require a successful job.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SHA_PATTERN = re.compile(r"^[0-9a-f]{40}$")
INTEGER_PATTERN = re.compile(r"^[1-9][0-9]*$")
REPOSITORY_PATTERN = re.compile(r"^[^/\s]+/[^/\s]+$")
TOKEN_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:/-]{0,255}$")


def git(*args: str) -> str:
    proc = subprocess.run(
        ["git", *args], cwd=ROOT, capture_output=True, text=True, timeout=20
    )
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or proc.stdout.strip() or "git failed")
    return proc.stdout.strip()


def _required(env: dict[str, str], name: str) -> str:
    value = env.get(name, "").strip()
    if not value:
        raise RuntimeError(f"{name} is required")
    return value


def _sha(env: dict[str, str], name: str, *, optional: bool = False) -> str | None:
    value = env.get(name, "").strip()
    if optional and not value:
        return None
    if not SHA_PATTERN.fullmatch(value):
        raise RuntimeError(f"{name} is not a full lowercase commit SHA")
    return value


def _positive_integer(env: dict[str, str], name: str) -> str:
    value = _required(env, name)
    if not INTEGER_PATTERN.fullmatch(value):
        raise RuntimeError(f"{name} is not a positive integer")
    return value


def _token(env: dict[str, str], name: str) -> str:
    value = _required(env, name)
    if not TOKEN_PATTERN.fullmatch(value):
        raise RuntimeError(f"{name} is not a bounded identity token")
    return value


def record(env: dict[str, str]) -> dict[str, str | None]:
    checkout = git("rev-parse", "--verify", "HEAD^{commit}")
    tree = git("rev-parse", "--verify", "HEAD^{tree}")
    if not SHA_PATTERN.fullmatch(checkout) or not SHA_PATTERN.fullmatch(tree):
        raise RuntimeError("checkout commit/tree identity is malformed")

    github_sha = _sha(env, "GITHUB_SHA")
    if github_sha != checkout:
        raise RuntimeError("GITHUB_SHA does not match the checked-out commit")

    repository = _required(env, "GITHUB_REPOSITORY")
    if not REPOSITORY_PATTERN.fullmatch(repository):
        raise RuntimeError("GITHUB_REPOSITORY is not an owner/name identity")

    event_name = _token(env, "GITHUB_EVENT_NAME")
    pr_head_sha = _sha(env, "LCA_PR_HEAD_SHA", optional=True)
    pr_base_sha = _sha(env, "LCA_PR_BASE_SHA", optional=True)
    if event_name == "pull_request" and (pr_head_sha is None or pr_base_sha is None):
        raise RuntimeError("pull_request evidence requires PR head and base SHAs")
    if (pr_head_sha is None) != (pr_base_sha is None):
        raise RuntimeError("PR head and base SHAs must be present together")

    workflow_ref = _required(env, "GITHUB_WORKFLOW_REF")
    if "@" not in workflow_ref or "/.github/workflows/" not in workflow_ref:
        raise RuntimeError("GITHUB_WORKFLOW_REF is not a workflow file identity")

    return {
        "schema": "lca.ci-job-evidence/2",
        "artifact_name": _token(env, "LCA_ARTIFACT_NAME"),
        "repository": repository,
        "repository_id": _positive_integer(env, "GITHUB_REPOSITORY_ID"),
        "server_url": _required(env, "GITHUB_SERVER_URL"),
        "api_url": _required(env, "GITHUB_API_URL"),
        "workflow": _required(env, "GITHUB_WORKFLOW"),
        # Observed execution metadata. A consumer still has to compare this with
        # workflow policy read from the trusted destination baseline.
        "workflow_ref": workflow_ref,
        "workflow_sha": _sha(env, "GITHUB_WORKFLOW_SHA"),
        "run_id": _positive_integer(env, "GITHUB_RUN_ID"),
        "run_attempt": _positive_integer(env, "GITHUB_RUN_ATTEMPT"),
        "event_name": event_name,
        "event_ref": _required(env, "GITHUB_REF"),
        "github_sha": github_sha,
        # GITHUB_JOB is the workflow/YAML key, not GitHub's numeric API job ID.
        "job_key": _token(env, "GITHUB_JOB"),
        "runner_os": _token(env, "RUNNER_OS"),
        "runner_arch": _token(env, "RUNNER_ARCH"),
        "declared_command_scope": _token(env, "LCA_COMMAND_SCOPE"),
        "checkout_commit_sha": checkout,
        "checkout_tree_sha": tree,
        "pr_head_sha": pr_head_sha,
        "pr_base_sha": pr_base_sha,
    }


def main() -> int:
    try:
        value = record(dict(os.environ))
        out = Path(os.environ.get("LCA_CHECKOUT_EVIDENCE", "ci-job-evidence.json"))
        out.write_text(
            json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n",
            encoding="utf-8",
        )
    except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
        print(f"REFUSED: CI job identity unavailable: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(value, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
