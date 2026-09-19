from __future__ import annotations

import ast
from pathlib import Path

INTERNAL = Path(__file__).resolve().parents[2]
LEGACY = {
    "local_agent.tools.base",
    "local_agent.tools.context",
    "local_agent.tools.runner",
    "local_agent.tools.testing",
    "local_agent.llm.models",
    "local_agent.llm.router",
}
SHIMS = {
    INTERNAL / "local_agent/tools/base.py",
    INTERNAL / "local_agent/tools/context.py",
    INTERNAL / "local_agent/tools/runner.py",
    INTERNAL / "local_agent/tools/testing.py",
    INTERNAL / "local_agent/llm/models.py",
    INTERNAL / "local_agent/llm/router.py",
}
ROOTS = (
    INTERNAL / "local_agent",
    INTERNAL / "tests",
    INTERNAL / "scripts",
    INTERNAL / "evaluation",
    INTERNAL / "measurement",
)


def _package_for(path: Path) -> list[str]:
    rel = path.relative_to(INTERNAL).with_suffix("")
    parts = list(rel.parts)
    return parts[:-1]


def _resolved_from(path: Path, node: ast.ImportFrom) -> str:
    if node.level == 0:
        return node.module or ""
    package = _package_for(path)
    keep = max(0, len(package) - (node.level - 1))
    prefix = package[:keep]
    if node.module:
        prefix.extend(node.module.split("."))
    return ".".join(prefix)


def test_pr_d_has_no_legacy_import_callers() -> None:
    """Turn PR-D migration leftovers into one deterministic, reviewable list.

    The six old modules are temporary compatibility shims while this draft is
    being migrated. The PR is not merge-ready until callers are zero and the
    shims themselves are deleted.
    """
    offenders: list[str] = []
    for root in ROOTS:
        if not root.exists():
            continue
        for path in sorted(root.rglob("*.py")):
            if path in SHIMS or path == Path(__file__).resolve():
                continue
            try:
                tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            except (OSError, SyntaxError):
                continue
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        if alias.name in LEGACY:
                            offenders.append(f"{path.relative_to(INTERNAL)}:{node.lineno}: import {alias.name}")
                elif isinstance(node, ast.ImportFrom):
                    module = _resolved_from(path, node)
                    if module in LEGACY:
                        offenders.append(f"{path.relative_to(INTERNAL)}:{node.lineno}: from {module} import ...")
                    for alias in node.names:
                        candidate = f"{module}.{alias.name}" if module else alias.name
                        if candidate in LEGACY:
                            offenders.append(f"{path.relative_to(INTERNAL)}:{node.lineno}: import {candidate}")
    assert not offenders, "legacy PR-D imports remain:\n" + "\n".join(offenders)
