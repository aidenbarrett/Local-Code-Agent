#!/usr/bin/env python3
"""Read-only, fail-closed checks for one committed change candidate.

This command can establish local structural readiness only. It never stamps,
stages, commits, pushes or merges, and it cannot claim PR readiness until trusted
CI evidence consumption exists.
"""

from __future__ import annotations

import argparse
import ast
import json
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
INSTRUMENT_PATH = "internal/INSTRUMENT.json"
CONTRACT_KEYS = ("base_prompt_sha256", "outcome_contract_sha256")
FROZEN_PREFIXES = ("internal/experiments/",)
RETIRED_MODULES = {
    "local_agent.tools.base",
    "local_agent.tools.context",
    "local_agent.tools.runner",
    "local_agent.tools.testing",
    "local_agent.llm.models",
    "local_agent.llm.router",
}
RETIRED_PATHS = tuple(
    "internal/" + module.replace(".", "/") + ".py" for module in RETIRED_MODULES
)
ACTIVE_PYTHON_PREFIXES = (
    "internal/local_agent/",
    "internal/tests/",
    "internal/scripts/",
    "internal/evaluation/",
    "internal/measurement/",
)
REQUIRED_LOCAL_CHECKS = {
    "native-pytest",
    "compatibility-runner",
    "fixture-integrity",
}
_DYNAMIC_LOADERS = {"__import__", "import_module", "spec_from_file_location"}


class ReadinessError(RuntimeError):
    pass


def _run_git(*args: str, cwd: Path = ROOT) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, timeout=120
    )


def git(*args: str, cwd: Path = ROOT) -> str:
    proc = _run_git(*args, cwd=cwd)
    if proc.returncode != 0:
        detail = proc.stderr.strip() or proc.stdout.strip() or "git command failed"
        raise ReadinessError(detail)
    return proc.stdout.strip()


def resolve(ref: str) -> str:
    value = git("rev-parse", "--verify", f"{ref}^{{commit}}")
    if len(value) != 40:
        raise ReadinessError(f"could not resolve commit ref: {ref}")
    return value


def tree_for(commit: str) -> str:
    value = git("rev-parse", "--verify", f"{commit}^{{tree}}")
    if len(value) != 40:
        raise ReadinessError(f"could not resolve tree for commit: {commit}")
    return value


def require_clean() -> None:
    if git("status", "--porcelain", "--untracked-files=all"):
        raise ReadinessError("index/worktree/untracked files are dirty")


def is_ancestor(base_sha: str, head_sha: str) -> bool:
    proc = _run_git("merge-base", "--is-ancestor", base_sha, head_sha)
    if proc.returncode == 0:
        return True
    if proc.returncode == 1:
        return False
    raise ReadinessError(proc.stderr.strip() or "could not establish ancestry")


def changed_paths(base_sha: str, head_sha: str) -> list[str]:
    raw = git("diff", "--name-only", "--no-renames", base_sha, head_sha)
    return [line for line in raw.splitlines() if line]


def files_at(commit: str) -> list[str]:
    raw = git("ls-tree", "-r", "--name-only", commit)
    return [line for line in raw.splitlines() if line]


def read_text_at(commit: str, path: str) -> str:
    return git("show", f"{commit}:{path}")


