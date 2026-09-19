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

import ast
import hashlib
import json
import subprocess
from pathlib import Path
from typing import Any

# The Python package now lives under internal/, but provenance is repository
# provenance. Keep the real checkout root explicit so moving behaviour-affecting
# files changes their canonical path in source_sha256 rather than disappearing
# behind a redefined root.
_ROOT = Path(__file__).resolve().parents[2]

# Everything that can change what the agent does. Prose docs are excluded, but
# normative runtime contract assets are behaviour-affecting and must move the
# source identity when their bytes change.
_HASHED = (
    ("internal/local_agent", "*.py"),
    # Every file under a skill, not just its SKILL.md. References are loaded on
    # demand and their names are model-facing, so they are behaviour-affecting.
    ("internal/skills", "*"),
    ("internal/evaluation", "*.py"),
    ("internal/benchmark_fixture/cpp_project", "*"),
    # Qualification and experiment launch policy are part of the instrument.
    ("internal/measurement", "*.py"),
    ("internal/measurement", "*.sh"),
    # The Session Hub validator loads these JSON schemas at runtime. A schema
    # edit therefore changes executable validation behaviour even though the
    # normative contract is stored under docs/.
    ("internal/docs/session-contract", "*.json"),
)

# Root packaging metadata decides what is installed and therefore executes.
_HASHED_FILES = ("pyproject.toml",)

_SKIP = {"__pycache__", "build", ".local-agent", ".git", ".venv", ".venv-workstation"}


def _key(path: Path) -> str:
    """Canonical repository path used both for ordering and hashing."""
    return path.relative_to(_ROOT).as_posix()


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
            if _SKIP.intersection(parts):
                continue
            out.append(path)
    for name in _HASHED_FILES:
        path = _ROOT / name
        if path.is_file():
            out.append(path)
    return sorted(out, key=_key)


def source_sha256() -> str:
    """A hash over the files that decide behaviour, path and content both."""
    digest = hashlib.sha256()
    for path in _files():
        digest.update(_key(path).encode())
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def _from_git() -> dict[str, Any] | None:
    try:
        rev = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=_ROOT,
            capture_output=True, text=True, timeout=10,
        )
        if rev.returncode != 0:
            return None
        status = subprocess.run(
            ["git", "status", "--porcelain"], cwd=_ROOT,
            capture_output=True, text=True, timeout=10,
        )
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
    only when what the model is told, or what it is judged by, changes.

    Tool names/descriptions/schemas are deliberately not covered here because
    they are the independent variable and every row records its exact
    tool_schema_hash separately.
    """
    import inspect

    from .agent import context as ctxmod
    from .agent.contracts import Orchestrator as ContractOrchestrator

    parts: list[tuple[str, str]] = [("SYSTEM_PROMPT", ctxmod.SYSTEM_PROMPT)]
    members = (
        ("context.build_system_message", ctxmod.build_system_message),
        ("context.build_skill_message", ctxmod.build_skill_message),
        ("context.tool_result_message", ctxmod.tool_result_message),
        ("contracts._execute", ContractOrchestrator._execute),
        ("contracts._accept_answer", ContractOrchestrator._accept_answer),
    )
    for label, member in members:
        try:
            parts.append((label, inspect.getsource(member)))
        except (OSError, TypeError):
            return "unavailable-no-source"

    digest = hashlib.sha256()
    for label, text in parts:
        digest.update(label.encode())
        digest.update(b"\0")
        digest.update(text.encode())
        digest.update(b"\0")
    return digest.hexdigest()


def _function_source(path: Path, name: str) -> str | None:
    """Return one top-level function exactly as written, without importing it."""
    try:
        text = path.read_bytes().decode("utf-8")
        tree = ast.parse(text)
    except (OSError, SyntaxError):
        return None
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            segment = ast.get_source_segment(text, node)
            return segment if segment is not None else None
    return None


def outcome_contract_sha256() -> str:
    """Fingerprint the byte-exact contract that makes an evaluation result count."""
    task_contracts = _ROOT / "internal" / "evaluation" / "task_contracts.py"
    oracle = _ROOT / "internal" / "evaluation" / "oracle.py"
    endpoints = _ROOT / "internal" / "evaluation" / "endpoints.py"
    evaluator = _ROOT / "internal" / "evaluation" / "run_evaluation.py"
    function_names = ("prepare", "establish", "_error_row", "run_case", "run_all")
    sources = {name: _function_source(evaluator, name) for name in function_names}
    required = (task_contracts, oracle, endpoints)
    if any(value is None for value in sources.values()) or not all(p.is_file() for p in required):
        return "unavailable-no-source"

    # Keep the semantic labels stable. The physical repository paths moved, but
    # the outcome contract is the same evaluator/task/oracle contract unless its
    # bytes or selected function bodies change.
    parts: list[tuple[str, bytes]] = [
        ("evaluation/task_contracts.py", task_contracts.read_bytes()),
        ("evaluation/oracle.py", oracle.read_bytes()),
        ("evaluation/endpoints.py", endpoints.read_bytes()),
    ]
    for name in function_names:
        source = sources[name]
        assert source is not None
        parts.append((f"evaluation.run_evaluation.{name}", source.encode("utf-8")))

    digest = hashlib.sha256()
    for label, content in parts:
        digest.update(label.encode())
        digest.update(b"\0")
        digest.update(content)
        digest.update(b"\0")
    return digest.hexdigest()


def package_identity() -> dict[str, Any]:
    """Commit, dirtiness and content hashes. Quoted with every run."""
    stamp = _ROOT / "PACKAGE.json"
    out: dict[str, Any] = {
        "package_commit": None,
        "package_dirty": None,
        "package_source": "unknown",
    }
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
    out["outcome_contract_sha256"] = outcome_contract_sha256()
    return out
