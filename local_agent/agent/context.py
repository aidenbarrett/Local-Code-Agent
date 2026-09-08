"""Context construction and compaction.

The rule that matters on a bandwidth-starved box: **the prompt is append-only**.

Every server that caches KV state does so by matching the longest common prefix
of the request against what it already holds. Rewrite any message in the middle
of the history and you throw away the cache from that point onward, which on
DDR4 with a 30B MoE costs tens of seconds of re-prefill. So the context manager
never edits a message it has already sent. When it genuinely has to reclaim
space it performs one explicit compaction, records that it happened, and
accepts the re-prefill knowingly rather than paying a little of it every turn.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from ..config import RepoConfig
from .skills import Skill

SYSTEM_PROMPT = """You are a local engineering agent working inside a C++ repository.

Rules that are enforced outside this prompt, so do not attempt to work around them:
- You cannot run arbitrary shell commands. Only the listed tools exist.
- Build and test commands come from the repository configuration. Never invent one.
- Edits are proposed as diffs first and applied only after approval.
- Paths outside the repository root are rejected.

How to work:
- Establish facts with tools before asserting anything. No guessing at file
  contents, line numbers, symbols or build results.
- Prefer the narrowest tool call that is sufficient for what was asked: a line
  range over a whole file. When the proof the task asks for is project-wide,
  the sufficient call is the project-wide one: a full build, or the whole test
  suite.
- Never claim something builds, passes or is fixed unless a tool result in this
  conversation shows it. Quote the exit code or the failing test name.
- If a tool returns an error, read it and change your approach. Do not repeat an
  identical call.
- When you have the answer, stop calling tools and finish by calling
  submit_answer exactly once. Cite the tool call ids that support it, and keep
  the summary brief and concrete: what you did, what the evidence was, what you
  recommend next. Do not end in prose.
- Choose the claim by what the task asked for and what the evidence shows:
    success       the goal was achieved and a tool result proves it
    diagnosis     the task was to explain a cause, and here it is
    failure       the goal was not achieved
    needs_action  a person has to decide before anything else can happen
