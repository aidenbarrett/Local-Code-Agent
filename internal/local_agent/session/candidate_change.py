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

import contextlib
import hashlib
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any, assert_never

from ..config import RepoConfig
from .contracts import TaskOutcome, TaskResult
from .contracts import MAX_MESSAGE_CHARS
from .intents import (
    candidate_referent,
    commit_referent,
    diff_referent,
    discard_referent,
    undo_referent,
)
from .proof_binding import repository_tree_sha256
from .workspaces import (
    CandidatePatch,
    CandidateRefusedError,
    CommitDrifted,
    CommitMismatched,
    CommitRefused,
    CommitResult,
    Committed,
    GitWorkspaceManager,
    Workspace,
    WorkspaceError,
)

if TYPE_CHECKING:
    from ..agent.orchestrator import RunResult
    from ..agent.policy import Decision
    from ..tools.tool_primitives import Tool

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


def candidate_proof_satisfied(skill: str, run: RunResult) -> bool:
    """Whether the worker's current-epoch history holds the proof this skill requires.

    Requires the orchestrator's own `verified` (no later contradiction on the current
    tree) and at least one current-epoch proof of a required kind.
    """
    if not run.state.verified:
        return False
    required = _REQUIRED_PROOF_KINDS[skill]
    epoch = run.state.mutation_epoch
    return any(
        record.epoch == epoch and record.proof in required for record in run.state.history
    )

