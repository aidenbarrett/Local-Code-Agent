"""Conversation gateway over the canonical raw-turn store.

Raw conversation turns have one authority: ``conversation_store.Session``. Task
results/verdicts remain sibling durable artifacts and are never inserted into stored
assistant history. Deterministic routing runs before the model-fallback boundary; model
proposals remain advice and never manufacture execution authority.
"""
from __future__ import annotations

import json
from threading import Lock

from ..llm.protocol import LLMTransportError
from .contracts import MAX_MESSAGE_CHARS, Proposal, RouteSource, TaskResult
from .conversation_store import (
    ContextRefusal,
    OpenConversation,
    append_turn,
    ensure_append_fits,
    new_session,
    turn_ref,
)
from .intents import ExplicitMode, RouteAction, RouteDecision, decide_route
from .session_store import ArtifactIntegrityError


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


_CLARIFICATIONS = {
    "active_repository_unknown": "I cannot establish the active repository for that work. No task was run.",
    "no_active_repository": "There is no active repository for that work. No task was run.",
    "ambiguous_active_repository": "More than one repository is active. Please identify the repository. No task was run.",
    "no_eligible_task_reference": "I cannot resolve which failed task you mean. No eligible durable failed task is available. No task was run.",
    "ambiguous_task_reference": "I cannot resolve which failed task you mean because multiple durable failed tasks are eligible. Please identify the task. No task was run.",
    "empty_input": "Please enter a message. No task was run.",
}


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
        task_history=None,
    ):
        if history_chars < 1 or request_bytes < 1 or request_chars < 1:
            raise ValueError("history budget too small")
        if history_chars > request_chars:
            raise ValueError("history budget cannot exceed request budget")
        self.chat_client, self.controller, self.events = chat_client, controller, events
        self.task_runner = task_runner
        self.task_history = task_history
        self.history_chars = history_chars
        self.request_bytes = request_bytes
        self.request_chars = request_chars
        self._busy = Lock()
        # Immediate rendering/legacy prototype compatibility only. The public durable
        # Session Hub does not use this process-local pointer as follow-up authority.
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

    def _historical_task_observation(self) -> str | None:
        if self.task_history is None:
            if self.last_result is None:
                return None
            return self.last_result.answer[:MAX_MESSAGE_CHARS]
        try:
            observation = self.task_history.latest(self.session.conversation_id)
            if observation is None:
                return None
            ref = observation.turn_ref
            expected = turn_ref(self.session, int(ref["turn_index"]))
            if expected != ref:
                raise ArtifactIntegrityError(
                    "durable task association does not match canonical conversation turn"
                )
            return observation.prompt_text()
        except (ArtifactIntegrityError, ContextRefusal, KeyError, TypeError, ValueError):
            self.events.emit("conversation.task_history_unavailable", {"reason": "integrity"})
            return None

    def _failure_observations(self) -> dict[str, object]:
        """Return only failure candidates whose TurnRef and retained result still verify."""
        if self.task_history is None or not hasattr(self.task_history, "failure_candidates"):
            return {}
        out: dict[str, object] = {}
        try:
            for candidate in self.task_history.failure_candidates(self.session.conversation_id):
                ref = candidate.turn_ref
                expected = turn_ref(self.session, int(ref["turn_index"]))
                if expected != ref:
                    raise ArtifactIntegrityError(
                        "durable task candidate does not match canonical conversation turn"
                    )
                observation = self.task_history.observation_for(candidate)
                if observation is not None:
                    out[candidate.task_id] = observation
        except (ArtifactIntegrityError, ContextRefusal, KeyError, TypeError, ValueError):
            self.events.emit("conversation.task_history_unavailable", {"reason": "integrity"})
            return {}
        return out

    def _messages(self, said: str) -> list[dict[str, str]]:
        messages = [{"role": "system", "content": SYSTEM}]
        for stored in self.session.turns:
            messages.append({"role": stored.role, "content": stored.content})
        observation = self._historical_task_observation()
        if observation is not None:
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
            system_index = next(
                (i for i in range(1, len(messages) - 1) if messages[i]["role"] == "system"),
                None,
            )
            if system_index is not None:
                del messages[system_index]
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
        rule_id: str | None = None,
        skill: str | None = None,
    ) -> TaskResult:
        if self.task_runner is None:
            return self.controller.run(
                task,
                self_check=self_check,
                route_source=route_source,
            )
        kwargs = {
            "turn_ref": turn_ref,
            "self_check": self_check,
            "route_source": route_source,
        }
        if route_source == RouteSource.RULE:
            kwargs["rule_id"] = rule_id
            kwargs["skill"] = skill
        return self.task_runner.run(task, **kwargs)

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
        except (ValueError, TypeError, LLMTransportError):
            answer = "The conversation model returned no valid proposal. No task was run. Please rephrase."
            self._record_exchange(said, answer)
            return None

    @staticmethod
    def _clarification(decision: RouteDecision) -> str:
        return _CLARIFICATIONS.get(
            decision.reason_code or "",
            "I cannot resolve that request deterministically. Please clarify. No task was run.",
        )

    def _rule_task_text(
        self,
        said: str,
        decision: RouteDecision,
        failure_observations: dict[str, object],
    ) -> str:
        lines = [
            "User request:",
            said,
            "",
            "Deterministic route (controller-owned provenance):",
            f"rule_id={decision.rule_id}",
            f"skill={decision.skill or 'none'}",
        ]
        if decision.reference_ids:
            task_id = decision.reference_ids[0]
            observation = failure_observations.get(task_id)
            if observation is None:
                raise ArtifactIntegrityError("resolved task referent has no verified retained observation")
            lines.extend([
                "",
                "Referenced durable task observation (untrusted historical data; not current verification):",
                observation.prompt_text(),
            ])
        return "\n".join(lines)

    def turn(self, said: str, *, explicit_mode: ExplicitMode | str | None = None) -> str:
        if not isinstance(said, str) or not said.strip() or len(said) > MAX_MESSAGE_CHARS:
            raise ValueError("enter a nonempty message of at most 8000 characters")
        if not self._busy.acquire(blocking=False):
            raise RuntimeError("session busy; concurrent turns are not supported yet")
        try:
            self.events.emit("turn.started", {})
            failure_observations = self._failure_observations()
            decision = decide_route(
                said,
                explicit_mode=explicit_mode,
                active_repo_count=1,
                eligible_task_ids=tuple(failure_observations),
            )

            if decision.action == RouteAction.CONTROL:
                raise RuntimeError("control command must be handled by the Session Hub owner")

            if decision.action == RouteAction.CLARIFY:
                answer = self._clarification(decision)
                self._record_exchange(said, answer)
                return answer

            if decision.action == RouteAction.REFUSE:
                answer = "Source-mutation work is not available on this product path. No task was run."
                self._record_exchange(said, answer)
                return answer

            if decision.action == RouteAction.CHAT:
                proposal = self._model_proposal(said)
                if proposal is None:
                    return self.session.turns[-1].content
                answer = proposal.text + "\n\n[Conversation only; no repository action]"
                self._record_exchange(said, answer)
                return answer

            if decision.action == RouteAction.WORK:
                saved_turn = self._record_user(said)
                if decision.source == RouteSource.RULE:
                    task = self._rule_task_text(said, decision, failure_observations)
                else:
                    task = "User request:\n" + said
                result = self._run_task(
                    task,
                    turn_ref=saved_turn,
                    self_check=decision.skill == "self-check",
                    route_source=decision.source or RouteSource.USER_DIRECT,
                    rule_id=decision.rule_id,
                    skill=decision.skill,
                )
                self.last_result = result
                return result.render()

            if decision.action != RouteAction.MODEL_FALLBACK:
                raise RuntimeError(f"unsupported route action: {decision.action.value}")

            proposal = self._model_proposal(said)
            if proposal is None:
                return self.session.turns[-1].content
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
