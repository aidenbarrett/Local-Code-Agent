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
from .contracts import MAX_MESSAGE_CHARS
from .intents import candidate_referent, commit_referent, diff_referent, undo_referent
from .proof_binding import repository_tree_sha256
from .workspaces import CandidatePatch, GitWorkspaceManager, Workspace, WorkspaceError

# Skills whose purpose is to change source. They never run against the user's checkout.
# Each maps to the full proof its candidate must pass, in the skill's own terms.
CANDIDATE_PROOF = {
    "fix-build-failure": "full build",
    "fix-test-failure": "full test run",
    "implement-change": "full build and full test run",
}
CANDIDATE_CHANGE_SKILLS = frozenset(CANDIDATE_PROOF)

# The proof kinds that satisfy each skill's contract. The orchestrator's `verified`
# accepts either a full build or a full test pass; a test fix must not be reported as
# proven by a build alone.
_REQUIRED_PROOF_KINDS = {
    "fix-build-failure": frozenset({"full_build_pass", "full_test_pass"}),
    "fix-test-failure": frozenset({"full_test_pass"}),
    # run_test refuses stale or unbuilt binaries, so a current full test pass also
    # establishes that the edited tree built.
    "implement-change": frozenset({"full_test_pass"}),
}


def candidate_proof_satisfied(skill: str, run) -> bool:
    """Whether the worker's current-epoch history holds the proof this skill requires.

    Requires the orchestrator's own `verified` (no later contradiction on the current
    tree) and at least one current-epoch proof of a required kind.
    """
    if not run.state.verified:
        return False
    required = _REQUIRED_PROOF_KINDS[skill]
    epoch = int(run.state.mutation_epoch)
    return any(
        int(getattr(record, "epoch", -1)) == epoch and getattr(record, "proof", None) in required
        for record in run.state.history
    )

# Controller-owned actions admitted like skills but executed without a model or tools.
APPLY_CANDIDATE_ACTION = "apply-candidate"
UNDO_CANDIDATE_ACTION = "undo-candidate"
COMMIT_CANDIDATE_ACTION = "commit-candidate"
DIFF_CANDIDATE_ACTION = "diff-candidate"
CONTROLLER_ACTIONS = frozenset({
    APPLY_CANDIDATE_ACTION, UNDO_CANDIDATE_ACTION, COMMIT_CANDIDATE_ACTION,
    DIFF_CANDIDATE_ACTION,
})

_PREVIEW_LINES = 40


def readable_patch(patch: bytes) -> str:
    """The patch as text, with binary payloads replaced by a one-line marker."""
    out: list[str] = []
    in_binary = False
    text = patch.decode("utf-8", "replace")
    for line in text.splitlines():
        if line.startswith("diff --git "):
            in_binary = False
        if line == "GIT binary patch":
            in_binary = True
            out.append("(binary content not shown)")
            continue
        if not in_binary:
            out.append(line)
    return "\n".join(out) + ("\n" if text.endswith("\n") and out else "")


def diffstat(patch: bytes) -> list[str]:
    """One `path | +added -removed` line per file in the patch."""
    stats: list[list] = []
    for line in patch.decode("utf-8", "replace").splitlines():
        if line.startswith("diff --git "):
            path = line.split(" b/", 1)[-1] if " b/" in line else line[len("diff --git "):]
            stats.append([path, 0, 0])
        elif stats and line.startswith("+") and not line.startswith("+++"):
            stats[-1][1] += 1
        elif stats and line.startswith("-") and not line.startswith("---"):
            stats[-1][2] += 1
    return [f"  {p} | +{a} -{r}" for p, a, r in stats]


def bounded(text: str, limit: int) -> tuple[str, bool]:
    if len(text) <= limit:
        return text, False
    cut = text[:limit]
    return cut[: cut.rfind("\n") + 1 or limit], True


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


def candidate_blocker(
    declared: RepoConfig, *, allow_execution: bool, skill: str = "fix-build-failure",
) -> str | None:
    """Why a candidate change cannot run, or None. Checked before any workspace exists."""
    if not declared.policy.allow_patch:
        return "repository policy does not allow patches (policy.allow_patch = false)"
    if not (allow_execution and declared.policy.allow_build):
        return (
            f"a candidate change must be proven by a {CANDIDATE_PROOF[skill]}, and configured "
            "build execution is not enabled for this session"
        )
    if skill in ("fix-test-failure", "implement-change") and not declared.policy.allow_test:
        return (
            f"this change must be proven by a {CANDIDATE_PROOF[skill]}, and repository "
            "policy does not allow running tests (policy.allow_test = false)"
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
            "untracked_included": list(workspace.untracked_included),
        }


