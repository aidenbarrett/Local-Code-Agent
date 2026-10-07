"""Product adapter onto the existing hardened orchestrator, not a second agent loop."""
from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Final, Protocol
from uuid import UUID, uuid4

from ..agent import Orchestrator, SkillLibrary, default_search_path
from ..agent.policy import deny_all_approvals
from ..agent.state import HaltCause
from ..config import RepoConfig
from ..tools import build_registry
from ..tools.tool_primitives import Risk, ToolRegistry
from ..verification import CONTRADICTS_CURRENT_TREE, ProofKind
from .contracts import RouteSource, TaskOutcome, TaskResult
from .durable_tool_registry import wrap_registry_with_durable_activity
from .event_buffer import EventBuffer
from .execution_source import TaskExecutionSource
from .candidate_change import (
    APPLY_CANDIDATE_ACTION,
    UNDO_CANDIDATE_ACTION,
    undo_candidate,
    COMMIT_CANDIDATE_ACTION,
    commit_candidate,
    DIFF_CANDIDATE_ACTION,
    diff_candidate,
    DISCARD_CANDIDATE_ACTION,
    discard_candidate,
    CANDIDATE_CHANGE_SKILLS,
    CANDIDATE_PROOF,
    candidate_proof_satisfied,
    CONTROLLER_ACTIONS,
    apply_candidate,
    controller_action_sha256,
    CandidateBlocker,
    candidate_blockers,
    candidate_repo,
    candidate_workspace_approval,
    excluded_dirs,
    settle_candidate,
)
from .configured_checks import (
    CONFIGURED_CHECK_SKILL,
    CONFIGURED_CHECKS,
    RUN_BUILD_CHECK,
    RUN_TEST_CHECK,
    ConfiguredCheckPlan,
    configured_check_sha256,
)
from .proof_binding import binding_from_run
from .workspaces import Workspace, WorkspaceError, WorkspaceStoppedError

if TYPE_CHECKING:
    from ..agent.orchestrator import RunResult
    from ..agent.skills import Skill
    from ..llm.client import LLMClient
    from ..tools.process_runner import CancellationProbe
    from .durable_activity import DurableToolActivity
    from .workspaces import GitWorkspaceManager, WorkspaceReadiness

# A controller action is deterministic and model-free: (manager, declared repository,
# task id, request text) -> result. One table owns which names are actions and what
# each one runs, so dispatch cannot drift from CONTROLLER_ACTIONS.
class ControllerAction(Protocol):
    def __call__(
        self, manager: GitWorkspaceManager | None, declared: RepoConfig, /,
        *, task_id: str, request_text: str,
    ) -> TaskResult: ...


_CONTROLLER_ACTION_HANDLERS: Final[Mapping[str, ControllerAction]] = {
    APPLY_CANDIDATE_ACTION: apply_candidate,
    UNDO_CANDIDATE_ACTION: undo_candidate,
    COMMIT_CANDIDATE_ACTION: commit_candidate,
    DIFF_CANDIDATE_ACTION: diff_candidate,
    DISCARD_CANDIDATE_ACTION: discard_candidate,
}
if frozenset(_CONTROLLER_ACTION_HANDLERS) != CONTROLLER_ACTIONS:
    raise ImportError("every controller action needs exactly one handler")


_PROGRAMMER_ERRORS = (TypeError, AttributeError, NameError, AssertionError)


# Typed worker block reasons projected into the product reason vocabulary. Anything not
# listed (including no recorded reason) is an unavailable capability.
_BLOCKED_REASON_CODES: Final[Mapping[str, str]] = {
    "policy_denied": "policy_denied",
    "approval_declined": "user_denied",
    "missing_executable": "missing_dependency",
    "orchestrator_timeout": "tool_timeout",
    "output_limit": "tool_timeout",  # a tool-side bound ended it (v1 vocabulary)
    "cleanup_unknown": "cleanup_unknown",
    "command_cancelled": "cancelled",
    "bad_arguments": "invalid_input",
    "invalid_model_response": "invalid_input",
    "unknown_tool": "unavailable_capability",
    "tool_not_allowed": "unavailable_capability",
}


