"""What code produced a number.

Two package markers passing is not proof of provenance: markers say a feature
is present, not that the tree is the tree we think it is. A run that cannot
name its own source cannot be compared with another run six weeks later.

`package_commit` comes from PACKAGE.json, written when the package is built,
or from git when running out of a working tree. `source_sha256` is computed
from the files themselves at run time and needs neither, so a zip with no
git metadata and a hand-edited file still reports honestly.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path
from typing import Any

# local_agent/provenance.py -> local_agent/ -> repository root. The package
# sits at the top level rather than under a src/ wrapper, so this is two
# parents, not three.
_ROOT = Path(__file__).resolve().parent.parent

# Everything that can change what the agent does. Not the tests, not the docs:
# a change to those does not change a measured number, and including them would
# make the hash churn for reasons nobody cares about.
_HASHED = (
    ("local_agent", "*.py"),
    # Every file under a skill, not just its SKILL.md.
    #
    # A skill may carry `references/` that the agent loads on demand through
    # `Skill.reference()`, and the list of available reference names is injected
    # into the skill message the model sees. So editing
    # `skills/build-and-test/references/failure-taxonomy.md` changes what the
    # model can be told, and adding or removing a reference changes what the
    # model IS told, while a SKILL.md-only hash records neither.
    #
    # This gap predates the move out of `.github/`: the old entry had the same
    # shape. It is fixed here, in a new generation, and no dataset collected
    # under the previous hash is reinterpreted because of it.
    ("skills", "*"),
    ("evaluation", "*.py"),
    ("benchmark_fixture/cpp_project", "*"),
    # The qualification gate decides whether a run is allowed to start, and the
    # runbook script decides what runs at all. A change to either changes the
    # experiment. Leaving measurement out meant an edit to qualify.py produced a
    # package with an unchanged hash, which is the one failure mode this hash
    # exists to prevent.
    ("measurement", "*.py"),
    ("measurement", "*.sh"),
)

# Files at the repository root that decide what gets installed. pyproject.toml
# names the dependencies and the entry points, and run_experiment.sh runs
# `pip install -e .[dev]`, so editing it changes what executes while leaving
# every hashed directory untouched. It did not move the hash. It does now.
_HASHED_FILES = ("pyproject.toml",)


_SKIP = {"__pycache__", "build", ".local-agent", ".git", ".venv"}


def _files() -> list[Path]:
    out: list[Path] = []
    for folder, pattern in _HASHED:
        base = _ROOT / folder
        if not base.exists():
            continue
        for path in base.rglob(pattern):
            if not path.is_file():
                continue
            parts = path.relative_to(_ROOT).parts
            # Generated state is not source. A doctor run leaves .local-agent
            # inside the fixture, and if that counted, the hash would change
            # for reasons that cannot change a result.
            if _SKIP.intersection(parts):
                continue
            out.append(path)
    for name in _HASHED_FILES:
        path = _ROOT / name
        if path.is_file():
            out.append(path)
    # Ordered by the same canonical string the digest records, not by Path
    # comparison. `PurePath.__lt__` compares the host flavour's normcase form,
    # which on Windows is `str(path).lower()`: case-insensitive, and with
    # backslash separators. So `cpp_project/README.md` sorts before
    # `cpp_project/include/...` here and after it there, and the identical tree
    # hashed two different ways. Sorting on the key that is actually hashed
    # makes the order a property of the repository rather than of the host.
    return sorted(out, key=_key)


def _key(path: Path) -> str:
    """The canonical repository path: what is hashed, and what orders it."""
    return path.relative_to(_ROOT).as_posix()


def source_sha256() -> str:
    """A hash over the files that decide behaviour, path and content both.

    Paths are canonical POSIX-style repository paths on every host, and so is
    the order they are visited in. Both mattered: using `str(Path)` as the key
    made an identical tree hash differently on Windows because the separators
    changed, and sorting `Path` objects made it hash differently again because
    `PurePath` ordering is case-insensitive there.
    """
    digest = hashlib.sha256()
    for path in _files():
        digest.update(_key(path).encode())
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def _from_git() -> dict[str, Any] | None:
    try:
        rev = subprocess.run(["git", "rev-parse", "HEAD"], cwd=_ROOT,
                             capture_output=True, text=True, timeout=10)
        if rev.returncode != 0:
            return None
        status = subprocess.run(["git", "status", "--porcelain"], cwd=_ROOT,
                                capture_output=True, text=True, timeout=10)
        return {
            "package_commit": rev.stdout.strip(),
            "package_dirty": bool(status.stdout.strip()),
            "package_source": "git",
        }
    except Exception:
        return None


def base_prompt_sha256() -> str:
    """The model-facing contract every condition receives, hashed.

    Distinct from source_sha256, which moves for any change at all. This moves
    only when what the model is told, or what it is judged by, changes. A
    dataset collected under a different value cannot be re-scored like for
    like: the frozen 30B run finished review-restraint in prose because the
    prompt of the day sanctioned prose, and re-scoring it under a prompt that
    requires submit_answer marks it a failure of the model, which it is not.

    This used to hash SYSTEM_PROMPT and nothing else, and that was too narrow.
    Two changes landed that altered what the model sees and how its answer is
    judged, while this value sat still:

      * every tool result payload gained an `evidence_id` field
      * the answer contract went from resolving several citation schemes to
        accepting canonical `name:index` ids only

    A dataset from that tree would have advertised comparability with the
    2026-09-08 run and not had it. The lesson is that "the base prompt" is not
    one string: it is everything the model is handed and everything its answer
    is measured against.

    What is covered, and why by source rather than by value: these are
    functions, so their output depends on the repository, the skill and the
    call at hand. Hashing one rendered example would miss a change in a branch
    the example did not take. Hashing the function source misses nothing.

    Deliberately NOT covered: tool names, descriptions and JSON schemas. Those
    are model-facing, but they are the independent variable. They differ by
    condition on purpose, so a single per-run number cannot describe them.
    Every row already carries `tool_schema_hash` over the exact toolset that
    row was offered, which is the honest place for it.
    """
    import inspect

    from .agent import context as ctxmod
    from .agent.contracts import Orchestrator as ContractOrchestrator

    parts: list[tuple[str, str]] = [("SYSTEM_PROMPT", ctxmod.SYSTEM_PROMPT)]
    members = (
        # What the model is told.
        ("context.build_system_message", ctxmod.build_system_message),
        ("context.build_skill_message", ctxmod.build_skill_message),
        # The shape of every tool result it reads back.
        ("context.tool_result_message", ctxmod.tool_result_message),
        ("contracts._execute", ContractOrchestrator._execute),
        # What its answer has to look like to be accepted.
        ("contracts._accept_answer", ContractOrchestrator._accept_answer),
    )
    for label, member in members:
        try:
            parts.append((label, inspect.getsource(member)))
        except (OSError, TypeError):
            # Source is unavailable, so this hash cannot describe the contract.
            # Say so in a form nobody can mistake for a digest, rather than
            # emitting a confident hash over a partial surface.
            return "unavailable-no-source"

    digest = hashlib.sha256()
    for label, text in parts:
        digest.update(label.encode())
        digest.update(b"\0")
        digest.update(text.encode())
        digest.update(b"\0")
    return digest.hexdigest()


def package_identity() -> dict[str, Any]:
    """Commit, dirtiness and content hash. Quoted with every run."""
    stamp = _ROOT / "PACKAGE.json"
    out: dict[str, Any] = {"package_commit": None, "package_dirty": None,
                           "package_source": "unknown"}
    if stamp.exists():
        try:
            data = json.loads(stamp.read_text())
            out.update({
                "package_commit": data.get("commit"),
                "package_dirty": data.get("dirty"),
                "package_built_at": data.get("built_at"),
                "package_source": "stamp",
            })
        except Exception:
            pass
    if out["package_commit"] is None:
        out.update(_from_git() or {})
    out["source_sha256"] = source_sha256()
    out["base_prompt_sha256"] = base_prompt_sha256()
    return out