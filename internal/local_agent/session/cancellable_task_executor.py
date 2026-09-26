"""Cancellation-aware durable executor with controller-owned epoch fencing.

Cancellation intent and cleanup proof are separate facts. This adapter closes the
execution-authority race first: once a task epoch is revoked, that execution can no
longer dispatch controller work or commit a late terminal result. Effectful endpoint,
process-tree and mutation cleanup remain owned by their respective adapters and require
separate reconciliation before a cancelled terminal claim can be made.
"""
from __future__ import annotations

import traceback
from threading import Lock, Thread
from typing import Any

from .cancellation import CancellationSite, CancellationSource, StaleExecutionEpoch
from .cancellation_runtime import (
    CancellationDecision,
    CancellationRuntime,
    CancellationRuntimeError,
)
from .contracts import RouteSource, TaskOutcome, TaskResult
from .results import verdict_block_from_task_result
from .session_event_service import DurableSessionService, DurableTaskExecutor, TaskHandle
from .terminal_truth import (
    CancelUnreconciled,
    DurableWriteFailed,
    derive_terminal_activity_truth,
)


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
        # The controller consults the same runtime that Stop writes to, keyed by task
        # and epoch, so a Stop reaches configured commands already running.
        binder = getattr(controller, "bind_cancellation_tokens", None)
        if callable(binder):
            binder(self.cancellation.token)
        self._ownership_lock = Lock()
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

    def _process_spawning_tools(self) -> frozenset[str]:
        resolver = getattr(self.controller, "process_spawning_tools", None)
        return frozenset(resolver()) if callable(resolver) else frozenset()

    def _terminal_truth(self, task_id: str, execution_epoch: int):
        return derive_terminal_activity_truth(
            self.service,
            task_id=task_id,
            execution_epoch=execution_epoch,
            process_spawning_tools=self._process_spawning_tools(),
        )

    def _finalize_result(
        self,
        handle: TaskHandle,
        result: TaskResult,
        *,
        execution_epoch: int,
        worker_artifact_is_result: bool = False,
    ) -> None:
        truth = self._terminal_truth(handle.task_id, execution_epoch)
        if result.projection.terminal_state.value == "completed" and truth.open_call_ids:
            raise RuntimeError(
                "controller returned completed while durable tool activity remained open: "
                + ", ".join(truth.open_call_ids)
            )
        result_ref, result_bytes = self._result_artifact(result)
        status = result.projection.terminal_state.value
        verdict_block = verdict_block_from_task_result(result)
        try:
            terminal = self.service.finalize_task(
                handle.task_id,
                verdict_payload={
                    "completion": {
                        "task_id": handle.task_id,
                        "status": status,
                        "verdict_block": verdict_block.as_payload(),
                        "worker_artifact_ref": result_ref if worker_artifact_is_result else None,
                        "result_ref": result_ref,
                    }
                },
                closed_payload={
                    "status": status,
                    "result_ref": result_ref,
                    "cleanup": truth.cleanup,
                },
                result_bytes=result_bytes,
            )
            terminal.wait(30)
        except Exception as exc:
            raise DurableWriteFailed(f"durable terminal write failed: {exc}") from exc

    def _finalize_controller_fault(
        self,
        handle: TaskHandle,
        exc: Exception,
        *,
        execution_epoch: int,
    ) -> None:
        trace = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
        bounded = trace[-7000:] if trace else f"{type(exc).__name__}: {exc}"
        result = TaskResult(
            handle.task_id,
            TaskOutcome.NO_VERDICT,
            "Controller fault. Retained traceback:\n" + bounded,
            False,
            reason_code="controller_fault",
        )
        self._finalize_result(
            handle,
            result,
            execution_epoch=execution_epoch,
            worker_artifact_is_result=True,
        )

    def _finalize_cancel_unreconciled(
        self,
        handle: TaskHandle,
        exc: StaleExecutionEpoch,
        *,
        execution_epoch: int,
    ) -> None:
        """Close a fenced execution without fabricating cancellation cleanup.

        The revoked worker result has no authority to commit, but leaving the admitted
        task non-terminal forever is also false. Persist an explicit unknown/no-verdict
        result whose cleanup field is still derived from durable activity for the revoked
        epoch. This is not a `cancelled`/`stopped` claim.
        """
        result = TaskResult(
            handle.task_id,
            TaskOutcome.NO_VERDICT,
            (
                "Stop requested; the execution epoch was revoked before its late result "
                "could commit. Cleanup has not been fully reconciled."
            ),
            False,
            verification_ran=False,
            reason_code="cancel_unreconciled",
        )
        self._finalize_result(
            handle,
            result,
            execution_epoch=execution_epoch,
            worker_artifact_is_result=True,
        )
        handle.error = CancelUnreconciled(str(exc))

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
                self.cancellation.set_site(
                    handle.task_id,
                    execution_epoch,
                    CancellationSite.WAITING_INFERENCE,
                )

            try:
                result = self.controller.run(
                    task,
                    self_check=self_check,
                    route_source=route_source,
                    task_id=handle.task_id,
                    skill_name=skill_name,
                )
            except Exception as exc:
                self._finalize_controller_fault(handle, exc, execution_epoch=execution_epoch)
                handle.error = exc
                if owns_registration:
                    self._release_registration(handle.task_id, execution_epoch)
                return

            self.cancellation.require_dispatch_authority(handle.task_id, execution_epoch)

            with self._authority_lock:
                self.cancellation.set_site(
                    handle.task_id,
                    execution_epoch,
                    CancellationSite.FINALISING,
                )
                self.cancellation.require_dispatch_authority(handle.task_id, execution_epoch)
                try:
                    self._finalize_result(handle, result, execution_epoch=execution_epoch)
                except RuntimeError as exc:
                    if isinstance(exc, DurableWriteFailed):
                        raise
                    self._finalize_controller_fault(handle, exc, execution_epoch=execution_epoch)
                    handle.error = exc
                    if owns_registration:
                        self._release_registration(handle.task_id, execution_epoch)
                    return
                handle.result = result
                if owns_registration:
                    self._release_registration(handle.task_id, execution_epoch)
        except StaleExecutionEpoch as exc:
            try:
                self._finalize_cancel_unreconciled(
                    handle,
                    exc,
                    execution_epoch=execution_epoch,
                )
            except DurableWriteFailed as write_exc:
                handle.error = write_exc
            finally:
                if owns_registration:
                    try:
                        current_epoch = self.cancellation.current_epoch(handle.task_id)
                        self._release_registration(handle.task_id, current_epoch)
                    except (CancellationRuntimeError, StaleExecutionEpoch):
                        pass
        except DurableWriteFailed as exc:
            handle.error = exc
            if owns_registration:
                try:
                    self._release_registration(handle.task_id, execution_epoch)
                except StaleExecutionEpoch:
                    pass
        except BaseException as exc:
            handle.error = exc
            if owns_registration:
                try:
                    self._release_registration(handle.task_id, execution_epoch)
                except StaleExecutionEpoch:
                    pass
        finally:
            handle.done.set()


__all__ = ["CancellableDurableTaskExecutor"]
