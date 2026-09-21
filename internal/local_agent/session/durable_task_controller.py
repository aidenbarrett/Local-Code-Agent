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
    """Controller adapter used only after durable task admission."""

    def __init__(self, service, controller) -> None:
        if not callable(getattr(controller, "run", None)):
            raise TypeError("admitted durable controller requires a run-capable controller")
        self.service = service
        self.controller = controller

    def run(
        self,
        task: str,
        *,
        self_check: bool = False,
        route_source=None,
        task_id: str | None = None,
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
        )


__all__ = ["AdmittedDurableTaskController"]
