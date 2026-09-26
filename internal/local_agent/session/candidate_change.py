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

import hashlib
from dataclasses import dataclass, replace
from typing import Any

from ..config import RepoConfig
from .contracts import TaskOutcome, TaskResult
from .intents import candidate_referent
from .proof_binding import repository_tree_sha256
from .workspaces import CandidatePatch, GitWorkspaceManager, Workspace, WorkspaceError

# Skills whose purpose is to change source. They never run against the user's checkout.
CANDIDATE_CHANGE_SKILLS = frozenset({"fix-build-failure"})

# Controller-owned actions admitted like skills but executed without a model or tools.
APPLY_CANDIDATE_ACTION = "apply-candidate"
CONTROLLER_ACTIONS = frozenset({APPLY_CANDIDATE_ACTION})


def controller_action_sha256(name: str) -> str:
    """Admission fingerprint for a controller action; changes only with its contract."""
    if name not in CONTROLLER_ACTIONS:
        raise ValueError(f"not a controller action: {name}")
    return hashlib.sha256(f"lca-controller-action:{name}:v1".encode("utf-8")).hexdigest()

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


def apply_candidate(
    manager: GitWorkspaceManager | None,
    declared: RepoConfig,
    *,
    task_id: str,
    request_text: str,
) -> TaskResult:
    """Import one retained, reviewed candidate into the user's checkout, or refuse.

    The user's request names the exact candidate task. Import writes files only when
    every touched path still holds the candidate's base content; the index is never
    touched. A successful import is single-use: the workspace and record are removed.
    """
    def refuse(outcome: TaskOutcome, reason: str, answer: str) -> TaskResult:
        return TaskResult(task_id, outcome, answer, False, reason_code=reason)

    referent = candidate_referent(request_text)
    if referent is None:
        return refuse(TaskOutcome.BLOCKED, "invalid_input",
                      "No change was applied: the request must name one full candidate task ID.")
    if manager is None:
        return refuse(TaskOutcome.BLOCKED, "unavailable_capability",
                      "No change was applied: candidate workspaces are not configured.")
    if not declared.policy.allow_patch:
        return refuse(TaskOutcome.BLOCKED, "policy_denied",
                      "No change was applied: repository policy does not allow patches.")
    try:
        workspace, candidate = manager.load(referent)
    except (WorkspaceError, ValueError, KeyError) as exc:
        return refuse(TaskOutcome.BLOCKED, "invalid_input",
                      f"No change was applied: no usable candidate for task {referent} ({exc}).")
    if workspace.repository_root.resolve() != declared.root.resolve():
        return refuse(TaskOutcome.BLOCKED, "invalid_input",
                      f"No change was applied: task {referent} prepared a change for another repository.")

    imported = manager.import_patch(workspace, candidate)
    facts = {
        "candidate_task_id": referent,
        "patch_sha256": candidate.sha256,
        "paths": list(candidate.paths),
        "conflicts": list(imported.conflicts),
        "applied": imported.applied,
        "import_verified": imported.verified,
        "pre_blobs": [[p, b] for p, b in imported.pre_blobs],
    }
    if not imported.applied and imported.unresolved:
        facts["unresolved"] = list(imported.unresolved)
        return TaskResult(
            task_id, TaskOutcome.NO_VERDICT,
            "The import failed part-way and could not be fully undone. These files are "
            "in an unknown state and need your attention: "
            + ", ".join(imported.unresolved) + ".\n" + (imported.refused_reason or ""),
            False, metrics={"candidate_import": facts}, reason_code="cleanup_unknown",
        )
    if not imported.applied:
        detail = (
            "Changed since the candidate was prepared: " + ", ".join(imported.conflicts)
            if imported.conflicts else (imported.refused_reason or "refused")
        )
        return TaskResult(
            task_id, TaskOutcome.FAIL,
            (
                "No change remains applied: the import failed part-way and what it wrote "
                "was restored. Your checkout is back exactly as it was.\n"
                if imported.rolled_back
                else "No change was applied. Your checkout is exactly as it was.\n"
            ) + detail,
            False, metrics={"candidate_import": facts}, reason_code="scope_changed",
        )
    if not imported.verified:
        return TaskResult(
            task_id, TaskOutcome.FAIL,
            "The candidate was applied, but the resulting files do not match the reviewed "
            "change. Inspect: " + ", ".join(candidate.paths),
            False, metrics={"candidate_import": facts}, reason_code="verification_failed",
        )

    try:
        matches = manager.checkout_matches_candidate(workspace)
    except WorkspaceError:
        matches = None
    facts["checkout_matches_candidate_tree"] = matches
    try:
        manager.discard(workspace)
    except WorkspaceError:
        # The import itself is complete and verified; a leftover worktree is not a
        # reason to misreport it. It is recorded for cleanup instead.
        facts["workspace_discard_failed"] = True
    if matches is True:
        proof_line = (
            "Your tracked files now match the candidate tree exactly, so the candidate's "
            "build proof covers them."
        )
    elif matches is False:
        proof_line = (
            "Your checkout also differs from the candidate in other tracked files, so the "
            "candidate's build proof does not cover it. Ask me to build it to verify."
        )
    else:
        proof_line = (
            "The candidate tree could not be compared with your checkout, so the "
            "candidate's build proof is not claimed for it. Ask me to build it to verify."
        )
    return TaskResult(
        task_id, TaskOutcome.PASS,
        "Applied the reviewed change from task " + referent + " to: "
        + ", ".join(candidate.paths) + ".\nNothing was staged or committed.\n" + proof_line,
        True,
        metrics={
            "candidate_import": facts,
            "tree_sha256": repository_tree_sha256(declared.root),
        },
        verification_ran=True,
        reason_code="verification_passed",
    )


__all__ = [
    "APPLY_CANDIDATE_ACTION",
    "CONTROLLER_ACTIONS",
    "apply_candidate",
    "controller_action_sha256",
    "CANDIDATE_CHANGE_SKILLS",
    "CandidateOutcome",
    "candidate_blocker",
    "candidate_repo",
    "candidate_workspace_approval",
    "excluded_dirs",
    "settle_candidate",
]
