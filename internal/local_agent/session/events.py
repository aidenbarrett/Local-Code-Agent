"""Bounded, sequenced activity feed. No repository content in default UI events."""
from __future__ import annotations

import json
import time
from collections import deque
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

    def emit(self, kind: str, payload: dict, task_id: str | None = None) -> Event:
        encoded = json.dumps(payload, allow_nan=False)
        self._sequence += 1
        event = Event(self.session_id, self._sequence, task_id, kind, time.monotonic(), encoded)
        self._events.append(event)
        if self.sink:
            try:
                self.sink(event)
            except Exception:
                # A broken presenter must not re-execute or interrupt a tool.
                self.delivery_errors += 1
        return event

    def after(self, cursor: int) -> list[Event]:
        if cursor < 0 or cursor > self._sequence:
            raise ValueError("invalid event cursor")
        if self._events and cursor < self._events[0].sequence - 1:
            raise ValueError("event gap: refresh session state")
        return [event for event in self._events if event.sequence > cursor]
