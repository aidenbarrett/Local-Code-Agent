"""Compatibility facade for the canonical product conversation store.

New product code imports `local_agent.session.conversation_store` directly. The
script-level names remain so direct chat and historical tests keep one storage
format while there is only one implementation of raw turns/composition.
"""
from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from local_agent.session import conversation_store as _impl

SCHEMA_VERSION = _impl.SCHEMA_VERSION
DEFAULT_BUDGET_CHARS = _impl.DEFAULT_BUDGET_CHARS
DEFAULT_DISK_CAP_BYTES = _impl.DEFAULT_DISK_CAP_BYTES
ContextRefusal = _impl.ContextRefusal
RuntimeSegment = _impl.RuntimeSegment
Turn = _impl.Turn
Session = _impl.Session
Composed = _impl.Composed
OpenConversation = _impl.OpenConversation

new_session = _impl.new_session
ensure_runtime = _impl.ensure_runtime
append_turn = _impl.append_turn
canonical_turn_bytes = _impl.canonical_turn_bytes
turn_ref = _impl.turn_ref
session_path = _impl.session_path
ensure_append_fits = _impl.ensure_append_fits
create_session = _impl.create_session
compose = _impl.compose

# Private aliases remain only for deterministic failure/interleaving injection in
# the direct-chat regression suite. Application code uses the owned context. The
# canonical product module deliberately uses different private names so the
# acceptance gate can prove no application caller reaches these test hooks.
_load_session = _impl._read_session
_save_session = _impl._write_session
_conversation_lock = _impl._conversation_lock


@contextmanager
def conversation(
    runtime_root: Path,
    conversation_id: str,
    *,
    disk_cap_bytes: int = DEFAULT_DISK_CAP_BYTES,
) -> Iterator[OpenConversation]:
    # Reference the facade aliases so the existing deterministic stale-writer
    # regression can replace the lock with a controlled interleaving.
    with _conversation_lock(runtime_root, conversation_id):
        session = _load_session(runtime_root, conversation_id)
        yield OpenConversation(
            session=session,
            _runtime_root=Path(runtime_root),
            _disk_cap_bytes=disk_cap_bytes,
        )
