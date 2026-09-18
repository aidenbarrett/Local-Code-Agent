#!/usr/bin/env python3
"""Executable acceptance gates for the Session Hub milestone.

Each implementation slice extends this file with assertions for behaviour it
actually ships. The script is intentionally dependency-free so CI and local
bring-up can run the same gates.
"""
from __future__ import annotations

import ast
from importlib.util import module_from_spec, spec_from_file_location
import json
from pathlib import Path
import sys
import tempfile


REPO = Path(__file__).resolve().parents[2]
INTERNAL = REPO / "internal"
SCHEMA = INTERNAL / "docs" / "session-contract" / "v1" / "events.schema.json"
CHAT_CONTEXT = INTERNAL / "scripts" / "chat_context.py"

V1_REQUIRED_EVENT_KINDS = {
    "session.opened",
    "turn.recorded",
    "route.proposed",
    "route.resolved",
    "task.admitted",
    "task.state_changed",
    "endpoint.state_changed",
    "tool.started",
    "tool.finished",
    "artifact.recorded",
    "task.cancel_requested",
    "task.verdict",
    "task.closed",
    "watch.state_changed",
    "watch.run_recorded",
    "telemetry.policy",
    "fault.reported",
    "telemetry.sample",
    "conversation.delta",
}


class AcceptanceFailure(RuntimeError):
    pass


def require(condition: bool, name: str) -> None:
    if not condition:
        raise AcceptanceFailure(name)
    print(f"ok  {name}")


def _collect_dotted_kind_consts(value) -> list[str]:
    found: list[str] = []
    if isinstance(value, dict):
        props = value.get("properties")
        if isinstance(props, dict):
            kind = props.get("kind")
            if isinstance(kind, dict):
                candidate = kind.get("const")
                if isinstance(candidate, str) and "." in candidate:
                    found.append(candidate)
        for child in value.values():
            found.extend(_collect_dotted_kind_consts(child))
    elif isinstance(value, list):
        for child in value:
            found.extend(_collect_dotted_kind_consts(child))
    return found


def _load_chat_context():
    spec = spec_from_file_location("accept_session_hub_chat_context", CHAT_CONTEXT)
    if not spec or not spec.loader:
        raise AcceptanceFailure("cannot import persisted conversation context")
    module = module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _application_reaches_private_storage() -> list[str]:
    offenders: list[str] = []
    roots = (INTERNAL / "scripts", INTERNAL / "local_agent")
    for root in roots:
        for path in root.rglob("*.py"):
            if path == CHAT_CONTEXT:
                continue
            try:
                tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            except (OSError, SyntaxError) as exc:
                raise AcceptanceFailure(f"cannot inspect {path.relative_to(REPO)}: {exc}") from exc
            for node in ast.walk(tree):
                if isinstance(node, ast.Name) and node.id in {"_load_session", "_save_session"}:
                    offenders.append(str(path.relative_to(REPO)))
                elif isinstance(node, ast.Attribute) and node.attr in {"_load_session", "_save_session"}:
                    offenders.append(str(path.relative_to(REPO)))
    return sorted(set(offenders))


def schema_gates() -> None:
    require(SCHEMA.is_file(), "session event schema exists")
    schema = json.loads(SCHEMA.read_text(encoding="utf-8"))
    require(schema.get("$id") == "urn:lca:session:events:1", "event schema id is v1")
    kinds = _collect_dotted_kind_consts(schema)
    require(len(kinds) == len(set(kinds)), "event schema has no duplicate typed event kinds")
    require(
        V1_REQUIRED_EVENT_KINDS.issubset(set(kinds)),
        "event schema retains every reviewed v1 event kind",
    )


def conversation_ownership_gates() -> None:
    require(CHAT_CONTEXT.is_file(), "persisted conversation context exists")
    ctx = _load_chat_context()
    require(not hasattr(ctx, "load_session"), "no public unlocked persisted-session loader remains")
    require(not hasattr(ctx, "save_session"), "no public persisted-session replacement API remains")
    require(hasattr(ctx, "conversation"), "existing conversations open through owned context")
    require(hasattr(ctx, "create_session"), "new conversation creation is explicit")
    require(not _application_reaches_private_storage(), "application code cannot reach private session storage")

    with tempfile.TemporaryDirectory(prefix="lca-hub-accept-") as temp:
        root = Path(temp)
        session = ctx.new_session("accept", "model", "NPU")
        ctx.create_session(root, session)
        with ctx.conversation(root, session.conversation_id) as opened:
            ctx.append_turn(opened.session, "user", "first", 0)
            opened.save()
        with ctx.conversation(root, session.conversation_id) as opened:
            ctx.append_turn(opened.session, "assistant", "second", 0)
            opened.save()
        with ctx.conversation(root, session.conversation_id) as opened:
            require(
                [turn.content for turn in opened.session.turns] == ["first", "second"],
                "sequential owners preserve every committed turn",
            )
            ctx.append_turn(opened.session, "user", "abandoned", 0)
        with ctx.conversation(root, session.conversation_id) as opened:
            require(
                [turn.content for turn in opened.session.turns] == ["first", "second"],
                "context exit does not implicitly persist abandoned edits",
            )


def main() -> int:
    try:
        schema_gates()
        conversation_ownership_gates()
    except (AcceptanceFailure, OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"FAIL  {exc}", file=sys.stderr)
        return 1
    print("Session Hub foundation acceptance gates passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
