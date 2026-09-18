"""Product adapter onto the existing hardened orchestrator, not a second agent loop."""
from __future__ import annotations

from dataclasses import replace
from uuid import uuid4

from ..agent import Orchestrator, SkillLibrary, default_search_path
from ..agent.policy import deny_all_approvals
from ..config import RepoConfig
from ..tools import build_registry
from .contracts import TaskResult
from .events import EventBuffer


class TaskController:
    def __init__(self, repo: RepoConfig, worker_factory, events: EventBuffer,
                 *, allow_execution: bool = False, context_budget_tokens: int = 12_000):
        # Product v0 cannot write source or history, even if repo policy permits it.
        self.repo = replace(repo, policy=replace(
            repo.policy, allow_patch=False, allow_commit=False,
            allow_build=allow_execution and repo.policy.allow_build,
            allow_test=allow_execution and repo.policy.allow_test,
        ))
        self.worker_factory = worker_factory
        self.events = events
        self.allow_execution = allow_execution
        self.context_budget_tokens = context_budget_tokens

    def run(self, task: str, *, self_check: bool = False) -> TaskResult:
        task_id = uuid4().hex
        self.events.emit("task.started", {}, task_id)
        try:
            if self_check:
                from .selfcheck import run_self_check
                result = run_self_check(self.repo, task_id, self.events)
            else:
                registry, _ctx, _store = build_registry(self.repo)
                skills = SkillLibrary.discover_many(default_search_path(self.repo.root, self.repo.skills_dir))

                def observe(kind, payload):
                    # Explicit projection: tool arguments/results can contain secrets
                    # and must not automatically enter the activity feed.
                    fields = {
                        "route": ("skill", "tier"), "tool": ("name",),
                        "observe": ("ok",), "llm": ("total_s", "ttft_s", "prompt_tokens", "completion_tokens"),
                        "escalate": ("from", "to"), "blocked": (),
                        "router_uncertain": ("skill", "score"),
                    }
                    if kind in fields:
                        self.events.emit("worker." + kind,
                                         {k: payload[k] for k in fields[kind] if k in payload}, task_id)

                worker = Orchestrator(
                    repo=self.repo, registry=registry, client=self.worker_factory(), skills=skills,
                    approval=deny_all_approvals, observer=observe,
                    context_budget_tokens=self.context_budget_tokens,
                    allow_escalation=False,
                )
                run = worker.run(task)
                result = TaskResult(
                    task_id, run.outcome.value, run.answer,
                    bool(run.outcome.succeeded and run.state.verified),
                    tuple(f"{task_id}/{h.name}:{i}" for i, h in enumerate(run.state.history)),
                    run.state.metrics.as_dict(),
                )
        except Exception as exc:
            # Report type only: exception text may include credentials/paths.
            result = TaskResult(task_id, "error", f"Task stopped: {type(exc).__name__}.")
        except KeyboardInterrupt:
            self.events.emit("task.interrupted", {"process_cleanup_confirmed": False}, task_id)
            raise
        self.events.emit("task.finished", {
            "outcome": result.outcome,
            "verified_at_completion": result.verified_at_completion,
            "evidence_count": len(result.evidence_ids),
        }, task_id)
        return result