"""
# The finishing protocol lives here, in the prompt every condition gets, and
# not in any skill. It is infrastructure, like the tool schemas and the sandbox
# rules, and it was previously taught only by the skill bodies. That made it
# part of the treatment twice over: the control could not be held to a claim it
# was never told to make, and when the orchestrator later nudged it for one,
# the control paid an extra turn for the privilege. Both would have shown up as
# a skill effect that was really a protocol asymmetry.


def build_system_message(repo: RepoConfig, catalogue: str | None) -> dict[str, str]:
    """The rules of engagement, the repository, and what skills exist.

    `catalogue` is None in the no-skill condition. That cell is the control for
    the whole experiment, so it has to differ from the treatment in the
    treatment and nothing else: no procedure text, no toolset narrowing, and no
    catalogue either. A list of named procedures is itself a hint about how the
    work is meant to be decomposed, so leaving it in would smuggle part of the
    treatment into the control and shrink whatever effect there is to measure.
    """
    content = (
        SYSTEM_PROMPT
        + "\n\nRepository: "
        + repo.name
        + f"\nBuild profiles: {', '.join(sorted(repo.profiles))} "
        + f"(default: {repo.default_profile})"
        + "\nPermissions: "
        + json.dumps(
            {
                "build": repo.policy.allow_build,
                "test": repo.policy.allow_test,
                "patch": repo.policy.allow_patch,
                "commit": repo.policy.allow_commit,
            }
        )
    )
    if catalogue is not None:
        content += "\n\nAvailable skills:\n" + catalogue
    return {"role": "system", "content": content}


def build_skill_message(skill: Skill) -> dict[str, str]:
    extra = ""
    if skill.references:
        extra += (
            "\n\nAdditional references available on request (ask for them by name "
            "and they will be provided): " + ", ".join(skill.references)
        )
    return {
        "role": "system",
        "content": f"Active skill: {skill.name}\n\n{skill.body}{extra}",
    }


def tool_result_message(call_id: str, name: str, payload: str) -> dict[str, Any]:
    return {"role": "tool", "tool_call_id": call_id, "name": name, "content": payload}


def approximate_tokens(messages: list[dict[str, Any]]) -> int:
    """Cheap proxy. Four characters per token is close enough for budgeting."""
    total = 0
    for m in messages:
        content = m.get("content") or ""
        if isinstance(content, list):
            content = json.dumps(content)
        total += len(str(content))
        for call in m.get("tool_calls") or []:
            total += len(json.dumps(call, default=str))
    return total // 4


def _collapse(message: dict[str, Any]) -> dict[str, Any]:
    """Reduce a tool result to its summary line, keeping the artifact handle."""
    raw = message.get("content") or ""
    try:
        parsed = json.loads(raw)
        summary = parsed.get("summary", "")
        ok = parsed.get("ok")
        artifacts = parsed.get("artifacts", [])
    except (ValueError, AttributeError):
        summary, ok, artifacts = str(raw)[:200], None, []
    out = dict(message)
    out["content"] = json.dumps(
        {
            "ok": ok,
            "summary": summary,
            "artifacts": artifacts,
            "collapsed": True,
            "note": "Earlier result; payload dropped. Re-run the tool if needed.",
        }
    )
    return out


@dataclass
class CompactionEvent:
    at_message_index: int
    tokens_before: int
    tokens_after: int
    collapsed_results: int

    @property
    def tokens_reclaimed(self) -> int:
        return self.tokens_before - self.tokens_after


@dataclass
class ContextManager:
    """Owns the message list and the decision of when to break the prefix.

    `budget_tokens` should be set generously. Compaction is a cost, not a
    saving: it is only worth paying when the alternative is exceeding what the
    server will accept. On an NPU pipeline compiled with a fixed
    `MAX_PROMPT_LEN`, set `budget_tokens` a few hundred below that value, since
    overrunning it there is not an error, it is garbage output.
    """

    budget_tokens: int = 12_000
    keep_recent_tool_results: int = 6
    hysteresis: float = 0.55
    messages: list[dict[str, Any]] = field(default_factory=list)
    compactions: list[CompactionEvent] = field(default_factory=list)

    # ------------------------------------------------------------ appending

    def append(self, message: dict[str, Any]) -> None:
        self.messages.append(message)

    def extend(self, messages: list[dict[str, Any]]) -> None:
        self.messages.extend(messages)

    @property
    def tokens(self) -> int:
        return approximate_tokens(self.messages)

    @property
    def stable_prefix_len(self) -> int:
        """Messages that have never been rewritten, so the server can cache them."""
        if not self.compactions:
            return len(self.messages)
        return len(self.messages) - self.compactions[-1].at_message_index

    # ----------------------------------------------------------- compaction

    def needs_compaction(self) -> bool:
        return self.tokens > self.budget_tokens

    def compact(self) -> CompactionEvent | None:
        """Collapse old tool payloads in one go. Invalidates the KV prefix.

        Compacts down to `hysteresis` of the budget rather than to exactly the
        budget, so the next append does not immediately trigger another one.
        A second compaction costs a second full re-prefill.
        """
        before = self.tokens
        if before <= self.budget_tokens:
            return None

        target = int(self.budget_tokens * self.hysteresis)
        tool_indices = [i for i, m in enumerate(self.messages) if m.get("role") == "tool"]
        collapsible = (
            tool_indices[: -self.keep_recent_tool_results]
            if self.keep_recent_tool_results
            else tool_indices
        )

        first_touched = len(self.messages)
        collapsed = 0
        for idx in collapsible:
            try:
                already = json.loads(self.messages[idx].get("content") or "{}")
            except (ValueError, TypeError):
                already = {}
            if isinstance(already, dict) and already.get("collapsed"):
                continue
            self.messages[idx] = _collapse(self.messages[idx])
            first_touched = min(first_touched, idx)
            collapsed += 1
            if self.tokens <= target:
                break

        if not collapsed:
            return None

        event = CompactionEvent(
            at_message_index=first_touched,
            tokens_before=before,
            tokens_after=self.tokens,
            collapsed_results=collapsed,
        )
        self.compactions.append(event)
        return event

    def for_request(self) -> list[dict[str, Any]]:
        """The message list to send. Identical to what was sent last turn plus
        whatever was appended since, unless a compaction just happened."""
        return self.messages

    def stats(self) -> dict[str, Any]:
        return {
            "messages": len(self.messages),
            "approx_tokens": self.tokens,
            "budget_tokens": self.budget_tokens,
            "compactions": len(self.compactions),
            "tokens_reclaimed": sum(c.tokens_reclaimed for c in self.compactions),
            "stable_prefix_messages": self.stable_prefix_len,
        }


def trim(
    messages: list[dict[str, Any]],
    budget_tokens: int,
    keep_recent_tool_results: int = 4,
) -> list[dict[str, Any]]:
    """Deprecated. Kept because it is a useful pure function to test against.

    Prefer `ContextManager`, which knows how often it has broken the prefix and
    therefore how much re-prefill it has cost you.
    """
    manager = ContextManager(
        budget_tokens=budget_tokens,
        keep_recent_tool_results=keep_recent_tool_results,
        messages=[dict(m) for m in messages],
        hysteresis=1.0,
    )
    manager.compact()
    return manager.messages
