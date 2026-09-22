"""Fail closed when a merge-ready pull request is not an integration candidate.

Stacked pull requests are useful for early CI. Draft stacks are explicitly development
state rather than merge candidates, so they may stay green while still reporting that
fact. Once a pull request is ready for review, a non-main base fails closed until the
branch is reconciled onto the canonical integration branch.
"""
from __future__ import annotations

import argparse
import os
import sys


DEFAULT_INTEGRATION_BRANCH = "main"


def _parse_bool(value: str, *, name: str) -> bool:
    normalized = value.strip().lower()
    if normalized == "true":
        return True
    if normalized == "false":
        return False
    raise ValueError(f"{name} must be 'true' or 'false'")


def integration_base_error(
    event_name: str,
    base_ref: str,
    *,
    draft: bool = False,
    integration_branch: str = DEFAULT_INTEGRATION_BRANCH,
) -> str | None:
    """Return the merge-gate error, or ``None`` when the event may stay green.

    Non-pull-request events are outside this gate. Pull requests always fail closed when
    their base is unavailable. A draft stack may run early CI without a red merge gate;
    a merge-ready stack must target the canonical integration branch directly.
    """
    if not isinstance(draft, bool):
        raise TypeError("draft state must be boolean")
    if event_name != "pull_request":
        return None
    if not base_ref:
        return "pull-request integration gate cannot determine the base branch"
    if base_ref != integration_branch and not draft:
        return (
            f"stacked pull request targets {base_ref!r}; it is valid for early CI but "
            f"not merge-eligible until reconciled directly onto {integration_branch!r}"
        )
    return None


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--event-name",
        default=os.environ.get("GITHUB_EVENT_NAME", ""),
        help="GitHub event name (defaults to GITHUB_EVENT_NAME)",
    )
    parser.add_argument(
        "--base-ref",
        default=os.environ.get("GITHUB_BASE_REF", ""),
        help="pull-request base branch (defaults to GITHUB_BASE_REF)",
    )
    parser.add_argument(
        "--draft",
        default=os.environ.get("LCA_PR_DRAFT", "false"),
        help="whether the pull request is draft (true/false; defaults to LCA_PR_DRAFT)",
    )
    parser.add_argument(
        "--integration-branch",
        default=DEFAULT_INTEGRATION_BRANCH,
        help="canonical merge target (default: main)",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        draft = _parse_bool(args.draft, name="draft state")
    except ValueError as exc:
        print(f"INTEGRATION BASE GATE: {exc}", file=sys.stderr)
        return 2

    error = integration_base_error(
        args.event_name,
        args.base_ref,
        draft=draft,
        integration_branch=args.integration_branch,
    )
    if error is not None:
        print(f"INTEGRATION BASE GATE: {error}", file=sys.stderr)
        return 2

    if args.event_name != "pull_request":
        print(f"event {args.event_name!r} is outside the pull-request integration gate")
    elif args.base_ref != args.integration_branch:
        print(
            f"draft stacked pull request targets {args.base_ref!r}; early CI allowed, "
            "not an integration candidate"
        )
    else:
        print(f"integration candidate targets {args.integration_branch!r}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
