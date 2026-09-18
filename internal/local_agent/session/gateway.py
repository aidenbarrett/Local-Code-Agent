"""Conversation gateway over the canonical raw-turn store.

Raw conversation turns have one authority: ``conversation_store.Session``. Task
results/verdicts remain controller artifacts and are never inserted into model
history as assistant prose.
"""
from __future__ import annotations

import json
from threading import Lock

from .contracts import Proposal, RouteSource
from .conversation_store import (
    OpenConversation,
    Session,
    append_turn,
    ensure_append_fits,
    new_session,
    turn_ref,
)


SYSTEM = """You are the conversation router for Local Code Agent.
Return exactly one JSON object and nothing else:
{"kind":"reply|repository|self_check","text":"..."}
Use reply for normal conversation. Use repository only when the user wants work on
this repository. Use self_check only when they explicitly ask to check Local Code
Agent itself. Never claim repository facts in reply unless they came from a task.
Keep reply answers concise; repository text is the objective for the controlled
worker. Do not propose edits, commits or shell commands yourself."""


class ConversationGateway:
    def __init__(
        self,
        client,
        controller,
        events,
        *,
        history_chars: int = 16_000,
        request_chars: int = 24_000,
        request_bytes: int = 100_000,
        conversation: OpenConversation | None = None,
        runtime_index: int | None = None,
    ):
        if min(history_chars, request_chars, request_bytes) < 1:
            raise ValueError("conversation budgets must be positive")
        if history_chars > request_chars:
            raise ValueError("history budget cannot exceed request budget")
        self.client = client
        self.controller = controller
        self.events = events
        self.history_chars = history_chars
        self.request_chars = request_chars
        self.request_bytes = request_bytes
        self._busy = Lock()
        self._owned = conversation
        if conversation is None:
            # Tests/legacy prototype callers still get the canonical Session model,
            # merely without disk persistence. There is no second `_history` list.
            self.session = new_session(
                "session-gateway", "conversation-router", "shared",
                budget_chars=request_chars,
            )
            self.runtime_index = 0
        else:
            self.session = conversation.session
            self.runtime_index = (
                len(self.session.runtime) - 1 if runtime_index is None else runtime_index
            )
        self.last_turn_ref: dict[str, object] | None = None

    def _validate_messages(self, messages: list[dict[str, str]]) -> None:
        chars = sum(len(m["content"]) for m in messages)
        if chars > self.request_chars:
            raise ValueError("conversation request exceeds character budget")
        raw = json.dumps(messages, ensure_ascii=False).encode("utf-8")
        if len(raw) > self.request_bytes:
            raise ValueError("conversation request exceeds byte budget")

    def _messages(self, said: str) -> list[dict[str, str]]:
        history = [
            {"role": stored.role, "content": stored.content}
            for stored in self.session.turns
        ]
        history_chars = sum(len(item["content"]) for item in history)
        while history and history_chars > self.history_chars:
            if history[0]["role"] != "user":
                raise ValueError("stored gateway history does not begin with a user turn")
            removed = history.pop(0)
            history_chars -= len(removed["content"])
            if history and history[0]["role"] == "assistant":
                removed = history.pop(0)
                history_chars -= len(removed["content"])
        messages = [{"role": "system", "content": SYSTEM}, *history,
                    {"role": "user", "content": said}]
        self._validate_messages(messages)
        return messages

    def _save_appended(self, before: int) -> None:
        if self._owned is None:
            return
        try:
            self._owned.save()
        except BaseException:
            # A failed atomic replace leaves disk on the previous version. Match it
            # in memory too so the gateway cannot continue from an uncommitted turn.
            del self.session.turns[before:]
            raise

    def _record_user(self, said: str) -> dict[str, object]:
        before = len(self.session.turns)
        ensure_append_fits(self.session, [("user", said)], self.runtime_index)
        append_turn(self.session, "user", said, self.runtime_index)
        self._save_appended(before)
        ref = turn_ref(self.session, before)
        self.last_turn_ref = ref
        return ref

    def _record_exchange(self, said: str, answer: str) -> dict[str, object]:
        before = len(self.session.turns)
        ensure_append_fits(
            self.session,
            [("user", said), ("assistant", answer)],
            self.runtime_index,
        )
        append_turn(self.session, "user", said, self.runtime_index)
        append_turn(self.session, "assistant", answer, self.runtime_index)
        self._save_appended(before)
        ref = turn_ref(self.session, before)
        self.last_turn_ref = ref
        return ref

    def turn(self, said: str) -> str:
        if not isinstance(said, str) or not said.strip():
            raise ValueError("message must be nonempty")
        if not self._busy.acquire(blocking=False):
            raise RuntimeError("another turn is already active")
        said = said.strip()
        try:
            self.events.emit("conversation.turn", {"chars": len(said)})
            if said == "/check":
                # Persist the originating user turn before any effectful work. The
                # future durable admission handshake binds this TurnRef to task_id.
                self._record_user(said)
                result = self.controller.run(
                    "Check Local Code Agent",
                    self_check=True,
                    route_source=RouteSource.USER_DIRECT,
                )
                return result.render()

            reply = self.client.chat(self._messages(said))
            try:
                proposal = Proposal.parse(reply.content)
            except (ValueError, json.JSONDecodeError):
                self.events.emit("router.invalid", {})
                return "I couldn't classify that request safely. Please say whether you want chat or repository work."

            if proposal.kind == "reply":
                self._record_exchange(said, proposal.text)
                self.events.emit("conversation.reply", {"chars": len(proposal.text)})
                return proposal.text + "\n\n[Conversation only — no repository inspection was run.]"

            if proposal.kind == "self_check":
                self._record_user(said)
                result = self.controller.run(
                    "Check Local Code Agent",
                    self_check=True,
                    route_source=RouteSource.MODEL_PROPOSAL,
                )
                return result.render()

            # Controller results are sibling task artifacts. Only the user's actual
            # work request enters raw turn history; result/verdict text does not get
            # replayed to the chat model as if it were model-authored conversation.
            self._record_user(said)
            result = self.controller.run(
                proposal.text,
                route_source=RouteSource.MODEL_PROPOSAL,
            )
            return result.render()
        finally:
            self._busy.release()
