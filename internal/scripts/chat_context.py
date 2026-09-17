"""Deterministic persistent context for direct local chat only.

Conversation history is work content, not experimental evidence. It is stored raw
under the managed runtime root so resume replays exactly what the user/model said.
Redaction belongs at an explicit future export boundary, not persistence.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
import json
import os
from pathlib import Path
from typing import Iterator
import uuid


SCHEMA_VERSION = 1
DEFAULT_BUDGET_CHARS = 24_000
DEFAULT_DISK_CAP_BYTES = 1_048_576


class ContextRefusal(RuntimeError):
    pass


def _utc() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


@dataclass(frozen=True)
class RuntimeSegment:
    from_turn: int
    profile: str
    model: str
    device: str


@dataclass(frozen=True)
class Turn:
    role: str
    content: str
    at_utc: str
    runtime: int


@dataclass
class Session:
    conversation_id: str
    created_utc: str
    updated_utc: str
    runtime: list[RuntimeSegment] = field(default_factory=list)
    turns: list[Turn] = field(default_factory=list)
    budget_chars: int = DEFAULT_BUDGET_CHARS


@dataclass(frozen=True)
class Composed:
    messages: list[dict[str, str]]
    parts: list[tuple[str, int]]
    dropped_turns: int
    total_chars: int
    budget_chars: int


def new_session(profile: str, model: str, device: str, *, budget_chars: int = DEFAULT_BUDGET_CHARS) -> Session:
    now = _utc()
    cid = now.replace(":", "-").replace(".", "-") + "-" + uuid.uuid4().hex[:4]
    return Session(
        conversation_id=cid,
        created_utc=now,
        updated_utc=now,
        runtime=[RuntimeSegment(0, profile, model, device)],
        budget_chars=budget_chars,
    )


def ensure_runtime(session: Session, profile: str, model: str, device: str) -> int:
    current = session.runtime[-1]
    if (current.profile, current.model, current.device) == (profile, model, device):
        return len(session.runtime) - 1
    session.runtime.append(RuntimeSegment(len(session.turns), profile, model, device))
    session.updated_utc = _utc()
    return len(session.runtime) - 1


def append_turn(session: Session, role: str, content: str, runtime_index: int) -> None:
    if role not in {"user", "assistant"}:
        raise ContextRefusal(f"unsupported stored turn role: {role}")
    if not (0 <= runtime_index < len(session.runtime)):
        raise ContextRefusal(f"invalid runtime segment: {runtime_index}")
    session.turns.append(Turn(role, content, _utc(), runtime_index))
    session.updated_utc = _utc()


def session_path(runtime_root: Path, conversation_id: str) -> Path:
    if not conversation_id or any(ch not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_." for ch in conversation_id):
        raise ContextRefusal("invalid conversation id")
    return Path(runtime_root) / "chat" / f"{conversation_id}.json"


def _as_dict(session: Session) -> dict:
    return {
        "schema_version": SCHEMA_VERSION,
        "conversation_id": session.conversation_id,
        "created_utc": session.created_utc,
        "updated_utc": session.updated_utc,
        "runtime": [vars(item) for item in session.runtime],
        "turns": [vars(item) for item in session.turns],
        "budget": {"kind": "characters", "limit": session.budget_chars},
    }


def save_session(runtime_root: Path, session: Session, *, disk_cap_bytes: int = DEFAULT_DISK_CAP_BYTES) -> Path:
    path = session_path(runtime_root, session.conversation_id)
    payload = json.dumps(_as_dict(session), indent=2, ensure_ascii=False) + "\n"
    encoded = payload.encode("utf-8")
    if len(encoded) > disk_cap_bytes:
        raise ContextRefusal(
            f"conversation {session.conversation_id} exceeds disk cap ({len(encoded)} > {disk_cap_bytes} bytes); history was not dropped"
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(".tmp")
    temp.write_bytes(encoded)
    os.replace(temp, path)
    return path


def load_session(runtime_root: Path, conversation_id: str) -> Session:
    path = session_path(runtime_root, conversation_id)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ContextRefusal(f"cannot read conversation {conversation_id}: {exc}") from exc
    version = data.get("schema_version")
    if version != SCHEMA_VERSION:
        raise ContextRefusal(f"unsupported conversation schema {version}; supported schema is {SCHEMA_VERSION}")
    try:
        budget = data["budget"]
        if budget.get("kind") != "characters":
            raise ValueError("unsupported budget kind")
        session = Session(
            conversation_id=data["conversation_id"],
            created_utc=data["created_utc"],
            updated_utc=data["updated_utc"],
            runtime=[RuntimeSegment(**item) for item in data["runtime"]],
            turns=[Turn(**item) for item in data["turns"]],
            budget_chars=int(budget["limit"]),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise ContextRefusal(f"malformed conversation {conversation_id}: {exc}") from exc
    if session.conversation_id != conversation_id:
        raise ContextRefusal("conversation id does not match its filename")
    if not session.runtime:
        raise ContextRefusal("conversation has no runtime segment")
    for turn in session.turns:
        if turn.role not in {"user", "assistant"} or not (0 <= turn.runtime < len(session.runtime)):
            raise ContextRefusal("conversation contains an invalid turn")
    return session


@contextmanager
def conversation_lock(runtime_root: Path, conversation_id: str) -> Iterator[None]:
    """Non-blocking per-conversation lock; OS releases it after a crash."""
    path = session_path(runtime_root, conversation_id).with_suffix(".lock")
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as fh:
        fh.seek(0)
        if os.name == "nt":
            import msvcrt
            if path.stat().st_size == 0:
                fh.write(b"0")
                fh.flush()
                fh.seek(0)
            lock = lambda: msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
            unlock = lambda: (fh.seek(0), msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1))
        else:
            import fcntl
            lock = lambda: fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
            unlock = lambda: fcntl.flock(fh, fcntl.LOCK_UN)
        try:
            lock()
        except OSError as exc:
            raise ContextRefusal(f"conversation {conversation_id} is already open in another process") from exc
        try:
            yield
        finally:
            unlock()


def compose(
    *,
    contract: dict[str, str],
    persona: dict[str, str] | None,
    session: Session,
    current_turn: str,
) -> Composed:
    """Pure deterministic composition. Oldest stored messages are dropped whole."""
    fixed = [contract]
    parts: list[tuple[str, int]] = [("contract", len(contract["content"]))]
    if persona is not None:
        fixed.append(persona)
        parts.append(("persona", len(persona["content"])))
    current = {"role": "user", "content": current_turn}
    fixed_chars = sum(len(item["content"]) for item in fixed) + len(current_turn)
    if fixed_chars > session.budget_chars:
        raise ContextRefusal(
            f"contract, persona and current turn exceed character budget ({fixed_chars} > {session.budget_chars})"
        )

    history = [{"role": turn.role, "content": turn.content} for turn in session.turns]
    history_chars = sum(len(item["content"]) for item in history)
    dropped = 0
    while history and fixed_chars + history_chars > session.budget_chars:
        removed = history.pop(0)
        history_chars -= len(removed["content"])
        dropped += 1

    messages = fixed + history + [current]
    parts.append(("history", history_chars))
    parts.append(("current_turn", len(current_turn)))
    return Composed(
        messages=messages,
        parts=parts,
        dropped_turns=dropped,
        total_chars=fixed_chars + history_chars,
        budget_chars=session.budget_chars,
    )