def read_json_at(commit: str, path: str) -> dict[str, Any]:
    try:
        value = json.loads(read_text_at(commit, path))
    except json.JSONDecodeError as exc:
        raise ReadinessError(f"invalid JSON at {commit}:{path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ReadinessError(f"{commit}:{path} is not a JSON object")
    return value


def contract_drift(
    trusted: dict[str, Any], candidate: dict[str, Any]
) -> dict[str, tuple[Any, Any]]:
    return {
        key: (trusted.get(key), candidate.get(key))
        for key in CONTRACT_KEYS
        if trusted.get(key) != candidate.get(key)
    }


def _package_for(path: str) -> list[str]:
    parts = list(Path(path).with_suffix("").parts)
    if parts and parts[0] == "internal":
        parts = parts[1:]
    return parts[:-1]


def _resolved_from(path: str, node: ast.ImportFrom) -> str:
    if node.level == 0:
        return node.module or ""
    package = _package_for(path)
    keep = max(0, len(package) - (node.level - 1))
    prefix = package[:keep]
    if node.module:
        prefix.extend(node.module.split("."))
    return ".".join(prefix)


def _call_name(node: ast.Call) -> str:
    if isinstance(node.func, ast.Name):
        return node.func.id
    if isinstance(node.func, ast.Attribute):
        return node.func.attr
    return ""


def _retired_literal(value: str, retired_modules: set[str]) -> bool:
    normalized = value.replace("\\", "/")
    retired_paths = {
        "internal/" + module.replace(".", "/") + ".py"
        for module in retired_modules
    }
    return value in retired_modules or any(
        normalized.endswith(path) for path in retired_paths
    )


def stale_imports(
    commit: str, retired_modules: set[str] | None = None
) -> list[str]:
    retired_modules = RETIRED_MODULES if retired_modules is None else retired_modules
    offenders: list[str] = []
    for path in files_at(commit):
        if not path.endswith(".py") or not path.startswith(ACTIVE_PYTHON_PREFIXES):
            continue
        try:
            tree = ast.parse(read_text_at(commit, path), filename=path)
        except SyntaxError as exc:
            raise ReadinessError(f"cannot audit imports in {path}: {exc}") from exc
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name in retired_modules:
                        offenders.append(f"{path}:{node.lineno}: {alias.name}")
            elif isinstance(node, ast.ImportFrom):
                module = _resolved_from(path, node)
                if module in retired_modules:
                    offenders.append(f"{path}:{node.lineno}: {module}")
                for alias in node.names:
                    candidate = f"{module}.{alias.name}" if module else alias.name
                    if candidate in retired_modules:
                        offenders.append(f"{path}:{node.lineno}: {candidate}")
            elif isinstance(node, ast.Call) and _call_name(node) in _DYNAMIC_LOADERS:
                for arg in node.args:
                    if (
                        isinstance(arg, ast.Constant)
                        and isinstance(arg.value, str)
                        and _retired_literal(arg.value, retired_modules)
                    ):
                        offenders.append(f"{path}:{node.lineno}: {arg.value}")
    return offenders


def verify_generated_output(head_sha: str) -> None:
    """Regenerate the committed fixture in an isolated checkout."""
    with tempfile.TemporaryDirectory(prefix="lca-check-change-") as temp:
        checkout = Path(temp) / "candidate"
        git("worktree", "add", "--detach", "--quiet", str(checkout), head_sha)
        try:
            proc = subprocess.run(
                [sys.executable, "internal/benchmark_fixture/generate_project.py"],
                cwd=checkout,
                capture_output=True,
                text=True,
                timeout=120,
            )
            if proc.returncode != 0:
                raise ReadinessError(
                    "fixture generation unavailable: "
                    + (proc.stderr.strip() or proc.stdout.strip() or "generator failed")
                )
            dirty = git(
                "status",
                "--porcelain",
                "--untracked-files=all",
                "--",
                "internal/benchmark_fixture/cpp_project",
                cwd=checkout,
            )
            if dirty:
                raise ReadinessError("generated fixture output differs from committed output")
        finally:
            proc = _run_git("worktree", "remove", "--force", str(checkout))
            if proc.returncode != 0:
                raise ReadinessError("could not remove isolated verification checkout")


def load_acceptance_evidence(path: Path, expected: dict[str, str]) -> None:
    resolved = path.resolve()
    try:
        resolved.relative_to(ROOT.resolve())
    except ValueError:
        pass
    else:
        raise ReadinessError("acceptance evidence must be outside the candidate checkout")
    try:
        value = json.loads(resolved.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ReadinessError(f"acceptance evidence unavailable: {exc}") from exc
    if not isinstance(value, dict) or value.get("schema") != "lca.change-acceptance/1":
        raise ReadinessError("acceptance evidence has an unsupported schema")
    for key, wanted in expected.items():
        if value.get(key) != wanted:
            raise ReadinessError(f"acceptance evidence has stale {key}")
    checks = value.get("checks")
    if not isinstance(checks, list):
        raise ReadinessError("acceptance evidence has no check list")
    passed = {
        item.get("id")
        for item in checks
        if isinstance(item, dict) and item.get("status") == "passed"
    }
    missing = sorted(REQUIRED_LOCAL_CHECKS - passed)
    if missing:
        raise ReadinessError("acceptance evidence missing passed checks: " + ", ".join(missing))


def check_candidate(
    *,
    parent_ref: str,
    destination_ref: str,
    trusted_baseline_ref: str,
    head_ref: str,
    evidence_path: Path,
    scope: str,
) -> dict[str, str]:
    if scope == "pr":
        raise ReadinessError(
            "PR scope is unavailable: trusted CI evidence consumption is not implemented"
        )
    require_clean()
    destination_sha = resolve(destination_ref)
    parent_sha = resolve(parent_ref)
    trusted_sha = resolve(trusted_baseline_ref)
    head_sha = resolve(head_ref)
    head_tree = tree_for(head_sha)
    if trusted_sha != destination_sha:
        raise ReadinessError("trusted baseline must resolve to the eventual destination in v1")
    if not is_ancestor(destination_sha, parent_sha):
        raise ReadinessError("declared stack parent does not contain the eventual destination")
    if not is_ancestor(parent_sha, head_sha):
        raise ReadinessError("candidate is not a descendant of the declared stack parent")
    if parent_sha == head_sha:
        raise ReadinessError("candidate contains no commits beyond its declared parent")

    paths = changed_paths(trusted_sha, head_sha)
    frozen = sorted(path for path in paths if path.startswith(FROZEN_PREFIXES))
    if frozen:
        raise ReadinessError("candidate edits frozen artifacts: " + ", ".join(frozen))
    trusted_files = set(files_at(trusted_sha))
    retired_paths = {
        path for path in RETIRED_PATHS if path not in trusted_files
    }
    retired_modules = {
        module
        for module in RETIRED_MODULES
        if "internal/" + module.replace(".", "/") + ".py" in retired_paths
    }
    candidate_files = set(files_at(head_sha))
    present = sorted(path for path in retired_paths if path in candidate_files)
    if present:
        raise ReadinessError("candidate restores retired paths: " + ", ".join(present))
    stale = stale_imports(head_sha, retired_modules)
    if stale:
        raise ReadinessError("candidate contains retired imports: " + "; ".join(stale))

    trusted = read_json_at(trusted_sha, INSTRUMENT_PATH)
    candidate = read_json_at(head_sha, INSTRUMENT_PATH)
    drift = contract_drift(trusted, candidate)
    if drift:
        raise ReadinessError(
            "frozen contract identity differs from trusted baseline: "
            + ", ".join(sorted(drift))
        )
    verify_generated_output(head_sha)
    load_acceptance_evidence(
        evidence_path,
        {
            "destination_commit_sha": destination_sha,
            "parent_commit_sha": parent_sha,
            "candidate_commit_sha": head_sha,
            "candidate_tree_sha": head_tree,
        },
    )

    require_clean()
    if resolve(head_ref) != head_sha or tree_for(head_sha) != head_tree:
        raise ReadinessError("candidate identity changed during the check")
    return {
        "scope": "local",
        "destination": destination_sha,
        "parent": parent_sha,
        "head": head_sha,
        "tree": head_tree,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Check committed change readiness.")
    parser.add_argument("--parent", required=True, help="declared stack parent ref")
    parser.add_argument("--destination", required=True, help="eventual destination ref")
    parser.add_argument("--trusted-baseline", required=True, help="trusted external baseline ref")
    parser.add_argument("--head", default="HEAD", help="candidate ref")
    parser.add_argument("--evidence", required=True, type=Path)
    parser.add_argument("--scope", choices=("local", "pr"), default="local")
    args = parser.parse_args(argv)
    try:
        result = check_candidate(
            parent_ref=args.parent,
            destination_ref=args.destination,
            trusted_baseline_ref=args.trusted_baseline,
            head_ref=args.head,
            evidence_path=args.evidence,
            scope=args.scope,
        )
    except (ReadinessError, OSError, subprocess.SubprocessError) as exc:
        print(f"NOT_READY scope={args.scope}: {exc}", file=sys.stderr)
        return 2
    print(
        "READY scope=local "
        f"destination={result['destination']} parent={result['parent']} "
        f"head={result['head']} tree={result['tree']}"
    )
    print("Local structural/evidence checks passed. This is not PR or merge authorization.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