def settle_candidate(
    manager: GitWorkspaceManager,
    workspace: Workspace,
    *,
    task_id: str,
    verified: bool,
    proof: str = "full build",
) -> tuple[CandidateOutcome, CandidatePatch | None]:
    """Retain a candidate with changes for review and import; discard an empty one."""
    candidate = manager.candidate_patch(workspace)
    if not candidate.paths:
        manager.discard(workspace)
        return CandidateOutcome(False, (), None, "No source change was produced; nothing to apply."), None

    manager.retain(workspace, candidate)
    listed = ", ".join(candidate.paths[:8]) + (" …" if len(candidate.paths) > 8 else "")
    proof_line = (
        f"A {proof} of the candidate passed."
        if verified
        else f"The candidate is NOT proven: no {proof} passed after the last edit."
    )
    lines = [
        "Candidate change prepared in an isolated copy. Your checkout has not been modified.",
        f"Changed: {listed}",
        proof_line,
    ]
    if workspace.untracked_included:
        lines.append(
            f"Included {len(workspace.untracked_included)} untracked file(s) from your "
            "checkout so the candidate saw the same source (ignored files never are)."
        )
    if workspace.untracked_excluded:
        lines.append(
            f"{len(workspace.untracked_excluded)} untracked file(s) over 5 MiB were left out "
            "of the candidate, so its proof does not cover them: "
            + ", ".join(workspace.untracked_excluded[:5])
        )
    lines.append("Diffstat:")
    lines.extend(diffstat(candidate.patch))
    preview_lines = readable_patch(candidate.patch).splitlines()
    preview, cut = bounded("\n".join(preview_lines[:_PREVIEW_LINES]), 2500)
    lines.append("Preview:")
    lines.append(preview)
    if cut or len(preview_lines) > _PREVIEW_LINES:
        lines.append(f"(preview truncated; full diff: /diff {task_id})")
    lines.append(f"Candidate patch sha256 {candidate.sha256}. Task {task_id}.")
    lines.append(f"Review: /diff {task_id}   Apply: /apply {task_id}")
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
        manager.record_applied(referent, declared.root, candidate, imported)
        facts["undo_available"] = True
    except (WorkspaceError, OSError):
        facts["undo_available"] = False
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
        + ", ".join(candidate.paths) + ".\nNothing was staged or committed.\n" + proof_line
        + (
            f"\nTo reverse exactly this change: /undo {referent}"
            f"\nTo commit exactly this change on the current branch: /commit {referent}"
            if facts["undo_available"] else ""
        ),
        True,
        metrics={
            "candidate_import": facts,
            "tree_sha256": repository_tree_sha256(declared.root),
        },
        verification_ran=True,
        reason_code="verification_passed",
    )


def diff_candidate(
    manager: GitWorkspaceManager | None,
    declared: RepoConfig,
    *,
    task_id: str,
    request_text: str,
) -> TaskResult:
    """Show the exact retained candidate patch. Read-only; changes nothing anywhere."""
    referent = diff_referent(request_text)
    if referent is None:
        return TaskResult(task_id, TaskOutcome.BLOCKED,
                          "No diff shown: the request must name one full task ID.",
                          False, reason_code="invalid_input")
    if manager is None:
        return TaskResult(task_id, TaskOutcome.BLOCKED,
                          "No diff shown: candidate workspaces are not configured.",
                          False, reason_code="unavailable_capability")
    try:
        workspace, candidate = manager.load(referent)
    except (WorkspaceError, ValueError, KeyError) as exc:
        return TaskResult(task_id, TaskOutcome.BLOCKED,
                          f"No diff shown: no retained candidate for task {referent} ({exc}).",
                          False, reason_code="invalid_input")
    if workspace.repository_root.resolve() != declared.root.resolve():
        return TaskResult(task_id, TaskOutcome.BLOCKED,
                          f"No diff shown: task {referent} prepared a change for another repository.",
                          False, reason_code="invalid_input")
    header = [
        f"Candidate from task {referent}: exactly what /apply {referent} would write.",
        f"Patch sha256 {candidate.sha256} (checked against the retained record).",
        *diffstat(candidate.patch),
        "",
    ]
    body, cut = bounded(readable_patch(candidate.patch), MAX_MESSAGE_CHARS - 1200)
    answer = "\n".join(header) + body + (
        "\n(diff truncated for display; the patch applied is the full reviewed patch)" if cut else ""
    )
    return TaskResult(
        task_id, TaskOutcome.PASS, answer, True,
        metrics={"candidate_diff": {
            "candidate_task_id": referent, "patch_sha256": candidate.sha256,
            "paths": list(candidate.paths), "truncated": cut,
        }},
        verification_ran=True,
        reason_code="verification_passed",
    )


