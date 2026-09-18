"""DESIGN CONTRACT ONLY. No scheduler implementation is enabled in product v0.

Implement in milestone M2: one lease per endpoint by default, fair FIFO queues,
deadlines, bounded admission and chat/control priority between worker calls.
Never preempt a mutation mid-write. Endpoint identity is the normalized URL plus
served model/revision and device, not a friendly profile name. Two contexts on
one endpoint do not imply two loaded weight copies or simultaneous generation.
"""
from typing import ContextManager, Protocol


class EndpointScheduler(Protocol):
    def lease(self, endpoint_id: str, task_id: str, *, deadline_s: float) -> ContextManager[None]: ...
    def cancel_queued(self, task_id: str) -> bool: ...
