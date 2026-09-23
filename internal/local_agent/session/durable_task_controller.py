"""Bind an admitted durable task to the controller's tool-effect boundary.

`DurableTaskExecutor` owns admission and the transition to running. This adapter sits at
its controller boundary: once the executor invokes the admitted task UUID, recover the
exact durable execution epoch/deadline and provide `DurableToolActivity` to the real
`TaskController`. No tool effect gains authority here; this only makes activity history
part of the already-authorised execution path.
"""
from __future__ import annotations

from .durable_activity import DurableToolActivity


class AdmittedDurableTaskController:
    """Controller adapter used only after durable task admission.

    The admission runner hashes the controller's effective repo/policy/budget before it
    admits work. Those trusted attributes are delegated unchanged so inserting this
    activity adapter cannot alter the execution-contract identity.
    """

    def __init__(self, service, controller) -> None:
        if not callable(getattr(controller, "run", None)):
            raise TypeError("admitted durable controller requires a run-capable controller")
        if not callable(getattr(controller, "resolve_skill", None)):
            raise TypeError("admitted durable controller requires controller.resolve_skill")
        if not callable(getattr(controller, "effective_skill_sha256", None)):
            raise TypeError(
                "admitted durable controller requires controller.effective_skill_sha256"
            )
        for name in ("repo", "allow_execution", "context_budget_tokens"):
            if not hasattr(controller, name):
                raise TypeError(f"admitted durable controller requires controller.{name}")
        self.service = service
        self.controller = controller

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
        return self.controller.effective_skill_sha256(skill_name)

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
        return self.controller.run(
            task,
            self_check=self_check,
            route_source=route_source,
            task_id=task_id,
            durable_activity=activity,
            skill_name=skill_name,
        )


__all__ = ["AdmittedDurableTaskController"]
