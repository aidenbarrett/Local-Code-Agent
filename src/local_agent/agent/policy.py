"""Permission policy.

The model proposes. This decides. Nothing in the system prompt can talk its way
past this, because it is not in the prompt.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Callable

from ..config import Policy
from ..tools.base import Risk, Tool


class Verdict(str, Enum):
    ALLOW = "allow"
    APPROVE = "approve"
    DENY = "deny"


@dataclass(frozen=True)
class Decision:
    verdict: Verdict
    reason: str

    @property
    def allowed(self) -> bool:
        return self.verdict is not Verdict.DENY

    @property
    def requires_approval(self) -> bool:
        return self.verdict is Verdict.APPROVE


# Operations that simply do not exist as tools. Listed so the intent is explicit
# in the source, and so a future contributor has to delete a line labelled
# "never automatic" rather than quietly add a handler.
FORBIDDEN_OPERATIONS = (
    "push",
    "push --force",
    "reset --hard",
    "clean -fdx",
    "checkout <branch>",
    "rebase",
    "merge",
    "bisect",
    "stash drop",
    "arbitrary shell",
)


ApprovalFn = Callable[[Tool, dict[str, Any], Decision], bool]


# Operations that put something into git's index or history. These are the ones
# that must not happen inside a speculative attempt: a worktree edit can be
# journalled and reverted, an index entry is noise in the escalation state, and
# a commit cannot be undone at all without touching history that is not ours.
_HISTORY_MUTATING = {"git_stage", "git_commit"}


class PolicyEngine:
    def __init__(self, policy: Policy) -> None:
        self.policy = policy

    def check(
        self,
        tool: Tool,
        arguments: dict[str, Any],
        speculative: bool = False,
    ) -> Decision:
        # `speculative` means this attempt may still be discarded and retried on
        # another tier. Commits belong in the finalise phase, after the run is
        # known to be terminal, and never inside the loop that might be redone.
        if speculative and tool.name in _HISTORY_MUTATING:
            return Decision(
                Verdict.DENY,
                f"{tool.name} is not permitted while this attempt can still be "
                "escalated and retried; staging and committing happen only once "
                "the run is final",
            )
        if tool.risk is Risk.FORBIDDEN:
            return Decision(Verdict.DENY, f"{tool.name} is permanently disabled")

        if tool.risk is Risk.READ:
            return Decision(Verdict.ALLOW, "read-only")

        if tool.risk is Risk.EXECUTE:
            if tool.name in ("build_target", "configure_project") and not self.policy.allow_build:
                return Decision(Verdict.DENY, "policy.allow_build is false")
            if tool.name in ("run_test", "list_tests") and not self.policy.allow_test:
                return Decision(Verdict.DENY, "policy.allow_test is false")
            return Decision(Verdict.ALLOW, "configured command from the build profile")

        if tool.risk is Risk.WRITE:
            if not self.policy.allow_patch:
                return Decision(Verdict.DENY, "policy.allow_patch is false")
            return Decision(Verdict.APPROVE, "modifies the worktree")

        # DANGEROUS
        if tool.name == "apply_patch" and not self.policy.allow_patch:
            return Decision(Verdict.DENY, "policy.allow_patch is false")
        if tool.name in ("git_stage", "git_commit") and not self.policy.allow_commit:
            return Decision(Verdict.DENY, "policy.allow_commit is false")
        return Decision(Verdict.APPROVE, "can change the working tree or history")


def deny_all_approvals(tool: Tool, arguments: dict[str, Any], decision: Decision) -> bool:
    """Non-interactive default: propose, never apply."""
    return False


def cli_approval(tool: Tool, arguments: dict[str, Any], decision: Decision) -> bool:
    import json
    import sys

    print(f"\n  APPROVAL REQUIRED: {tool.name} ({decision.reason})")
    print("  arguments: " + json.dumps(arguments, indent=2)[:2000])
    if not sys.stdin.isatty():
        print("  no tty; denied")
        return False
    answer = input("  allow? [y/N] ").strip().lower()
    return answer in ("y", "yes")
