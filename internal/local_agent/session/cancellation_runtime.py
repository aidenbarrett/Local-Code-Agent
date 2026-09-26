"""Runtime ownership and reconciliation for Session Hub cancellation.

The primitives in ``cancellation.py`` describe truthful cancellation contracts. This
module adds the in-process registry that owns one execution epoch per task, revokes old
dispatch authority when cancellation is requested, and refuses to call cleanup complete
without the evidence required by the current cancellation site.

It deliberately does not send OS signals or cancel inference itself. Those effectful
operations belong to the process/endpoint adapters; this runtime decides what evidence
they must return before a caller may terminalise the task.
"""
from __future__ import annotations

from dataclasses import dataclass
from threading import Lock

from .cancellation import (
    CancelRequest,
    CancellationPlan,
    CancellationSite,
    CancellationSource,
    CancellationToken,
    EpochFence,
    OwnedProcessHandle,
    StaleExecutionEpoch,
    cancellation_plan,
)


class CancellationRuntimeError(RuntimeError):
    pass


@dataclass(frozen=True)
class CancellationDecision:
    request: CancelRequest
    plan: CancellationPlan
    revoked_epoch: int
    current_epoch: int
    owned_process: OwnedProcessHandle | None


@dataclass(frozen=True)
class CancellationReconciliation:
    complete: bool
    missing_evidence: tuple[str, ...]


@dataclass
class _TaskCancellationState:
    task_id: str
    fence: EpochFence
    token: CancellationToken
    site: CancellationSite
    owned_process: OwnedProcessHandle | None = None
    decision: CancellationDecision | None = None