@dataclass(frozen=True, slots=True)
class _ExecutionScope:
    """The one admitted execution a worker runs inside: its identity and its controls."""

    task_id: str
    durable_activity: DurableToolActivity | None
    cancellation_probe: CancellationProbe | None


# The configured check that proves each candidate skill when its worker did not:
# a build fix needs a full build, and a test fix or a change needs a full test run
# (run_test refuses stale binaries, so the plan builds first).
_CONTROLLER_CHECK = {
    "fix-build-failure": RUN_BUILD_CHECK,
    "fix-test-failure": RUN_TEST_CHECK,
    "implement-change": RUN_TEST_CHECK,
}


@dataclass(frozen=True, slots=True)
class _CandidateProof:
    outcome: TaskOutcome
    verified: bool
    reason_code: str
    proof_run: RunResult
    check: RunResult | None


def _controller_check_line(skill: str, check: RunResult, *, verified: bool) -> str:
    what = ("full build" if _CONTROLLER_CHECK[skill] == RUN_BUILD_CHECK
            else "full build and test run")
    if verified:
        return f"The controller ran the configured {what} on the candidate: it passed."
    if _current_tree_observed_failure(check):
        return f"The controller ran the configured {what} on the candidate: it failed."
    return (f"The controller could not complete the configured {what} on the candidate, "
            "so the change is not proven.")


def _needs_controller_check(run: RunResult) -> bool:
    """Whether an unproven candidate is the controller's to check.

    Only an edited candidate whose worker ran to completion and did not report a
    failure, a diagnosis or a needed action qualifies. A worker that observed the
    current tree fail has already produced the verdict, and one that halted on an
    outage or budget may have left a half-made change nobody should certify.
    """
    state = run.state
    return (
        state.mutation_epoch > 0
        and state.halt_cause is None
        and state.claim in (None, "success")
        and not _current_tree_observed_failure(run)
    )


def _process_cleanup_unknown(run: RunResult) -> bool:
    """Did any tool run end with a process tree the runner could not show ended?

    Read from the typed tool record (Reason.CLEANUP_UNKNOWN), never from prose.
    """
    return any(item.reason == "cleanup_unknown" for item in run.state.history)


def _current_tree_observed_failure(run: RunResult) -> bool:
    """Whether the last proof-bearing call on the current tree observed a failure."""
    epoch = run.state.mutation_epoch
    last: str | None = None
    for record in run.state.history:
        if record.epoch != epoch:
            continue
        if record.proof != ProofKind.NO_CURRENT_PROOF.value:
            last = record.proof
    return last in {k.value for k in CONTRADICTS_CURRENT_TREE}


def _stopped_answer(
    stopped: WorkspaceStoppedError | None, *, workspace_existed: bool, discarded: bool,
) -> str:
    """Say exactly what a Stop left behind, from typed evidence, never more."""
    if not workspace_existed:
        lead = "Stopped before the isolated copy was ready. No candidate exists."
    elif discarded:
        lead = "Stopped. The isolated candidate was discarded; nothing is available to apply."
    else:
        lead = (
            "Stopped, but the isolated candidate could not be shown removed; nothing was "
            "retained for apply, and the leftover is kept for orphan cleanup."
        )
    parts = [lead, "Your files, index and branches were not changed."]
    if stopped is not None:
        if stopped.worktree_removed is False:
            parts[0] = (
                "Stopped while the isolated copy was being made, and that half-made copy "
                "could not be shown removed; it is kept for orphan cleanup."
            )
        if stopped.process_cleanup_confirmed is False:
            parts.append(
                f"A running git {stopped.step} was ended; that its whole process tree "
                "ended could not be confirmed."
            )
        if stopped.writes_finished:
            parts.append(
                "Git steps that had already started writing were allowed to finish ("
                + ", ".join(stopped.writes_finished)
                + "); they can leave unreferenced objects in the repository's object "
                "store, which git gc removes."
            )
    return " ".join(parts)


