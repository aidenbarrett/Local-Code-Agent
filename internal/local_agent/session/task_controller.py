"""Product adapter onto the existing hardened orchestrator, not a second agent loop."""
from __future__ import annotations

from dataclasses import replace
from uuid import UUID, uuid4

from ..agent import Orchestrator, SkillLibrary, default_search_path
from ..agent.policy import deny_all_approvals
from ..config import RepoConfig
from ..tools import build_registry
from .contracts import RouteSource, TaskOutcome, TaskResult
from .durable_tool_registry import wrap_registry_with_durable_activity
from .event_buffer import EventBuffer
from .execution_source import TaskExecutionSource


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

    def _skill_library(self) -> SkillLibrary:
        return SkillLibrary.discover_many(
            default_search_path(self.repo.root, self.repo.skills_dir)
        )

    def resolve_skill(self, skill_name: str) -> str:
        """Resolve one controller-selected skill before durable admission/effects.

        Routing authority belongs to the controller. A recorded deterministic skill must
        therefore exist in the effective skill search path before work is admitted; the
        worker is never allowed to silently broaden to heuristic/default routing.
        """
        if not isinstance(skill_name, str) or not skill_name.strip():
            raise ValueError("selected skill must be a nonempty string")
        skill = self._skill_library().get(skill_name)
        if skill is None:
            raise ValueError(f"selected skill is not installed: {skill_name}")
        if not skill.tools:
            raise ValueError(f"selected skill has an empty tool allowlist: {skill_name}")
        return skill.name

    def run(
        self,
        task: str,
        *,
        self_check: bool = False,
        route_source: RouteSource | TaskExecutionSource | str = RouteSource.MODEL_PROPOSAL,
        task_id: str | None = None,
        durable_activity=None,
        skill_name: str | None = None,
    ) -> TaskResult:
        # ``route_source`` is retained as the existing call-surface name while the
        # controller now validates the broader execution provenance vocabulary. The
        # conversation layer still owns RouteSource; watch is never a synthetic route.
        source = TaskExecutionSource.coerce(route_source)
        if self_check:
            if skill_name not in (None, "self-check"):
                raise ValueError("self-check execution cannot consume a worker skill")
            resolved_skill = None
        else:
            resolved_skill = self.resolve_skill(skill_name) if skill_name is not None else None
        if task_id is None:
            task_id = uuid4().hex
        else:
            # Durable Session Hub task ids are UUIDs assigned at admission. The
            # controller may consume that identity but never replace it.
            UUID(task_id)
        self.events.emit("task.started", {"route_source": source.value}, task_id)
        try:
            if self_check:
                from .self_check import run_self_check
                result = run_self_check(self.repo, task_id, self.events)
            else:
                registry, _ctx, _store = build_registry(self.repo)
                if durable_activity is not None:
                    # The wrapper commits tool.started before entering an effectful
                    # handler and typed tool.finished afterwards. Policy still lives
                    # in the orchestrator; durable activity is evidence, not authority.
                    registry = wrap_registry_with_durable_activity(registry, durable_activity)
                skills = self._skill_library()

                def observe(kind, payload):
                    fields = {
                        "route": ("skill", "tier"), "tool": ("name",),
                        "observe": ("ok",), "llm": ("total_s", "ttft_s", "prompt_tokens", "completion_tokens"),
                        "escalate": ("from", "to"), "blocked": ("reason", "skill"),
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
                run = worker.run(task, skill_name=resolved_skill)
                verified = bool(run.outcome.succeeded and run.state.verified)
                task_outcome = TaskOutcome(run.outcome.value)
                if task_outcome.succeeded and not verified:
                    task_outcome = TaskOutcome.NO_VERDICT
                result = TaskResult(
                    task_id, task_outcome, run.answer,
                    verified,
                    tuple(f"{h.name}:{i}" for i, h in enumerate(run.state.history)),
                    run.state.metrics.as_dict(),
                    verification_ran=bool(run.state.verification_attempted),
                )
        except KeyboardInterrupt:
            self.events.emit("task.interrupted", {
                "outcome": TaskOutcome.NO_VERDICT.value,
                "process_cleanup_confirmed": False,
            }, task_id)
            raise
        except Exception as exc:
            result = TaskResult(
                task_id,
                TaskOutcome.NO_VERDICT,
                f"Task stopped: {type(exc).__name__}.",
                False,
            )
            self.events.emit("task.interrupted", {
                "outcome": result.outcome.value,
                "process_cleanup_confirmed": False,
            }, task_id)
            return result
        self.events.emit("task.finished", {
            "outcome": result.outcome.value,
            "terminal_state": result.projection.terminal_state.value,
            "verdict": result.projection.verdict.value,
            "verification_ran": result.verification_ran,
            "verified_at_completion": result.verified_at_completion,
            "evidence_count": len(result.evidence_ids),
            "evidence_ids": list(result.evidence_ids),
            "route_source": source.value,
        }, task_id)
        return result
