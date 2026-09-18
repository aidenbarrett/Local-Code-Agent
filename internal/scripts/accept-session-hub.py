#!/usr/bin/env python3
"""Executable acceptance gates for the Session Hub milestone.

Each implementation slice extends this file with assertions for behaviour it
actually ships. The script is intentionally dependency-free so CI and local
bring-up can run the same gates.
"""
from __future__ import annotations

import json
from pathlib import Path
import sys


REPO = Path(__file__).resolve().parents[2]
INTERNAL = REPO / "internal"
SCHEMA = INTERNAL / "docs" / "session-contract" / "v1" / "events.schema.json"
DESIGN = INTERNAL / "docs" / "session-hub-design.md"
CONTROLLER = INTERNAL / "local_agent" / "session" / "controller.py"
CHAT_CONTEXT = INTERNAL / "scripts" / "chat_context.py"
CHAT = INTERNAL / "scripts" / "chat.py"


class AcceptanceFailure(RuntimeError):
    pass


def require(condition: bool, name: str) -> None:
    if not condition:
        raise AcceptanceFailure(name)
    print(f"ok  {name}")


def _collect_dotted_kind_consts(value) -> set[str]:
    found: set[str] = set()
    if isinstance(value, dict):
        props = value.get("properties")
        if isinstance(props, dict):
            kind = props.get("kind")
            if isinstance(kind, dict):
                candidate = kind.get("const")
                if isinstance(candidate, str) and "." in candidate:
                    found.add(candidate)
        for child in value.values():
            found.update(_collect_dotted_kind_consts(child))
    elif isinstance(value, list):
        for child in value:
            found.update(_collect_dotted_kind_consts(child))
    return found


def foundation_gates() -> None:
    require(SCHEMA.is_file(), "session event schema exists")
    schema = json.loads(SCHEMA.read_text(encoding="utf-8"))
    require(schema.get("$id") == "urn:lca:session:events:1", "event schema id is v1")
    kinds = _collect_dotted_kind_consts(schema)
    require(len(kinds) == 19, "event schema exposes exactly 19 typed event kinds")

    require(DESIGN.is_file(), "accepted Session Hub design exists")
    design = DESIGN.read_text(encoding="utf-8")
    for phrase, gate in (
        ("NO_VERDICT", "design keeps unknown execution separate from success"),
        ("endpoint lease", "design covers shared endpoint arbitration"),
        ("deterministic-first admission", "design keeps deterministic routing first"),
        ("Textual event loop never blocks", "design requires off-thread blocking work"),
        ("No retry of unknown effects on restart", "design forbids replaying unknown effects"),
    ):
        require(phrase.lower() in design.lower(), gate)

    require(CONTROLLER.is_file(), "session controller exists")
    controller = CONTROLLER.read_text(encoding="utf-8")
    require("allow_patch=False" in controller, "Hub v0 source mutation remains disabled")
    require("allow_commit=False" in controller, "Hub v0 commit mutation remains disabled")
    require(
        '"process_cleanup_confirmed": False' in controller,
        "current interruption path does not invent cleanup proof",
    )


def conversation_ownership_gates() -> None:
    require(CHAT_CONTEXT.is_file(), "persisted conversation context exists")
    context = CHAT_CONTEXT.read_text(encoding="utf-8")
    require("def _load_session(" in context, "raw persisted-session loader is private")
    require("def _save_session(" in context, "raw persisted-session saver is private")
    require("def load_session(" not in context, "no public unlocked persisted-session loader remains")
    require("def save_session(" not in context, "no public persisted-session replacement API remains")
    require("def conversation(" in context, "existing conversations open through owned context")
    require("def create_session(" in context, "new conversation creation is explicit and non-replacing")
    require("yield OpenConversation(" in context, "owned conversation exposes explicit save handle")

    require(CHAT.is_file(), "direct chat entrypoint exists")
    chat = CHAT.read_text(encoding="utf-8")
    require("with conversation(runtime_root, cid) as opened:" in chat,
            "direct chat resumes by loading under the conversation lock")
    require("opened.save()" in chat, "direct chat saves existing conversations through owned handle")
    for forbidden in ("load_session", "save_session", "conversation_lock"):
        require(forbidden not in chat, f"direct chat does not bypass ownership via {forbidden}")


def main() -> int:
    try:
        foundation_gates()
        conversation_ownership_gates()
    except (AcceptanceFailure, OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"FAIL  {exc}", file=sys.stderr)
        return 1
    print("Session Hub foundation acceptance gates passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
