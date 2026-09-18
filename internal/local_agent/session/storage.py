"""DESIGN CONTRACT ONLY. M2 persistent sessions and crash recovery.

Use SQLite WAL for state/event transactions, immutable task artifact directories
for logs/diffs, schema migrations and explicit retention. Store outside target
checkout; never make the database a verification oracle. Unique request_id binds
to payload hash: identical retry returns prior task, conflicting retry fails.
Recover in-flight tasks as interrupted/unknown; never replay effects on startup.
Do not persist raw repository text, prompts or secrets by default. Encryption and
redaction requirements must be implemented before optional transcript retention.
"""
from typing import Protocol

from .events import Event


class SessionStore(Protocol):
    def admit(self, session_id: str, request_id: str, payload_sha256: str) -> str: ...
    def append(self, event: Event, expected_sequence: int) -> None: ...
    def replay(self, session_id: str, after: int, limit: int) -> list[Event]: ...
    def recover_interrupted(self) -> list[str]: ...
