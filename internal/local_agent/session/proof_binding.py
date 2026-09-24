"""Typed binding between a task request, the proof actually obtained and one tree.

The worker may describe success in prose, but product verdict authority needs a
machine-checkable answer to three separate questions: what request was being proved,
what kind of check supplied the proof, and which repository state that check applies to.
"""
from __future__ import annotations

import hashlib
import subprocess
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from ..tools.tool_primitives import resolve_in_repo
from ..verification import CURRENT_TREE_PROOFS, ProofKind


class ProofScope(str, Enum):
    NONE = "none"
    FULL_BUILD = "full_build"
    FULL_TEST = "full_test"
    TARGETED_BUILD = "targeted_build"
    TARGETED_TEST = "targeted_test"
    OBSERVED_BUILD_FAILURE = "observed_build_failure"
    OBSERVED_TEST_FAILURE = "observed_test_failure"


_SCOPE_BY_KIND = {
    ProofKind.FULL_BUILD_PASS: ProofScope.FULL_BUILD,
    ProofKind.FULL_TEST_PASS: ProofScope.FULL_TEST,
    ProofKind.TARGETED_BUILD_PASS: ProofScope.TARGETED_BUILD,
    ProofKind.TARGETED_TEST_PASS: ProofScope.TARGETED_TEST,
    ProofKind.OBSERVED_BUILD_FAIL: ProofScope.OBSERVED_BUILD_FAILURE,
    ProofKind.OBSERVED_TEST_FAIL: ProofScope.OBSERVED_TEST_FAILURE,
    ProofKind.NO_CURRENT_PROOF: ProofScope.NONE,
}


def repository_tree_sha256(root: Path) -> str:
    """Hash HEAD, tracked/index identity and all nonignored working-tree file bytes.

    This is product proof identity, not experiment source provenance. Ignored build
    outputs are deliberately outside the tree contract; tracked edits, deletions and
    nonignored untracked files are inside it.
    """
    def git(*args: str) -> bytes:
        return subprocess.run(
            ["git", *args],
            cwd=root,
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=30,
        ).stdout

    digest = hashlib.sha256(git("rev-parse", "HEAD") + git("ls-files", "--stage", "-z"))
    names = set(git("ls-files", "-z", "--cached", "--others", "--exclude-standard").split(b"\0"))
    for name in sorted(names - {b""}):
        path = resolve_in_repo(root, name.decode("utf-8"))
        digest.update(name + b"\0")
        if not path.exists():
            digest.update(b"deleted\0")
        elif path.is_file():
            digest.update(path.read_bytes())
            digest.update(b"\0")
        else:
            raise ValueError("unsupported repository entry while binding proof tree")
    return digest.hexdigest()


@dataclass(frozen=True)
class ProofBinding:
    request_sha256: str
    scope: ProofScope
    tree_sha256: str
    evidence_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        for name, value in (
            ("request_sha256", self.request_sha256),
            ("tree_sha256", self.tree_sha256),
        ):
            if (
                not isinstance(value, str)
                or len(value) != 64
                or any(ch not in "0123456789abcdef" for ch in value)
            ):
                raise ValueError(f"{name} must be a lowercase SHA-256 digest")
        object.__setattr__(self, "scope", ProofScope(self.scope))
        ids = tuple(self.evidence_ids)
        if any(not isinstance(value, str) or not value for value in ids):
            raise ValueError("proof evidence ids must be nonempty strings")
        object.__setattr__(self, "evidence_ids", ids)

    def as_dict(self) -> dict[str, object]:
        return {
            "request_sha256": self.request_sha256,
            "scope": self.scope.value,
            "tree_sha256": self.tree_sha256,
            "evidence_ids": list(self.evidence_ids),
        }


def binding_from_run(task: str, run, repo_root: Path) -> ProofBinding:
    """Project current-epoch worker proof into one immutable product fact."""
    request_sha256 = hashlib.sha256(task.encode("utf-8")).hexdigest()
    current_epoch = int(run.state.mutation_epoch)
    candidates: list[tuple[int, ProofKind]] = []
    for index, record in enumerate(run.state.history):
        if int(getattr(record, "epoch", -1)) != current_epoch:
            continue
        try:
            kind = ProofKind(getattr(record, "proof", ProofKind.NO_CURRENT_PROOF.value))
        except ValueError:
            kind = ProofKind.NO_CURRENT_PROOF
        if kind is not ProofKind.NO_CURRENT_PROOF:
            candidates.append((index, kind))

    full = [(index, kind) for index, kind in candidates if kind in CURRENT_TREE_PROOFS]
    if run.state.verified:
        if not full:
            raise RuntimeError("worker claims verified current tree without a current full proof")
        proof_index, proof_kind = full[-1]
    elif candidates:
        proof_index, proof_kind = candidates[-1]
    else:
        proof_index, proof_kind = -1, ProofKind.NO_CURRENT_PROOF

    evidence_ids = () if proof_index < 0 else (f"{run.state.history[proof_index].name}:{proof_index}",)
    return ProofBinding(
        request_sha256=request_sha256,
        scope=_SCOPE_BY_KIND[proof_kind],
        tree_sha256=repository_tree_sha256(repo_root),
        evidence_ids=evidence_ids,
    )
