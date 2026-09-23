"""Session Hub gateway surface for deterministic product/runtime questions.

Help and runtime identity are controller-owned facts. They must not spend an inference
call or let a transport outage turn into advice to rephrase the user's question.
"""
from __future__ import annotations

from ..llm.protocol import LLMTransportError
from .contracts import MAX_MESSAGE_CHARS, Proposal
from .conversation_gateway import ConversationGateway
from .runtime_facts import RuntimeFacts


_HELP_INPUTS = frozenset({"help", "/help", "what can you do", "what can you do?"})
_RUNTIME_INPUTS = frozenset({
    "what are you running on",
    "what are you running on?",
    "what model are you running",
    "what model are you running?",
    "what model are you using",
    "what model are you using?",
})


class RuntimeFactsGateway(ConversationGateway):
    """Public gateway with deterministic help/runtime truth before model fallback."""

    def __init__(self, *args, runtime_facts: RuntimeFacts, **kwargs) -> None:
        if not isinstance(runtime_facts, RuntimeFacts):
            raise TypeError("runtime-aware gateway requires RuntimeFacts")
        self.runtime_facts = runtime_facts
        super().__init__(*args, **kwargs)

    def _deterministic_answer(self, said: str) -> str | None:
        normalized = " ".join(said.strip().lower().split())
        if normalized in _HELP_INPUTS:
            execution = "enabled" if self.runtime_facts.execution_enabled else "disabled"
            return (
                "I can chat, inspect the configured repository with read-only tools, review Git state, "
                "and run the deterministic self-check. Configured build/test execution is "
                f"{execution}. Source edits, staging, commits, push and Stop are not available on this path."
            )
        if normalized in _RUNTIME_INPUTS:
            return self.runtime_facts.answer()
        return None

    def turn(self, said: str, *, explicit_mode=None) -> str:
        if not isinstance(said, str) or not said.strip() or len(said) > MAX_MESSAGE_CHARS:
            raise ValueError("enter a nonempty message of at most 8000 characters")
        answer = self._deterministic_answer(said)
        if answer is None:
            return super().turn(said, explicit_mode=explicit_mode)
        if not self._busy.acquire(blocking=False):
            raise RuntimeError("session busy; concurrent turns are not supported yet")
        try:
            self.events.emit("turn.started", {})
            self._record_exchange(said, answer)
            return answer
        finally:
            self.events.emit("turn.finished", {})
            self._busy.release()

    def _model_proposal(self, said: str) -> Proposal | None:
        messages = self._messages(said)
        if self._over_budget(messages):
            answer = "Message exceeds this profile's conversation budget. Please shorten it. No task was run."
            self._record_exchange(said, answer)
            self.events.emit("turn.refused", {"reason": "context_budget", "task_started": False})
            return None
        try:
            response = self.chat_client.chat(messages, tools=None)
            self.events.emit("conversation.metrics", response.stats.as_dict())
            if response.tool_calls:
                raise ValueError("conversation model attempted tool use")
            return Proposal.parse(response.content)
        except LLMTransportError:
            answer = (
                f"Model endpoint unreachable at {self.runtime_facts.endpoint}. "
                "Reason: endpoint_unavailable. No task was run."
            )
            self._record_exchange(said, answer)
            return None
        except (ValueError, TypeError):
            answer = "The conversation model returned no valid proposal. No task was run. Please rephrase."
            self._record_exchange(said, answer)
            return None


__all__ = ["RuntimeFactsGateway"]
