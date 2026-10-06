"""Atomically reconcile the user's live Git index after an LCA private-index commit.

The caller prepares a transaction before ``git commit`` and exposes its path only to
LCA's controller-owned ``post-commit`` hook. The hook takes Git's conventional index
lock, edits a private copy, then atomically replaces the live index. User-staged entries
on candidate paths are never replaced: only entries still equal to the pre-commit
snapshot are advanced to the new commit.
"""
from __future__ import annotations

import base64
import contextlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

_GIT_TIMEOUT_S = 30
# How long the controller's own reconciliation pass waits for another Git process to
# release the live index lock before reporting the paths as not reconciled.
_LOCK_WAIT_S = 2.0
_LOCK_POLL_S = 0.05
_POST_COMMIT_HOOK = """#!/bin/sh
if test -n "$LCA_COMMIT_INDEX_TRANSACTION" \
   && test -n "$LCA_PYTHON" \
   && test -n "$LCA_COMMIT_INDEX_HOOK"
then
    "$LCA_PYTHON" "$LCA_COMMIT_INDEX_HOOK" "$LCA_COMMIT_INDEX_TRANSACTION" || true
fi
exit 0
"""


@dataclass(frozen=True, slots=True)
class IndexEntry:
    """Exact stage entries and user-controlled index flags for one literal path."""

    stage: bytes
    assume_unchanged: bool
    skip_worktree: bool


class IndexLockBusyError(OSError):
    """Another process holds the live index lock; nothing was changed."""


def index_entry_from_listings(stage: bytes, tagged: bytes, verbose: bytes) -> IndexEntry:
    """Build one entry from ``ls-files --stage``, ``-t`` and ``-v`` output for a path."""
    return IndexEntry(
        stage=stage,
        assume_unchanged=bool(verbose[:1] and verbose[:1].islower()),
        skip_worktree=tagged.startswith(b"S "),
    )


def install_post_commit_hook(hooks_dir: Path) -> None:
    """Atomically install LCA's no-user-hook post-commit reconciler."""
    hooks_dir.mkdir(parents=True, exist_ok=True)
    target = hooks_dir / "post-commit"
    fd, raw_tmp = tempfile.mkstemp(prefix=".post-commit-", dir=hooks_dir)
    tmp = Path(raw_tmp)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(_POST_COMMIT_HOOK)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(tmp, 0o700)
        os.replace(tmp, target)
    finally:
        tmp.unlink(missing_ok=True)


def prepare_transaction(
    workspaces_root: Path,
    repository_root: Path,
    live_index: Path,
    parent: str,
    entries: tuple[tuple[str, bytes, bytes], ...],
) -> Path:
    """Persist the exact clean-path index transition the post-commit hook may own."""
    resolved_index = live_index if live_index.is_absolute() else repository_root / live_index
    payload = {
        "repository_root": str(repository_root.resolve()),
        "live_index": str(resolved_index.resolve()),
        "parent": parent,
        "entries": [
            {
                "path": name,
                "expected_stage_b64": base64.b64encode(expected).decode("ascii"),
                "target_stage_b64": base64.b64encode(target).decode("ascii"),
            }
            for name, expected, target in entries
        ],
    }
    path = workspaces_root / f".commit-index-{os.getpid()}-{uuid4().hex}.json"
    tmp = path.with_suffix(".tmp")
    try:
        tmp.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)
    return path


def hook_environment(transaction: Path) -> dict[str, str]:
    """Environment consumed only by LCA's controller-owned post-commit hook."""
    return {
        "LCA_COMMIT_INDEX_TRANSACTION": str(transaction).replace("\\", "/"),
        "LCA_PYTHON": sys.executable.replace("\\", "/"),
        "LCA_COMMIT_INDEX_HOOK": str(Path(__file__).resolve()).replace("\\", "/"),
    }


def _git(
    root: Path,
    index: Path,
    *args: str,
    stdin: bytes | None = None,
    check: bool = True,
) -> subprocess.CompletedProcess[bytes]:
    env = dict(os.environ)
    env.update({
        "GIT_INDEX_FILE": str(index),
        "GIT_LITERAL_PATHSPECS": "1",
        "GIT_OPTIONAL_LOCKS": "0",
        "GIT_PAGER": "cat",
        "LC_ALL": "C",
    })
    git = shutil.which("git")
    if git is None:
        raise RuntimeError("git executable is not available")
    done = subprocess.run(  # noqa: S603 -- resolved executable; literal paths only.
        [
            git,
            "-c", f"core.hooksPath={os.devnull}",
            "-c", "core.fsmonitor=false",
            "-c", "core.quotepath=false",
            *args,
        ],
        cwd=str(root),
        env=env,
        input=stdin,
        capture_output=True,
        timeout=_GIT_TIMEOUT_S,
        check=False,
    )
    if check and done.returncode != 0:
        detail = done.stderr.decode("utf-8", "replace").strip()[:500]
        raise RuntimeError(f"git {args[0]} failed ({done.returncode}): {detail}")
    return done


def _entry(root: Path, index: Path, path: str) -> IndexEntry:
    return index_entry_from_listings(
        _git(root, index, "ls-files", "--stage", "-z", "--", path).stdout,
        _git(root, index, "ls-files", "-t", "-z", "--", path).stdout,
        _git(root, index, "ls-files", "-v", "-z", "--", path).stdout,
    )


