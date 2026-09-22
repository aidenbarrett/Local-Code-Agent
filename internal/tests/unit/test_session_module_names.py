"""Pin the user-facing Session Hub module names against accidental backsliding."""
from __future__ import annotations

import ast
from importlib import import_module
from pathlib import Path


INTERNAL = Path(__file__).resolve().parents[2]
SESSION_DIR = INTERNAL / "local_agent" / "session"
WIRE_SCHEMA_ID = "lca.session.events"

RENAMES = {
    "events.py": "event_buffer.py",
    "controller.py": "task_controller.py",
    "gateway.py": "conversation_gateway.py",
    "service.py": "session_event_service.py",
    "storage.py": "session_store.py",
    "selfcheck.py": "self_check.py",
}
OLD_STEMS = {Path(name).stem for name in RENAMES}
OLD_QUALIFIED = {f"local_agent.session.{stem}" for stem in OLD_STEMS}
ACTIVE_PYTHON_ROOTS = (
    INTERNAL / "local_agent",
    INTERNAL / "scripts",
    INTERNAL / "tests",
)


def test_session_modules_use_explicit_role_names():
    for old_name, new_name in RENAMES.items():
        assert not (SESSION_DIR / old_name).exists(), f"ambiguous Session module returned: {old_name}"
        assert (SESSION_DIR / new_name).is_file(), f"expected Session module is missing: {new_name}"
        import_module(f"local_agent.session.{Path(new_name).stem}")


def test_endpoint_arbitration_has_one_policy_owner():
    """Do not reintroduce the duplicate scheduler merged in PR #148.

    EndpointArbiter is the controller-owned queue/lease policy. EndpointRuntime and
    EndpointCallAdapter may compose it, but a sibling scheduler module is a second
    authority and must fail structurally before callers can accidentally select it.
    """
    assert not (SESSION_DIR / "scheduler.py").exists(), (
        "duplicate endpoint scheduler authority returned; extend endpoint_lease.py / "
        "endpoint_runtime.py / endpoint_call.py instead"
    )
    endpoint_lease = import_module("local_agent.session.endpoint_lease")
    assert hasattr(endpoint_lease, "EndpointArbiter")


def test_python_module_names_do_not_collide_with_durable_wire_namespace():
    module_names = {
        path.stem
        for path in SESSION_DIR.glob("*.py")
        if path.name != "__init__.py"
    }
    namespace_tokens = set(WIRE_SCHEMA_ID.split("."))
    collisions = module_names & namespace_tokens
    assert not collisions, (
        "Python module names collide with durable wire namespace tokens: "
        + ", ".join(sorted(collisions))
    )


def _stale_imports(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    offenders: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name in OLD_QUALIFIED:
                    offenders.append(f"{path.relative_to(INTERNAL)}:{node.lineno}: import {alias.name}")
            continue

        if isinstance(node, ast.ImportFrom):
            if node.level == 0:
                if node.module in OLD_QUALIFIED:
                    offenders.append(
                        f"{path.relative_to(INTERNAL)}:{node.lineno}: from {node.module} import ..."
                    )
                elif node.module == "local_agent.session":
                    for alias in node.names:
                        if alias.name in OLD_STEMS:
                            offenders.append(
                                f"{path.relative_to(INTERNAL)}:{node.lineno}: "
                                f"from local_agent.session import {alias.name}"
                            )
            elif path.parent == SESSION_DIR and node.level == 1 and node.module in OLD_STEMS:
                offenders.append(
                    f"{path.relative_to(INTERNAL)}:{node.lineno}: from .{node.module} import ..."
                )
            continue

        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "import_module"
            and node.args
            and isinstance(node.args[0], ast.Constant)
            and isinstance(node.args[0].value, str)
            and node.args[0].value in OLD_QUALIFIED
        ):
            offenders.append(
                f"{path.relative_to(INTERNAL)}:{node.lineno}: import_module({node.args[0].value!r})"
            )
    return offenders


def test_active_python_has_no_imports_of_retired_session_modules():
    offenders: list[str] = []
    for root in ACTIVE_PYTHON_ROOTS:
        for path in sorted(root.rglob("*.py")):
            if "__pycache__" in path.parts:
                continue
            offenders.extend(_stale_imports(path))
    assert not offenders, "retired Session module imports remain:\n" + "\n".join(offenders)