def _stopped_metrics(
    stopped: WorkspaceStoppedError | None, *, workspace_existed: bool, discarded: bool,
) -> dict[str, object]:
    candidate: dict[str, object] = {
        "retained": False,
        "stopped": True,
        "workspace_existed": workspace_existed,
        "workspace_removed": discarded if workspace_existed else None,
    }
    if stopped is not None:
        candidate["stop"] = stopped.evidence()
    return {"candidate": candidate}


def _stopped_result(
    task_id: str, stopped: WorkspaceStoppedError | None, *,
    workspace_existed: bool, discarded: bool,
) -> TaskResult:
    # A Stop whose cleanup was not shown complete is not a clean "cancelled": it is
    # the established terminal for unresolved cleanup, NO_VERDICT / cleanup_unknown,
    # and the result carries the evidence of what is unknown (#437 review).
    cleanup_unknown = (
        (workspace_existed and not discarded)
        or (stopped is not None and stopped.worktree_removed is False)
        or (stopped is not None and stopped.process_cleanup_confirmed is False)
    )
    return TaskResult(
        task_id, TaskOutcome.NO_VERDICT if cleanup_unknown else TaskOutcome.BLOCKED,
        _stopped_answer(stopped, workspace_existed=workspace_existed, discarded=discarded),
        False,
        metrics=_stopped_metrics(
            stopped, workspace_existed=workspace_existed, discarded=discarded,
        ),
        reason_code="cleanup_unknown" if cleanup_unknown else "cancelled",
    )


def _prepare_stoppable(
    manager: GitWorkspaceManager, repo: RepoConfig, task_id: str,
    cancellation_probe: CancellationProbe | None,
) -> WorkspaceReadiness | Workspace | TaskResult:
    """Readiness, then the candidate workspace, both under the task's Stop.

    Returns the readiness when it failed (so the caller can report it), the workspace,
    or the stopped result. Creation removes anything it began before raising.
    """
    try:
        with manager.stoppable(cancellation_probe):
            readiness = manager.readiness(repo.root)
            if not readiness.ready:
                return readiness
            return manager.create(repo.root, task_id, excluded_dirs=excluded_dirs(repo))
    except WorkspaceStoppedError as stopped:
        return _stopped_result(
            task_id, stopped,
            workspace_existed=False, discarded=stopped.worktree_removed is not False,
        )


