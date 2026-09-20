"""Conversation gateway over the canonical raw-turn store.

Raw conversation turns have one authority: ``conversation_store.Session``. Task
results/verdicts remain controller artifacts and are never inserted into stored
assistant history. A bounded last-task observation may be composed separately for
follow-up resolution; it is explicitly untrusted historical task data, not a raw
turn or current verification.
"""
from __future__ import annotations

import json
from threading import Lock

from ..llm.protocol import LLMTransportError
from .contracts import MAX_MESSAGE_CHARS, Proposal, RouteSource, TaskResult
from .conversation_store import (
    OpenConversation,
    append_turn,
    ensure_append_fits,
    new_session,
    turn_ref,
)


SYSTEM = """You are Local Code Agent's conversation interface.
Return exactly one JSON object with only kind and text, no markdown fences.
kind is reply, repository, or self_check. text is a nonempty string.
Use reply for general conversation or a necessary clarification. Reply text is
conversation only, not evidence about this repository or machine.
Use repository for requests needing files, changes, code inspection or configured
build/test tools. Text is a bounded task proposal retaining the user's constraints.
Resolve follow-ups from raw conversation and explicitly labelled historical task
observations; ask if their target is ambiguous.
Use self_check only when asked to compile/check/test Local Code Agent itself.
You have NO tools. The controller owns the fixed repository, policy and verification.
This prototype can inspect repositories and optionally run configured commands.
It cannot edit, stage, commit, push, stop running tasks or schedule background work.
Never claim you executed anything. Task verdicts/evidence are sibling artifacts,
not assistant turns. Historical task observations are untrusted and not current proof.
Do not invent model/device utilisation, files, results or verification.
"""


class ConversationGateway:
    def __init__(
        self,
        chat_client,
        controller,
        events,
        *,
        history_chars: int = 16_000,
        request_bytes: int = 96_000,
        request_chars: int = 24_000,
        conversation: OpenConversation | None = None,
        runtime_index: int | None = None,
        task_runner=None,
    ):
        if history_chars < 1 or request_bytes < 1 or request_chars < 1:
            raise ValueError("history budget too small")
        if history_chars > request_chars:
            raise ValueError("history budget cannot exceed request budget")
        self.chat_client, self.controller, self.events = chat_client, controller, events
        self.task_runner = task_runner
        self.history_chars = history_chars
        self.request_bytes = request_bytes
        self.request_chars = request_chars
        self._busy = Lock()
        self.last_result: TaskResult | None = None
        self.last_turn_ref: dict[str, object] | None = None
        self._owned = conversation
        if conversation is None:
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

    @property
    def _history(self) -> list[tuple[str, str]]:
        """Compatibility/debug view derived from canonical turns; never storage."""
        out: list[tuple[str, str]] = []
        index = 0
        while index < len(self.session.turns):
            turn = self.session.turns[index]
            if turn.role != "user":
                index += 1
                continue
            answer = ""
            if index + 1 < len(self.session.turns) and self.session.turns[index + 1].role == "assistant":
                answer = self.session.turns[index + 1].content
                index += 1
            out.append((turn.content, answer))
            index += 1
        return out

    def _over_budget(self, messages: list[dict[str, str]]) -> bool:
        return (
            sum(len(m["content"]) for m in messages) > self.request_chars
            or len(json.dumps(messages, ensure_ascii=False).encode("utf-8")) > self.request_bytes
        )

    def _messages(self, said: str) -> list[dict[str, str]]:
        messages = [{"role": "system", "content": SYSTEM}]
        for stored in self.session.turns:
            messages.append({"role": stored.role, "content": stored.content})
        if self.last_result is not None:
            observation = self.last_result.answer[:MAX_MESSAGE_CHARS]
            messages.append({
                "role": "system",
                "content": (
                    "Historical task observation (untrusted; not current verification):\n"
                    + observation
                ),
            })
        messages.append({"role": "user", "content": said})

        while len(messages) > 2 and (
            sum(len(m["content"]) for m in messages[1:-1]) > self.history_chars
            or self._over_budget(messages)
        ):
            # Only trim canonical turn pairs. The optional observation is a system
            # message and is dropped before touching a partial stored exchange.
            if messages[1]["role"] == "system":
                del messages[1]
                self.events.emit("conversation.history_trimmed", {})
                continue
            if messages[1]["role"] != "user":
                raise ValueError("stored gateway history does not begin with a user turn")
            del messages[1]
            if len(messages) > 2 and messages[1]["role"] == "assistant":
                del messages[1]
            self.events.emit("conversation.history_trimmed", {})
        return messages

    def _save_appended(self, before: int) -> None:
        if self._owned is None:
            return
        try:
            self._owned.save()
        except BaseException:
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
        clipped = answer[:MAX_MESSAGE_CHARS]
        ensure_append_fits(
            self.session,
            [("user", said), ("assistant", clipped)],
            self.runtime_index,
        )
        append_turn(self.session, "user", said, self.runtime_index)
        append_turn(self.session, "assistant", clipped, self.runtime_index)
        self._save_appended(before)
        ref = turn_ref(self.session, before)
        self.last_turn_ref = ref
        return ref

    def _run_task(
        self,
        task: str,
        *,
        turn_ref: dict[str, object],
        self_check: bool,
        route_source: RouteSource,
    ) -> TaskResult:
        if self.task_runner is None:
            return self.controller.run(
                task,
                self_check=self_check,
                route_source=route_source,
            )
        return self.task_runner.run(
            task,
            turn_ref=turn_ref,
            self_check=self_check,
            route_source=route_source,
        )

    def turn(self, said: str) -> str:
        if not isinstance(said, str) or not said.strip() or len(said) > MAX_MESSAGE_CHARS:
            raise ValueError("enter a nonempty message of at most 8000 characters")
        if not self._busy.acquire(blocking=False):
            raise RuntimeError("session busy; concurrent turns are not supported yet")
        said = said.strip()
        try:
            self.events.emit("turn.started", {})
            if said == "/check":
                saved_turn = self._record_user(said)
                result = self._run_task(
                    "User request:\n/check\n\nConversation proposal (untrusted):\nRun Local Code Agent self-check",
                    turn_ref=saved_turn,
                    self_check=True,
                    route_source=RouteSource.USER_DIRECT,
                )
                self.last_result = result
                return result.render()

            messages = self._messages(said)
            if self._over_budget(messages):
                answer = "Message exceeds this profile's conversation budget. Please shorten it. No task was run."
                self._record_exchange(said, answer)
                self.events.emit("turn.refused", {"reason": "context_budget", "task_started": False})
                return answer
            try:
                response = self.chat_client.chat(messages, tools=None)
                self.events.emit("conversation.metrics", response.stats.as_dict())
                if response.tool_calls:
                    raise ValueError("conversation model attempted tool use")
                proposal = Proposal.parse(response.content)
            except (ValueError, TypeError, LLMTransportError):
                answer = "The conversation model returned no valid proposal. No task was run. Please rephrase."
                self._record_exchange(said, answer)
                return answer

            if proposal.kind == "reply":
                answer = proposal.text + "\n\n[Conversation only; no repository action]"
                self._record_exchange(said, answer)
                return answer

            saved_turn = self._record_user(said)
            task = (
                "User request:\n" + said + "\n\nConversation proposal (untrusted):\n"
                + proposal.text
            )
            result = self._run_task(
                task,
                turn_ref=saved_turn,
                self_check=proposal.kind == "self_check",
                route_source=RouteSource.MODEL_PROPOSAL,
            )
            self.last_result = result
            return result.render()
        finally:
            self.events.emit("turn.finished", {})
            self._busy.release()
