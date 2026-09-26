"""Bind an admitted durable task to the controller's tool-effect boundary.

`DurableTaskExecutor` owns admission and the transition to running. This adapter sits at
its controller boundary: once the executor invokes the admitted task UUID, recover the
exact durable execution epoch/deadline and provide `DurableToolActivity` to the real
`TaskController`. No tool effect gains authority here; this only makes activity history
part of the already-authorised execution path.
"""
from __future__ import annotations

from contextlib import nullcontext

from ..provenance import source_sha256
from .durable_activity import DurableToolActivity


class AdmittedDurableTaskController:
    """Controller adapter used only after durable task admission."""

    def __init__(self, service, controller) -> None:
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
        self._cancellation_tokens = None

    def bind_cancellation_tokens(self, lookup) -> None:
        """Accept the executor's ``(task_id, execution_epoch) -> token`` lookup once."""
        if not callable(lookup):
            raise TypeError("cancellation token lookup must be callable")
        if self._cancellation_tokens is not None and self._cancellation_tokens != lookup:
            raise RuntimeError("admitted controller is already bound to a cancellation runtime")
        self._cancellation_tokens = lookup

    @property
    def repo(self):
        return self.controller.repo

    @property
    def allow_execution(self):
        return self.controller.allow_execution

    @property
    def context_budget_tokens(self):
        return self.controller.context_budget_tokens

    def resolve_skill(self, skill_name: str) -> str:
        return self.controller.resolve_skill(skill_name)

    def effective_skill_sha256(self, skill_name: str) -> str:
        fingerprint = getattr(self.controller, "effective_skill_sha256", None)
        if not callable(fingerprint):
            raise TypeError("admitted durable controller cannot fingerprint admitted skills")
        return fingerprint(skill_name)

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
        route_source=None,
        task_id: str | None = None,
        skill_name: str | None = None,
    ):
        if task_id is None:
            raise ValueError("durable controller execution requires an admitted task id")
        activity = DurableToolActivity.from_task(self.service, task_id)
        worker_factory = getattr(self.controller, "worker_factory", None)
        binder = getattr(worker_factory, "bind_task", None)
        authority = (
            binder(task_id, activity.execution_epoch)
            if callable(binder) and not self_check
            else nullcontext()
        )
        extra = {}
        if self._cancellation_tokens is not None and not self_check:
            extra["cancellation_probe"] = self._cancellation_tokens(
                task_id, activity.execution_epoch
            )
        with authority:
            return self.controller.run(
                task,
                self_check=self_check,
                route_source=route_source,
                task_id=task_id,
                durable_activity=activity,
                skill_name=skill_name,
                **extra,
            )


__all__ = ["AdmittedDurableTaskController"]
