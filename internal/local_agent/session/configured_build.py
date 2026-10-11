"""Explain configured commands through the existing read-only repo_info owner."""
from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Final

from ..llm.client import tool_call
from ..llm.protocol import ChatResponse

CONFIGURED_BUILD: Final = "configured-build"
CONFIGURED_BUILD_PROCEDURE: Final = "repo-navigation"
_STEP: Final = "repo_info"
_PLAN_VERSION: Final = 1
_PLAIN_ARGUMENT: Final = re.compile(r"[A-Za-z0-9_./:=+,-]+")


def configured_build_sha256(skill_sha256: str) -> str:
    identity = json.dumps({"route": CONFIGURED_BUILD, "plan": _PLAN_VERSION,
                           "steps": [_STEP], "skill_sha256": skill_sha256}, sort_keys=True)
    return hashlib.sha256(f"lca-configured-build:{identity}".encode()).hexdigest()


def explain_configured_build(data: object) -> str:
    """Validate the tool boundary once; report argv without inventing shell quoting."""
    if not isinstance(data, dict):
        raise ValueError("repo_info did not retain its configuration")
    name, default, profiles = data.get("name"), data.get("default_profile"), data.get("profiles")
    if (not isinstance(name, str) or not isinstance(default, str)
            or not isinstance(profiles, dict) or default not in profiles):
        raise ValueError("repo_info did not retain a valid default profile")
    lines = [f"Repository {name}; default profile: {default}.",
             "Configured command arguments (not evidence that a build or test passed):"]
    for profile, commands in profiles.items():
        if not isinstance(profile, str) or not isinstance(commands, dict):
            raise ValueError("repo_info retained an invalid profile")
        lines.append(f"Profile {profile}:")
        for stage in ("configure", "build", "test"):
            argv = commands.get(stage)
            if not isinstance(argv, list) or not all(isinstance(arg, str) for arg in argv):
                raise ValueError(f"repo_info did not retain {stage} command arguments")
            if not argv:
                rendered = "not configured"
            elif all(_PLAIN_ARGUMENT.fullmatch(arg) for arg in argv):
                rendered = " ".join(argv)
            else:
                # Preserve argument boundaries when spaces or shell metacharacters
                # would make a command-line display ambiguous. This is never run.
                rendered = "argument vector " + json.dumps(argv, ensure_ascii=False)
            lines.append(f"  {stage}: {rendered}")
    lines.append("These are the declared commands; no build or test was run for this question.")
    return "\n".join(lines)


class ConfiguredBuildPlan:
    """One read-only tool call, then the recorded configuration; no model or execution."""

    model_free: Final = True

    def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,  # noqa: ARG002 - LLMClient shape
        max_tokens: int | None = None,  # noqa: ARG002 - LLMClient shape
    ) -> ChatResponse:
        for message in reversed(messages):
            if message.get("role") != "tool" or message.get("name") != _STEP:
                continue
            try:
                payload: object = json.loads(str(message.get("content") or "{}"))
                if (not isinstance(payload, dict) or payload.get("execution") != "ok"
                        or payload.get("result") != "pass"):
                    raise ValueError("repo_info could not be read")
                summary = explain_configured_build(payload.get("data"))
            except ValueError as exc:
                summary = f"Configured build/test commands are unknown: {exc}."
            return ChatResponse(tool_calls=[tool_call("submit_answer", {
                "claim": "diagnosis", "summary": summary, "evidence_ids": [f"{_STEP}:0"],
            }, "configuration-1")])
        return ChatResponse(tool_calls=[tool_call(_STEP, {}, "configuration-0")])
