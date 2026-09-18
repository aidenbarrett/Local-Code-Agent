"""Bounded, sequenced activity feed. No repository content in default UI events."""
from __future__ import annotations

import json
import time
from collections import deque
from datetime import datetime, timezone
from threading import Lock, RLock
from dataclasses import dataclass
from typing import Callable


@dataclass(frozen=True)
class Event:
    session_id: str
    sequence: int
    task_id: str | None
    kind: str
    monotonic_s: float
    payload_json: str
    schema_version: int = 1
    # Monotonic time orders events and measures durations; it cannot be placed on
    # a clock. A watch pane showing "03:00 last night" needs wall time, and a
    # per-event stamp is cheaper to reason about than a session anchor plus
    # arithmetic that breaks whenever the buffer is replayed out of process.
    wall_utc: str = ""

    @property
    def payload(self) -> dict:
        return json.loads(self.payload_json)


class EventBuffer:
    def __init__(self, session_id: str, sink: Callable[[Event], None] | None = None,
                 capacity: int = 512):
        if capacity < 1:
            raise ValueError("event capacity must be positive")
        self.session_id = session_id
        self._events: deque[Event] = deque(maxlen=capacity)
        self._sequence = 0
        self.sink = sink
        self.delivery_errors = 0
        # `self._sequence += 1` is load-add-store. Gap detection in `after()`
        # depends on sequences being unique and monotonic, so the increment and
        # the append happen under one lock. Telemetry samplers and a scheduler
        # will emit from other threads.
        self._lock = Lock()
        # Serialize synchronous callbacks in sequence order too. Keep the state
        # lock separate so a slow sink does not block after(). Reentrant sinks
        # may read/re-emit, but must not wait for another producer to emit.
        self._delivery_lock = RLock()

    def emit(self, kind: str, payload: dict, task_id: str | None = None) -> Event:
        encoded = json.dumps(payload, allow_nan=False)
        with self._delivery_lock:
            with self._lock:
                self._sequence += 1
                stamp = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
                event = Event(self.session_id, self._sequence, task_id, kind,
                              time.monotonic(), encoded, wall_utc=stamp)
                self._events.append(event)
            if self.sink:
                try:
                    self.sink(event)
                except Exception:
                    # A broken presenter must not re-execute or interrupt a tool.
                    with self._lock:
                        self.delivery_errors += 1
        return event

    def after(self, cursor: int) -> list[Event]:
        with self._lock:
            if cursor < 0 or cursor > self._sequence:
                raise ValueError("invalid event cursor")
            if self._events and cursor < self._events[0].sequence - 1:
                raise ValueError("event gap: refresh session state")
            return [event for event in self._events if event.sequence > cursor]
