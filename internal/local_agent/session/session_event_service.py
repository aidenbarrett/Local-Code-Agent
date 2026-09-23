"""Single-writer durable Session Hub service boundary.

Submission is non-blocking for callers. The writer thread owns sequence assignment,
validation, durable commit and publication in that order. Subscribers receive only
committed events; replay/live handoff is cursor-bounded and overflow is explicit.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from queue import Empty, Full, Queue
from threading import Event as ThreadEvent, Lock, Thread
from time import monotonic
from typing import Any
from uuid import UUID, uuid4, uuid5

from .contracts import MAX_MESSAGE_CHARS, RouteSource, TaskOutcome, TaskResult, TaskVerdict
from .event_contract import build_event
from .results import verdict_block_from_task_result
from .session_store import SQLiteSessionStore
from .terminal_completion import validate_terminal_completion


_RESULT_SCHEMA = "lca.task-result/1"
_RESULT_MEDIA_TYPE = "application/vnd.lca.task-result+json"


class ServiceClosed(RuntimeError):
    pass


class SubscriptionGap(RuntimeError):
    pass


@dataclass
class WriteReceipt:
    task_id: str | None = None
    committed: ThreadEvent = field(default_factory=ThreadEvent)
    error: BaseException | None = None
    created: bool | None = None

    def wait(self, timeout: float | None = None) -> None:
        if not self.committed.wait(timeout):
            raise TimeoutError("durable write did not commit before timeout")
        if self.error is not None:
            raise self.error


class Subscription:
    def __init__(self, capacity: int, *, live_after: int = 0):
        if capacity < 1:
            raise ValueError("subscription capacity must be positive")
        if live_after < 0:
            raise ValueError("subscription live cursor cannot be negative")
        self._queue: Queue[dict[str, Any]] = Queue(maxsize=capacity)
        self._gap = False
        self._lock = Lock()
        self._live_after = live_after

    def _offer(self, event: dict[str, Any]) -> None:
        sequence = int(event["sequence"])
        with self._lock:
            if sequence <= self._live_after:
                return
            if self._gap:
                return
            try:
                self._queue.put_nowait(event)
            except Full:
                self._gap = True

    def drain(self, *, limit: int = 256) -> list[dict[str, Any]]:
        if limit < 1:
            raise ValueError("drain limit must be positive")
        with self._lock:
            if self._gap:
                raise SubscriptionGap("subscriber overflowed; replay durable state from the last cursor")
        out: list[dict[str, Any]] = []
        for _ in range(limit):
            try:
                out.append(self._queue.get_nowait())
            except Empty:
                break
        return out


@dataclass(frozen=True)
class ReplaySubscription:
    stream_id: str
    replay_after: int
    replay_through: int
    live: Subscription


@dataclass(frozen=True)
class _Command:
    operation: str
    receipt: WriteReceipt
    data: dict[str, Any]


class DurableSessionService:
    def __init__(
        self,
        store: SQLiteSessionStore,
        *,
        stream_id: str,
        producer_epoch: str | None = None,
        session_id: str | None = None,
        command_capacity: int = 256,
    ):
        UUID(stream_id)
        if session_id is not None:
            UUID(session_id)
        self.store = store
        self.stream_id = stream_id
        self.producer_epoch = producer_epoch or str(uuid4())
        UUID(self.producer_epoch)
        self.session_id = session_id
        self._commands: Queue[_Command | None] = Queue(maxsize=command_capacity)
        self._subscribers: list[Subscription] = []
        self._subscribers_lock = Lock()
        self._lifecycle_lock = Lock()
        self._closed = False
        self._shutdown_enqueued = False
        self._thread = Thread(target=self._writer_main, name="lca-session-writer", daemon=True)
        self._thread.start()

    def subscribe(self, *, capacity: int = 512) -> Subscription:
        sub = Subscription(capacity)
        with self._lifecycle_lock:
            if self._closed:
                raise ServiceClosed("session service is closed")
            with self._subscribers_lock:
                self._subscribers.append(sub)
        return sub

    def subscribe_from(self, *, after: int = 0, capacity: int = 512) -> ReplaySubscription:
        if after < 0:
            raise ValueError("replay cursor cannot be negative")
        with self._lifecycle_lock:
            if self._closed:
                raise ServiceClosed("session service is closed")
            with self._subscribers_lock:
                replay_through = self.store.next_sequence(self.stream_id) - 1
                if after > replay_through:
                    raise ValueError("replay cursor is beyond the durable stream")
                sub = Subscription(capacity, live_after=replay_through)
                self._subscribers.append(sub)
        return ReplaySubscription(
            stream_id=self.stream_id,
            replay_after=after,
            replay_through=replay_through,
            live=sub,
        )

    def replay(self, *, after: int = 0, limit: int = 1000) -> list[dict[str, Any]]:
        return self.store.replay(self.stream_id, after=after, limit=limit)

    def replay_subscription(
        self,
        handoff: ReplaySubscription,
        *,
        after: int | None = None,
        limit: int = 1000,
    ) -> list[dict[str, Any]]:
        if handoff.stream_id != self.stream_id:
            raise ValueError("replay subscription belongs to a different durable stream")
        cursor = handoff.replay_after if after is None else after
        if cursor < handoff.replay_after:
            raise ValueError("replay cursor cannot move behind the subscription start")
        if cursor > handoff.replay_through:
            raise ValueError("replay cursor is beyond the subscription boundary")
        if cursor == handoff.replay_through:
            return []
        return [
            event for event in self.replay(after=cursor, limit=limit)
            if int(event["sequence"]) <= handoff.replay_through
        ]

    def _enqueue(self, command: _Command) -> WriteReceipt:
        with self._lifecycle_lock:
            if self._closed:
                raise ServiceClosed("session service is closed")
            try:
                self._commands.put_nowait(command)
            except Full as exc:
                raise RuntimeError("session writer queue is full") from exc
        return command.receipt

    def submit_task(
        self,
        *,
        request_id: str,
        payload_sha256: str,
        admission_payload: dict[str, Any],
        request_bytes: bytes | None = None,
    ) -> WriteReceipt:
        task_id = str(uuid5(UUID(self.stream_id), request_id))
        receipt = WriteReceipt(task_id=task_id)
        return self._enqueue(_Command("admit", receipt, {
            "request_id": request_id,
            "payload_sha256": payload_sha256,
            "task_id": task_id,
            "payload": admission_payload,
            "request_bytes": request_bytes,
        }))

    def append(self, kind: str, payload: dict[str, Any], *, task_id: str | None = None) -> WriteReceipt:
        if kind in {"task.verdict", "task.closed"}:
            raise ValueError("terminal task events must use finalize_task")
        receipt = WriteReceipt(task_id=task_id)
        return self._enqueue(_Command("append", receipt, {
            "kind": kind,
            "payload": payload,
            "task_id": task_id,
        }))

    def finalize_task(
        self,
        task_id: str,
        *,
        verdict_payload: dict[str, Any],
        closed_payload: dict[str, Any],
        result_bytes: bytes | None = None,
    ) -> WriteReceipt:
        UUID(task_id)
        validate_terminal_completion(
            task_id,
            verdict_payload=verdict_payload,
            closed_payload=closed_payload,
            result_bytes=result_bytes,
        )
        receipt = WriteReceipt(task_id=task_id)
        return self._enqueue(_Command("finalize", receipt, {
            "task_id": task_id,
            "verdict_payload": verdict_payload,
            "closed_payload": closed_payload,
            "result_bytes": result_bytes,
        }))

    def request_cancel(
        self,
        task_id: str,
        *,
        execution_epoch: int,
        source: str = "user",
        reason_code: str = "requested",
    ) -> WriteReceipt:
        UUID(task_id)
        return self.append("task.cancel_requested", {
            "request_id": str(uuid4()),
            "source": source,
            "reason_code": reason_code,
            "execution_epoch": execution_epoch,
        }, task_id=task_id)

    @staticmethod
    def _result_ref(payload: bytes) -> dict[str, Any]:
        return {
            "artifact_id": str(uuid4()),
            "sha256": hashlib.sha256(payload).hexdigest(),
            "media_type": _RESULT_MEDIA_TYPE,
            "size_bytes": len(payload),
            "availability": "retained",
        }

    def recover_unknown_tasks(self) -> list[str]:
        """Fence this stream's old admissions as NO_VERDICT/unknown; never retry effects."""
        recovered: list[str] = []
        for task in self.store.unterminated_tasks(self.stream_id):
            task_id = str(task["task_id"])
            result_bytes = json.dumps({
                "schema": _RESULT_SCHEMA,
                "task_id": task_id,
                "outcome": TaskOutcome.NO_VERDICT.value,
                "terminal_state": "unknown",
                "verdict": TaskVerdict.NO_VERDICT.value,
                "verification_ran": False,
                "verified_at_completion": False,
                "evidence_ids": [],
                "answer": "Controller restarted before a durable terminal result; cleanup is unknown.",
            }, sort_keys=True, separators=(",", ":")).encode("utf-8")
            result_ref = self._result_ref(result_bytes)
            terminal = self.finalize_task(
                task_id,
                verdict_payload={
                    "completion": {
                        "task_id": task_id,
                        "status": "unknown",
                        "verdict_block": {
                            "verdict": "NO_VERDICT",
                            "reason_code": "controller_crash",
                            "scope": "effects and cleanup after controller restart",
                            "evidence_ids": [],
                            "tree_sha256": None,
                            "rendered_lines": [
                                "NO_VERDICT: controller restarted before a durable terminal result.",
                                "Cleanup unknown; task was not retried.",
                            ],
                        },
                        "worker_artifact_ref": None,
                        "result_ref": result_ref,
                    }
                },
                closed_payload={
                    "status": "unknown",
                    "result_ref": result_ref,
                    "cleanup": "unknown",
                },
                result_bytes=result_bytes,
            )
            terminal.wait(10)
            recovered.append(task_id)
        return recovered

    def close(self, timeout: float = 10) -> None:
        if timeout < 0:
            raise ValueError("shutdown timeout cannot be negative")
        deadline = monotonic() + timeout
        with self._lifecycle_lock:
            self._closed = True
            if not self._shutdown_enqueued:
                remaining = max(0.0, deadline - monotonic())
                try:
                    self._commands.put(None, timeout=remaining)
                except Full as exc:
                    raise TimeoutError("session writer shutdown could not be queued before timeout") from exc
                self._shutdown_enqueued = True
        remaining = max(0.0, deadline - monotonic())
        self._thread.join(remaining)
        if self._thread.is_alive():
            raise TimeoutError("session writer did not stop")

    def _publish(self, event: dict[str, Any]) -> None:
        with self._subscribers_lock:
            subscribers = list(self._subscribers)
        for subscriber in subscribers:
            subscriber._offer(event)

    def _writer_main(self) -> None:
        while True:
            command = self._commands.get()
            if command is None:
                return
            receipt = command.receipt
            try:
                sequence = self.store.next_sequence(self.stream_id)
                task_id = command.data.get("task_id")
                if command.operation == "admit":
                    event = build_event(
                        stream_id=self.stream_id,
                        sequence=sequence,
                        producer_epoch=self.producer_epoch,
                        session_id=self.session_id,
                        task_id=task_id,
                        kind="task.admitted",
                        payload=command.data["payload"],
                    )
                    _, created = self.store.admit(
                        request_id=command.data["request_id"],
                        payload_sha256=command.data["payload_sha256"],
                        envelope=event,
                        expected_sequence=sequence,
                        request_bytes=command.data.get("request_bytes"),
                    )
                    receipt.created = created
                    if created:
                        self._publish(event)
                elif command.operation == "finalize":
                    verdict = build_event(
                        stream_id=self.stream_id,
                        sequence=sequence,
                        producer_epoch=self.producer_epoch,
                        session_id=self.session_id,
                        task_id=task_id,
                        kind="task.verdict",
                        payload=command.data["verdict_payload"],
                    )
                    closed = build_event(
                        stream_id=self.stream_id,
                        sequence=sequence + 1,
                        producer_epoch=self.producer_epoch,
                        session_id=self.session_id,
                        task_id=task_id,
                        kind="task.closed",
                        payload=command.data["closed_payload"],
                    )
                    self.store.finalize_task(
                        verdict,
                        closed,
                        expected_sequence=sequence,
                        result_bytes=command.data.get("result_bytes"),
                    )
                    self._publish(verdict)
                    self._publish(closed)
                else:
                    event = build_event(
                        stream_id=self.stream_id,
                        sequence=sequence,
                        producer_epoch=self.producer_epoch,
                        session_id=self.session_id,
                        task_id=task_id,
                        kind=command.data["kind"],
                        payload=command.data["payload"],
                    )
                    self.store.append(event, expected_sequence=sequence)
                    self._publish(event)
            except BaseException as exc:
                receipt.error = exc
            finally:
                receipt.committed.set()


