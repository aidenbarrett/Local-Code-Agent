"""Controller-side task execution provenance, separate from conversation routing.

``RouteSource`` answers who selected a conversation turn's repository route. A durable
task may also originate from a scheduled watch, which is not a conversation route at
all. Keep that distinction explicit instead of teaching route policy that watches are
synthetic users or rules.
"""
from __future__ import annotations

from enum import Enum

from .contracts import RouteSource


class TaskExecutionSource(str, Enum):
    RULE = "rule"
    MODEL_PROPOSAL = "model_proposal"
    USER_DIRECT = "user_direct"
    WATCH = "watch"

    @classmethod
    def coerce(
        cls,
        value: "TaskExecutionSource | RouteSource | str",
    ) -> "TaskExecutionSource":
        if isinstance(value, cls):
            return value
        if isinstance(value, RouteSource):
            return cls(value.value)
        return cls(value)


__all__ = ["TaskExecutionSource"]
