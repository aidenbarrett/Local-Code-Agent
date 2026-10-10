"""'What's wrong with my repo?': the controller reads Git state and explains it (#464).

Every fact here comes from one ``git_status`` call through the existing read-only Git
tool, the same owner the git-review worker uses. No model decides what the repository
state is: a fixed plan runs the tool and renders what it observed. Next steps are
advice for the user to run; this module never runs, stages, continues, aborts, resets,
pushes or otherwise changes anything, and it never offers to.

Each next step says what it would affect and what it could lose, because the user is
the one who will run it. When git did not report a fact (no upstream, an upstream that
is not fetched), the answer says that it is unknown rather than guessing.
"""
from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final

from ..llm.client import tool_call
from ..llm.protocol import ChatResponse

# The route: its own name for admission identity, run under the git-review procedure
# (whose tool allowlist already contains git_status).
REPOSITORY_STATE: Final = "repository-state"
REPOSITORY_STATE_PROCEDURE: Final = "git-review"
_PLAN_VERSION: Final = 1
_STEP: Final = "git_status"


def repository_state_sha256(skill_sha256: str) -> str:
    """Admission fingerprint: this plan's identity bound to the procedure bytes."""
    identity = json.dumps(
        {"route": REPOSITORY_STATE, "plan": _PLAN_VERSION, "steps": [_STEP],
         "skill_sha256": skill_sha256},
        sort_keys=True, separators=(",", ":"),
    )
    return hashlib.sha256(f"lca-repository-state:{identity}".encode()).hexdigest()


def _code(text: str) -> str:
    return f"`{text}`"


def _plural(count: int, noun: str) -> str:
    return f"{count} {noun}" + ("" if count == 1 else "s")


@dataclass(frozen=True, slots=True)
class _State:
    """The ``git_status`` facts this explanation uses, read once."""

    head: str
    oid: str
    upstream: str | None
    divergence: tuple[int, int] | None
    operation: str | None
    commands: Mapping[str, str]
    conflicted: tuple[Mapping[str, Any], ...]
    untracked: int
    staged_only: int
    unstaged_only: int
    both: int
    bisecting: bool

    @property
    def detached(self) -> bool:
        return self.head == "(detached)"

    @property
    def unborn(self) -> bool:
        return self.oid == "(initial)"

    @property
    def staged(self) -> bool:
        return bool(self.staged_only or self.both)

    @classmethod
    def read(cls, data: Mapping[str, Any]) -> _State:
        branch = data.get("branch") or {}
        divergence = data.get("upstream_divergence")
        staged_only = unstaged_only = both = 0
        for item in data.get("changed") or []:
            # git's own XY letters: index (staged) and worktree (unstaged).
            staged, worktree = bool(item.get("staged")), bool(item.get("worktree"))
            both += staged and worktree
            staged_only += staged and not worktree
            unstaged_only += worktree and not staged
        upstream = branch.get("upstream")
        return cls(
            head=str(branch.get("head") or ""),
            oid=str(branch.get("oid") or ""),
            upstream=None if upstream is None else str(upstream),
            divergence=(None if divergence is None
                        else (int(divergence["ahead"]), int(divergence["behind"]))),
            operation=data.get("operation") or None,
            commands=data.get("operation_commands") or {},
            conflicted=tuple(data.get("conflicted") or ()),
            untracked=len(data.get("untracked") or ()),
            staged_only=staged_only, unstaged_only=unstaged_only, both=both,
            bisecting=bool(data.get("bisecting")),
        )


def _branch_facts(state: _State) -> list[str]:
    name = state.head or "unknown"
    if state.detached:
        return [f"HEAD is detached at {state.oid[:12] or 'an unknown commit'}: "
                "no branch is checked out."]
    facts = [f"On branch {name}, which has no commits yet." if state.unborn
             else f"On branch {name}."]
    if state.upstream is None:
        facts.append(f"No upstream is configured for {name}, so how far it is ahead of "
                     "or behind a remote is unknown.")
    elif state.divergence is None:
        facts.append(f"The upstream {state.upstream} is configured but git has no copy of "
                     "it (never fetched, or deleted on the remote), so ahead/behind is unknown.")
    else:
        ahead, behind = state.divergence
        facts.append(f"{_plural(ahead, 'commit')} ahead of and {behind} behind "
                     f"{state.upstream}, as of the last fetch.")
    return facts


def _operation_facts(state: _State) -> list[str]:
    facts: list[str] = []
    if state.operation:
        facts.append(f"A {state.operation} has stopped part-way and is still in progress.")
        label = "Conflicted"
    else:
        # Unmerged paths with no operation marker: unusual, so report them as they are.
        label = "Unmerged (no operation in progress)"
    facts.extend(f"{label}: {item.get('path')} ({item.get('state')})."
                 for item in state.conflicted)
    if state.bisecting:
        facts.append("A bisect is in progress.")
    return facts


def _worktree_fact(state: _State) -> str:
    if not (state.staged_only or state.unstaged_only or state.both or state.untracked):
        return "Working tree: no changes and no untracked files."
    return (f"Working tree: {_plural(state.staged_only, 'file')} staged only, "
            f"{state.unstaged_only} unstaged only, {state.both} with both staged and unstaged "
            f"changes, {state.untracked} untracked.")