class TaskController:
    def __init__(self, repo: RepoConfig, worker_factory: Callable[[], LLMClient],
                 events: EventBuffer, *, allow_execution: bool = False,
                 context_budget_tokens: int = 12_000,
                 workspaces: GitWorkspaceManager | None = None):
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

    @staticmethod
    def _retain_rejected_tool(
        activity: DurableToolActivity, payload: Mapping[str, object]
    ) -> None:
        """Retain a refusal that happened before a registered handler was entered."""
        name = str(payload["name"])
        opened = activity.start_tool(name)
        activity.finish_tool(
            call_id=opened.call_id,
            tool_name=name,
            execution="error",
            domain="unknown",
            reason=str(payload["reason"]),
            exit_code=None,
            duration_ms=0,
            failure_detail=str(payload["detail"]),
        )

    def _skill_library(self) -> SkillLibrary:
        return SkillLibrary.discover_many(
            default_search_path(self.repo.root, self.repo.skills_dir)
        )

    def _resolved_skill(self, skill_name: object) -> Skill:
        if not isinstance(skill_name, str) or not skill_name.strip():
            raise ValueError("selected skill must be a nonempty string")
        skill = self._skill_library().get(skill_name)
        if skill is None:
            raise ValueError(f"selected skill is not installed: {skill_name}")
        if not skill.tools:
            raise ValueError(f"selected skill has an empty tool allowlist: {skill_name}")
        return skill

    def resolve_skill(self, skill_name: str) -> str:
        if skill_name in CONTROLLER_ACTIONS:
            return skill_name
        if skill_name in CONFIGURED_CHECKS:
            # The check runs under the build-and-test procedure, which must exist.
            self._resolve_worker_skill(CONFIGURED_CHECK_SKILL)
            return skill_name
        return self._resolve_worker_skill(skill_name)

    def _resolve_worker_skill(self, skill_name: str) -> str:
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
        if skill_name in CONTROLLER_ACTIONS:
            return controller_action_sha256(skill_name)
        if skill_name in CONFIGURED_CHECKS:
            return configured_check_sha256(
                skill_name, self.effective_skill_sha256(CONFIGURED_CHECK_SKILL),
            )
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
    def _product_outcome(run: RunResult) -> TaskOutcome:
        """Project worker outcome into product lifecycle without calling outages refusals."""
        outcome = TaskOutcome(run.outcome.value)
        if run.state.halt_cause in (
            HaltCause.SERVER_UNAVAILABLE, HaltCause.INFERENCE_STALLED, HaltCause.STOPPED,
        ):
            return TaskOutcome.NO_VERDICT
        if run.state.halt_cause is HaltCause.CONTEXT_BUDGET_EXHAUSTED:
            # The profile could not hold the task. Nothing about the code was decided,
            # so this is not a FAILED verdict, whatever the worker's own outcome was.
            return TaskOutcome.BLOCKED
        if _process_cleanup_unknown(run):
            # A command's process tree could not be shown ended, so its effects are not
            # owned: neither a pass nor an engineering failure (#436 review).
            return TaskOutcome.NO_VERDICT
        if outcome.succeeded and not run.state.verified:
            # The worker completed its job, but the tree is not proven. If the last
            # current-epoch proof is an observed build or test failure, the honest
            # product answer is that the check FAILED, not that the outcome is unknown.
            if _current_tree_observed_failure(run):
                return TaskOutcome.FAIL
            return TaskOutcome.NO_VERDICT
        return outcome

    @staticmethod
    def _answer_with_model_failure(answer: str, run: RunResult) -> str:
        """Say why the model could not be used, not only that it could not.

        ``endpoint_unavailable`` alone does not tell anyone whether the server was
        down, refused the connection, or the client could not even be built. The
        orchestrator already recorded the transport's own words in the halt reason.
        """
        if run.state.halt_cause not in (
            HaltCause.SERVER_UNAVAILABLE, HaltCause.INFERENCE_STALLED,
            HaltCause.CONTEXT_BUDGET_EXHAUSTED,
        ):
            return answer
        detail = run.state.halt_reason or ""
        if not detail or detail in answer:
            return answer
        return f"{answer}\n\n{detail}" if answer else detail

    @staticmethod
    def _reason_code(run: RunResult, outcome: TaskOutcome) -> str:
        """Project typed worker facts into the product reason vocabulary."""
        if run.state.halt_cause is HaltCause.SERVER_UNAVAILABLE:
            return "endpoint_unavailable"
        if run.state.halt_cause is HaltCause.INFERENCE_STALLED:
            return "inference_timeout"
        if run.state.halt_cause is HaltCause.CONTEXT_BUDGET_EXHAUSTED:
            return "unavailable_capability"
        if run.state.halt_cause is HaltCause.STOPPED:
            # Stop revokes the execution, so this result never commits; if it ever
            # did, it must not claim a clean cancellation it did not reconcile.
            return "cancel_unreconciled"
        if outcome is TaskOutcome.NO_VERDICT and _process_cleanup_unknown(run):
            return "cleanup_unknown"
        if outcome.succeeded:
            return "verification_passed"
        if outcome in (TaskOutcome.FAIL, TaskOutcome.ESCALATED_FAIL):
            return ("verification_failed" if run.state.verification_attempted
                    else "missing_evidence")
        if outcome is TaskOutcome.BLOCKED:
            blocked = next((item for item in run.state.history if item.blocked), None)
            reason = blocked.reason if blocked is not None else None
            return _BLOCKED_REASON_CODES.get(reason or "", "unavailable_capability")
        if outcome is TaskOutcome.NO_VERDICT:
            return ("missing_evidence" if not run.state.verification_attempted
                    else "cleanup_unknown")
        return "cleanup_unknown"

    def _run_worker(
        self, task: str, skill_name: str | None, client: LLMClient, scope: _ExecutionScope,
    ) -> TaskResult:
        """Run one skill in the hardened orchestrator against the user's checkout."""
        task_id, durable_activity = scope.task_id, scope.durable_activity
        registry, tool_ctx, _store = build_registry(
            self.repo, cancellation_probe=scope.cancellation_probe,
        )
        if durable_activity is not None:
            # The wrapper commits tool.started before entering an effectful handler
            # and typed tool.finished afterwards. Policy still lives in the
            # orchestrator; durable activity is evidence, not authority.
            registry = wrap_registry_with_durable_activity(
                registry,
                durable_activity,
                process_starts=lambda: tool_ctx.processes_started,
            )

        def observe(kind: str, payload: Mapping[str, object]) -> None:
            if kind == "tool_rejected" and durable_activity is not None:
                self._retain_rejected_tool(durable_activity, payload)
            fields = {
                "route": ("skill", "tier"), "tool": ("name",),
                "observe": ("ok",),
                "llm": ("total_s", "ttft_s", "prompt_tokens", "completion_tokens"),
                "escalate": ("from", "to"), "blocked": ("reason", "skill"),
                "router_uncertain": ("skill", "score"),
            }
            if kind in fields:
                self.events.emit(
                    "worker." + kind,
                    {k: payload[k] for k in fields[kind] if k in payload}, task_id,
                )

        worker = Orchestrator(
            repo=self.repo, registry=registry, client=client, skills=self._skill_library(),
            approval=deny_all_approvals, observer=observe,
            context_budget_tokens=self.context_budget_tokens,
            allow_escalation=False,
        )
        run = worker.run(task, skill_name=skill_name)
        task_outcome = self._product_outcome(run)
        if skill_name == CONFIGURED_CHECK_SKILL and any(item.blocked for item in run.state.history):
            # A configured check is a fixed plan: every step is the one requested. If
            # policy refused a step (the tests), proof from an earlier step (the build)
            # must not turn the refusal into PASS. Only the configured plan gets this
            # rule; a model-driven task that also tried a refused tool is judged on the
            # proof it actually has.
            task_outcome = TaskOutcome.BLOCKED
        verified = bool(task_outcome.succeeded and run.state.verified)
        metrics = run.state.metrics.as_dict()
        metrics["proof_binding"] = binding_from_run(task, run, self.repo.root).as_dict()
        return TaskResult(
            task_id, task_outcome, self._answer_with_model_failure(run.answer, run),
            verified,
            tuple(f"{h.name}:{i}" for i, h in enumerate(run.state.history)),
            metrics,
            verification_ran=bool(run.state.verification_attempted),
            reason_code=self._reason_code(run, task_outcome),
        )

    def _prepare_candidate(
        self, task_id: str, resolved_skill: str,
        cancellation_probe: CancellationProbe | None,
    ) -> tuple[GitWorkspaceManager, Workspace] | TaskResult:
        """The manager and a fresh candidate workspace, or why there is none."""
        manager = self.workspaces
        blockers = candidate_blockers(
            self.declared_repo, allow_execution=self.allow_execution, skill=resolved_skill,
        )
        if manager is None:
            blockers = (
                CandidateBlocker(
                    "workspace_unavailable",
                    "candidate workspaces are not configured for this session",
                ),
                *blockers,
            )
        if blockers:
            return TaskResult(
                task_id, TaskOutcome.BLOCKED,
                "No change was prepared: " + "; ".join(b.explanation for b in blockers) + ".",
                False,
                metrics={"candidate_blockers": [
                    {"code": b.code, "explanation": b.explanation} for b in blockers
                ]},
                reason_code="policy_denied",
            )
        if manager is None:
            raise RuntimeError("candidate manager missing after blocker evaluation")
        prepared = _prepare_stoppable(
            manager, self.declared_repo, task_id, cancellation_probe,
        )
        if isinstance(prepared, TaskResult):
            return prepared
        if not isinstance(prepared, Workspace):
            readiness = prepared
            # An environment limit, reported before any worktree or model call exists.
            return TaskResult(
                task_id, TaskOutcome.BLOCKED,
                "No change was prepared: candidate changes cannot work here ("
                + "; ".join(readiness.problems) + ").",
                False,
                metrics={"candidate_readiness": {
                    "ready": False, "git_version": readiness.git_version,
                    "problems": list(readiness.problems),
                }},
                reason_code="missing_dependency",
            )
        return manager, prepared

    def _run_candidate_change(
        self, task: str, task_id: str, resolved_skill: str,
        durable_activity: DurableToolActivity | None,
        cancellation_probe: CancellationProbe | None = None,
    ) -> TaskResult:
        """Run a source-changing skill in its own worktree and retain the candidate."""
        prepared_candidate = self._prepare_candidate(task_id, resolved_skill, cancellation_probe)
        if isinstance(prepared_candidate, TaskResult):
            return prepared_candidate
        manager, workspace = prepared_candidate
        settled = False
        try:
            work_repo = candidate_repo(
                self.declared_repo, workspace, allow_execution=self.allow_execution,
            )
            # The same Stop token as any other task: a candidate build is a configured
            # command and must end when the user stops the task.
            registry, candidate_ctx, _store = build_registry(
                work_repo, cancellation_probe=cancellation_probe
            )
            if durable_activity is not None:
                registry = wrap_registry_with_durable_activity(
                    registry,
                    durable_activity,
                    process_starts=lambda: candidate_ctx.processes_started,
                )
            self.events.emit("worker.workspace", {
                "workspace_id": workspace.workspace_id,
                "base_commit": workspace.base_commit,
            }, task_id)
            worker = Orchestrator(
                repo=work_repo, registry=registry, client=self.worker_factory(),
                skills=self._skill_library(), approval=candidate_workspace_approval,
                observer=lambda kind, payload: self._retain_rejected_tool(
                    durable_activity, payload
                ) if kind == "tool_rejected" and durable_activity is not None else None,
                context_budget_tokens=self.context_budget_tokens, allow_escalation=False,
            )
            run = worker.run(task, skill_name=resolved_skill)

            def stopped(exc: WorkspaceStoppedError | None = None) -> TaskResult:
                # A stopped task never leaves a reviewable change behind, whatever the
                # worker did after the Stop landed. The candidate is discarded unseen;
                # whether that removal is confirmed is part of the result.
                discarded = True
                try:
                    manager.discard(workspace)
                except WorkspaceError:
                    discarded = False
                return _stopped_result(
                    task_id, exc, workspace_existed=True, discarded=discarded,
                )

            if cancellation_probe is not None and cancellation_probe.requested:
                settled = True
                return stopped()
            proof = self._prove_candidate(resolved_skill, run, work_repo, registry)
            if cancellation_probe is not None and cancellation_probe.requested:
                settled = True
                return stopped()
            task_outcome, verified, reason_code = proof.outcome, proof.verified, proof.reason_code
            proof_run, check = proof.proof_run, proof.check
            metrics = run.state.metrics.as_dict()
            # Proof identity is the candidate tree the build ran against.
            # A controller check's history follows the worker's in the evidence ids.
            metrics["proof_binding"] = binding_from_run(
                task, proof_run, workspace.root,
                evidence_offset=len(run.state.history) if proof_run is check else 0,
            ).as_dict()
            try:
                with manager.stoppable(cancellation_probe):
                    outcome, _candidate = settle_candidate(
                        manager, workspace, task_id=task_id, verified=verified,
                        proof=CANDIDATE_PROOF[resolved_skill],
                    )
            except WorkspaceStoppedError as exc:
                settled = True
                return stopped(exc)
            settled = True
            metrics["candidate"] = outcome.as_metrics(workspace)
            if outcome.refused:
                # Whatever the build said, there is no change the user can take.
                task_outcome, verified, reason_code = TaskOutcome.BLOCKED, False, "policy_denied"
            history = list(run.state.history)
            if check is not None:
                history += check.state.history
                metrics["controller_check"] = {
                    "check": _CONTROLLER_CHECK[resolved_skill],
                    "decided": proof_run is check,
                }
            answer = self._answer_with_model_failure(
                outcome.summary + ("\n\n" + run.answer if run.answer else ""), run,
            )
            if check is not None:
                answer += "\n\n" + _controller_check_line(resolved_skill, check, verified=verified)
            return TaskResult(
                task_id, task_outcome, answer, verified,
                tuple(f"{h.name}:{i}" for i, h in enumerate(history)),
                metrics,
                verification_ran=bool(
                    run.state.verification_attempted
                    or (check is not None and check.state.verification_attempted)
                ),
                reason_code=reason_code,
            )
        finally:
            if not settled:
                # Nothing reviewable was produced; never leave an orphaned worktree.
                manager.discard(workspace)

    def _prove_candidate(
        self, resolved_skill: str, run: RunResult, work_repo: RepoConfig,
        registry: ToolRegistry,
    ) -> _CandidateProof:
        """The candidate's verdict: the worker's own proof, else the controller's check."""
        outcome = self._product_outcome(run)
        verified = bool(outcome.succeeded and run.state.verified)
        reason_code = self._reason_code(run, outcome)
        if verified and not candidate_proof_satisfied(resolved_skill, run):
            # Verified by the wrong kind of proof for this request (a test fix that
            # only rebuilt). Not a success, and not a failure of the code.
            verified, outcome, reason_code = False, TaskOutcome.NO_VERDICT, "missing_evidence"
        if verified or not _needs_controller_check(run):
            return _CandidateProof(outcome, verified, reason_code, run, None)
        # The worker changed the candidate and did not say it failed, but did not
        # prove it either. Proof is the controller's job, not the model's memory:
        # run the configured check this request requires on the candidate, through
        # the same tools and durable activity, and let its result decide.
        check = self._controller_check(resolved_skill, work_repo, registry)
        if _process_cleanup_unknown(check):
            # The check ran on the candidate after the worker changed it; a tree it
            # could not show ended is the later, stronger fact and decides (#436 review).
            return _CandidateProof(TaskOutcome.NO_VERDICT, False, "cleanup_unknown", check, check)
        if candidate_proof_satisfied(resolved_skill, check):
            return _CandidateProof(TaskOutcome.PASS, True, "verification_passed", check, check)
        if check.state.verification_attempted and _current_tree_observed_failure(check):
            return _CandidateProof(TaskOutcome.FAIL, False, "verification_failed", check, check)
        return _CandidateProof(outcome, verified, reason_code, run, check)

    def _controller_check(
        self, resolved_skill: str, work_repo: RepoConfig, registry: ToolRegistry,
    ) -> RunResult:
        """Run the configured check a candidate skill requires, with no model deciding."""
        checker = Orchestrator(
            repo=work_repo, registry=registry,
            client=ConfiguredCheckPlan(_CONTROLLER_CHECK[resolved_skill]),
            skills=self._skill_library(), approval=candidate_workspace_approval,
            context_budget_tokens=self.context_budget_tokens, allow_escalation=False,
        )
        result: RunResult = checker.run(
            f"Controller check of the candidate prepared for {resolved_skill}.",
            skill_name=CONFIGURED_CHECK_SKILL,
        )
        return result

    def run(
        self,
        task: str,
        *,
        self_check: bool = False,
        route_source: RouteSource | TaskExecutionSource | str = RouteSource.MODEL_PROPOSAL,
        task_id: str | None = None,
        durable_activity: DurableToolActivity | None = None,
        skill_name: str | None = None,
        cancellation_probe: CancellationProbe | None = None,
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
            elif (action := _CONTROLLER_ACTION_HANDLERS.get(resolved_skill or "")) is not None:
                result = action(
                    self.workspaces, self.declared_repo, task_id=task_id, request_text=task,
                )
            elif resolved_skill is not None and resolved_skill in CANDIDATE_CHANGE_SKILLS:
                result = self._run_candidate_change(
                    task, task_id, resolved_skill, durable_activity,
                    cancellation_probe=cancellation_probe,
                )
            elif resolved_skill in CONFIGURED_CHECKS:
                # No judgement is needed to run the configured build or tests, so no
                # model decides whether they run: a fixed plan drives the same worker.
                result = self._run_worker(
                    task, CONFIGURED_CHECK_SKILL, ConfiguredCheckPlan(resolved_skill),
                    _ExecutionScope(task_id, durable_activity, cancellation_probe),
                )
            else:
                result = self._run_worker(
                    task, resolved_skill, self.worker_factory(),
                    _ExecutionScope(task_id, durable_activity, cancellation_probe),
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
        except Exception as exc:  # noqa: BLE001 - the one operational-fault boundary
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
