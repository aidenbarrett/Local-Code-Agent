"""Product adapter onto the existing hardened orchestrator, not a second agent loop."""
from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from uuid import UUID, uuid4

from ..agent import Orchestrator, SkillLibrary, default_search_path
from ..agent.policy import deny_all_approvals
from ..agent.state import HaltCause
from ..config import RepoConfig
from ..tools import build_registry
from ..tools.tool_primitives import Risk
from .contracts import RouteSource, TaskOutcome, TaskResult
from .durable_tool_registry import wrap_registry_with_durable_activity
from .event_buffer import EventBuffer
from .execution_source import TaskExecutionSource
from .candidate_change import (
    CANDIDATE_CHANGE_SKILLS,
    candidate_blocker,
    candidate_repo,
    candidate_workspace_approval,
    excluded_dirs,
    settle_candidate,
)
from .proof_binding import binding_from_run


_PROGRAMMER_ERRORS = (TypeError, AttributeError, NameError, AssertionError)


class TaskController:
    def __init__(self, repo: RepoConfig, worker_factory, events: EventBuffer,
                 *, allow_execution: bool = False, context_budget_tokens: int = 12_000,
                 workspaces=None):
        # The user's checkout is never written by a worker, even if repo policy permits
        # it. Source-changing skills run in an LCA-owned candidate worktree instead, and
        # take their patch authority from the declared policy kept here.
        self.declared_repo = repo
        self.workspaces = workspaces
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

    def _resolved_skill(self, skill_name: str):
        if not isinstance(skill_name, str) or not skill_name.strip():
            raise ValueError("selected skill must be a nonempty string")
        skill = self._skill_library().get(skill_name)
        if skill is None:
            raise ValueError(f"selected skill is not installed: {skill_name}")
        if not skill.tools:
            raise ValueError(f"selected skill has an empty tool allowlist: {skill_name}")
        return skill

    def resolve_skill(self, skill_name: str) -> str:
        """Resolve one controller-selected skill before durable admission/effects.

        Routing authority belongs to the controller. A recorded deterministic skill must
        therefore exist in the effective skill search path before work is admitted; the
        worker is never allowed to silently broaden to heuristic/default routing.
        """
        return self._resolved_skill(skill_name).name

    def effective_skill_sha256(self, skill_name: str) -> str:
        """Fingerprint every effective byte beneath the selected skill directory.

        External/repository-local skills can override packaged procedures. Durable
        admission therefore binds the actual selected directory contents, not merely its
        configured search path or skill name. Paths are relative so identical procedure
        bytes have the same identity when installed at a different absolute location.
        Symlinks escaping the skill directory are refused rather than leaving provenance
        dependent on unbound external bytes.
        """
        skill = self._resolved_skill(skill_name)
        root = skill.path.resolve()
        manifest: list[dict[str, object]] = []
        for path in sorted(skill.path.rglob("*"), key=lambda item: item.as_posix()):
            if path.is_symlink():
                resolved = path.resolve()
                if resolved != root and root not in resolved.parents:
                    raise ValueError(f"selected skill contains escaping symlink: {path.name}")
            if not path.is_file():
                continue
            data = path.read_bytes()
            manifest.append({
                "path": path.relative_to(skill.path).as_posix(),
                "sha256": hashlib.sha256(data).hexdigest(),
                "size_bytes": len(data),
            })
        if not manifest:
            raise ValueError(f"selected skill has no fingerprintable files: {skill_name}")
        encoded = json.dumps(
            manifest,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    def process_spawning_tools(self) -> frozenset[str]:
        """Return the effective configured-command tools for cleanup reconciliation."""
        registry, _ctx, _store = build_registry(self.repo)
        return frozenset(
            name for name in registry.names()
            if registry.get(name).risk is Risk.EXECUTE
        )

    @staticmethod
    def _product_outcome(run) -> TaskOutcome:
        """Project worker outcome into product lifecycle without calling outages refusals."""
        outcome = TaskOutcome(run.outcome.value)
        if run.state.halt_cause in (HaltCause.SERVER_UNAVAILABLE, HaltCause.INFERENCE_STALLED):
            return TaskOutcome.NO_VERDICT
        if outcome.succeeded and not run.state.verified:
            return TaskOutcome.NO_VERDICT
        return outcome

    @staticmethod
    def _reason_code(run, outcome: TaskOutcome) -> str:
        """Project typed worker facts into the product reason vocabulary."""
        if run.state.halt_cause is HaltCause.SERVER_UNAVAILABLE:
            return "endpoint_unavailable"
        if run.state.halt_cause is HaltCause.INFERENCE_STALLED:
            return "inference_timeout"
        if run.state.halt_cause is HaltCause.CONTEXT_BUDGET_EXHAUSTED:
            return "unavailable_capability"
        if outcome.succeeded:
            return "verification_passed"
        if outcome in (TaskOutcome.FAIL, TaskOutcome.ESCALATED_FAIL):
            return "verification_failed" if run.state.verification_attempted else "missing_evidence"
        if outcome is TaskOutcome.BLOCKED:
            blocked = next((item for item in run.state.history if item.blocked), None)
            reason = getattr(blocked, "reason", None)
            return {
                "policy_denied": "policy_denied",
                "approval_declined": "user_denied",
                "missing_executable": "missing_dependency",
                "orchestrator_timeout": "tool_timeout",
                "bad_arguments": "invalid_input",
                "invalid_model_response": "invalid_input",
                "unknown_tool": "unavailable_capability",
                "tool_not_allowed": "unavailable_capability",
            }.get(reason, "unavailable_capability")
        if outcome is TaskOutcome.NO_VERDICT:
            return "missing_evidence" if not run.state.verification_attempted else "cleanup_unknown"
        return "cleanup_unknown"

    def _run_candidate_change(self, task, task_id, resolved_skill, source, durable_activity) -> TaskResult:
        """Run a source-changing skill in its own worktree and retain the candidate."""
        blocker = (
            "candidate workspaces are not configured for this session"
            if self.workspaces is None
            else candidate_blocker(self.declared_repo, allow_execution=self.allow_execution)
        )
        if blocker is not None:
            return TaskResult(
                task_id, TaskOutcome.BLOCKED,
                f"No change was prepared: {blocker}.", False,
                reason_code="policy_denied",
            )
        workspace = self.workspaces.create(
            self.declared_repo.root, task_id, excluded_dirs=excluded_dirs(self.declared_repo),
        )
        settled = False
        try:
            work_repo = candidate_repo(
                self.declared_repo, workspace, allow_execution=self.allow_execution,
            )
            registry, _ctx, _store = build_registry(work_repo)
            if durable_activity is not None:
                registry = wrap_registry_with_durable_activity(registry, durable_activity)
            self.events.emit("worker.workspace", {
                "workspace_id": workspace.workspace_id,
                "base_commit": workspace.base_commit,
            }, task_id)
            worker = Orchestrator(
                repo=work_repo, registry=registry, client=self.worker_factory(),
                skills=self._skill_library(), approval=candidate_workspace_approval,
                context_budget_tokens=self.context_budget_tokens, allow_escalation=False,
            )
            run = worker.run(task, skill_name=resolved_skill)
            task_outcome = self._product_outcome(run)
            verified = bool(task_outcome.succeeded and run.state.verified)
            metrics = run.state.metrics.as_dict()
            # Proof identity is the candidate tree the build ran against.
            metrics["proof_binding"] = binding_from_run(task, run, workspace.root).as_dict()
            outcome, _candidate = settle_candidate(
                self.workspaces, workspace, task_id=task_id, verified=verified,
            )
            settled = True
            metrics["candidate"] = outcome.as_metrics(workspace)
            answer = outcome.summary + ("\n\n" + run.answer if run.answer else "")
            return TaskResult(
                task_id, task_outcome, answer, verified,
                tuple(f"{h.name}:{i}" for i, h in enumerate(run.state.history)),
                metrics,
                verification_ran=bool(run.state.verification_attempted),
                reason_code=self._reason_code(run, task_outcome),
            )
        finally:
            if not settled:
                # Nothing reviewable was produced; never leave an orphaned worktree.
                self.workspaces.discard(workspace)

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
            elif resolved_skill in CANDIDATE_CHANGE_SKILLS:
                result = self._run_candidate_change(
                    task, task_id, resolved_skill, source, durable_activity,
                )
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
                task_outcome = self._product_outcome(run)
                verified = bool(task_outcome.succeeded and run.state.verified)
                metrics = run.state.metrics.as_dict()
                metrics["proof_binding"] = binding_from_run(task, run, self.repo.root).as_dict()
                result = TaskResult(
                    task_id, task_outcome, run.answer,
                    verified,
                    tuple(f"{h.name}:{i}" for i, h in enumerate(run.state.history)),
                    metrics,
                    verification_ran=bool(run.state.verification_attempted),
                    reason_code=self._reason_code(run, task_outcome),
                )
        except KeyboardInterrupt:
            self.events.emit("task.interrupted", {
                "outcome": TaskOutcome.NO_VERDICT.value,
                "process_cleanup_confirmed": False,
            }, task_id)
            raise
        except _PROGRAMMER_ERRORS:
            # Programmer faults are never ordinary task outcomes. The durable executor
            # retains the traceback and then re-raises the original exception to caller.
            self.events.emit("task.interrupted", {
                "outcome": TaskOutcome.NO_VERDICT.value,
                "process_cleanup_confirmed": False,
            }, task_id)
            raise
        except Exception as exc:
            # Operational/controller failures remain typed task failures rather than
            # escaping as programmer faults. Durable execution still records truthful
            # cleanup from tool activity when this result is terminalised.
            result = TaskResult(
                task_id,
                TaskOutcome.NO_VERDICT,
                f"Task stopped: {type(exc).__name__}.",
                False,
                reason_code="controller_fault",
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
            "reason_code": result.reason_code,
            "verification_ran": result.verification_ran,
            "verified_at_completion": result.verified_at_completion,
            "evidence_count": len(result.evidence_ids),
            "evidence_ids": list(result.evidence_ids),
            "route_source": source.value,
        }, task_id)
        return result
