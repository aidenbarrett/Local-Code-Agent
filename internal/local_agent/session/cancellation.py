"""Truthful Session Hub cancellation and execution-fencing primitives.

Requesting cancellation is not proof that inference stopped, a subprocess tree was
reaped, or cleanup succeeded. These types record intent, fence stale execution and
describe the reconciliation work required before a terminal claim can be made.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from threading import Lock
from uuid import UUID, uuid4

from .contracts import TaskVerdict, TerminalState


class StaleExecutionEpoch(RuntimeError):
    """Raised when work from an old execution epoch attempts to regain authority."""


class CancellationSource(str, Enum):
    USER = "user"
    CONTROLLER = "controller"
    WATCHDOG = "watchdog"
    SHUTDOWN = "shutdown"


class CancellationSite(str, Enum):
    QUEUED = "queued"
    WAITING_INFERENCE = "waiting_inference"
    SUBPROCESS = "subprocess"
    MUTATION = "mutation"
    FINALISING = "finalising"


class ProcessContainment(str, Enum):
    UNKNOWN = "unknown"
    DIRECT_CHILD = "direct_child"
    POSIX_PROCESS_GROUP = "posix_process_group"
    WINDOWS_JOB = "windows_job"


class EpochFence:
    """Controller-owned monotonic execution epoch.

    Advancing the fence revokes future authority from all holders of the previous
    epoch. It does not terminate those holders; they may still need reconciliation.
    """

    def __init__(self, initial_epoch: int = 0):
        if not isinstance(initial_epoch, int) or isinstance(initial_epoch, bool) or initial_epoch < 0:
            raise ValueError("initial execution epoch must be a nonnegative integer")
        self._lock = Lock()
        self._epoch = initial_epoch

    @property
    def current(self) -> int:
        with self._lock:
            return self._epoch

    def advance(self) -> int:
        with self._lock:
            self._epoch += 1
            return self._epoch

    def accepts(self, execution_epoch: int) -> bool:
        if not isinstance(execution_epoch, int) or isinstance(execution_epoch, bool) or execution_epoch < 0:
            raise ValueError("execution epoch must be a nonnegative integer")
        with self._lock:
            return execution_epoch == self._epoch

    def require_current(self, execution_epoch: int) -> None:
        if not self.accepts(execution_epoch):
            raise StaleExecutionEpoch(
                f"stale execution epoch {execution_epoch}; current epoch is {self.current}"
            )


@dataclass(frozen=True)
class CancelRequest:
    request_id: str
    task_id: str
    execution_epoch: int
    source: CancellationSource | str
    reason_code: str = "requested"

    def __post_init__(self) -> None:
        for name, value in (("request_id", self.request_id), ("task_id", self.task_id)):
            try:
                UUID(value)
            except (TypeError, ValueError) as exc:
                raise ValueError(f"{name} must be a UUID") from exc
        if not isinstance(self.execution_epoch, int) or isinstance(self.execution_epoch, bool) or self.execution_epoch < 0:
            raise ValueError("cancel request execution epoch must be nonnegative")
        object.__setattr__(self, "source", CancellationSource(self.source))
        if not isinstance(self.reason_code, str) or not self.reason_code.strip():
            raise ValueError("cancel reason code must be nonempty")
        if len(self.reason_code) > 128:
            raise ValueError("cancel reason code exceeds size limit")


class CancellationToken:
    """Idempotent cancellation-request token tied to one task execution epoch.

    The token says only that cancellation was requested. It intentionally exposes no
    ``cancelled`` or ``cleanup_complete`` property.
    """

    def __init__(self, task_id: str, execution_epoch: int):
        try:
            UUID(task_id)
        except (TypeError, ValueError) as exc:
            raise ValueError("cancellation token task id must be a UUID") from exc
        if not isinstance(execution_epoch, int) or isinstance(execution_epoch, bool) or execution_epoch < 0:
            raise ValueError("cancellation token execution epoch must be nonnegative")
        self.task_id = task_id
        self.execution_epoch = execution_epoch
        self._lock = Lock()
        self._request: CancelRequest | None = None

    @property
    def requested(self) -> bool:
        with self._lock:
            return self._request is not None

    @property
    def first_request(self) -> CancelRequest | None:
        with self._lock:
            return self._request

    def request(
        self,
        *,
        source: CancellationSource | str = CancellationSource.USER,
        reason_code: str = "requested",
        request_id: str | None = None,
    ) -> CancelRequest:
        candidate = CancelRequest(
            request_id=request_id or str(uuid4()),
            task_id=self.task_id,
            execution_epoch=self.execution_epoch,
            source=source,
            reason_code=reason_code,
        )
        with self._lock:
            if self._request is None:
                self._request = candidate
            return self._request


@dataclass(frozen=True)
class OwnedProcessHandle:
    """Observed child identity; not proof that descendants are contained or stopped."""

    task_id: str
    execution_epoch: int
    pid: int
    birth_token: str
    containment: ProcessContainment | str
    containment_id: int | str | None = None

    def __post_init__(self) -> None:
        try:
            UUID(self.task_id)
        except (TypeError, ValueError) as exc:
            raise ValueError("owned process task id must be a UUID") from exc
        if not isinstance(self.execution_epoch, int) or isinstance(self.execution_epoch, bool) or self.execution_epoch < 0:
            raise ValueError("owned process execution epoch must be nonnegative")
        if not isinstance(self.pid, int) or isinstance(self.pid, bool) or self.pid < 1:
            raise ValueError("owned process pid must be positive")
        if not isinstance(self.birth_token, str) or not self.birth_token.strip():
            raise ValueError("owned process birth token must be nonempty")
        if len(self.birth_token) > 256:
            raise ValueError("owned process birth token exceeds size limit")
        object.__setattr__(self, "containment", ProcessContainment(self.containment))

        if self.containment == ProcessContainment.POSIX_PROCESS_GROUP:
            if not isinstance(self.containment_id, int) or isinstance(self.containment_id, bool) or self.containment_id < 1:
                raise ValueError("POSIX process-group containment requires a positive group id")
        elif self.containment == ProcessContainment.WINDOWS_JOB:
            if not isinstance(self.containment_id, str) or not self.containment_id.strip():
                raise ValueError("Windows job containment requires a nonempty job identity")
        elif self.containment_id is not None:
            raise ValueError("unknown/direct-child containment cannot carry a tree-container id")

    @property
    def has_tree_container(self) -> bool:
        """Whether the handle names a container that proves whole-tree membership.

        A POSIX process group is a signalling scope, not a containment boundary:
        descendants can escape it with ``setsid()``/``setpgid()``. Therefore group
        disappearance can prove only that the group disappeared, never that every
        descendant owned by the task is gone. Windows job ownership is the only
        whole-tree container represented by this contract today.
        """
        return self.containment == ProcessContainment.WINDOWS_JOB

    @property
    def cleanup_proof_scope(self) -> str:
        """Return the strongest cleanup scope this handle can represent."""
        if self.containment == ProcessContainment.WINDOWS_JOB:
            return "whole_tree"
        if self.containment == ProcessContainment.POSIX_PROCESS_GROUP:
            return "process_group"
        if self.containment == ProcessContainment.DIRECT_CHILD:
            return "direct_child"
        return "unknown"


@dataclass(frozen=True)
class CancellationPlan:
    site: CancellationSite | str
    revoke_future_dispatch: bool
    remove_queued_atomically: bool
    quarantine_endpoint: bool
    terminate_owned_process_tree: bool
    reconcile_mutation: bool
    terminal_commit_race: bool
    requires_reconciliation: bool
    terminal_state_if_immediate: TerminalState | None = None
    verdict_if_immediate: TaskVerdict | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "site", CancellationSite(self.site))
        if self.terminal_state_if_immediate is not None:
            object.__setattr__(
                self,
                "terminal_state_if_immediate",
                TerminalState(self.terminal_state_if_immediate),
            )
        if self.verdict_if_immediate is not None:
            object.__setattr__(self, "verdict_if_immediate", TaskVerdict(self.verdict_if_immediate))
        if not self.revoke_future_dispatch:
            raise ValueError("all cancellation plans must revoke future dispatch authority")
        if (self.terminal_state_if_immediate is None) != (self.verdict_if_immediate is None):
            raise ValueError("immediate terminal state and verdict must appear together")
        if self.requires_reconciliation and self.terminal_state_if_immediate is not None:
            raise ValueError("a plan requiring reconciliation cannot claim an immediate terminal result")


def cancellation_plan(site: CancellationSite | str) -> CancellationPlan:
    value = CancellationSite(site)
    if value == CancellationSite.QUEUED:
        return CancellationPlan(
            site=value,
            revoke_future_dispatch=True,
            remove_queued_atomically=True,
            quarantine_endpoint=False,
            terminate_owned_process_tree=False,
            reconcile_mutation=False,
            terminal_commit_race=False,
            requires_reconciliation=False,
            terminal_state_if_immediate=TerminalState.CANCELLED,
            verdict_if_immediate=TaskVerdict.NO_VERDICT,
        )
    if value == CancellationSite.WAITING_INFERENCE:
        return CancellationPlan(
            site=value,
            revoke_future_dispatch=True,
            remove_queued_atomically=False,
            quarantine_endpoint=True,
            terminate_owned_process_tree=False,
            reconcile_mutation=False,
            terminal_commit_race=False,
            requires_reconciliation=True,
        )
    if value == CancellationSite.SUBPROCESS:
        return CancellationPlan(
            site=value,
            revoke_future_dispatch=True,
            remove_queued_atomically=False,
            quarantine_endpoint=False,
            terminate_owned_process_tree=True,
            reconcile_mutation=False,
            terminal_commit_race=False,
            requires_reconciliation=True,
        )
    if value == CancellationSite.MUTATION:
        return CancellationPlan(
            site=value,
            revoke_future_dispatch=True,
            remove_queued_atomically=False,
            quarantine_endpoint=False,
            terminate_owned_process_tree=False,
            reconcile_mutation=True,
            terminal_commit_race=False,
            requires_reconciliation=True,
        )
    if value == CancellationSite.FINALISING:
        return CancellationPlan(
            site=value,
            revoke_future_dispatch=True,
            remove_queued_atomically=False,
            quarantine_endpoint=False,
            terminate_owned_process_tree=False,
            reconcile_mutation=False,
            terminal_commit_race=True,
            requires_reconciliation=True,
        )
    raise AssertionError(f"unmapped cancellation site: {value}")