def commit_candidate(
    manager: GitWorkspaceManager | None,
    declared: RepoConfig,
    *,
    task_id: str,
    request_text: str,
) -> TaskResult:
    """Commit exactly one applied candidate on the current branch; never push."""
    referent = commit_referent(request_text)
    if referent is None:
        return TaskResult(task_id, TaskOutcome.BLOCKED,
                          "Nothing was committed: the request must name one full task ID.",
                          False, reason_code="invalid_input")
    if manager is None:
        return TaskResult(task_id, TaskOutcome.BLOCKED,
                          "Nothing was committed: candidate workspaces are not configured.",
                          False, reason_code="unavailable_capability")
    if not declared.policy.allow_commit:
        return TaskResult(task_id, TaskOutcome.BLOCKED,
                          "Nothing was committed: repository policy does not allow commits "
                          "(policy.allow_commit = false).",
                          False, reason_code="policy_denied")
    message = (
        f"Apply Local Code Agent change from task {referent}\n\n"
        f"LCA-Task: {referent}\n"
    )
    done = manager.commit_applied(referent, declared.root, message)
    facts = {
        "candidate_task_id": referent,
        "commit": done.commit,
        "branch": done.branch,
        "paths": list(done.paths),
        "drifted": list(done.drifted),
    }
    if done.committed:
        return TaskResult(
            task_id, TaskOutcome.PASS,
            f"Committed the change from task {referent} as {done.commit[:12]} on "
            f"{done.branch}: " + ", ".join(done.paths) + ".\n"
            "Only those files were committed; anything else you had staged is still "
            "staged. Your git hooks were not run. Nothing was pushed.",
            True,
            metrics={"candidate_commit": facts, "tree_sha256": repository_tree_sha256(declared.root)},
            verification_ran=True,
            reason_code="verification_passed",
        )
    if done.mismatched:
        return TaskResult(
            task_id, TaskOutcome.FAIL,
            f"A commit {done.commit[:12]} was created but does not contain exactly the applied "
            "change. Inspect it before doing anything else.",
            False, metrics={"candidate_commit": facts}, reason_code="verification_failed",
        )
    if done.drifted:
        return TaskResult(
            task_id, TaskOutcome.FAIL,
            "Nothing was committed. These files changed after the change was applied: "
            + ", ".join(done.drifted) + ".",
            False, metrics={"candidate_commit": facts}, reason_code="scope_changed",
        )
    return TaskResult(
        task_id, TaskOutcome.BLOCKED,
        "Nothing was committed: " + (done.refused_reason or "refused") + ".",
        False, metrics={"candidate_commit": facts}, reason_code="invalid_input",
    )


def undo_candidate(
    manager: GitWorkspaceManager | None,
    declared: RepoConfig,
    *,
    task_id: str,
    request_text: str,
) -> TaskResult:
    """Reverse one applied candidate where its files still hold exactly what it wrote."""
    referent = undo_referent(request_text)
    if referent is None:
        return TaskResult(task_id, TaskOutcome.BLOCKED,
                          "Nothing was undone: the request must name one full task ID.",
                          False, reason_code="invalid_input")
    if manager is None:
        return TaskResult(task_id, TaskOutcome.BLOCKED,
                          "Nothing was undone: candidate workspaces are not configured.",
                          False, reason_code="unavailable_capability")
    if not declared.policy.allow_patch:
        return TaskResult(task_id, TaskOutcome.BLOCKED,
                          "Nothing was undone: repository policy does not allow patches.",
                          False, reason_code="policy_denied")
    undone = manager.undo_applied(referent, declared.root)
    facts = {
        "candidate_task_id": referent,
        "paths": list(undone.paths),
        "drifted": list(undone.drifted),
        "unresolved": list(undone.unresolved),
    }
    if undone.unresolved:
        return TaskResult(
            task_id, TaskOutcome.NO_VERDICT,
            "The undo could not restore every file. These need your attention: "
            + ", ".join(undone.unresolved) + ".",
            False, metrics={"candidate_undo": facts}, reason_code="cleanup_unknown",
        )
    if not undone.undone and undone.drifted:
        return TaskResult(
            task_id, TaskOutcome.FAIL,
            "Nothing was undone. These files changed after the change was applied, and "
            "undoing would overwrite that work: " + ", ".join(undone.drifted) + ".",
            False, metrics={"candidate_undo": facts}, reason_code="scope_changed",
        )
    if not undone.undone:
        return TaskResult(task_id, TaskOutcome.BLOCKED,
                          "Nothing was undone: " + (undone.refused_reason or "refused") + ".",
                          False, metrics={"candidate_undo": facts}, reason_code="invalid_input")
    return TaskResult(
        task_id, TaskOutcome.PASS,
        f"Undid the change applied from task {referent}. Restored exactly: "
        + ", ".join(undone.paths) + ". Nothing was staged or committed.",
        True,
        metrics={"candidate_undo": facts, "tree_sha256": repository_tree_sha256(declared.root)},
        verification_ran=True,
        reason_code="verification_passed",
    )


__all__ = [
    "DIFF_CANDIDATE_ACTION",
    "diff_candidate",
    "COMMIT_CANDIDATE_ACTION",
    "commit_candidate",
    "UNDO_CANDIDATE_ACTION",
    "undo_candidate",
    "APPLY_CANDIDATE_ACTION",
    "CONTROLLER_ACTIONS",
    "apply_candidate",
    "controller_action_sha256",
    "CANDIDATE_CHANGE_SKILLS",
    "CANDIDATE_PROOF",
    "candidate_proof_satisfied",
    "CandidateOutcome",
    "candidate_blocker",
    "candidate_repo",
    "candidate_workspace_approval",
    "excluded_dirs",
    "settle_candidate",
]
