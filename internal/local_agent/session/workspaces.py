"""DESIGN CONTRACT ONLY. M3 isolated edits; M5 remote execution.

One writable worktree per task, exclusive lease, clean checkout at explicit SHA.
Snapshot user dirty/index state separately. Never auto-copy secrets, ignored files
or live build directories. Patch import is a new approved action with conflict
check, not an implicit git reset/checkout. Never auto-merge or auto-push.
Protect control-plane code/config/evidence from the worker even in its own repo:
run a pinned controller checkout against a separate candidate worktree, then
restart from a reviewed candidate only after validation. No live self-reloading.
"""
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol


@dataclass(frozen=True)
class Workspace:
    workspace_id: str
    root: Path
    base_commit: str
    controller_commit: str


class WorkspaceManager(Protocol):
    def create(self, repository_id: str, base_commit: str, task_id: str) -> Workspace: ...
    def candidate_patch(self, workspace_id: str) -> bytes: ...
    def close(self, workspace_id: str, *, preserve_artifacts: bool) -> None: ...
