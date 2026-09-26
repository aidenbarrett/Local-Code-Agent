"""Isolated candidate worktrees for LCA-owned source changes.

One writable worktree per task, under an exclusive lease, checked out detached at an
explicit commit. The user's checkout is never the place LCA edits:

- The base is the user's exact tracked state. A clean checkout bases on ``HEAD``. A dirty
  one bases on ``git stash create``, a commit object that records the dirty tracked
  state without touching the user's index, working files, stash list or refs.
  Untracked files are not copied (secrets, scratch, ignored build output) and are
  listed so a result can say what it did not see.
- The candidate worktree lives outside the user's checkout and never receives the
  build or run directories.
- The user's hooks never run: every controller git call pins ``core.hooksPath`` to an
  empty directory owned by LCA.
- Importing a candidate into the user's checkout is a separate action with an exact
  precondition. Every path the candidate touches must still hold the base content,
  compared through git's own clean filters so line-ending conversion is not mistaken
  for a conflict. Any mismatch refuses the whole import and writes nothing. The index is
  never touched; nothing is staged, committed, merged or pushed.

This module owns workspace state only. Whether an import is authorised is decided by
the caller's approval authority before ``import_patch`` is called.
"""
from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID, uuid4

_GIT_TIMEOUT_S = 300


class WorkspaceError(RuntimeError):
    """A workspace precondition failed or git refused a controller operation."""


@dataclass(frozen=True)
class Workspace:
    workspace_id: str
    task_id: str
    root: Path
    repository_root: Path
    head_commit: str
    base_commit: str
    controller_commit: str
    dirty_paths: tuple[str, ...]
    untracked_excluded: tuple[str, ...]
    excluded_dirs: tuple[str, ...]


@dataclass(frozen=True)
class CandidatePatch:
    workspace_id: str
    base_commit: str
    patch: bytes
    sha256: str
    paths: tuple[str, ...]
    # Blob ids of each touched path in the candidate. None for a deletion.
    post_blobs: tuple[tuple[str, str | None], ...]


@dataclass(frozen=True)
class ImportResult:
    applied: bool
    paths: tuple[str, ...]
    conflicts: tuple[str, ...]
    refused_reason: str | None
    # True only if every touched path in the user's checkout now hashes to the
    # candidate's reviewed post-image.
    verified: bool
    # Blob ids the user's paths held immediately before import, for owned revert.
    pre_blobs: tuple[tuple[str, str | None], ...] = ()


def _split_z(raw: bytes) -> tuple[str, ...]:
    return tuple(item.decode("utf-8", "surrogateescape") for item in raw.split(b"\0") if item)


