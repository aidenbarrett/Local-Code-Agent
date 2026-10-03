"""Isolated candidate worktrees for LCA-owned source changes.

One writable worktree per task, under an exclusive lease, checked out detached at an
explicit commit. The user's checkout is never the place LCA edits:

- The base is the user's exact working state: ``HEAD`` plus tracked changes plus
  untracked, non-ignored files up to a size bound, snapshotted through a private
  temporary index (``GIT_INDEX_FILE``) so the user's index, working files, stash list
  and refs are never touched. Ignored files (secrets, build output) are never copied;
  oversized untracked files are left out and listed so a result can say what it did
  not see.
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

import base64
import contextlib
import hashlib
import json
import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID, uuid4

import psutil

from ..tools.tool_primitives import SandboxError, resolve_in_repo

_GIT_TIMEOUT_S = 300
# Untracked files above this size stay out of the candidate base and are reported. The
# base commit's objects are written to the user's object store; a stray dataset or
# binary must not bloat it.
MAX_UNTRACKED_BYTES = 5 * 1024 * 1024


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
    # Untracked, non-ignored files left out of the base because they are too large.
    untracked_excluded: tuple[str, ...]
    excluded_dirs: tuple[str, ...]
    # Untracked, non-ignored files carried into the base so the candidate sees the
    # same source tree the user has. Ignored files are never included.
    untracked_included: tuple[str, ...] = ()


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
    # Paths left in neither their pre-import nor post-import state after a failed
    # import and owned rollback. Nonempty means the checkout needs the user's attention.
    unresolved: tuple[str, ...] = ()
    # True when git wrote part of the candidate and this import restored what it wrote.
    rolled_back: bool = False
    # Exact bytes each touched path held in the user's checkout before import (None for
    # a path the candidate creates). Restores are byte-exact, never re-filtered by git,
    # and the same bytes are kept so an applied change can later be undone.
    pre_contents: tuple[tuple[str, bytes | None], ...] = ()
    # Refused because every touched path already holds exactly the candidate's
    # post-image: the change is in the checkout already (for example an earlier import
    # interrupted before it was recorded). Nothing was written.
    already_applied: bool = False


# The outcome of committing one applied candidate is one of four distinct states. Each is
# its own type, so a caller cannot read a commit id that does not exist or treat a refusal
# as a commit: the type checker makes it match every case (see commit_candidate).
@dataclass(frozen=True, slots=True)
class Committed:
    """A commit exists on ``branch`` and contains exactly the applied change."""

    commit: str
    branch: str
    paths: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class CommitMismatched:
    """A commit was created but does not contain exactly the applied change."""

    commit: str
    branch: str
    paths: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class CommitDrifted:
    """Nothing was committed: these applied paths changed after the import."""

    branch: str
    paths: tuple[str, ...]
    drifted: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class CommitRefused:
    """Nothing was committed, for ``reason``. ``existing_commit`` names an earlier commit
    of the same change when that is why it was refused."""

    reason: str
    paths: tuple[str, ...] = ()
    branch: str | None = None
    existing_commit: str | None = None
    # Paths this attempt staged that could not be unstaged because their index entry
    # changed meanwhile; empty means the user's index is exactly as it was.
    index_left: tuple[str, ...] = ()


CommitResult = Committed | CommitMismatched | CommitDrifted | CommitRefused


@dataclass(frozen=True)
class UndoResult:
    undone: bool
    paths: tuple[str, ...]
    drifted: tuple[str, ...]
    unresolved: tuple[str, ...]
    refused_reason: str | None


# `git add --pathspec-from-file` (used to build the candidate base) arrived in 2.25.
MIN_GIT_VERSION = (2, 25)


@dataclass(frozen=True)
class WorkspaceReadiness:
    """Whether isolated candidate changes can work for one repository on this machine."""

    ready: bool
    git_version: str | None
    problems: tuple[str, ...]


def _parse_git_version(text: str) -> tuple[int, int] | None:
    parts = text.strip().split()
    if len(parts) < 3 or parts[0] != "git" or parts[1] != "version":
        return None
    numbers = parts[2].split(".")
    try:
        return int(numbers[0]), int(numbers[1])
    except (IndexError, ValueError):
        return None


def _failed_commit_reason(
    done: subprocess.CompletedProcess[bytes], index_left: tuple[str, ...],
) -> str:
    detail = (done.stderr or done.stdout).decode("utf-8", "replace").strip()[:500]
    reason = "git commit failed: " + detail
    if index_left:
        reason += ("; these files are still staged because their staged content changed "
                   "meanwhile and was left alone: " + ", ".join(index_left))
    return reason


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
        env_extra: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[bytes]:
        env = dict(os.environ)
        env.update(env_extra or {})
        env.update({
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_OPTIONAL_LOCKS": "0",
            "GIT_PAGER": "cat",
            "LC_ALL": "C",
            # Every path the controller hands git is a file name, never a pattern.
            # Without this, `note[1].txt` after `--` also matches `note1.txt` (#393).
            "GIT_LITERAL_PATHSPECS": "1",
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

    def _out(self, cwd: Path, *args: str, env_extra: dict[str, str] | None = None) -> str:
        return self._git(cwd, *args, env_extra=env_extra).stdout.decode(
            "utf-8", "surrogateescape"
        ).strip()

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

    def _snapshot(
        self, repository_root: Path, head: str, excluded_dirs: tuple[str, ...],
    ) -> tuple[str, tuple[str, ...], tuple[str, ...]]:
        """Commit the user's current source tree without touching their index or files.

        Built in a private temporary index: HEAD, plus every tracked change, plus
        untracked files that are not ignored and not under an instrument directory,
        each no larger than MAX_UNTRACKED_BYTES. Returns (commit, included, oversized).
        The commit is HEAD itself when nothing differs.
        """
        untracked = [
            p for p in _split_z(self._git(
                repository_root, "ls-files", "--others", "--exclude-standard", "-z"
            ).stdout)
            if not any(p == d or p.startswith(d.rstrip("/") + "/") for d in excluded_dirs)
        ]
        included: list[str] = []
        oversized: list[str] = []
        for rel in untracked:
            target = repository_root / rel
            try:
                small = target.is_symlink() or target.stat().st_size <= MAX_UNTRACKED_BYTES
            except OSError:
                continue
            (included if small else oversized).append(rel)

        fd, index_path = tempfile.mkstemp(prefix="lca-index-", dir=self.workspaces_root)
        os.close(fd)
        os.unlink(index_path)  # git must create it; an empty file is not a valid index
        env = {"GIT_INDEX_FILE": index_path}
        try:
            self._git(repository_root, "read-tree", head, env_extra=env)
            self._git(repository_root, "add", "--update", "--", ".", env_extra=env)
            if included:
                self._git(
                    repository_root, "add", "--pathspec-from-file=-", "--pathspec-file-nul",
                    stdin=b"\0".join(p.encode("utf-8", "surrogateescape") for p in included),
                    env_extra=env,
                )
            tree = self._out(repository_root, "write-tree", env_extra=env)
        finally:
            Path(index_path).unlink(missing_ok=True)
        if tree == self._out(repository_root, "rev-parse", f"{head}^{{tree}}"):
            return head, tuple(included), tuple(oversized)
        commit = self._git(
            repository_root, "commit-tree", tree, "-p", head,
            stdin=b"Local Code Agent candidate base\n",
            env_extra={
                "GIT_AUTHOR_NAME": "Local Code Agent", "GIT_AUTHOR_EMAIL": "lca@localhost",
                "GIT_COMMITTER_NAME": "Local Code Agent", "GIT_COMMITTER_EMAIL": "lca@localhost",
            },
        ).stdout.decode().strip()
        return commit, tuple(included), tuple(oversized)

    def readiness(self, repository_root: Path) -> WorkspaceReadiness:
        """Cheap, side-effect-free checks that candidate changes can work here.

        Checks: git runs and is new enough, the path is the top of a git checkout
        with a commit, and the workspaces root is writable and outside it. Nothing
        in the repository is written.
        """
        problems: list[str] = []
        version_text: str | None = None
        root_dir = Path(repository_root)
        done = self._git(root_dir, "version", check=False) if root_dir.is_dir() else None
        if done is None or done.returncode != 0:
            problems.append("git is not runnable here")
        else:
            version_text = done.stdout.decode("utf-8", "replace").strip()
            parsed = _parse_git_version(version_text)
            if parsed is None:
                problems.append(f"cannot read the git version from {version_text!r}")
            elif parsed < MIN_GIT_VERSION:
                problems.append(
                    f"{version_text} is too old; candidate changes need git "
                    f"{MIN_GIT_VERSION[0]}.{MIN_GIT_VERSION[1]} or newer"
                )
        root = Path(repository_root).resolve()
        top = (
            self._git(root, "rev-parse", "--show-toplevel", check=False) if root.is_dir() else None
        )
        if top is None or top.returncode != 0:
            problems.append(f"{root} is not a git checkout")
        elif Path(top.stdout.decode().strip()).resolve() != root:
            problems.append(f"{root} is not the top of its git checkout")
        elif self._git(
            root, "rev-parse", "--verify", "--quiet", "HEAD^{commit}", check=False,
        ).returncode != 0:
            problems.append("the repository has no commit yet")
        if self.workspaces_root == root or self.workspaces_root.is_relative_to(root):
            problems.append("the candidate workspaces folder is inside the repository")
        probe = self.workspaces_root / f".write-probe-{os.getpid()}"
        try:
            probe.write_bytes(b"")
            probe.unlink()
        except OSError as exc:
            problems.append(
                f"the candidate workspaces folder is not writable: {exc.strerror or exc}"
            )
        return WorkspaceReadiness(not problems, version_text, tuple(problems))

    # -- lifecycle ----------------------------------------------------------

    def _lease_path(self, task_id: str) -> Path:
        return self.workspaces_root / f"{task_id}.lease"

    @staticmethod
    def _owner_token(pid: int) -> str | None:
        """PID plus process start time: a reused PID is a different owner."""
        try:
            return f"{pid}:{psutil.Process(pid).create_time():.6f}"
        except (psutil.NoSuchProcess, psutil.ZombieProcess, psutil.AccessDenied):
            return None

    def reap_orphans(self) -> tuple[str, ...]:
        """Remove worktrees whose owning controller process is gone and left nothing to apply.

        A lease is reaped only when its recorded owner process is provably not the same
        live process (by PID and start time) and no retained candidate exists for its
        task. Leases held by a live process, including another Session Hub, are never
        touched. Unreadable leases are left for a human rather than guessed at.
        """
        reaped: list[str] = []
        for lease in sorted(self.workspaces_root.glob("*.lease")):
            task_id = lease.name[: -len(".lease")]
            try:
                UUID(task_id)
                record = json.loads(lease.read_text(encoding="utf-8"))
                owner = record["owner"]
                root = Path(record["root"])
                repository_root = Path(record["repository_root"])
            except (ValueError, KeyError, TypeError, OSError):
                continue
            if self._record_path(task_id).exists():
                continue
            if isinstance(owner, str) and owner == self._owner_token(int(owner.split(":", 1)[0])):
                continue
            if repository_root.is_dir():
                self._git(repository_root, "worktree", "remove", "--force", str(root), check=False)
                self._git(repository_root, "worktree", "prune", check=False)
            if root.exists():
                shutil.rmtree(root, ignore_errors=True)
            if not root.exists():
                lease.unlink(missing_ok=True)
                reaped.append(task_id)
        return tuple(reaped)

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
        workspace_id = str(uuid4())
        root = self.workspaces_root / workspace_id[:12]
        try:
            # The lease describes its owner and target before anything exists on disk,
            # so a crash at any later point leaves a lease that reap_orphans can act on.
            os.write(fd, json.dumps({
                "workspace_id": workspace_id,
                "root": str(root),
                "repository_root": str(repository_root),
                "owner": self._owner_token(os.getpid()),
            }).encode("utf-8"))
        finally:
            os.close(fd)

        try:
            head = self._out(repository_root, "rev-parse", "--verify", "HEAD^{commit}")
            base, included, oversized = self._snapshot(
                repository_root, head, tuple(excluded_dirs),
            )
            dirty = _split_z(self._git(
                repository_root, "diff", "--name-only", "-z", "--no-renames", head, base, "--"
            ).stdout)
            self._git(repository_root, "worktree", "add", "--detach", "--quiet", str(root), base)
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
            untracked_excluded=oversized,
            excluded_dirs=tuple(excluded_dirs),
            untracked_included=included,
        )

    def candidate_patch(self, workspace: Workspace) -> CandidatePatch:
        """Return the exact binary diff from the base to the candidate's current files."""
        # The candidate worktree's index is LCA's own; staging there records new files.
        # Instrument directories are then reset to the base in that index, which drops
        # build/run output whether or not the user's .gitignore covers it, and keeps any
        # base-tracked files under them unchanged. (Exclude pathspecs cannot be used: git
        # refuses them outright when they name ignored directories.)
        self._git(workspace.root, "add", "-A", "--", ".")
        if workspace.excluded_dirs:
            self._git(
                workspace.root, "reset", "-q", workspace.base_commit, "--",
                *workspace.excluded_dirs,
            )
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
            done = self._git(
                workspace.root, "rev-parse", "--verify", "--quiet", f":{path}", check=False,
            )
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
        for path in candidate.paths:
            try:
                resolve_in_repo(user, path)
            except SandboxError as exc:
                raise WorkspaceError(f"candidate path {path!r} is outside the repository") from exc
        unsupported = self._unsupported_entry_changes(user, candidate.patch)
        if unsupported:
            # Undo restores bytes and proves blobs; it does not carry modes or file types.
            # A candidate that changes either is refused before anything is written (#396).
            return ImportResult(
                False, candidate.paths, (),
                "the candidate changes file permissions or file types, which apply and undo "
                "do not support: " + ", ".join(unsupported) + ". Nothing was written.",
                False,
            )
        conflicts: list[str] = []
        pre: list[tuple[str, str | None]] = []
        for path in candidate.paths:
            expected = self._blob_at(user, workspace.base_commit, path)
            current = self._worktree_blob(user, path)
            pre.append((path, current))
            if current != expected:
                conflicts.append(path)
        if conflicts:
            already = dict(pre) == dict(candidate.post_blobs)
            return ImportResult(
                False, candidate.paths, tuple(conflicts),
                "your checkout already holds exactly this change" if already
                else "files changed in your checkout since the candidate's base",
                False, tuple(pre), already_applied=already,
            )

        check = self._git(
            user, "apply", "--check", "--whitespace=nowarn", "-",
            stdin=candidate.patch, check=False,
        )
        if check.returncode != 0:
            return ImportResult(
                False, candidate.paths, (),
                "git apply refused the candidate: "
                + check.stderr.decode("utf-8", "replace").strip()[:500],
                False, tuple(pre),
            )
        pre_contents = tuple(
            (path, (user / path).read_bytes() if before is not None else None)
            for path, before in pre
        )
        applied = self._git(
            user, "apply", "--whitespace=nowarn", "-", stdin=candidate.patch, check=False,
        )
        if applied.returncode != 0:
            # git apply can stop part-way through writing. Put back exactly what this
            # import wrote, and nothing else: a path is restored only if it now holds
            # the candidate's post-image. Anything else is reported, never overwritten.
            unresolved = self._rollback_owned(user, candidate, pre_contents)
            reason = "git apply failed while writing: " + applied.stderr.decode(
                "utf-8", "replace"
            ).strip()[:500]
            return ImportResult(
                False, candidate.paths, (), reason, False, tuple(pre), unresolved,
                rolled_back=not unresolved, pre_contents=pre_contents,
            )

        verified = all(
            self._worktree_blob(user, path) == blob for path, blob in candidate.post_blobs
        )
        return ImportResult(
            True, candidate.paths, (), None, verified, tuple(pre), pre_contents=pre_contents,
        )

    def _rollback_owned(
        self,
        user: Path,
        candidate: CandidatePatch,
        pre_contents: tuple[tuple[str, bytes | None], ...],
    ) -> tuple[str, ...]:
        """Restore paths this import wrote to their exact pre-import bytes; return the rest.

        Restoration is byte-exact from the snapshot taken just before writing. Content
        re-filtered through git could differ in line endings from what the user had
        (for example an LF checkout under core.autocrlf=true).
        """
        post = dict(candidate.post_blobs)
        unresolved: list[str] = []
        for path, before in pre_contents:
            target = user / path
            try:
                current_bytes = target.read_bytes() if target.is_file() else None
            except OSError:
                unresolved.append(path)
                continue
            if current_bytes == before:
                continue
            if self._worktree_blob(user, path) != post.get(path):
                unresolved.append(path)
                continue
            try:
                if before is None:
                    target.unlink()
                else:
                    target.write_bytes(before)
                restored = target.read_bytes() if target.is_file() else None
            except OSError:
                unresolved.append(path)
                continue
            if restored != before:
                unresolved.append(path)
        return tuple(unresolved)

    def checkout_matches_candidate(self, workspace: Workspace) -> bool | None:
        """Whether the user's source tree now equals the candidate tree exactly.

        Compared on the same snapshot as the base: tracked files plus untracked,
        non-ignored files outside instrument directories. True means a proof taken on
        the candidate tree is a proof about the user's source. Nothing in the user's
        checkout is modified to answer this. None means the comparison could not be made (for
        example the candidate worktree is gone), which never counts as a match.
        """
        if not workspace.root.is_dir():
            return None
        user = workspace.repository_root
        snapshot, _included, oversized = self._snapshot(
            user, self._out(user, "rev-parse", "--verify", "HEAD^{commit}"),
            workspace.excluded_dirs,
        )
        if oversized:
            # Files the candidate never saw are part of the user's tree.
            return False
        user_tree = self._out(user, "rev-parse", f"{snapshot}^{{tree}}")
        done = self._git(workspace.root, "write-tree", check=False)
        if done.returncode != 0:
            return None
        return user_tree == done.stdout.decode().strip()

    # -- applied changes and owned undo --------------------------------------

    def _applied_path(self, task_id: str) -> Path:
        UUID(task_id)
        return self.workspaces_root / f"{task_id}.applied.json"

    def record_applied(
        self, task_id: str, repository_root: Path, candidate: CandidatePatch, result: ImportResult,
    ) -> Path:
        """Keep what a verified import replaced, so exactly that change can be undone."""
        if not (result.applied and result.verified):
            raise WorkspaceError("only a verified import can be recorded for undo")
        record = {
            "task_id": task_id,
            "repository_root": str(Path(repository_root).resolve()),
            "patch_sha256": candidate.sha256,
            "paths": list(candidate.paths),
            "post_blobs": [[p, b] for p, b in candidate.post_blobs],
            "pre_blobs": [[p, b] for p, b in result.pre_blobs],
            "pre_contents": [
                [p, None if data is None else base64.b64encode(data).decode("ascii")]
                for p, data in result.pre_contents
            ],
        }
        path = self._applied_path(task_id)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(record, sort_keys=True), encoding="utf-8")
        os.replace(tmp, path)
        return path

    def commit_applied(self, task_id: str, repository_root: Path, message: str) -> CommitResult:
        """Commit exactly one applied candidate's files on the current branch, or refuse.

        Preconditions, all checked before anything is written: a verified apply record
        for this repository that is not already committed; every touched path still
        holds exactly what the import wrote; HEAD is on a branch; no merge, rebase,
        cherry-pick or revert is in progress. Only the touched paths are committed
        (`git commit --only`), so anything else the user has staged stays staged and
        uncommitted. User hooks do not run. Nothing is ever pushed.
        """
        path = self._applied_path(task_id)
        if not path.is_file():
            return CommitRefused(f"no applied change recorded for task {task_id}")
        record = json.loads(path.read_text(encoding="utf-8"))
        user = Path(repository_root).resolve()
        paths = tuple(record["paths"])
        if Path(record["repository_root"]) != user:
            return CommitRefused("that change was applied to another repository", paths)
        if record.get("committed"):
            existing = str(record["committed"])
            return CommitRefused(f"that change is already committed as {existing[:12]}", paths,
                                 existing_commit=existing)
        branch = self._git(user, "symbolic-ref", "--quiet", "--short", "HEAD", check=False)
        if branch.returncode != 0:
            return CommitRefused("HEAD is detached; check out a branch first", paths)
        branch_name = branch.stdout.decode().strip()
        in_progress = (
            "MERGE_HEAD", "CHERRY_PICK_HEAD", "REVERT_HEAD", "rebase-merge", "rebase-apply",
        )
        for marker in in_progress:
            marker_path = Path(self._out(user, "rev-parse", "--git-path", marker))
            if not marker_path.is_absolute():
                marker_path = user / marker_path
            if marker_path.exists():
                return CommitRefused("a merge, rebase, cherry-pick or revert is in progress",
                                     paths, branch_name)
        post = {p: b for p, b in record["post_blobs"]}
        drifted = tuple(p for p in paths if self._worktree_blob(user, p) != post.get(p))
        if drifted:
            return CommitDrifted(branch_name, paths, drifted)
        parent = self._out(user, "rev-parse", "--verify", "HEAD^{commit}")
        if all(self._blob_at(user, parent, p) == post.get(p) for p in paths):
            return CommitRefused(
                "the applied change is already what HEAD contains; nothing to commit",
                paths, branch_name,
            )
        # Paths the candidate created are untracked, and `commit --only` accepts only
        # paths git knows. Stage exactly those; every other index entry is left alone.
        created = [p for p, b in record["pre_blobs"] if b is None and (user / p).exists()]
        if created:
            self._git(user, "add", "--", *created)
        done = self._git(
            user, "commit", "--quiet", "--no-verify", "--only", "-F", "-", "--", *paths,
            stdin=message.encode("utf-8"), check=False,
        )
        if done.returncode != 0:
            # Staging the created paths was this attempt's own effect on the user's
            # index; a refusal leaves the index as it found it (#395).
            left = self._unstage_owned(user, created, post)
            return CommitRefused(_failed_commit_reason(done, left), paths, branch_name,
                                 index_left=left)
        commit = self._out(user, "rev-parse", "--verify", "HEAD^{commit}")
        if not self._commit_is_exactly(user, commit, parent, paths, post):
            return CommitMismatched(commit, branch_name, paths)
        record["committed"] = commit
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(record, sort_keys=True), encoding="utf-8")
        os.replace(tmp, path)
        return Committed(commit, branch_name, paths)

    def _commit_is_exactly(
        self, user: Path, commit: str, parent: str, paths: tuple[str, ...],
        post: dict[str, str | None],
    ) -> bool:
        """The commit has one parent, changes exactly ``paths`` and holds their blobs.

        The complete delta, not only the named blobs, must be the reviewed scope: an
        extra path means user work went into history under the controller's name."""
        if self._out(user, "rev-parse", f"{commit}^@").split() != [parent]:
            return False
        changed = set(_split_z(self._git(
            user, "diff-tree", "--no-commit-id", "--name-only", "-r", "-z", "--no-renames",
            parent, commit,
        ).stdout))
        return changed == set(paths) and all(
            self._blob_at(user, commit, p) == post.get(p) for p in paths
        )

    def _unsupported_entry_changes(self, user: Path, patch: bytes) -> tuple[str, ...]:
        """Entries in the reviewed patch that are not plain file content (#396).

        Read from the patch that will actually be applied (``git apply --summary``
        writes nothing). Supported: a regular file (100644 or 100755) created, deleted
        or edited with its mode unchanged. Refused: any mode change, and creating or
        deleting a symlink or submodule (which is also how a type change appears).
        """
        summary = self._git(user, "apply", "--summary", "-", stdin=patch).stdout
        regular = ("100644", "100755")
        refused: list[str] = []
        for line in summary.decode("utf-8", "surrogateescape").splitlines():
            words = line.split(" ", 3)
            if line.startswith(" mode change "):
                refused.append(line.strip())
            elif line.startswith((" create mode ", " delete mode ")) and len(words) == 4:
                mode_and_path = words[3].split(" ", 1)
                if mode_and_path[0] not in regular:
                    refused.append(line.strip())
        return tuple(refused)

    def _unstage_owned(
        self, user: Path, created: list[str], staged_blobs: dict[str, str | None],
    ) -> tuple[str, ...]:
        """Remove index entries this attempt added, only where they still hold what it
        staged. Never a blind reset: anything else in the index is the user's."""
        left: list[str] = []
        for path in created:
            entry = self._git(user, "ls-files", "--stage", "-z", "--", path).stdout
            fields = entry.split(b"\t", 1)[0].split() if entry else []
            if len(fields) == 3 and fields[1].decode() == staged_blobs.get(path):
                self._git(user, "rm", "--cached", "--quiet", "--", path)
            elif entry:
                left.append(path)
        return tuple(left)

    def undo_applied(self, task_id: str, repository_root: Path) -> UndoResult:
        """Undo one applied candidate, only where files still hold exactly what it wrote.

        Every touched path must still hash to the candidate's post-image, or the whole
        undo is refused and nothing is written: later work on those files is the user's.
        Paths are restored to their exact pre-import bytes. The record is single-use.
        """
        path = self._applied_path(task_id)
        if not path.is_file():
            return UndoResult(False, (), (), (), f"no applied change recorded for task {task_id}")
        record = json.loads(path.read_text(encoding="utf-8"))
        user = Path(repository_root).resolve()
        if Path(record["repository_root"]) != user:
            return UndoResult(False, (), (), (), "that change was applied to another repository")
        if record.get("committed"):
            return UndoResult(
                False, tuple(record["paths"]), (), (),
                f"that change was committed as {record['committed'][:12]}; undoing files "
                "would leave the commit in place, so revert the commit with git instead",
            )
        paths = tuple(record["paths"])
        post = {p: b for p, b in record["post_blobs"]}
        pre_blob = {p: b for p, b in record["pre_blobs"]}
        pre_bytes = {
            p: None if data is None else base64.b64decode(data, validate=True)
            for p, data in record["pre_contents"]
        }
        drifted = tuple(p for p in paths if self._worktree_blob(user, p) != post.get(p))
        if drifted:
            return UndoResult(
                False, paths, drifted, (),
                "files changed since the change was applied; undo would overwrite that work",
            )
        unresolved: list[str] = []
        for rel in paths:
            target = user / rel
            try:
                if pre_bytes.get(rel) is None:
                    target.unlink()
                else:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(pre_bytes[rel])
            except OSError:
                unresolved.append(rel)
                continue
            if self._worktree_blob(user, rel) != pre_blob.get(rel):
                unresolved.append(rel)
        if not unresolved:
            path.unlink(missing_ok=True)
        return UndoResult(not unresolved, paths, (), tuple(unresolved), None)

    # -- retention -----------------------------------------------------------

    def _record_path(self, task_id: str) -> Path:
        UUID(task_id)
        return self.workspaces_root / f"{task_id}.candidate.json"

    def prune_instrument_dirs(self, workspace: Workspace) -> int:
        """Delete build/run output inside a retained candidate; return bytes freed.

        After the proof is recorded, nothing reads it: /apply, /diff and the proof
        comparison use the retained record and the candidate's index. Only real
        directories inside the candidate worktree are removed; symlinks are left alone.
        """
        root = workspace.root.resolve()
        freed = 0
        for name in workspace.excluded_dirs:
            target = workspace.root / name
            if target.is_symlink() or not target.is_dir():
                continue
            if not target.resolve().is_relative_to(root):
                continue
            for dirpath, _dirs, files in os.walk(target):
                for f in files:
                    # A file that vanished or cannot be stat'ed is simply not counted.
                    with contextlib.suppress(OSError):
                        freed += (Path(dirpath) / f).lstat().st_size
            shutil.rmtree(target, ignore_errors=True)
        return freed

    def retain(self, workspace: Workspace, candidate: CandidatePatch) -> Path:
        """Persist a reviewed candidate so a later, separate import can find it."""
        if candidate.workspace_id != workspace.workspace_id:
            raise WorkspaceError("candidate does not belong to this workspace")
        record = {
            "workspace": {
                "workspace_id": workspace.workspace_id,
                "task_id": workspace.task_id,
                "root": str(workspace.root),
                "repository_root": str(workspace.repository_root),
                "head_commit": workspace.head_commit,
                "base_commit": workspace.base_commit,
                "controller_commit": workspace.controller_commit,
                "dirty_paths": list(workspace.dirty_paths),
                "untracked_excluded": list(workspace.untracked_excluded),
                "untracked_included": list(workspace.untracked_included),
                "excluded_dirs": list(workspace.excluded_dirs),
            },
            "candidate": {
                "workspace_id": candidate.workspace_id,
                "base_commit": candidate.base_commit,
                "patch_b64": base64.b64encode(candidate.patch).decode("ascii"),
                "sha256": candidate.sha256,
                "paths": list(candidate.paths),
                "post_blobs": [[path, blob] for path, blob in candidate.post_blobs],
            },
        }
        path = self._record_path(workspace.task_id)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(record, sort_keys=True), encoding="utf-8")
        os.replace(tmp, path)
        return path

    def load(self, task_id: str) -> tuple[Workspace, CandidatePatch]:
        """Load a retained candidate, refusing any record whose patch bytes were altered."""
        path = self._record_path(task_id)
        if not path.is_file():
            raise WorkspaceError(f"no retained candidate for task {task_id}")
        raw = json.loads(path.read_text(encoding="utf-8"))
        w, c = raw["workspace"], raw["candidate"]
        workspace = Workspace(
            workspace_id=w["workspace_id"],
            task_id=w["task_id"],
            root=Path(w["root"]),
            repository_root=Path(w["repository_root"]),
            head_commit=w["head_commit"],
            base_commit=w["base_commit"],
            controller_commit=w["controller_commit"],
            dirty_paths=tuple(w["dirty_paths"]),
            untracked_excluded=tuple(w["untracked_excluded"]),
            untracked_included=tuple(w.get("untracked_included", ())),
            excluded_dirs=tuple(w["excluded_dirs"]),
        )
        if workspace.task_id != task_id:
            raise WorkspaceError("retained candidate record names a different task")
        patch = base64.b64decode(c["patch_b64"], validate=True)
        if hashlib.sha256(patch).hexdigest() != c["sha256"]:
            raise WorkspaceError("retained candidate patch does not match its recorded hash")
        candidate = CandidatePatch(
            workspace_id=c["workspace_id"],
            base_commit=c["base_commit"],
            patch=patch,
            sha256=c["sha256"],
            paths=tuple(c["paths"]),
            post_blobs=tuple((p, b) for p, b in c["post_blobs"]),
        )
        return workspace, candidate

    def discard(self, workspace: Workspace) -> None:
        """Close the workspace and forget its retained candidate."""
        try:
            self.close(workspace)
        finally:
            self._record_path(workspace.task_id).unlink(missing_ok=True)

    def close(self, workspace: Workspace) -> None:
        """Remove the candidate worktree and release the task's lease."""
        try:
            if workspace.root.exists():
                self._git(
                    workspace.repository_root, "worktree", "remove", "--force", str(workspace.root),
                )
        finally:
            self._git(workspace.repository_root, "worktree", "prune", check=False)
            if workspace.root.exists():
                shutil.rmtree(workspace.root, ignore_errors=True)
            self._lease_path(workspace.task_id).unlink(missing_ok=True)


__all__ = [
    "MIN_GIT_VERSION",
    "WorkspaceReadiness",
    "CommitDrifted",
    "CommitMismatched",
    "CommitRefused",
    "CommitResult",
    "Committed",
    "UndoResult",
    "CandidatePatch",
    "GitWorkspaceManager",
    "ImportResult",
    "Workspace",
    "WorkspaceError",
]
