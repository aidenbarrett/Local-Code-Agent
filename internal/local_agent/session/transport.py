"""DESIGN CONTRACT ONLY. M2 stdio RPC, then loopback GUI transport in M4.

Versioned messages: session.create, turn.submit, task.cancel, approval.respond,
events.subscribe, session.snapshot. Every mutating request has a unique request
ID and expected session revision. Max frame size, bounded queues, replay cursor
gap response and cancellation acknowledgement must be tested before exposure.
Use stdio for VS Code Remote SSH: gateway/tools live beside the Linux repository;
model may sit behind an explicitly configured authenticated tunnel to Windows.
No wildcard HTTP binding. Loopback still needs random bearer token and Origin
validation. User input/approval channels are separate from model output events.
"""
from typing import Protocol


class GatewayTransport(Protocol):
    def serve(self) -> None: ...
    def close(self) -> None: ...
