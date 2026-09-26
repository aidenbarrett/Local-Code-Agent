from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class EnforcementResult:
    enforced: bool
    source: str
    required_checks: tuple[str, ...]
    reason: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "enforced": self.enforced,
            "source": self.source,
            "required_checks": list(self.required_checks),
            "reason": self.reason,
        }


def _request_json(url: str, token: str | None = None) -> Any:
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "local-code-agent-merge-enforcement-check",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(request, timeout=15) as response:
        return json.load(response)


def _classic_protection(branch: dict[str, Any]) -> EnforcementResult | None:
    if not branch.get("protected"):
        return None
    protection = branch.get("protection") or {}
    required = protection.get("required_status_checks") or {}
    contexts = tuple(sorted(str(value) for value in required.get("contexts") or ()))
    checks = required.get("checks") or ()
    contexts += tuple(
        sorted(
            str(check.get("context"))
            for check in checks
            if isinstance(check, dict) and check.get("context")
        )
    )
    contexts = tuple(sorted(set(contexts)))
    if not contexts:
        return EnforcementResult(
            False,
            "classic_branch_protection",
            (),
            "branch is protected but no required status checks are configured",
        )
    return EnforcementResult(
        True,
        "classic_branch_protection",
        contexts,
        "required status checks are enforced by branch protection",
    )


def _ruleset_protection(rulesets: list[dict[str, Any]]) -> EnforcementResult | None:
    required: set[str] = set()
    active = False
    for ruleset in rulesets:
        if not isinstance(ruleset, dict):
            continue
        if ruleset.get("enforcement") not in {"active", "evaluate"}:
            continue
        rules = ruleset.get("rules") or ()
        for rule in rules:
            if not isinstance(rule, dict) or rule.get("type") != "required_status_checks":
                continue
            active = True
            parameters = rule.get("parameters") or {}
            for check in parameters.get("required_status_checks") or ():
                if isinstance(check, dict) and check.get("context"):
                    required.add(str(check["context"]))
    if not active:
        return None
    if not required:
        return EnforcementResult(
            False,
            "ruleset",
            (),
            "a required-status-check ruleset exists but declares no check contexts",
        )
    return EnforcementResult(
        True,
        "ruleset",
        tuple(sorted(required)),
        "required status checks are enforced by an active repository ruleset",
    )


def evaluate_merge_enforcement(
    branch: dict[str, Any],
    rulesets: list[dict[str, Any]],
    *,
    expected_checks: tuple[str, ...] = (),
) -> EnforcementResult:
    candidate = _classic_protection(branch) or _ruleset_protection(rulesets)
    if candidate is None:
        return EnforcementResult(
            False,
            "none",
            (),
            "main is unprotected and no active required-status-check ruleset was found",
        )
    if not candidate.enforced:
        return candidate
    if expected_checks:
        missing = sorted(set(expected_checks) - set(candidate.required_checks))
        if missing:
            return EnforcementResult(
                False,
                candidate.source,
                candidate.required_checks,
                "required checks missing from merge enforcement: " + ", ".join(missing),
            )
    return candidate


def _parse_repository(value: str) -> tuple[str, str]:
    if value.count("/") != 1:
        raise ValueError("repository must be owner/name")
    owner, repo = value.split("/", 1)
    if not owner or not repo:
        raise ValueError("repository must be owner/name")
    return owner, repo


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Fail closed unless GitHub merge policy actually requires status checks.",
    )
    parser.add_argument(
        "--repository",
        default=os.environ.get("GITHUB_REPOSITORY", "aidenbarrett/Local-Code-Agent"),
        help="GitHub repository in owner/name form",
    )
    parser.add_argument("--branch", default="main")
    parser.add_argument(
        "--require",
        action="append",
        default=[],
        metavar="CHECK",
        help="required status-check context that must be enforced (repeatable)",
    )
    args = parser.parse_args(argv)

    try:
        owner, repo = _parse_repository(args.repository)
        token = os.environ.get("GITHUB_TOKEN")
        branch = _request_json(
            f"https://api.github.com/repos/{owner}/{repo}/branches/{args.branch}", token
        )
        rulesets = _request_json(f"https://api.github.com/repos/{owner}/{repo}/rulesets", token)
        if not isinstance(branch, dict) or not isinstance(rulesets, list):
            raise ValueError("GitHub returned an unexpected merge-policy response shape")
        result = evaluate_merge_enforcement(
            branch,
            rulesets,
            expected_checks=tuple(args.require),
        )
    except (ValueError, urllib.error.URLError, TimeoutError, OSError) as exc:
        result = EnforcementResult(False, "unknown", (), f"merge enforcement could not be proven: {exc}")

    print(json.dumps(result.as_dict(), indent=2, sort_keys=True))
    return 0 if result.enforced else 2


if __name__ == "__main__":
    sys.exit(main())
