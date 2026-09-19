"""Pin the user-facing Session Hub module names against accidental backsliding."""
from __future__ import annotations

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


def test_session_modules_use_explicit_role_names():
    for old_name, new_name in RENAMES.items():
        assert not (SESSION_DIR / old_name).exists(), f"ambiguous Session module returned: {old_name}"
        assert (SESSION_DIR / new_name).is_file(), f"expected Session module is missing: {new_name}"
        import_module(f"local_agent.session.{Path(new_name).stem}")


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