"""Trusted durable-admission adapter for conversation-originated tasks.

Conversation prose may propose work, but it cannot manufacture admission authority.
This module derives the durable request identity and execution-contract digest from the
saved user turn plus the controller's effective trusted configuration, then delegates
to ``DurableTaskExecutor``. The executor commits admission before controller effects.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID, uuid5

from ..provenance import source_sha256
from .contracts import RouteSource, TaskResult
from .session_event_service import DurableTaskExecutor


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def repository_id(repo) -> str:
    """Stable local identity for one configured checkout without exposing its path."""
    root_digest = hashlib.sha256(str(repo.root.resolve()).encode("utf-8")).hexdigest()[:16]
    return f"{repo.name}:{root_digest}"


def execution_contract_sha256(controller) -> str:
    """Hash the source plus effective controller configuration that can affect effects."""
    repo = controller.repo
    profiles: dict[str, Any] = {}
    for name, profile in sorted(repo.profiles.items()):
        profiles[name] = {
            "configure": list(profile.configure),
            "build": list(profile.build),
            "test": list(profile.test),
            "env_sha256": {
                key: hashlib.sha256(value.encode("utf-8")).hexdigest()
                for key, value in sorted(profile.env.items())
            },
        }
    contract = {
        "source_sha256": source_sha256(),
        "repository_id": repository_id(repo),
        "build_dir": repo.build_dir,
        "run_dir": repo.run_dir,
        "skills_dir": repo.skills_dir,
        "default_profile": repo.default_profile,
        "profiles": profiles,
        "policy": asdict(repo.policy),
        "allow_execution": bool(controller.allow_execution),
        "context_budget_tokens": int(controller.context_budget_tokens),
    }
    return hashlib.sha256(_canonical_bytes(contract)).hexdigest()


def _turn_ref(value: dict[str, object]) -> dict[str, object]:
    required = {"conversation_id", "turn_index", "turn_sha256"}
    if not isinstance(value, dict) or set(value) != required:
        raise ValueError("durable task admission requires an exact saved user TurnRef")
    return {
        "conversation_id": value["conversation_id"],
        "turn_index": value["turn_index"],
        "turn_sha256": value["turn_sha256"],
    }


def _deadline_utc(controller) -> str:
    # Whole-task cancellation is a later Session Hub slice. The v1 admission
    # contract nevertheless requires a deadline, so record a conservative envelope
    # over the controller's existing per-command and bounded-call limits. This field
    # is provenance only for now; it must not be rendered as proof of cleanup.
    policy = controller.repo.policy
    seconds = max(
        3600,
        int(policy.command_timeout_seconds) * max(1, int(policy.max_tool_calls)) + 3600,
    )
    deadline = datetime.now(timezone.utc) + timedelta(seconds=seconds)
    return deadline.isoformat(timespec="seconds").replace("+00:00", "Z")


def _optional_label(value: str | None, *, name: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a nonempty string when present")
    if len(value) > 128:
        raise ValueError(f"{name} exceeds size limit")
    return value


class DurableTaskAdmissionRunner:
    """Synchronous gateway adapter over the non-blocking durable executor."""

    def __init__(self, executor: DurableTaskExecutor):
        self.executor = executor

    def _origin(
        self,
        source: RouteSource,
        *,
        turn_ref: dict[str, object],
        request_id: str,
        rule_id: str | None,
    ) -> dict[str, object]:
        if source == RouteSource.USER_DIRECT:
            if rule_id is not None:
                raise ValueError("user-direct admission cannot carry rule identity")
            return {"kind": "user_direct", "turn_ref": turn_ref}
        if source == RouteSource.MODEL_PROPOSAL:
            if rule_id is not None:
                raise ValueError("model-proposal admission cannot carry rule identity")
            proposal_id = uuid5(UUID(self.executor.service.stream_id), request_id + ":proposal")
            return {
                "kind": "model_proposal",
                "turn_ref": turn_ref,
                "proposal_id": str(proposal_id),
            }
        if source == RouteSource.RULE:
            if rule_id is None:
                raise ValueError("rule admission requires a resolved rule identity")
            return {
                "kind": "user_rule",
                "turn_ref": turn_ref,
                "rule_id": rule_id,
            }
        raise ValueError(f"unsupported route source: {source.value}")

    def run(
        self,
        task: str,
        *,
        turn_ref: dict[str, object],
        self_check: bool = False,
        route_source: RouteSource | str = RouteSource.MODEL_PROPOSAL,
        rule_id: str | None = None,
        skill: str | None = None,
    ) -> TaskResult:
        source = route_source if isinstance(route_source, RouteSource) else RouteSource(route_source)
        saved_turn = _turn_ref(turn_ref)
        rule_id = _optional_label(rule_id, name="rule_id")
        skill = _optional_label(skill, name="skill")
        if source != RouteSource.RULE and rule_id is not None:
            raise ValueError("only rule-origin admission may carry rule identity")
        if source == RouteSource.RULE and rule_id is None:
            raise ValueError("rule admission requires a resolved rule identity")

        request_identity = {
            "turn_ref": saved_turn,
            "route_source": source.value,
            "self_check": bool(self_check),
            "rule_id": rule_id,
            "skill": skill,
        }
        request_id = "gateway:" + hashlib.sha256(_canonical_bytes(request_identity)).hexdigest()
        request_bytes = _canonical_bytes({**request_identity, "task": task})
        payload_sha256 = hashlib.sha256(request_bytes).hexdigest()
        request_artifact_id = uuid5(
            UUID(self.executor.service.stream_id),
            request_id + ":request",
        )
        admission_payload = {
            "origin": self._origin(
                source,
                turn_ref=saved_turn,
                request_id=request_id,
                rule_id=rule_id,
            ),
            "request_ref": {
                "artifact_id": str(request_artifact_id),
                "sha256": payload_sha256,
                "media_type": "application/vnd.lca.task-request+json",
                "size_bytes": len(request_bytes),
                "availability": "retained",
            },
            "contract_sha256": execution_contract_sha256(self.executor.controller),
            "repository_id": repository_id(self.executor.controller.repo),
            "skill": skill,
            "execution_epoch": 0,
            "deadline_utc": _deadline_utc(self.executor.controller),
        }
        handle = self.executor.submit(
            task=task,
            request_id=request_id,
            payload_sha256=payload_sha256,
            admission_payload=admission_payload,
            request_bytes=request_bytes,
            self_check=self_check,
            route_source=source,
        )
        result = handle.wait()
        if result is None:
            raise RuntimeError(
                f"task {handle.task_id} was already admitted; refusing to replay task effects"
            )
        if result.task_id != handle.task_id:
            raise RuntimeError("controller result task id does not match durable admission")
        return result