def _operation_steps(state: _State) -> list[str]:
    if not (state.operation and state.commands):
        return []
    operation = state.operation
    cont = _code(str(state.commands.get("continue")))
    abort = _code(str(state.commands.get("abort")))
    if state.conflicted:
        finish = (f"Finish the {operation}: resolve each conflicted file, "
                  f"{_code('git add <path>')} each one, then {cont}.")
    else:
        finish = f"No conflicted files remain: {cont} finishes the {operation}."
    if state.staged:
        # continue commits the whole index, not only the operation's own files.
        finish += (" Everything staged, including unrelated staged changes, goes into the "
                   "commit it creates; unstage what does not belong first.")
    loss = (f"Or back out with {abort}: it returns to where you were before the {operation} "
            "started and discards any conflict resolution made so far.")
    if operation == "merge" and state.staged:
        # Observed with git 2.43 (and pinned by a test): abort resets the index and the
        # files it names, so fully staged work is deleted without an error; a staged file
        # that also has unstaged edits makes abort refuse instead.
        loss += (" Abort also throws away changes you have staged since the merge started "
                 "(a newly added file is deleted), or refuses if a staged file also has "
                 "unstaged edits: copy anything you need first.")
    elif operation == "merge":
        loss += (" If you had uncommitted changes when the merge started, git may not be "
                 "able to restore them.")
    return [finish, loss]


def _divergence_step(upstream: str, ahead: int, behind: int) -> list[str]:
    if ahead and behind:
        return [f"The branch and {upstream} have diverged: a push would be rejected until "
                f"you integrate them ({_code('git pull --rebase')} rewrites your local commits "
                f"on top; {_code('git pull --no-rebase')} adds a merge commit). Do not "
                "force-push shared history without agreement."]
    if behind:
        return [f"{_code('git pull')} brings in the {_plural(behind, 'commit')} from {upstream}."]
    if ahead:
        return [f"{_code('git push')} publishes the {_plural(ahead, 'local commit')}."]
    return []


def _upstream_steps(state: _State) -> list[str]:
    if state.detached or state.unborn:
        return []
    if state.upstream is None:
        track = _code(f"git branch --set-upstream-to=<remote>/{state.head}")
        publish = _code(f"git push -u <remote> {state.head}")
        return [f"To track a remote branch, {track}; to publish this branch, {publish}."]
    if state.divergence is None:
        return [f"{_code('git fetch')} updates what git knows about {state.upstream}; if it was "
                f"deleted on the remote, {_code('git branch --unset-upstream')} removes the "
                "stale setting."]
    return _divergence_step(state.upstream, *state.divergence)


def _other_steps(state: _State) -> list[str]:
    steps: list[str] = []
    if state.bisecting:
        steps.append(f"When the search is done, {_code('git bisect reset')} returns to the "
                     "commit you started from.")
    if state.detached and not state.operation:
        steps.append("Commits made here belong to no branch. To keep them, create one at this "
                     f"commit with {_code('git switch -c <new-branch>')} before you switch away; "
                     "afterwards they can only be found through the reflog.")
    if state.both and not state.operation:
        verb = "has" if state.both == 1 else "have"
        steps.append(f"{_plural(state.both, 'file')} {verb} staged and unstaged changes: "
                     f"{_code('git commit')} records only the staged part. "
                     f"{_code('git diff --staged')} shows what would be committed; "
                     f"{_code('git diff')} shows what would not.")
    return steps


def explain_repository_state(data: Mapping[str, Any]) -> str:
    """Render ``git_status`` data as observed facts, then next steps the user may run."""
    state = _State.read(data)
    facts = [*_branch_facts(state), *_operation_facts(state), _worktree_fact(state)]
    steps = [*_operation_steps(state), *_upstream_steps(state), *_other_steps(state)]
    lines = ["Repository state, observed now with git status (nothing was changed):"]
    lines.extend(f"- {fact}" for fact in facts)
    if steps:
        lines.append("Next steps, for you to run (Local Code Agent will not run them):")
        lines.extend(f"- {step}" for step in steps)
    else:
        lines.append("Nothing needs attention: no operation is in progress and nothing is changed.")
    return "\n".join(lines)


def _tool_payload(messages: Sequence[Mapping[str, Any]]) -> dict[str, Any] | None:
    for message in messages:
        if message.get("role") != "tool":
            continue
        try:
            payload = json.loads(str(message.get("content") or "{}"))
        except json.JSONDecodeError:
            return {}
        return payload if isinstance(payload, dict) else {}
    return None


class RepositoryStatePlan:
    """The worker for 'what state is my repo in': one git_status call, then the explanation.

    It never decides anything beyond rendering: when git status did not run, the
    answer says so and claims nothing about the repository.
    """

    model_free: Final = True

    def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,  # noqa: ARG002 - LLMClient shape
        max_tokens: int | None = None,  # noqa: ARG002 - LLMClient shape
    ) -> ChatResponse:
        payload = _tool_payload(messages)
        if payload is None:
            return ChatResponse(tool_calls=[tool_call(_STEP, {}, "state-0")])
        data = payload.get("data")
        if payload.get("execution") == "ok" and isinstance(data, dict):
            summary = explain_repository_state(data)
        else:
            summary = ("git status could not be run, so the repository state is unknown: "
                       f"{payload.get('summary') or 'no detail was reported'}")
        return ChatResponse(tool_calls=[tool_call("submit_answer", {
            "claim": "diagnosis", "summary": summary, "evidence_ids": [f"{_STEP}:0"],
        }, "state-1")])


__all__ = [
    "REPOSITORY_STATE",
    "REPOSITORY_STATE_PROCEDURE",
    "RepositoryStatePlan",
    "explain_repository_state",
    "repository_state_sha256",
]
