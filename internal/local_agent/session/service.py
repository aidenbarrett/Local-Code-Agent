"""Single-writer durable Session Hub service boundary.

Submission is non-blocking for callers. The writer thread owns sequence assignment,
validation, durable commit and publication in that order. Subscribers receive only
committed events and must replay after an overflow gap.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from queue import Empty, Full, Queue
from threading import Event as ThreadEvent, Lock, Thread
from typing import Any
from uuid import UUID, uuid4, uuid5

from .event_contract import build_event
from .storage import SQLiteSessionStore


class ServiceClosed(RuntimeError):
    pass


class SubscriptionGap(RuntimeError):
    pass


@dataclass
class WriteReceipt:
    task_id: str | None = None
    committed: ThreadEvent = field(default_factory=ThreadEvent)
    error: BaseException | None = None

    def wait(self, timeout: float | None = None) -> None:
        if not self.committed.wait(timeout):
            raise TimeoutError("durable write did not commit before timeout")
        if self.error is not None:
            raise self.error


class Subscription:
    def __init__(self, capacity: int):
        if capacity < 1:
            raise ValueError("subscription capacity must be positive")
        self._queue: Queue[dict[str, Any]] = Queue(maxsize=capacity)
        self._gap = False
        self._lock = Lock()

    def _offer(self, event: dict[str, Any]) -> None:
        with self._lock:
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
        self._closed = False
        self._thread = Thread(target=self._writer_main, name="lca-session-writer", daemon=True)
        self._thread.start()

    def subscribe(self, *, capacity: int = 512) -> Subscription:
        sub = Subscription(capacity)
        with self._subscribers_lock:
            self._subscribers.append(sub)
        return sub

    def replay(self, *, after: int = 0, limit: int = 1000) -> list[dict[str, Any]]:
        return self.store.replay(self.stream_id, after=after, limit=limit)

    def _enqueue(self, command: _Command) -> WriteReceipt:
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
    ) -> WriteReceipt:
        """Return a stable task id immediately; execution must wait for receipt.commit."""
        task_id = str(uuid5(UUID(self.stream_id), request_id))
        receipt = WriteReceipt(task_id=task_id)
        return self._enqueue(_Command("admit", receipt, {
            "request_id": request_id,
            "payload_sha256": payload_sha256,
            "task_id": task_id,
            "payload": admission_payload,
        }))

    def append(self, kind: str, payload: dict[str, Any], *, task_id: str | None = None) -> WriteReceipt:
        receipt = WriteReceipt(task_id=task_id)
        return self._enqueue(_Command("append", receipt, {
            "kind": kind,
            "payload": payload,
            "task_id": task_id,
        }))

    def close_task(self, task_id: str, payload: dict[str, Any]) -> WriteReceipt:
        UUID(task_id)
        receipt = WriteReceipt(task_id=task_id)
        return self._enqueue(_Command("close", receipt, {"task_id": task_id, "payload": payload}))

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

    def recover_unknown_tasks(self) -> list[str]:
        """Fence old admissions as NO_VERDICT. Never execute or retry their effects."""
        recovered: list[str] = []
        for task in self.store.unterminated_tasks():
            task_id = str(task["task_id"])
            result_bytes = b'{"verdict":"NO_VERDICT","reason":"controller_crash","cleanup":"unknown"}'
            result_ref = {
                "artifact_id": str(uuid4()),
                "sha256": hashlib.sha256(result_bytes).hexdigest(),
                "media_type": "application/vnd.lca.task-result+json",
                "size_bytes": len(result_bytes),
                "availability": "unavailable",
            }
            verdict = self.append("task.verdict", {
                "completion": {
                    "task_id": task_id,
                    "status": "interrupted",
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
            }, task_id=task_id)
            verdict.wait(10)
            closed = self.close_task(task_id, {
                "status": "interrupted",
                "result_ref": result_ref,
                "cleanup": "unknown",
            })
            closed.wait(10)
            recovered.append(task_id)
        return recovered

    def close(self, timeout: float = 10) -> None:
        if self._closed:
            return
        self._closed = True
        self._commands.put(None)
        self._thread.join(timeout)
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
                    )
                    if created:
                        self._publish(event)
                else:
                    kind = "task.closed" if command.operation == "close" else command.data["kind"]
                    event = build_event(
                        stream_id=self.stream_id,
                        sequence=sequence,
                        producer_epoch=self.producer_epoch,
                        session_id=self.session_id,
                        task_id=task_id,
                        kind=kind,
                        payload=command.data["payload"],
                    )
                    if command.operation == "close":
                        self.store.close_task(event, expected_sequence=sequence)
                    else:
                        self.store.append(event, expected_sequence=sequence)
                    self._publish(event)
            except BaseException as exc:
                receipt.error = exc
            finally:
                receipt.committed.set()