class CancellationRuntime:
    """Controller-owned cancellation registry for live task executions."""

    def __init__(self) -> None:
        self._lock = Lock()
        self._tasks: dict[str, _TaskCancellationState] = {}

    def register_task(
        self,
        task_id: str,
        *,
        execution_epoch: int = 0,
        site: CancellationSite | str = CancellationSite.QUEUED,
    ) -> CancellationToken:
        candidate = _TaskCancellationState(
            task_id=task_id,
            fence=EpochFence(execution_epoch),
            token=CancellationToken(task_id, execution_epoch),
            site=CancellationSite(site),
        )
        with self._lock:
            existing = self._tasks.get(task_id)
            if existing is not None:
                if existing.token.execution_epoch != execution_epoch:
                    raise CancellationRuntimeError(
                        "task is already registered under a different execution epoch"
                    )
                return existing.token
            self._tasks[task_id] = candidate
            return candidate.token

    def _state(self, task_id: str) -> _TaskCancellationState:
        try:
            return self._tasks[task_id]
        except KeyError as exc:
            raise CancellationRuntimeError("task is not registered for cancellation") from exc

    def token(self, task_id: str, execution_epoch: int) -> CancellationToken:
        """Return the registered token for exactly this task execution epoch.

        Tools poll it to stop configured commands. Handing out a token never grants
        authority; a stale epoch is refused rather than silently given a live token.
        """
        with self._lock:
            state = self._state(task_id)
            if state.token.execution_epoch != execution_epoch:
                raise StaleExecutionEpoch(
                    f"no cancellation token for stale execution epoch {execution_epoch}"
                )
            return state.token

    def current_epoch(self, task_id: str) -> int:
        with self._lock:
            return self._state(task_id).fence.current

    def require_dispatch_authority(self, task_id: str, execution_epoch: int) -> None:
        with self._lock:
            self._state(task_id).fence.require_current(execution_epoch)

    def set_site(
        self,
        task_id: str,
        execution_epoch: int,
        site: CancellationSite | str,
    ) -> None:
        with self._lock:
            state = self._state(task_id)
            if state.token.requested:
                raise StaleExecutionEpoch("cancelled execution cannot move to a new cancellation site")
            state.fence.require_current(execution_epoch)
            state.site = CancellationSite(site)

    def attach_process(self, handle: OwnedProcessHandle) -> None:
        with self._lock:
            state = self._state(handle.task_id)
            if state.token.requested:
                raise StaleExecutionEpoch("cancelled execution cannot acquire new process authority")
            state.fence.require_current(handle.execution_epoch)
            state.owned_process = handle
            state.site = CancellationSite.SUBPROCESS

    def request_cancel(
        self,
        task_id: str,
        execution_epoch: int,
        *,
        source: CancellationSource | str = CancellationSource.USER,
        reason_code: str = "requested",
        request_id: str | None = None,
    ) -> CancellationDecision:
        """Record intent once and revoke the old epoch immediately.

        Repeating the same task/epoch cancellation is idempotent and returns the first
        decision. It does not advance the epoch again or replace the first request.
        """
        with self._lock:
            state = self._state(task_id)
            if state.decision is not None:
                if execution_epoch != state.token.execution_epoch:
                    raise StaleExecutionEpoch(
                        "cancel retry names a different execution epoch from the first request"
                    )
                return state.decision
            state.fence.require_current(execution_epoch)
            request = state.token.request(
                source=source,
                reason_code=reason_code,
                request_id=request_id,
            )
            plan = cancellation_plan(state.site)
            revoked = state.fence.current
            current = state.fence.advance()
            decision = CancellationDecision(
                request=request,
                plan=plan,
                revoked_epoch=revoked,
                current_epoch=current,
                owned_process=state.owned_process,
            )
            state.decision = decision
            return decision

    def reconcile(
        self,
        task_id: str,
        *,
        queue_removed: bool | None = None,
        endpoint_stopped: bool | None = None,
        process_tree_stopped: bool | None = None,
        mutation_reconciled: bool | None = None,
        terminal_commit_resolved: bool | None = None,
    ) -> CancellationReconciliation:
        """Evaluate cleanup proof without inferring missing facts.

        ``True`` means the owning adapter proved the fact. ``False`` and ``None`` both
        leave the fact unresolved. Direct-child/unknown process ownership can never prove
        whole-tree cleanup, even if a caller claims the direct child exited.
        """
        for name, value in (
            ("queue_removed", queue_removed),
            ("endpoint_stopped", endpoint_stopped),
            ("process_tree_stopped", process_tree_stopped),
            ("mutation_reconciled", mutation_reconciled),
            ("terminal_commit_resolved", terminal_commit_resolved),
        ):
            if value is not None and not isinstance(value, bool):
                raise TypeError(f"{name} must be boolean or None")

        with self._lock:
            state = self._state(task_id)
            decision = state.decision
            if decision is None:
                raise CancellationRuntimeError("task has no cancellation request to reconcile")
            plan = decision.plan
            missing: list[str] = []

            if plan.remove_queued_atomically and queue_removed is not True:
                missing.append("queue_removal_unproven")
            if plan.quarantine_endpoint and endpoint_stopped is not True:
                missing.append("endpoint_stop_unproven")
            if plan.terminate_owned_process_tree:
                process = decision.owned_process
                if process is None:
                    missing.append("owned_process_identity_missing")
                elif not process.has_tree_container:
                    missing.append("process_tree_containment_unproven")
                elif process_tree_stopped is not True:
                    missing.append("process_tree_stop_unproven")
            if plan.reconcile_mutation and mutation_reconciled is not True:
                missing.append("mutation_reconciliation_unproven")
            if plan.terminal_commit_race and terminal_commit_resolved is not True:
                missing.append("terminal_commit_race_unresolved")

            return CancellationReconciliation(
                complete=not missing,
                missing_evidence=tuple(missing),
            )

    def unregister_task(self, task_id: str, *, expected_current_epoch: int) -> None:
        """Drop runtime ownership only when the caller names the current fence epoch."""
        with self._lock:
            state = self._state(task_id)
            state.fence.require_current(expected_current_epoch)
            del self._tasks[task_id]