@dataclass
class TaskHandle:
    task_id: str
    admission: WriteReceipt
    done: ThreadEvent = field(default_factory=ThreadEvent)
    result: TaskResult | None = None
    error: BaseException | None = None

    def wait(self, timeout: float | None = None) -> TaskResult | None:
        if not self.done.wait(timeout):
            raise TimeoutError("task did not reach a durable terminal result before timeout")
        if self.error is not None:
            raise self.error
        return self.result


class DurableTaskExecutor:
    def __init__(self, service: DurableSessionService, controller):
        self.service = service
        self.controller = controller

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
        admission = self.service.submit_task(
            request_id=request_id,
            payload_sha256=payload_sha256,
            admission_payload=admission_payload,
            request_bytes=request_bytes,
        )
        assert admission.task_id is not None
        handle = TaskHandle(admission.task_id, admission)
        Thread(
            target=self._run,
            args=(
                handle,
                task,
                self_check,
                route_source,
                int(admission_payload["execution_epoch"]),
                skill_name,
            ),
            name=f"lca-task-{admission.task_id[:8]}",
            daemon=True,
        ).start()
        return handle

    @staticmethod
    def _result_artifact(result: TaskResult) -> tuple[dict[str, Any], bytes]:
        payload = json.dumps({
            "schema": _RESULT_SCHEMA,
            "task_id": result.task_id,
            "outcome": result.outcome.value,
            "terminal_state": result.projection.terminal_state.value,
            "verdict": result.projection.verdict.value,
            "verification_ran": bool(result.verification_ran),
            "verified_at_completion": bool(result.verified_at_completion),
            "evidence_ids": list(result.evidence_ids),
            "answer": result.answer[:MAX_MESSAGE_CHARS],
        }, sort_keys=True, separators=(",", ":")).encode("utf-8")
        return DurableSessionService._result_ref(payload), payload

    def _run(
        self,
        handle: TaskHandle,
        task: str,
        self_check: bool,
        route_source: RouteSource | str,
        execution_epoch: int,
        skill_name: str | None,
    ) -> None:
        try:
            handle.admission.wait(30)
            if handle.admission.created is False:
                return
            running = self.service.append("task.state_changed", {
                "previous": "admitted",
                "current": "running",
                "reason_code": "requested",
                "execution_epoch": execution_epoch,
            }, task_id=handle.task_id)
            running.wait(30)
            result = self.controller.run(
                task,
                self_check=self_check,
                route_source=route_source,
                task_id=handle.task_id,
                skill_name=skill_name,
            )
            handle.result = result
            result_ref, result_bytes = self._result_artifact(result)
            status = result.projection.terminal_state.value
            verdict_block = verdict_block_from_task_result(result)
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
        except BaseException as exc:
            handle.error = exc
        finally:
            handle.done.set()
