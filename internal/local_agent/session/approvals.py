"""DESIGN CONTRACT ONLY. M3 approvals live in controller state, never chat text.

Bind approval to task+workspace+policy revision+tree digest+exact action/diff hash.
One use, expiry, explicit allowed paths, and invalidate on any relevant change.
UI can submit approve/deny by request ID; models cannot manufacture approval.
Prior user grants can cover matching actions; do not prompt again needlessly.
After revocation, stop before the next side effect. See architecture section 8.
"""
from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class ApprovalRequest:
    request_id: str
    task_id: str
    workspace_id: str
    policy_revision: int
    tree_sha256: str
    action_sha256: str
    allowed_paths: tuple[str, ...]
    expires_at_utc: str


class ApprovalBroker(Protocol):
    def request(self, request: ApprovalRequest) -> None: ...
    def consume(self, request_id: str, current: ApprovalRequest) -> bool: ...
    def revoke_task(self, task_id: str) -> None: ...
