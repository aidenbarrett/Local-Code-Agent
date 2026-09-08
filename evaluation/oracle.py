"""The evaluation oracle: expected truth the agent cannot reach.

The verification oracle for a fix task is "the tests pass". The shortest path
to that is to delete the failing assertion, and no model has to be malicious to
find it; it is the locally optimal edit. So the eval owns an immutable copy of
the oracle material, taken after the scenario is applied and kept outside the
writable worktree, and verification is done by the eval against a worktree in
which that material has been restored. The agent may edit test files all it
likes. The edit never reaches the verdict, and the attempt is recorded.

This is evaluation infrastructure only. Nothing here touches the agent, the
policy engine or production use, where there is no oracle and a test is
sometimes exactly the thing that needs fixing.
"""

from __future__ import annotations

import difflib
import os
import shutil
import stat
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# What the eval treats as expected truth. Test sources, and the build file that
# declares which tests exist: dropping an add_test() is as effective a cheat as
# deleting an assertion.
ORACLE_PATHS: tuple[str, ...] = ("tests", "CMakeLists.txt")

_DIFF_CAP = 4_000  # characters of diff preserved per tampered file


@dataclass
class TamperReport:
    tampered: bool
    modified: list[str] = field(default_factory=list)
    deleted: list[str] = field(default_factory=list)
    added: list[str] = field(default_factory=list)
    diffs: dict[str, str] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "tampered": self.tampered,
            "modified": self.modified,
            "deleted": self.deleted,
            "added": self.added,
            "diffs": self.diffs,
        }


def _files_under(base: Path, rel: str) -> dict[str, Path]:
    """Relative path -> absolute path for one oracle entry (file or tree)."""
    target = base / rel
    if target.is_file():
        return {rel: target}
    if target.is_dir():
        return {
            str(p.relative_to(base)).replace(os.sep, "/"): p
            for p in sorted(target.rglob("*"))
            if p.is_file()
        }
    return {}


def _read_only(path: Path) -> None:
    for p in [path, *path.rglob("*")]:
        try:
            p.chmod(p.stat().st_mode & ~(stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH))
        except OSError:  # pragma: no cover - best effort; the copy is already outside root
            pass


def _make_writable(path: Path) -> None:
    """Undo _read_only, on the root too. Deleting an entry needs write on its
    parent directory, so a read-only root makes its own children undeletable.
    The first version of this walked rglob("*") only, which skips the root; it
    passed its test because the test ran as root, and root ignores mode bits.
    """
    for p in [path, *path.rglob("*")]:
        try:
            p.chmod(p.stat().st_mode | stat.S_IWUSR)
        except OSError:  # pragma: no cover
            pass


def discard(dest: Path) -> None:
    """Remove a previous snapshot, whatever mode bits it was left with."""
    if not dest.exists():
        return
    _make_writable(dest)
    shutil.rmtree(dest)


def snapshot(root: Path, dest: Path) -> Path:
    """Copy the oracle material out of the worktree and make it read-only.

    Taken AFTER the scenario has been applied, because some scenarios (the
    timeout one) deliberately ship a broken test: in that scenario the broken
    test is the oracle, and the eval question is whether the agent diagnoses it.
    """
    discard(dest)
    dest.mkdir(parents=True)
    for rel in ORACLE_PATHS:
        src = root / rel
        if src.is_dir():
            shutil.copytree(src, dest / rel)
        elif src.is_file():
            (dest / rel).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dest / rel)
    _read_only(dest)
    return dest


def compare(root: Path, oracle: Path) -> TamperReport:
    """Did anything under the oracle paths change in the worktree?"""
    modified: list[str] = []
    deleted: list[str] = []
    added: list[str] = []
    diffs: dict[str, str] = {}

    for rel in ORACLE_PATHS:
        expected = _files_under(oracle, rel)
        actual = _files_under(root, rel)
        for name, exp_path in expected.items():
            act_path = actual.get(name)
            if act_path is None:
                deleted.append(name)
                continue
            exp_bytes = exp_path.read_bytes()
            act_bytes = act_path.read_bytes()
            if exp_bytes != act_bytes:
                modified.append(name)
                diff = "".join(
                    difflib.unified_diff(
                        exp_bytes.decode("utf-8", "replace").splitlines(keepends=True),
                        act_bytes.decode("utf-8", "replace").splitlines(keepends=True),
                        fromfile=f"oracle/{name}",
                        tofile=f"worktree/{name}",
                    )
                )
                diffs[name] = diff[:_DIFF_CAP] + ("\n[truncated]" if len(diff) > _DIFF_CAP else "")
        for name in actual:
            if name not in expected:
                added.append(name)

    return TamperReport(
        tampered=bool(modified or deleted or added),
        modified=modified, deleted=deleted, added=added, diffs=diffs,
    )


def restore(root: Path, oracle: Path) -> None:
    """Put the oracle material back exactly, removing anything the agent added."""
    for rel in ORACLE_PATHS:
        expected = _files_under(oracle, rel)
        actual = _files_under(root, rel)
        for name in actual:
            if name not in expected:
                (root / name).unlink()
        for name, exp_path in expected.items():
            target = root / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(exp_path, target)
            try:
                target.chmod(target.stat().st_mode | stat.S_IWUSR)
            except OSError:  # pragma: no cover
                pass


def verify(registry: Any) -> dict[str, Any]:
    """The eval's own verdict: configure, build, test, through the same tools.

    Reusing the agent's tools means the same log parsers and the same sandbox,
    so a disagreement between this and the agent's `verified` flag is about the
    worktree, not about two different definitions of passing.
    """
    out: dict[str, Any] = {"configure": None, "build": None, "tests": None, "ok": False}
    try:
        configure = registry.get("configure_project").handler()
        out["configure"] = configure.domain_status.value
        if not configure.ran or not configure.ok:
            out["detail"] = configure.summary
            return out
        build = registry.get("build_target").handler()
        out["build"] = build.domain_status.value
        if not build.ran or not build.ok:
            out["detail"] = build.summary
            return out
        tests = registry.get("run_test").handler()
        out["tests"] = tests.domain_status.value
        out["detail"] = tests.summary
        out["ok"] = bool(tests.ran and tests.ok)
    except Exception as exc:  # the eval must never crash on its own verdict
        out["detail"] = f"{type(exc).__name__}: {exc}"
    return out