# Controller-owned actions admitted like skills but executed without a model or tools.
APPLY_CANDIDATE_ACTION = "apply-candidate"
UNDO_CANDIDATE_ACTION = "undo-candidate"
COMMIT_CANDIDATE_ACTION = "commit-candidate"
DIFF_CANDIDATE_ACTION = "diff-candidate"
DISCARD_CANDIDATE_ACTION = "discard-candidate"
CONTROLLER_ACTIONS = frozenset({
    APPLY_CANDIDATE_ACTION, UNDO_CANDIDATE_ACTION, COMMIT_CANDIDATE_ACTION,
    DIFF_CANDIDATE_ACTION, DISCARD_CANDIDATE_ACTION,
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


@dataclass(slots=True)
class _FileStat:
    path: str
    added: int = 0
    removed: int = 0


def diffstat(patch: bytes) -> list[str]:
    """One `path | +added -removed` line per file in the patch."""
    stats: list[_FileStat] = []
    for line in patch.decode("utf-8", "replace").splitlines():
        if line.startswith("diff --git "):
            path = line.split(" b/", 1)[-1] if " b/" in line else line[len("diff --git "):]
            stats.append(_FileStat(path))
        elif stats and line.startswith("+") and not line.startswith("+++"):
            stats[-1].added += 1
        elif stats and line.startswith("-") and not line.startswith("---"):
            stats[-1].removed += 1
    return [f"  {s.path} | +{s.added} -{s.removed}" for s in stats]


def bounded(text: str, limit: int) -> tuple[str, bool]:
    if len(text) <= limit:
        return text, False
    cut = text[:limit]
    return cut[: cut.rfind("\n") + 1 or limit], True


def controller_action_sha256(name: str) -> str:
    """Admission fingerprint for a controller action; changes only with its contract."""
    if name not in CONTROLLER_ACTIONS:
        raise ValueError(f"not a controller action: {name}")
    return hashlib.sha256(f"lca-controller-action:{name}:v1".encode()).hexdigest()

# The only approval-gated tool a candidate run may use. Staging and committing remain
# denied even inside the candidate: history changes are separate capabilities.
_CANDIDATE_APPROVED_TOOLS = frozenset({"apply_patch"})


def candidate_workspace_approval(
    tool: Tool, _arguments: dict[str, Any], _decision: Decision,
) -> bool:
    """ApprovalFn for a candidate worktree: approval depends on the tool alone."""
    return tool.name in _CANDIDATE_APPROVED_TOOLS


@dataclass(frozen=True)
class CandidateBlocker:
    code: str
    explanation: str


def candidate_blockers(
    declared: RepoConfig, *, allow_execution: bool, skill: str = "fix-build-failure",
) -> tuple[CandidateBlocker, ...]:
    """All independent reasons a candidate cannot run, before any workspace exists."""
    blockers = []
    if not declared.policy.allow_patch:
        blockers.append(CandidateBlocker(
            "patch_disabled", "patches are switched off (allow_patch = false in .local-agent.toml)",
        ))
    if not allow_execution:
        blockers.append(CandidateBlocker(
            "execution_disabled", "execution is not enabled for this session",
        ))
    if not declared.policy.allow_build:
        blockers.append(CandidateBlocker(
            "build_disabled", "builds are switched off (allow_build = false in .local-agent.toml)",
        ))
    if skill in ("fix-test-failure", "implement-change") and not declared.policy.allow_test:
        blockers.append(CandidateBlocker(
            "test_disabled", "tests are switched off (allow_test = false in .local-agent.toml)",
        ))
    return tuple(blockers)


def excluded_dirs(repo: RepoConfig) -> tuple[str, ...]:
    """Top-level directories that belong to the instrument, never to a candidate diff."""
    dirs = {".local-agent"}
    for configured in (repo.build_dir, repo.run_dir):
        top = configured.replace("\\", "/").strip("/").split("/", 1)[0]
        if top and top not in {".", ".."}:
            dirs.add(top)
    return tuple(sorted(dirs))


def candidate_repo(
    declared: RepoConfig, workspace: Workspace, *, allow_execution: bool,
) -> RepoConfig:
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
    # The candidate was produced but cannot be kept safely (for example it uses Git
    # filters or changes Git attributes). Nothing is retained; the task is blocked.
    refused: bool = False

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
    try:
        candidate = manager.candidate_patch(workspace)
    except CandidateRefusedError as exc:
        manager.discard(workspace)
        return CandidateOutcome(
            False, (), None,
            f"The candidate was discarded: {exc}. Nothing is available to apply and your "
            "checkout has not been modified.",
            refused=True,
        ), None
    if not candidate.paths:
        manager.discard(workspace)
        nothing = "No source change was produced; nothing to apply."
        return CandidateOutcome(False, (), None, nothing), None

    manager.retain(workspace, candidate)
    # The proof is recorded; the candidate's build output is now only disk use.
    manager.prune_instrument_dirs(workspace)
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
                      f"No change was applied: task {referent} prepared a change "
                      "for another repository.")

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
        if imported.already_applied:
            detail = (
                "Your checkout already holds exactly this change in "
                + ", ".join(imported.conflicts)
                + ". An earlier apply of it may have been interrupted before it was "
                "recorded, so /undo cannot reverse it; inspect it with git."
            )
        elif imported.conflicts:
            detail = "Changed since the candidate was prepared: " + ", ".join(imported.conflicts)
        else:
            detail = imported.refused_reason or "refused"
        facts["already_applied"] = imported.already_applied
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


def discard_candidate(
    manager: GitWorkspaceManager | None,
    declared: RepoConfig,
    *,
    task_id: str,
    request_text: str,
) -> TaskResult:
    """Throw away one retained, unapplied candidate. Never touches the user's checkout."""
    referent = discard_referent(request_text)
    if referent is None:
        return TaskResult(task_id, TaskOutcome.BLOCKED,
                          "Nothing was discarded: the request must name one full task ID.",
                          False, reason_code="invalid_input")
    if manager is None:
        return TaskResult(task_id, TaskOutcome.BLOCKED,
                          "Nothing was discarded: candidate workspaces are not configured.",
                          False, reason_code="unavailable_capability")
    try:
        workspace, candidate = manager.load(referent)
    except (WorkspaceError, ValueError, KeyError) as exc:
        return TaskResult(task_id, TaskOutcome.BLOCKED,
                          "Nothing was discarded: no retained candidate for task "
                          f"{referent} ({exc}).",
                          False, reason_code="invalid_input")
    if workspace.repository_root.resolve() != declared.root.resolve():
        return TaskResult(task_id, TaskOutcome.BLOCKED,
                          f"Nothing was discarded: task {referent} belongs to another repository.",
                          False, reason_code="invalid_input")
    # discard always removes the retained record, so /apply is impossible afterwards; a
    # worktree that would not delete is reaped as an orphan at the next Session Hub start.
    with contextlib.suppress(WorkspaceError):
        manager.discard(workspace)
    return TaskResult(
        task_id, TaskOutcome.PASS,
        f"Discarded the candidate from task {referent}. Your checkout was not touched; "
        f"/apply {referent} is no longer possible.",
        True,
        metrics={"candidate_discard": {
            "candidate_task_id": referent, "patch_sha256": candidate.sha256,
            "paths": list(candidate.paths),
        }},
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
                          f"No diff shown: task {referent} prepared a change "
                          "for another repository.",
                          False, reason_code="invalid_input")
    header = [
        f"Candidate from task {referent}: exactly what /apply {referent} would write.",
        f"Patch sha256 {candidate.sha256} (checked against the retained record).",
        *diffstat(candidate.patch),
        "",
    ]
    body, cut = bounded(readable_patch(candidate.patch), MAX_MESSAGE_CHARS - 1200)
    truncated = "\n(diff truncated for display; the patch applied is the full reviewed patch)"
    answer = "\n".join(header) + body + (truncated if cut else "")
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
    facts = _commit_facts(referent, done)
    match done:
        case Committed(
            commit=commit, branch=branch, paths=paths, index_left=index_left
        ) if index_left:
            return TaskResult(
                task_id, TaskOutcome.NO_VERDICT,
                f"Committed the exact change from task {referent} as {commit[:12]} on "
                f"{branch}, but Git index reconciliation is incomplete for: "
                + ", ".join(index_left)
                + ". Inspect git status before continuing. Nothing was pushed.",
                False, metrics={"candidate_commit": facts},
                verification_ran=True, reason_code="verification_failed",
            )
        case Committed(commit=commit, branch=branch, paths=paths):
            return TaskResult(
                task_id, TaskOutcome.PASS,
                f"Committed the change from task {referent} as {commit[:12]} on "
                f"{branch}: " + ", ".join(paths) + ".\n"
                "Only those files were committed; anything else you had staged is still "
                "staged. Your git hooks were not run. Nothing was pushed.",
                True,
                metrics={"candidate_commit": facts,
                         "tree_sha256": repository_tree_sha256(declared.root)},
                verification_ran=True,
                reason_code="verification_passed",
            )
        case CommitMismatched(commit=commit):
            return TaskResult(
                task_id, TaskOutcome.FAIL,
                f"A commit {commit[:12]} was created but does not contain exactly the applied "
                "change. Inspect it before doing anything else.",
                False, metrics={"candidate_commit": facts}, reason_code="verification_failed",
            )
        case CommitDrifted(drifted=drifted):
            return TaskResult(
                task_id, TaskOutcome.FAIL,
                "Nothing was committed. These files changed after the change was applied: "
                + ", ".join(drifted) + ".",
                False, metrics={"candidate_commit": facts}, reason_code="scope_changed",
            )
        case CommitRefused(reason=reason):
            return TaskResult(
                task_id, TaskOutcome.BLOCKED,
                f"Nothing was committed: {reason}.",
                False, metrics={"candidate_commit": facts}, reason_code="invalid_input",
            )
        case _:
            assert_never(done)


def _commit_facts(referent: str, done: CommitResult) -> dict[str, object]:
    """The typed commit facts the retained result projects; unchanged wire shape."""
    match done:
        case (
            Committed(commit=commit, branch=branch)
            | CommitMismatched(commit=commit, branch=branch)
        ):
            known_commit: str | None = commit
            known_branch: str | None = branch
            drifted: tuple[str, ...] = ()
        case CommitDrifted(branch=branch, drifted=drifted):
            known_commit, known_branch = None, branch
        case CommitRefused(branch=branch, existing_commit=existing):
            known_commit, known_branch, drifted = existing, branch, ()
        case _:
            assert_never(done)
    return {
        "candidate_task_id": referent,
        "commit": known_commit,
        "branch": known_branch,
        "paths": list(done.paths),
        "drifted": list(drifted),
    }


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
    "APPLY_CANDIDATE_ACTION",
    "CANDIDATE_CHANGE_SKILLS",
    "CANDIDATE_PROOF",
    "COMMIT_CANDIDATE_ACTION",
    "CONTROLLER_ACTIONS",
    "DIFF_CANDIDATE_ACTION",
    "DISCARD_CANDIDATE_ACTION",
    "UNDO_CANDIDATE_ACTION",
    "CandidateBlocker",
    "CandidateOutcome",
    "apply_candidate",
    "candidate_blockers",
    "candidate_proof_satisfied",
    "candidate_repo",
    "candidate_workspace_approval",
    "commit_candidate",
    "controller_action_sha256",
    "diff_candidate",
    "discard_candidate",
    "excluded_dirs",
    "settle_candidate",
    "undo_candidate",
]
