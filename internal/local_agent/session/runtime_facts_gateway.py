"""Session Hub gateway surface for deterministic product/runtime questions.

Help and runtime identity are controller-owned facts. They must not spend an inference
call or let a transport outage turn into advice to rephrase the user's question.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from ..llm.protocol import LLMTransportError
from .contracts import MAX_MESSAGE_CHARS, Proposal
from .conversation_gateway import ConversationGateway, quarantined_endpoint_answer
from .endpoint_client import ModelEndpointQuarantinedError
from .runtime_facts import RuntimeFacts
from .session_store import ArtifactIntegrityError


_HELP_INPUTS = frozenset({"help", "/help", "what can you do", "what can you do?"})
_REPOSITORY_INPUTS = frozenset({
    "where are you working",
    "where are you working?",
    "what repository are you working on",
    "what repository are you working on?",
    "what repo are you working on",
    "what repo are you working on?",
    "what can you access",
    "what can you access?",
})
_EFFECT_LOCATION_INPUTS = frozenset({
    "where did you save it",
    "where did you save it?",
    "where did you put it",
    "where did you put it?",
})


@dataclass(frozen=True)
class RepositoryFacts:
    name: str
    root: Path
    branch: str
    execution_enabled: bool

    def answer(self) -> str:
        execution = "enabled" if self.execution_enabled else "disabled"
        return (
            f"Active repository: {self.name}. Root: {self.root}. "
            f"Observed branch: {self.branch}. My repository access is restricted to "
            "this root; "
            f"configured build/test execution is {execution}. I will not substitute paths "
            "outside it."
        )


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

    def __init__(
        self,
        *args,
        runtime_facts: RuntimeFacts,
        repository_facts: RepositoryFacts | None = None,
        **kwargs,
    ) -> None:
        if not isinstance(runtime_facts, RuntimeFacts):
            raise TypeError("runtime-aware gateway requires RuntimeFacts")
        if repository_facts is not None and not isinstance(
            repository_facts, RepositoryFacts
        ):
            raise TypeError("repository_facts must be RepositoryFacts when supplied")
        self.runtime_facts = runtime_facts
        self.repository_facts = repository_facts
        super().__init__(*args, **kwargs)

    def _candidate_location_answer(self, result) -> str:
        candidate = result.candidate
        paths = ", ".join(candidate.paths) if candidate.paths else "no retained paths"
        root = self.repository_facts.root if self.repository_facts is not None else None
        location = f"Active repository root: {root}. " if root is not None else ""
        answer = "I cannot establish where the latest change was saved. No task was run."
        if candidate.role == "prepared":
            proof = (
                "verified" if result.verified_at_completion
                else "not verified at completion"
            )
            answer = (
                f"{location}The latest change is a {proof} prepared candidate for: "
                f"{paths}. It is retained by Local Code Agent and has not been applied "
                "to the active repository. "
                f"Candidate task: {candidate.candidate_task_id}."
            )
        elif candidate.role == "applied":
            answer = (
                f"{location}The latest candidate was applied to these repository paths: "
                f"{paths}."
            )
        elif candidate.role == "committed":
            answer = (
                f"{location}The latest candidate was committed at {candidate.commit}; "
                f"its repository paths are: {paths}."
            )
        elif candidate.role == "undone":
            answer = (
                f"{location}The latest candidate was undone; its changes are no longer "
                f"applied. The restored repository paths are: {paths}."
            )
        elif candidate.role == "discarded":
            answer = (
                f"{location}The latest prepared candidate was discarded and is no longer "
                f"retained or applied. Its recorded paths were: {paths}."
            )
        elif candidate.role == "apply_refused":
            answer = (
                f"{location}The latest apply was refused, so I have no evidence that the "
                f"candidate was saved into the active repository. Its intended paths "
                f"were: {paths}."
            )
        return answer

    def _effect_location_answer(self) -> str:
        if self.task_history is None:
            return (
                "I have no durable task result proving where a change was saved. "
                "No task was run."
            )
        try:
            result = self.task_history.latest_result(self.session.conversation_id)
        except ArtifactIntegrityError:
            return (
                "I cannot trust the retained task result that would establish where the "
                "change was saved. No task was run."
            )
        if result is None or result.candidate is None:
            return (
                "I have no durable task result proving where a change was saved. "
                "No task was run."
            )
        return self._candidate_location_answer(result)

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
        if normalized in _EFFECT_LOCATION_INPUTS:
            return self._effect_location_answer()
        if normalized in _REPOSITORY_INPUTS:
            if self.repository_facts is None:
                return (
                    "I cannot establish the active repository authority for this session. "
                    "No task was run."
                )
            return self.repository_facts.answer()
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
        except ModelEndpointQuarantinedError as exc:
            self._record_exchange(said, quarantined_endpoint_answer(exc))
            self.events.emit(
                "turn.refused", {"reason": "endpoint_quarantined", "task_started": False},
            )
            return None
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


__all__ = ["RepositoryFacts", "RuntimeFactsGateway"]