class GitWorkspaceManager:
    """Create, inspect, import from and remove per-task candidate worktrees."""

    def __init__(self, workspaces_root: Path, *, controller_commit: str) -> None:
        if not controller_commit or not isinstance(controller_commit, str):
            raise ValueError("controller_commit must be a nonempty string")
        self.workspaces_root = Path(workspaces_root).resolve()
        self.workspaces_root.mkdir(parents=True, exist_ok=True)
        self.controller_commit = controller_commit
        self._empty_hooks = self.workspaces_root / ".no-hooks"
        self._empty_hooks.mkdir(exist_ok=True)

    # -- git ----------------------------------------------------------------

    def _git(
        self,
        cwd: Path,
        *args: str,
        stdin: bytes | None = None,
        check: bool = True,
    ) -> subprocess.CompletedProcess[bytes]:
        env = dict(os.environ)
        env.update({
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_OPTIONAL_LOCKS": "0",
            "GIT_PAGER": "cat",
            "LC_ALL": "C",
        })
        argv = [
            "git",
            "-c", f"core.hooksPath={self._empty_hooks}",
            "-c", "core.fsmonitor=false",
            "-c", "core.quotepath=false",
            "-c", "diff.noprefix=false",
            "-c", "diff.mnemonicPrefix=false",
            *args,
        ]
        try:
            done = subprocess.run(
                argv,
                cwd=str(cwd),
                env=env,
                input=stdin,
                capture_output=True,
                timeout=_GIT_TIMEOUT_S,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise WorkspaceError(f"git {args[0]} could not run: {exc}") from exc
        if check and done.returncode != 0:
            message = done.stderr.decode("utf-8", "replace").strip()
            raise WorkspaceError(f"git {args[0]} failed ({done.returncode}): {message}")
        return done

    def _out(self, cwd: Path, *args: str) -> str:
        return self._git(cwd, *args).stdout.decode("utf-8", "surrogateescape").strip()

    def _blob_at(self, cwd: Path, commit: str, path: str) -> str | None:
        done = self._git(cwd, "rev-parse", "--verify", "--quiet", f"{commit}:{path}", check=False)
        if done.returncode != 0:
            return None
        return done.stdout.decode().strip()

    def _worktree_blob(self, cwd: Path, path: str) -> str | None:
        """Hash a checkout file the way git would store it, applying clean filters."""
        target = cwd / path
        if not target.is_file() or target.is_symlink():
            return None if not target.exists() and not target.is_symlink() else "unhashable"
        return self._out(cwd, "hash-object", f"--path={path}", "--", path)

    # -- lifecycle ----------------------------------------------------------

    def _lease_path(self, task_id: str) -> Path:
        return self.workspaces_root / f"{task_id}.lease"

    def create(
        self,
        repository_root: Path,
        task_id: str,
        *,
        excluded_dirs: tuple[str, ...] = ("build", ".local-agent"),
    ) -> Workspace:
        UUID(task_id)
        repository_root = Path(repository_root).resolve()
        top = Path(self._out(repository_root, "rev-parse", "--show-toplevel")).resolve()
        if top != repository_root:
            raise WorkspaceError(f"{repository_root} is not the top of its git checkout ({top})")
        if repository_root == self.workspaces_root or self.workspaces_root.is_relative_to(
            repository_root
        ):
            raise WorkspaceError("workspaces root must live outside the user's checkout")

        lease = self._lease_path(task_id)
        try:
            fd = os.open(lease, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError as exc:
            raise WorkspaceError(f"task {task_id} already holds a workspace lease") from exc
        os.close(fd)

        try:
            head = self._out(repository_root, "rev-parse", "--verify", "HEAD^{commit}")
            snapshot = self._out(repository_root, "stash", "create")
            base = snapshot or head
            dirty = _split_z(self._git(
                repository_root, "diff", "--name-only", "-z", "--no-renames", "HEAD", "--"
            ).stdout)
            untracked = _split_z(self._git(
                repository_root, "ls-files", "--others", "--exclude-standard", "-z"
            ).stdout)
            workspace_id = str(uuid4())
            root = self.workspaces_root / workspace_id[:12]
            self._git(repository_root, "worktree", "add", "--detach", "--quiet", str(root), base)
            lease.write_text(workspace_id, encoding="utf-8")
        except BaseException:
            lease.unlink(missing_ok=True)
            raise

        return Workspace(
            workspace_id=workspace_id,
            task_id=task_id,
            root=root.resolve(),
            repository_root=repository_root,
            head_commit=head,
            base_commit=base,
            controller_commit=self.controller_commit,
            dirty_paths=dirty,
            untracked_excluded=untracked,
            excluded_dirs=tuple(excluded_dirs),
        )

    def candidate_patch(self, workspace: Workspace) -> CandidatePatch:
        """Return the exact binary diff from the base to the candidate's current files."""
        excludes = [f":(exclude){d}" for d in workspace.excluded_dirs]
        # The candidate worktree's index is LCA's own; staging there records new files.
        self._git(workspace.root, "add", "-A", "--", ".", *excludes)
        diff_args = ("--cached", "--no-renames", "--no-ext-diff", "--no-textconv")
        paths = _split_z(self._git(
            workspace.root, "diff", *diff_args, "--name-only", "-z", workspace.base_commit, "--"
        ).stdout)
        patch = self._git(
            workspace.root, "diff", *diff_args, "--binary", "--full-index",
            workspace.base_commit, "--",
        ).stdout
        post = []
        for path in paths:
            done = self._git(workspace.root, "rev-parse", "--verify", "--quiet", f":{path}", check=False)
            post.append((path, done.stdout.decode().strip() if done.returncode == 0 else None))
        return CandidatePatch(
            workspace_id=workspace.workspace_id,
            base_commit=workspace.base_commit,
            patch=patch,
            sha256=hashlib.sha256(patch).hexdigest(),
            paths=paths,
            post_blobs=tuple(post),
        )

    def import_patch(self, workspace: Workspace, candidate: CandidatePatch) -> ImportResult:
        """Apply a reviewed candidate to the user's checkout, or refuse without writing."""
        if candidate.workspace_id != workspace.workspace_id:
            raise WorkspaceError("candidate does not belong to this workspace")
        if candidate.base_commit != workspace.base_commit:
            raise WorkspaceError("candidate was computed against a different base")
        if hashlib.sha256(candidate.patch).hexdigest() != candidate.sha256:
            raise WorkspaceError("candidate patch bytes do not match their reviewed hash")
        if not candidate.paths:
            return ImportResult(False, (), (), "candidate changes nothing", False)

        user = workspace.repository_root
        conflicts: list[str] = []
        pre: list[tuple[str, str | None]] = []
        for path in candidate.paths:
            expected = self._blob_at(user, workspace.base_commit, path)
            current = self._worktree_blob(user, path)
            pre.append((path, current))
            if current != expected:
                conflicts.append(path)
        if conflicts:
            return ImportResult(
                False, candidate.paths, tuple(conflicts),
                "files changed in your checkout since the candidate's base", False, tuple(pre),
            )

        check = self._git(user, "apply", "--check", "--whitespace=nowarn", "-", stdin=candidate.patch, check=False)
        if check.returncode != 0:
            return ImportResult(
                False, candidate.paths, (),
                "git apply refused the candidate: "
                + check.stderr.decode("utf-8", "replace").strip()[:500],
                False, tuple(pre),
            )
        self._git(user, "apply", "--whitespace=nowarn", "-", stdin=candidate.patch)

        verified = all(
            self._worktree_blob(user, path) == blob for path, blob in candidate.post_blobs
        )
        return ImportResult(True, candidate.paths, (), None, verified, tuple(pre))

    def close(self, workspace: Workspace) -> None:
        """Remove the candidate worktree and release the task's lease."""
        try:
            if workspace.root.exists():
                self._git(workspace.repository_root, "worktree", "remove", "--force", str(workspace.root))
        finally:
            self._git(workspace.repository_root, "worktree", "prune", check=False)
            if workspace.root.exists():
                shutil.rmtree(workspace.root, ignore_errors=True)
            self._lease_path(workspace.task_id).unlink(missing_ok=True)


__all__ = [
    "CandidatePatch",
    "GitWorkspaceManager",
    "ImportResult",
    "Workspace",
    "WorkspaceError",
]
