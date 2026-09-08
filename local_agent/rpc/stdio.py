"""Newline-delimited JSON RPC over stdio.

This is the seam a VS Code extension plugs into later. Keeping it here from the
start means the CLI and the editor drive exactly the same orchestrator, so a
bug can never be "only in the extension".

Protocol, one JSON object per line:

    -> {"id": 1, "method": "skills"}
    -> {"id": 2, "method": "run", "params": {"task": "...", "skill": "..."}}
    <- {"id": 2, "event": "tool", "payload": {...}}
    <- {"id": 2, "result": {"answer": "...", "state": {...}}}

Approval requests surface as events; the host answers with an `approve` method
carrying the same request id.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, TextIO

from ..agent import Orchestrator, SkillLibrary
from ..agent.skills import default_search_path
from ..agent.policy import Decision
from ..config import ModelConfig, find_repo_root, load_repo_config
from ..llm.client import OpenAICompatibleClient
from ..tools import build_registry
from ..tools.base import Tool


class StdioServer:
    def __init__(
        self,
        repo_root: Path,
        model: ModelConfig,
        stdin: TextIO | None = None,
        stdout: TextIO | None = None,
        auto_approve: bool = False,
    ) -> None:
        root = find_repo_root(repo_root)
        self.repo = load_repo_config(root)
        self.registry, _, _ = build_registry(self.repo)
        self.skills = SkillLibrary.discover_many(
            default_search_path(root, self.repo.skills_dir)
        )
        self.model = model
        self.client = OpenAICompatibleClient(model)
        self.stdin = stdin or sys.stdin
        self.stdout = stdout or sys.stdout
        self.auto_approve = auto_approve
        self._current_id: int | None = None

    # ------------------------------------------------------------------ io

    def _send(self, payload: dict[str, Any]) -> None:
        self.stdout.write(json.dumps(payload, default=str) + "\n")
        self.stdout.flush()

    def _emit(self, event: str, payload: dict[str, Any]) -> None:
        self._send({"id": self._current_id, "event": event, "payload": payload})

    def _approve(self, tool: Tool, arguments: dict[str, Any], decision: Decision) -> bool:
        self._emit(
            "approval_required",
            {"tool": tool.name, "risk": tool.risk.value,
             "reason": decision.reason, "arguments": arguments},
        )
        if self.auto_approve:
            return True
        line = self.stdin.readline()
        if not line:
            return False
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            return False
        return bool(msg.get("method") == "approve" and msg.get("params", {}).get("granted"))

    # -------------------------------------------------------------- methods

    def handle(self, msg: dict[str, Any]) -> dict[str, Any]:
        method = msg.get("method")
        params = msg.get("params") or {}
        self._current_id = msg.get("id")

        if method == "ping":
            return {"ok": True, "repo": self.repo.name}

        if method == "skills":
            return {
                "skills": [
                    {"name": n, "description": (s.description if (s := self.skills.get(n)) else "")}
                    for n in self.skills.names()
                ]
            }

        if method == "tools":
            return {
                "tools": [
                    {"name": n, "risk": self.registry.get(n).risk.value,
                     "description": self.registry.get(n).description}
                    for n in self.registry.names()
                ]
            }

        if method == "route":
            return {"ranking": self.skills.rank(str(params.get("task", "")))}

        if method == "run":
            orch = Orchestrator(
                repo=self.repo,
                registry=self.registry,
                client=self.client,
                skills=self.skills,
                approval=self._approve,
                observer=self._emit,
                context_budget_tokens=self.model.context_budget_tokens,
            )
            result = orch.run(str(params.get("task", "")), skill_name=params.get("skill"))
            return {"answer": result.answer, "state": result.state.as_dict()}

        raise ValueError(f"unknown method {method!r}")

    def serve_forever(self) -> None:
        for line in self.stdin:
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError as exc:
                self._send({"id": None, "error": f"bad json: {exc}"})
                continue
            try:
                result = self.handle(msg)
                self._send({"id": msg.get("id"), "result": result})
            except Exception as exc:
                self._send({"id": msg.get("id"), "error": f"{type(exc).__name__}: {exc}"})


def main() -> int:  # pragma: no cover
    server = StdioServer(Path("."), ModelConfig.from_env())
    server.serve_forever()
    return 0
