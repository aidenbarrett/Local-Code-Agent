"""Run a source-changing skill inside an LCA-owned candidate worktree.

The worker may edit only its own candidate worktree. Approval for ``apply_patch`` there
is granted by construction, because nothing it writes reaches the user's checkout; every
other approval-gated tool (staging, committing) stays denied. The user's checkout
changes only through a later, separate import that the user asks for by task ID.

A verified result here means: the candidate tree, built from the user's exact tracked
state plus the candidate diff, passed a full build. It does not mean the user's checkout
builds; nothing has been applied to it.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any

from ..config import RepoConfig
from .workspaces import CandidatePatch, GitWorkspaceManager, Workspace

# Skills whose purpose is to change source. They never run against the user's checkout.
CANDIDATE_CHANGE_SKILLS = frozenset({"fix-build-failure"})

# The only approval-gated tool a candidate run may use. Staging and committing remain
# denied even inside the candidate: history changes are separate capabilities.
_CANDIDATE_APPROVED_TOOLS = frozenset({"apply_patch"})


def candidate_workspace_approval(tool, arguments: dict[str, Any], decision) -> bool:
    return tool.name in _CANDIDATE_APPROVED_TOOLS


def candidate_blocker(declared: RepoConfig, *, allow_execution: bool) -> str | None:
    """Why a candidate change cannot run, or None. Checked before any workspace exists."""
    if not declared.policy.allow_patch:
        return "repository policy does not allow patches (policy.allow_patch = false)"
    if not (allow_execution and declared.policy.allow_build):
        return (
            "a candidate change must be proven by a full build, and configured build "
            "execution is not enabled for this session"
        )
    return None


def excluded_dirs(repo: RepoConfig) -> tuple[str, ...]:
    """Top-level directories that belong to the instrument, never to a candidate diff."""
    dirs = {".local-agent"}
    for configured in (repo.build_dir, repo.run_dir):
        top = configured.replace("\\", "/").strip("/").split("/", 1)[0]
        if top and top not in {".", ".."}:
            dirs.add(top)
    return tuple(sorted(dirs))


def candidate_repo(declared: RepoConfig, workspace: Workspace, *, allow_execution: bool) -> RepoConfig:
    """The declared repository configuration rooted at the candidate worktree.

    Patch authority comes from the repository's declared policy, not the product-v0
    override that keeps the user's checkout read-only. Commit authority stays off.
    """
    return replace(
        declared,
        root=workspace.root,
        policy=replace(
            declared.policy,
            allow_patch=declared.policy.allow_patch,
            allow_commit=False,
            allow_build=allow_execution and declared.policy.allow_build,
            allow_test=allow_execution and declared.policy.allow_test,
        ),
    )


@dataclass(frozen=True)
class CandidateOutcome:
    retained: bool
    paths: tuple[str, ...]
    patch_sha256: str | None
    summary: str

    def as_metrics(self, workspace: Workspace) -> dict[str, Any]:
        return {
            "retained": self.retained,
            "workspace_id": workspace.workspace_id,
            "base_commit": workspace.base_commit,
            "head_commit": workspace.head_commit,
            "patch_sha256": self.patch_sha256,
            "paths": list(self.paths),
            "dirty_paths_in_base": list(workspace.dirty_paths),
            "untracked_excluded": list(workspace.untracked_excluded),
        }


def settle_candidate(
    manager: GitWorkspaceManager,
    workspace: Workspace,
    *,
    task_id: str,
    verified: bool,
) -> tuple[CandidateOutcome, CandidatePatch | None]:
    """Retain a candidate with changes for review and import; discard an empty one."""
    candidate = manager.candidate_patch(workspace)
    if not candidate.paths:
        manager.discard(workspace)
        return CandidateOutcome(False, (), None, "No source change was produced; nothing to apply."), None

    manager.retain(workspace, candidate)
    listed = ", ".join(candidate.paths[:8]) + (" …" if len(candidate.paths) > 8 else "")
    proof = (
        "A full build of the candidate passed."
        if verified
        else "The candidate is NOT proven: no full build passed after the last edit."
    )
    lines = [
        f"Candidate change prepared in an isolated copy. Your checkout has not been modified.",
        f"Changed: {listed}",
        proof,
    ]
    if workspace.untracked_excluded:
        lines.append(
            f"{len(workspace.untracked_excluded)} untracked file(s) in your checkout were not "
            "included in the candidate's base."
        )
    lines.append(f"Candidate patch sha256 {candidate.sha256}. Task {task_id}.")
    return (
        CandidateOutcome(True, candidate.paths, candidate.sha256, "\n".join(lines)),
        candidate,
    )


__all__ = [
    "CANDIDATE_CHANGE_SKILLS",
    "CandidateOutcome",
    "candidate_blocker",
    "candidate_repo",
    "candidate_workspace_approval",
    "excluded_dirs",
    "settle_candidate",
]