def _restore(root: Path, index: Path, path: str, entry: IndexEntry) -> None:
    _git(
        root, index, "update-index", "--no-assume-unchanged", "--no-skip-worktree", "--", path,
        check=False,
    )
    if entry.stage:
        _git(root, index, "update-index", "-z", "--index-info", stdin=entry.stage)
    else:
        removal = (
            b"0 " + (b"0" * 40) + b"\t" + path.encode("utf-8", "surrogateescape") + b"\0"
        )
        _git(root, index, "update-index", "-z", "--index-info", stdin=removal)
    if entry.assume_unchanged:
        _git(root, index, "update-index", "--assume-unchanged", "--", path)
    if entry.skip_worktree:
        _git(root, index, "update-index", "--skip-worktree", "--", path)


def _decode_stage(value: object) -> bytes:
    if not isinstance(value, str):
        raise ValueError("index transaction stage is not text")
    return base64.b64decode(value.encode("ascii"), validate=True)


def _load(path: Path) -> tuple[Path, Path, str, tuple[tuple[str, bytes, bytes], ...]]:
    raw: object = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError("index transaction is not an object")
    root_raw = raw.get("repository_root")
    index_raw = raw.get("live_index")
    parent_raw = raw.get("parent")
    entries_raw = raw.get("entries")
    if not isinstance(root_raw, str) or not isinstance(index_raw, str):
        raise ValueError("index transaction paths are invalid")
    if not isinstance(parent_raw, str) or not isinstance(entries_raw, list):
        raise ValueError("index transaction fields are invalid")
    entries: list[tuple[str, bytes, bytes]] = []
    for item in entries_raw:
        if not isinstance(item, dict):
            raise ValueError("index transaction entry is not an object")
        name = item.get("path")
        if not isinstance(name, str):
            raise ValueError("index transaction path is invalid")
        entries.append((
            name,
            _decode_stage(item.get("expected_stage_b64")),
            _decode_stage(item.get("target_stage_b64")),
        ))
    return Path(root_raw), Path(index_raw), parent_raw, tuple(entries)


def reconcile(transaction_path: Path) -> None:
    """Apply one all-or-nothing live-index reconciliation transaction.

    Raises :class:`IndexLockBusyError` without touching anything when another process holds
    the live index lock. A lock this call did not create is never removed: deleting it
    would let two writers race on the user's index.
    """
    root, live_index, parent, entries = _load(transaction_path)
    live_index = live_index.resolve()
    live_index.parent.mkdir(parents=True, exist_ok=True)
    lock = Path(str(live_index) + ".lock")
    try:
        opened = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError as exc:
        raise IndexLockBusyError(str(lock)) from exc
    lock_fd: int | None = opened
    lock_owned = True
    work: Path | None = None
    try:
        fd, raw_work = tempfile.mkstemp(prefix="lca-index-reconcile-", dir=live_index.parent)
        os.close(fd)
        work = Path(raw_work)
        if live_index.is_file():
            shutil.copyfile(live_index, work)
        else:
            work.unlink(missing_ok=True)
            _git(root, work, "read-tree", parent)

        for name, expected_stage, target_stage in entries:
            current = _entry(root, work, name)
            if current.stage != expected_stage:
                continue
            _restore(
                root,
                work,
                name,
                IndexEntry(target_stage, current.assume_unchanged, current.skip_worktree),
            )

        data = work.read_bytes()
        with os.fdopen(opened, "wb") as stream:
            lock_fd = None
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(lock, live_index)
        lock_owned = False
    finally:
        if lock_fd is not None:
            with contextlib.suppress(OSError):
                os.close(lock_fd)
        if lock_owned:
            lock.unlink(missing_ok=True)
        if work is not None:
            work.unlink(missing_ok=True)


def reconcile_and_report(transaction_path: Path) -> tuple[str, ...]:
    """Controller-side pass: reconcile under the lock, then name unadvanced clean paths.

    The same locked compare-and-update as the hook, so the controller never edits the
    live index outside Git's lock. Idempotent after a successful hook. A path is
    reported only while its live entry still equals the pre-commit snapshot; any other
    value is the hook's result or concurrent user work and is not LCA's to report.
    """
    deadline = time.monotonic() + _LOCK_WAIT_S
    while True:
        try:
            reconcile(transaction_path)
            break
        except IndexLockBusyError:
            if time.monotonic() >= deadline:
                break
            time.sleep(_LOCK_POLL_S)
        except (OSError, RuntimeError, ValueError, subprocess.SubprocessError):
            # Nothing was replaced; the report below says which paths stayed behind.
            break
    root, live_index, _parent, entries = _load(transaction_path)
    live_index = live_index.resolve()
    left: list[str] = []
    for name, expected_stage, target_stage in entries:
        if expected_stage == target_stage:
            continue
        if not live_index.is_file() or _entry(root, live_index, name).stage == expected_stage:
            left.append(name)
    return tuple(left)


def main(argv: Sequence[str] | None = None) -> int:
    args = tuple(sys.argv[1:] if argv is None else argv)
    if len(args) != 1:
        return 0
    try:
        reconcile(Path(args[0]))
    except (OSError, RuntimeError, ValueError, subprocess.SubprocessError):
        # A post-commit hook must never turn an already-created commit into a reported
        # failure. The controller verifies/reconciles again after Git returns. If the
        # controller dies here, leaving the live index untouched is safer than guessing.
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
