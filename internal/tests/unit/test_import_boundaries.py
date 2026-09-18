"""Architectural boundaries enforced from the actual import graph."""
from __future__ import annotations

import ast
from collections import defaultdict
from pathlib import Path

import pytest


INTERNAL = Path(__file__).resolve().parents[2]
PACKAGES = ("local_agent", "evaluation", "measurement", "scripts")


def _module_name(path: Path) -> str:
    rel = path.relative_to(INTERNAL).with_suffix("")
    parts = [part for part in rel.parts if part != "__init__"]
    return ".".join(parts)


def _sources() -> dict[str, Path]:
    out: dict[str, Path] = {}
    for package in PACKAGES:
        base = INTERNAL / package
        if not base.is_dir():
            continue
        for path in sorted(base.rglob("*.py")):
            if "__pycache__" in path.parts:
                continue
            out[_module_name(path)] = path
    return out


def _imports(path: Path, module: str, *, module_level_only: bool = False) -> set[str]:
    found: set[str] = set()
    tree = ast.parse(path.read_text(encoding="utf-8"))
    if module_level_only:
        scope = []
        for node in tree.body:
            scope.append(node)
            if isinstance(node, (ast.If, ast.Try)):
                scope.extend(ast.walk(node))
        candidates = [node for node in scope if isinstance(node, (ast.Import, ast.ImportFrom))]
    else:
        candidates = [node for node in ast.walk(tree) if isinstance(node, (ast.Import, ast.ImportFrom))]

    for node in candidates:
        if isinstance(node, ast.Import):
            for alias in node.names:
                found.add(alias.name)
            continue
        if node.level:
            base = module.split(".")
            anchor = (
                base[: len(base) - node.level]
                if path.name != "__init__.py"
                else base[: len(base) - node.level + 1]
            )
            target = ".".join(anchor + ([node.module] if node.module else []))
        else:
            target = node.module or ""
        if target:
            found.add(target)
            for alias in node.names:
                found.add(f"{target}.{alias.name}")
    return found


def _graph(*, module_level_only: bool = False) -> dict[str, set[str]]:
    sources = _sources()
    known = set(sources)
    graph: dict[str, set[str]] = defaultdict(set)
    for module, path in sources.items():
        for target in _imports(path, module, module_level_only=module_level_only):
            best = None
            parts = target.split(".")
            for size in range(len(parts), 0, -1):
                candidate = ".".join(parts[:size])
                if candidate in known:
                    best = candidate
                    break
            if best and best != module:
                package_init = INTERNAL / Path(*best.split(".")) / "__init__.py"
                if module.startswith(best + ".") and package_init.is_file():
                    continue
                graph[module].add(best)
    return graph


def _reaches(graph: dict[str, set[str]], start: str) -> set[str]:
    seen: set[str] = set()
    stack = [start]
    while stack:
        current = stack.pop()
        for target in graph.get(current, ()):
            if target not in seen:
                seen.add(target)
                stack.append(target)
    return seen


FORBIDDEN_FROM_EVALUATION = (
    "local_agent.session",
    "scripts.chat",
    "scripts.chat_context",
    "scripts.chat_persona",
)


def test_evaluation_never_reaches_the_product_layer():
    graph = _graph()
    offenders = {}
    for module in [name for name in _sources() if name.startswith("evaluation")]:
        reach = _reaches(graph, module)
        hits = sorted(
            target for target in reach if any(target.startswith(prefix) for prefix in FORBIDDEN_FROM_EVALUATION)
        )
        if hits:
            offenders[module] = hits
    assert not offenders, f"evaluation reaches product modules: {offenders}"


def test_agent_core_never_reaches_the_session_layer():
    graph = _graph()
    offenders = {}
    for module in [name for name in _sources() if name.startswith("local_agent.agent")]:
        hits = sorted(target for target in _reaches(graph, module) if target.startswith("local_agent.session"))
        if hits:
            offenders[module] = hits
    assert not offenders, f"agent core reaches the session layer: {offenders}"


def test_library_code_never_reaches_scripts():
    graph = _graph()
    offenders = {}
    for module in [name for name in _sources() if name.startswith("local_agent")]:
        hits = sorted(target for target in _reaches(graph, module) if target.startswith("scripts"))
        if hits:
            offenders[module] = hits
    assert not offenders, f"library code reaches scripts: {offenders}"


def _cycles(graph: dict[str, set[str]]) -> list[list[str]]:
    found: list[list[str]] = []
    colour: dict[str, int] = {}

    def visit(node: str, trail: list[str]) -> None:
        colour[node] = 1
        for target in sorted(graph.get(node, ())):
            if colour.get(target) == 1 and target in trail:
                found.append(trail[trail.index(target):] + [target])
            elif colour.get(target, 0) == 0:
                visit(target, trail + [target])
        colour[node] = 2

    for node in sorted(graph):
        if colour.get(node, 0) == 0:
            visit(node, [node])
    return found


def test_module_level_imports_have_no_cycles():
    assert not _cycles(_graph(module_level_only=True))


def test_cycle_breaking_imports_stay_deferred():
    """Load-bearing deferred imports must not be hoisted into import-time cycles."""
    full = _cycles(_graph())
    module_level = {tuple(cycle) for cycle in _cycles(_graph(module_level_only=True))}
    hoisted = [cycle for cycle in full if tuple(cycle) in module_level]
    assert not hoisted, f"a cycle-breaking import was hoisted to module level: {hoisted}"
    assert full, "no deferred cycles found; update this test because its premise is stale"


def test_no_shipped_module_imports_a_test_module():
    graph = _graph()
    offenders = {
        module: sorted(
            target
            for target in targets
            if target.split(".")[-1].startswith("test_") or target.split(".")[0] == "tests"
        )
        for module, targets in graph.items()
    }
    offenders = {module: targets for module, targets in offenders.items() if targets}
    assert not offenders, f"shipped modules importing tests: {offenders}"


@pytest.mark.parametrize("package", PACKAGES)
def test_every_package_has_at_least_one_module(package):
    modules = [module for module in _sources() if module.split(".")[0] == package]
    assert modules, f"{package} contributed no modules; boundary tests are vacuous"


def test_boundary_test_is_not_vacuous():
    edges = sum(len(targets) for targets in _graph().values())
    assert edges > 50, f"import graph only found {edges} edges; resolution is probably broken"
