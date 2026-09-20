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
RETIRED = (
    INTERNAL / "local_agent/tools/base.py",
    INTERNAL / "local_agent/tools/context.py",
    INTERNAL / "local_agent/tools/runner.py",
    INTERNAL / "local_agent/tools/testing.py",
    INTERNAL / "local_agent/llm/models.py",
    INTERNAL / "local_agent/llm/router.py",
)
ROOTS = (
    INTERNAL / "local_agent",
    INTERNAL / "tests",
    INTERNAL / "scripts",
    INTERNAL / "evaluation",
    INTERNAL / "measurement",
)
_DYNAMIC_LOADERS = {"__import__", "import_module", "spec_from_file_location"}


def _package_for(path: Path) -> list[str]:
    rel = path.relative_to(INTERNAL).with_suffix("")
    return list(rel.parts[:-1])


def _resolved_from(path: Path, node: ast.ImportFrom) -> str:
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


def _looks_retired_literal(value: str) -> bool:
    normalized = value.replace("\\", "/")
    return value in LEGACY or any(
        normalized.endswith(str(path.relative_to(INTERNAL)).replace("\\", "/"))
        for path in RETIRED
    )


def test_pr_d_retired_module_paths_stay_absent() -> None:
    present = [str(path.relative_to(INTERNAL)) for path in RETIRED if path.exists()]
    assert not present, "retired PR-D module paths reappeared:\n" + "\n".join(present)


def test_pr_d_retired_imports_stay_absent() -> None:
    offenders: list[str] = []
    this_file = Path(__file__).resolve()
    for root in ROOTS:
        if not root.exists():
            continue
        for path in sorted(root.rglob("*.py")):
            if path.resolve() == this_file:
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        if alias.name in LEGACY:
                            offenders.append(
                                f"{path.relative_to(INTERNAL)}:{node.lineno}: import {alias.name}"
                            )
                elif isinstance(node, ast.ImportFrom):
                    module = _resolved_from(path, node)
                    if module in LEGACY:
                        offenders.append(
                            f"{path.relative_to(INTERNAL)}:{node.lineno}: from {module} import ..."
                        )
                    for alias in node.names:
                        candidate = f"{module}.{alias.name}" if module else alias.name
                        if candidate in LEGACY:
                            offenders.append(
                                f"{path.relative_to(INTERNAL)}:{node.lineno}: import {candidate}"
                            )
                elif isinstance(node, ast.Call) and _call_name(node) in _DYNAMIC_LOADERS:
                    literals = [
                        arg.value
                        for arg in node.args
                        if isinstance(arg, ast.Constant) and isinstance(arg.value, str)
                    ]
                    for value in literals:
                        if _looks_retired_literal(value):
                            offenders.append(
                                f"{path.relative_to(INTERNAL)}:{node.lineno}: dynamic load {value}"
                            )
    assert not offenders, "retired PR-D imports remain:\n" + "\n".join(offenders)
