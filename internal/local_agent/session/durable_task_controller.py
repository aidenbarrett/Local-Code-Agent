"""Bind an admitted durable task to the controller's tool-effect boundary.

`DurableTaskExecutor` owns admission and the transition to running. This adapter sits at
its controller boundary: once the executor invokes the admitted task UUID, recover the
exact durable execution epoch/deadline and provide `DurableToolActivity` to the real
`TaskController`. No tool effect gains authority here; this only makes activity history
part of the already-authorised execution path.
"""
from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractContextManager, nullcontext
from typing import TYPE_CHECKING, Protocol

from ..provenance import source_sha256
from .contracts import RouteSource
from .durable_activity import DurableToolActivity

if TYPE_CHECKING:
    from ..config import RepoConfig
    from ..tools.process_runner import CancellationProbe
    from .contracts import TaskResult
    from .execution_source import TaskExecutionSource

# (task_id, execution_epoch) -> the Stop token for exactly that execution.
CancellationLookup = Callable[[str, int], "CancellationProbe"]


class AdmissibleController(Protocol):
    """What the adapter needs from the controller it wraps (TaskController satisfies it).

    ``effective_skill_sha256``, ``process_spawning_tools`` and ``worker_factory`` are
    optional capabilities, looked up by name and checked where they are used.
    """

    @property
    def repo(self) -> RepoConfig: ...
    @property
    def allow_execution(self) -> bool: ...
    @property
    def context_budget_tokens(self) -> int: ...

    def resolve_skill(self, skill_name: str) -> str: ...

    def run(  # noqa: PLR0913 - mirrors TaskController.run, keyword-only
        self,
        task: str,
        *,
        self_check: bool = ...,
        route_source: RouteSource | TaskExecutionSource | str = ...,
        task_id: str | None = ...,
        durable_activity: DurableToolActivity | None = ...,
        skill_name: str | None = ...,
        cancellation_probe: CancellationProbe | None = ...,
    ) -> TaskResult: ...


class AdmittedDurableTaskController:
    """Controller adapter used only after durable task admission."""

    def __init__(self, service: object, controller: AdmissibleController) -> None:
        if not callable(getattr(controller, "run", None)):
            raise TypeError("admitted durable controller requires a run-capable controller")
        if not callable(getattr(controller, "resolve_skill", None)):
            raise TypeError("admitted durable controller requires controller.resolve_skill")
        for name in ("repo", "allow_execution", "context_budget_tokens"):
            if not hasattr(controller, name):
                raise TypeError(f"admitted durable controller requires controller.{name}")
        self.service = service
        self.controller = controller
        self.source_sha256_at_start = source_sha256()
        self._cancellation_tokens: CancellationLookup | None = None

    def bind_cancellation_tokens(self, lookup: CancellationLookup) -> None:
        """Accept the executor's ``(task_id, execution_epoch) -> token`` lookup once."""
        if not callable(lookup):
            raise TypeError("cancellation token lookup must be callable")
        if self._cancellation_tokens is not None and self._cancellation_tokens != lookup:
            raise RuntimeError("admitted controller is already bound to a cancellation runtime")
        self._cancellation_tokens = lookup

    @property
    def repo(self) -> RepoConfig:
        return self.controller.repo

    @property
    def allow_execution(self) -> bool:
        return self.controller.allow_execution

    @property
    def context_budget_tokens(self) -> int:
        return self.controller.context_budget_tokens

    def resolve_skill(self, skill_name: str) -> str:
        return self.controller.resolve_skill(skill_name)

    def effective_skill_sha256(self, skill_name: str) -> str:
        fingerprint = getattr(self.controller, "effective_skill_sha256", None)
        if not callable(fingerprint):
            raise TypeError("admitted durable controller cannot fingerprint admitted skills")
        digest = fingerprint(skill_name)
        if not isinstance(digest, str):
            raise TypeError("admitted skill fingerprint must be a string")
        return digest

    def cancel_endpoint_execution(self, task_id: str, execution_epoch: int) -> tuple[str, ...] | None:
        """Delegate Stop to the worker factory endpoint authority when configured."""
        worker_factory = getattr(self.controller, "worker_factory", None)
        cancel = getattr(worker_factory, "cancel_execution", None)
        if not callable(cancel):
            return None
        removed = cancel(task_id, execution_epoch)
        return tuple(str(request_id) for request_id in removed)

    def process_spawning_tools(self) -> frozenset[str]:
        resolver = getattr(self.controller, "process_spawning_tools", None)
        if not callable(resolver):
            return frozenset()
        return frozenset(resolver())

    def run(
        self,
        task: str,
        *,
        self_check: bool = False,
        route_source: RouteSource | TaskExecutionSource | str = RouteSource.MODEL_PROPOSAL,
        task_id: str | None = None,
        skill_name: str | None = None,
    ) -> TaskResult:
        if task_id is None:
            raise ValueError("durable controller execution requires an admitted task id")
        activity = DurableToolActivity.from_task(self.service, task_id)
        worker_factory = getattr(self.controller, "worker_factory", None)
        binder = getattr(worker_factory, "bind_task", None)
        authority: AbstractContextManager[object] = (
            binder(task_id, activity.execution_epoch)
            if callable(binder) and not self_check
            else nullcontext()
        )
        probe = (
            self._cancellation_tokens(task_id, activity.execution_epoch)
            if self._cancellation_tokens is not None and not self_check
            else None
        )
        with authority:
            if probe is None:
                # No Stop runtime is bound: the controller keeps its own default.
                return self.controller.run(
                    task, self_check=self_check, route_source=route_source, task_id=task_id,
                    durable_activity=activity, skill_name=skill_name,
                )
            return self.controller.run(
                task, self_check=self_check, route_source=route_source, task_id=task_id,
                durable_activity=activity, skill_name=skill_name, cancellation_probe=probe,
            )


if TYPE_CHECKING:
    from .task_controller import TaskController

    def _task_controller_is_admissible(controller: TaskController) -> AdmissibleController:
        """Static assertion, checked by mypy only: the product controller fits the adapter."""
        return controller


__all__ = ["AdmissibleController", "AdmittedDurableTaskController", "CancellationLookup"]
