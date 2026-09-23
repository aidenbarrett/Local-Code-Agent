"""Cancellation-aware durable executor with controller-owned epoch fencing.

Cancellation intent and cleanup proof are separate facts. This adapter closes the
execution-authority race first: once a task epoch is revoked, that execution can no
longer dispatch controller work or commit a late terminal result. Effectful endpoint,
process-tree and mutation cleanup remain owned by their respective adapters and require
separate reconciliation before a cancelled terminal claim can be made.
"""
from __future__ import annotations

from threading import Lock, Thread
from typing import Any

from .cancellation import CancellationSite, CancellationSource, StaleExecutionEpoch
from .cancellation_runtime import CancellationDecision, CancellationRuntime
from .contracts import RouteSource, TaskResult
from .results import verdict_block_from_task_result
from .session_event_service import DurableSessionService, DurableTaskExecutor, TaskHandle


class CancellableDurableTaskExecutor(DurableTaskExecutor):
    """DurableTaskExecutor whose live task authority is fenced by execution epoch."""

    def __init__(
        self,
        service: DurableSessionService,
        controller,
        *,
        cancellation_runtime: CancellationRuntime | None = None,
    ) -> None:
        super().__init__(service, controller)
        self.cancellation = cancellation_runtime or CancellationRuntime()
        self._ownership_lock = Lock()
        # Serialises cancellation against dispatch and durable finalisation. The
        # terminal path holds this lock through commit and ownership release so a
        # cancellation can never revoke an epoch after terminal enqueue but before
        # terminal persistence.
        self._authority_lock = Lock()
        self._registered_tasks: set[str] = set()

    def _register_once(self, task_id: str, execution_epoch: int) -> bool:
        with self._ownership_lock:
            if task_id in self._registered_tasks:
                return False
            self.cancellation.register_task(task_id, execution_epoch=execution_epoch)
            self._registered_tasks.add(task_id)
            return True

    def _release_registration(self, task_id: str, execution_epoch: int) -> None:
        with self._ownership_lock:
            if task_id not in self._registered_tasks:
                return
            self.cancellation.unregister_task(
                task_id,
                expected_current_epoch=execution_epoch,
            )
            self._registered_tasks.remove(task_id)

    def submit(
        self,
        *,
        task: str,
        request_id: str,
        payload_sha256: str,
        admission_payload: dict[str, Any],
        request_bytes: bytes | None = None,
        self_check: bool = False,
        route_source: RouteSource | str = RouteSource.MODEL_PROPOSAL,
        skill_name: str | None = None,
    ) -> TaskHandle:
        execution_epoch = int(admission_payload["execution_epoch"])
        admission = self.service.submit_task(
            request_id=request_id,
            payload_sha256=payload_sha256,
            admission_payload=admission_payload,
            request_bytes=request_bytes,
        )
        assert admission.task_id is not None
        owns_registration = self._register_once(admission.task_id, execution_epoch)
        handle = TaskHandle(admission.task_id, admission)
        Thread(
            target=self._run_fenced,
            args=(
                handle,
                task,
                self_check,
                route_source,
                execution_epoch,
                skill_name,
                owns_registration,
            ),
            name=f"lca-task-{admission.task_id[:8]}",
            daemon=True,
        ).start()
        return handle

    def request_cancel(
        self,
        task_id: str,
        *,
        execution_epoch: int,
        source: CancellationSource | str = CancellationSource.USER,
        reason_code: str = "requested",
    ) -> CancellationDecision:
        """Revoke execution authority before durably publishing cancellation intent."""
        with self._authority_lock:
            decision = self.cancellation.request_cancel(
                task_id,
                execution_epoch,
                source=source,
                reason_code=reason_code,
            )
            receipt = self.service.append(
                "task.cancel_requested",
                {
                    "request_id": decision.request.request_id,
                    "source": decision.request.source.value,
                    "reason_code": decision.request.reason_code,
                    "execution_epoch": decision.request.execution_epoch,
                },
                task_id=task_id,
            )
            receipt.wait(30)
            return decision

    def _run_fenced(
        self,
        handle: TaskHandle,
        task: str,
        self_check: bool,
        route_source: RouteSource | str,
        execution_epoch: int,
        skill_name: str | None,
        owns_registration: bool,
    ) -> None:
        try:
            handle.admission.wait(30)
            if handle.admission.created is False:
                if owns_registration:
                    self._release_registration(handle.task_id, execution_epoch)
                return

            with self._authority_lock:
                self.cancellation.require_dispatch_authority(handle.task_id, execution_epoch)
                running = self.service.append(
                    "task.state_changed",
                    {
                        "previous": "admitted",
                        "current": "running",
                        "reason_code": "requested",
                        "execution_epoch": execution_epoch,
                    },
                    task_id=handle.task_id,
                )
                running.wait(30)
                # Current controller execution can include inference and tools. Until
                # those adapters expose finer cancellation sites, waiting_inference is
                # the conservative live site: it requires reconciliation and never
                # permits an immediate cancelled/clean claim.
                self.cancellation.set_site(
                    handle.task_id,
                    execution_epoch,
                    CancellationSite.WAITING_INFERENCE,
                )

            result = self.controller.run(
                task,
                self_check=self_check,
                route_source=route_source,
                task_id=handle.task_id,
                skill_name=skill_name,
            )

            # A cancellation that arrived while the controller was executing revokes
            # this epoch. Never expose that late worker result as authoritative.
            self.cancellation.require_dispatch_authority(handle.task_id, execution_epoch)
            result_ref, result_bytes = self._result_artifact(result)
            status = result.projection.terminal_state.value
            verdict_block = verdict_block_from_task_result(result)

            with self._authority_lock:
                self.cancellation.set_site(
                    handle.task_id,
                    execution_epoch,
                    CancellationSite.FINALISING,
                )
                self.cancellation.require_dispatch_authority(handle.task_id, execution_epoch)
                terminal = self.service.finalize_task(
                    handle.task_id,
                    verdict_payload={
                        "completion": {
                            "task_id": handle.task_id,
                            "status": status,
                            "verdict_block": verdict_block.as_payload(),
                            "worker_artifact_ref": None,
                            "result_ref": result_ref,
                        }
                    },
                    closed_payload={
                        "status": status,
                        "result_ref": result_ref,
                        "cleanup": "unknown" if status == "unknown" else "not_needed",
                    },
                    result_bytes=result_bytes,
                )
                terminal.wait(30)
                handle.result = result
                if owns_registration:
                    self._release_registration(handle.task_id, execution_epoch)
        except StaleExecutionEpoch as exc:
            # Cancellation is intent, not proof of cleanup. Leave the durable task
            # unterminated for explicit reconciliation; never convert this into a fake
            # CANCELLED or late worker result.
            handle.error = exc
        except BaseException as exc:
            handle.error = exc
            if owns_registration:
                try:
                    self._release_registration(handle.task_id, execution_epoch)
                except StaleExecutionEpoch:
                    # Revoked execution remains owned by cancellation reconciliation.
                    pass
        finally:
            handle.done.set()


__all__ = ["CancellableDurableTaskExecutor"]
