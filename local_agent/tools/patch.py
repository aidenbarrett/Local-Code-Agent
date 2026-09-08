"""Proposing and applying edits.

Two separate tools on purpose. `propose_patch` is free: it computes a unified
diff and shows it, touching nothing. `apply_patch` can only apply a patch that
was already proposed and shown, and is gated by policy. The model cannot write
a byte to the worktree that a human has not already seen as a diff.
"""

from __future__ import annotations

import difflib
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .base import (
    BlockedError,
    Risk,
    ToolError,
    ToolRegistry,
    ToolResult,
    assert_writable,
    relpath,
    resolve_in_repo,
)
from .context import ToolContext


def _protected(ctx: ToolContext) -> tuple[str, ...]:
    """Paths inside the repository that a patch may never touch.

    The build directory holds the binaries AND the record of when they were
    built. The run directory holds this run's own journal and logs. `.git`
    holds the history the evaluator reads. None of the three is the project.
    """
    return (ctx.repo.build_dir, ctx.repo.run_dir, ".local-agent", ".git")
from .context import ToolContext


@dataclass
class PendingPatch:
    patch_id: str
    path: Path
    original: str
    updated: str
    diff: str


class PatchStore:
    """Pending proposals, plus a journal of what was actually written.

    The journal is what lets an escalation hand the strong tier a clean tree
    instead of one the cheap tier has already broken.
    """

    def __init__(self, journal: Any | None = None) -> None:
        self._pending: dict[str, PendingPatch] = {}
        self.journal = journal

    def put(self, patch: PendingPatch) -> None:
        self._pending[patch.patch_id] = patch

    def take(self, patch_id: str) -> PendingPatch:
        if patch_id not in self._pending:
            raise ToolError(
                f"unknown patch id {patch_id!r}. Propose the patch first; the agent "
                "does not apply edits it has not shown."
            )
        return self._pending.pop(patch_id)

    def __len__(self) -> int:
        return len(self._pending)


def register(reg: ToolRegistry, ctx: ToolContext, store: PatchStore) -> None:
    @reg.add(
        "propose_patch",
        "Propose an exact text replacement in one file and return the unified diff. "
        "Changes nothing on disk. `find` must appear exactly once in the file.",
        {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "find": {
                    "type": "string",
                    "description": "Exact existing text, including indentation. Must be unique.",
                },
                "replace": {"type": "string"},
                "rationale": {"type": "string"},
            },
            "required": ["path", "find", "replace"],
            "additionalProperties": False,
        },
        Risk.READ,
    )
    def propose_patch(
        path: str, find: str, replace: str, rationale: str = ""
    ) -> ToolResult:
        target = resolve_in_repo(ctx.root, path)
        # Inside the repository is not the same as fair game. Refused here, at
        # the free step, so the attempt is recorded before anything is written.
        assert_writable(ctx.root, target, _protected(ctx))
        if not target.is_file():
            raise ToolError(f"{path!r} is not a file")

        original = target.read_text(encoding="utf-8")
        occurrences = original.count(find)
        if occurrences == 0:
            raise ToolError(
                "`find` text does not appear in the file. Read the exact lines first."
            )
        if occurrences > 1:
            raise ToolError(
                f"`find` text appears {occurrences} times. Widen it with surrounding "
                "lines until it is unique."
            )

        updated = original.replace(find, replace, 1)
        rel = relpath(ctx.root, target)
        diff = "".join(
            difflib.unified_diff(
                original.splitlines(keepends=True),
                updated.splitlines(keepends=True),
                fromfile=f"a/{rel}",
                tofile=f"b/{rel}",
                n=3,
            )
        )
        patch_id = uuid.uuid4().hex[:8]
        store.put(PendingPatch(patch_id, target, original, updated, diff))

        return ToolResult(
            ok=True,
            summary=f"patch {patch_id} proposed for {rel} (nothing written yet)",
            data={
                "patch_id": patch_id,
                "path": rel,
                "rationale": rationale,
                "diff": diff,
            },
        )

    @reg.add(
        "apply_patch",
        "Apply a patch that was previously returned by propose_patch. Requires "
        "approval. Refuses if the file changed since the patch was proposed.",
        {
            "type": "object",
            "properties": {"patch_id": {"type": "string"}},
            "required": ["patch_id"],
            "additionalProperties": False,
        },
        Risk.DANGEROUS,
    )
    def apply_patch(patch_id: str) -> ToolResult:
        if not ctx.repo.policy.allow_patch:
            raise BlockedError(
                "applying patches is disabled by repository policy "
                "(policy.allow_patch = false)"
            )
        patch = store.take(patch_id)
        # Checked again at the write. propose_patch already refused this, so
        # reaching here means the guard list changed underneath a stored patch,
        # and the write is the moment that matters.
        assert_writable(ctx.root, patch.path, _protected(ctx))
        current = patch.path.read_text(encoding="utf-8")
        if current != patch.original:
            raise ToolError(
                f"{relpath(ctx.root, patch.path)} changed since patch {patch_id} was "
                "proposed. Re-read the file and propose again."
            )
        patch.path.write_text(patch.updated, encoding="utf-8")
        if store.journal is not None:
            store.journal.record_write(
                patch.path, patch.original, patch.updated, "apply_patch"
            )
        return ToolResult(
            ok=True,
            summary=f"applied patch {patch_id} to {relpath(ctx.root, patch.path)}",
            data={"path": relpath(ctx.root, patch.path), "diff": patch.diff},
        )
