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
    return sorted(out)


def source_sha256() -> str:
    """A hash over the files that decide behaviour, path and content both."""
    digest = hashlib.sha256()
    for path in _files():
        digest.update(str(path.relative_to(_ROOT)).encode())
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
    only when what the model is told changes, and a dataset collected under a
    different value cannot be re-scored like for like: the frozen 30B run
    finished review-restraint in prose because the prompt of the day sanctioned
    prose, and re-scoring it under a prompt that requires submit_answer marks
    it a failure of the model, which it is not.
    """
    from .agent.context import SYSTEM_PROMPT

    return hashlib.sha256(SYSTEM_PROMPT.encode()).hexdigest()


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
