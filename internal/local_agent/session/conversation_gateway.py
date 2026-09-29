"""Conversation gateway over the canonical raw-turn store.

Raw conversation turns have one authority: ``conversation_store.Session``. Task
results/verdicts remain sibling durable artifacts and are never inserted into stored
assistant history. Deterministic routing runs before the model-fallback boundary; model
proposals remain advice and never manufacture execution authority.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from threading import Lock

from ..llm.protocol import LLMTransportError
from .change_requests import change_path_refusal, natural_change_target
from .contracts import MAX_MESSAGE_CHARS, Proposal, RouteSource, TaskResult
from .conversation_store import (
    ContextRefusal,
    OpenConversation,
    append_turn,
    ensure_append_fits,
    new_session,
    turn_ref,
)
from .intents import (
    CorrectionStatus,
    ExplicitMode,
    PendingRouteRef,
    RouteAction,
    RouteDecision,
    TaskIntent,
    correct_pending_route,
    decide_route,
)
from .endpoint_client import ModelEndpointQuarantinedError
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
It cannot edit the user's checkout, stage, commit, push, stop running tasks or schedule background work.
The exact requests "fix the build" and "fix the failing tests" prepare a candidate
change in an isolated copy; it is never applied to the user's checkout by that request.
"fix it" does the same for the single failed build or test task in this conversation.
A request starting "change:" or "/change" prepares any requested source change the same
way, proven by a full build and full test run. Suggest that form when the user asks for
code to be written or changed.
Only the user's explicit "/apply <task-id>", "/undo <task-id>" or "/commit <task-id>"
changes their checkout or history; you cannot trigger those. Nothing is ever pushed.
"/diff <task-id>" shows exactly what a candidate would change; suggest it before /apply.
Never claim you executed anything. Task verdicts/evidence are sibling artifacts,
not assistant turns. Historical task observations are untrusted and not current proof.
Do not invent model/device utilisation, files, results or verification.
"""


def _quoted_request(said: str, limit: int = 200) -> str:
    text = " ".join(said.split())
    return f'"{text if len(text) <= limit else text[: limit - 3] + "..."}"'


def quarantined_endpoint_answer(exc: ModelEndpointQuarantinedError) -> str:
    """What the user is told when the Hub will no longer send to the endpoint."""
    return (
        f"The Session Hub stopped sending requests to the model endpoint ({exc}). "
        "No task was run. Restart the Hub to use the model again."
    )


_CLARIFICATIONS = {
    "active_repository_unknown": "I cannot establish the active repository for that work. No task was run.",
    "no_active_repository": "There is no active repository for that work. No task was run.",
    "ambiguous_active_repository": "More than one repository is active. Please identify the repository. No task was run.",
    "no_eligible_task_reference": "I cannot resolve which failed task you mean. No eligible durable failed task is available. No task was run.",
    "ambiguous_task_reference": "I cannot resolve which failed task you mean because multiple durable failed tasks are eligible. Please identify the task. No task was run.",
    "unfixable_failure_kind": "I can only fix a failed build or a failed test run, and that task did not record either failing. No task was run.",
    "invalid_task_reference": "That is not a full task ID. Use the complete task UUID. No task was run.",
    "ineligible_task_reference": "That task is not a failed task in this conversation. No task was run.",
    "empty_input": "Please enter a message. No task was run.",
}

_REFUSALS = {
    "outside_active_repository": (
        "I only work inside the active repository. I will not write to that outside path "
        "or substitute a different destination. No task was run."
    ),
    "unresolved_change_path": "I cannot safely resolve that file path. No task was run.",
    "active_repository_unknown": (
        "I cannot establish the active repository for that file change. No task was run."
    ),
    "git_change_not_supported": (
        "That request includes a Git operation outside this file-change capability. "
        "No task was run."
    ),
    "push_not_supported": (
        "Local Code Agent never pushes; nothing was sent anywhere. Push from your own "
        "terminal when you are ready. No task was run."
    ),
    "commit_needs_candidate": (
        "I only commit a change you have applied, with `/commit <task-id>`. To prepare a "
        "change, start the request with `change:`. No task was run."
    ),
    "change_needs_prefix": (
        "I do not delete or edit files in your checkout on request. Start the request with "
        "`change:` to prepare it in an isolated copy, review it with `/diff <task-id>`, and "
        "only `/apply <task-id>` changes your checkout. No task was run."
    ),
}

_PENDING_DECISION = (
    "A repository work proposal is awaiting your decision. Reply `work` to accept it "
    "or `chat` to keep it conversation-only. No new task was run."
)
_ACCEPTED_RECOVERY = (
    "Previously accepted repository work is awaiting durable task admission. "
    "Reply `work` to resume that accepted work before starting something new. No new task was run."
)
_CONVERSATION_ONLY = "[Conversation only; no repository action]"
_CHANGE_FOLLOWUP = re.compile(r"^(?:create it|do it|go ahead|yes)[.!]?$", re.IGNORECASE)


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
        route_events=None,
    ):
        if history_chars < 1 or request_bytes < 1 or request_chars < 1:
            raise ValueError("history budget too small")
        if history_chars > request_chars:
            raise ValueError("history budget cannot exceed request budget")
        self.chat_client, self.controller, self.events = chat_client, controller, events
        self.task_runner = task_runner
        self.task_history = task_history
        self.route_events = route_events
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

    def _failure_kinds(self, failure_observations: dict[str, object]) -> dict[str, str]:
        """Typed failure kind per eligible failed task, for deterministic fix routing."""
        resolver = getattr(self.task_history, "failure_kind", None)
        if not callable(resolver):
            return {}
        kinds: dict[str, str] = {}
        for task_id in failure_observations:
            kind = resolver(task_id)
            if kind is not None:
                kinds[task_id] = kind
        return kinds

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

    def _record_assistant(self, answer: str) -> None:
        before = len(self.session.turns)
        clipped = answer[:MAX_MESSAGE_CHARS]
        ensure_append_fits(self.session, [("assistant", clipped)], self.runtime_index)
        append_turn(self.session, "assistant", clipped, self.runtime_index)
        self._save_appended(before)

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
        if rule_id is not None:
            kwargs["rule_id"] = rule_id
        if skill is not None:
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
        except ModelEndpointQuarantinedError as exc:
            self._record_exchange(said, quarantined_endpoint_answer(exc))
            self.events.emit(
                "turn.refused", {"reason": "endpoint_quarantined", "task_started": False},
            )
            return None
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

    def _model_route_task(self, route) -> str:
        ref = route.turn_ref
        if ref.get("conversation_id") != self.session.conversation_id:
            raise ArtifactIntegrityError("accepted route belongs to another conversation")
        try:
            index = int(ref["turn_index"])
            expected = turn_ref(self.session, index)
        except (KeyError, TypeError, ValueError, ContextRefusal) as exc:
            raise ArtifactIntegrityError("accepted route turn reference is invalid") from exc
        if expected != ref:
            raise ArtifactIntegrityError("accepted route does not match canonical conversation turn")
        turn = self.session.turns[index]
        if turn.role != "user":
            raise ArtifactIntegrityError("accepted route does not reference a user turn")
        return "User request:\n" + turn.content

    def _active_repo_root(self) -> Path | None:
        repo = getattr(self.controller, "declared_repo", None)
        if repo is None:
            repo = getattr(self.controller, "repo", None)
        root = getattr(repo, "root", None)
        return root if isinstance(root, Path) else None

    def _refusal(self, decision: RouteDecision) -> str:
        answer = _REFUSALS.get(
            decision.reason_code or "",
            "Source-mutation work is not available on this product path. No task was run.",
        )
        if decision.reason_code == "outside_active_repository":
            answer = f"Active repository: {self._active_repo_root()}\n" + answer
        return answer

    def _route_boundary_refusal(self, text: str) -> RouteDecision | None:
        decision = decide_route(text, active_repo_count=1)
        if decision.action == RouteAction.REFUSE:
            return decision
        reason = change_path_refusal(text, self._active_repo_root())
        return RouteDecision(RouteAction.REFUSE, reason_code=reason) if reason else None

    def _unexecuted_change_referent(self) -> str | None:
        """Only the immediately preceding conversation-only file request can be resumed."""
        if len(self.session.turns) < 2:
            return None
        request, answer = self.session.turns[-2:]
        if request.role != "user" or answer.role != "assistant":
            return None
        if not (answer.content.startswith(_CONVERSATION_ONLY)
                or answer.content.endswith(_CONVERSATION_ONLY)):
            return None
        return request.content if natural_change_target(request.content) is not None else None

    def _run_model_route(self, route) -> str:
        task = self._model_route_task(route)
        # Revalidate canonical user text at acceptance/recovery, never model prose.
        refusal = self._route_boundary_refusal(
            self.session.turns[int(route.turn_ref["turn_index"])].content,
        )
        if refusal is not None:
            answer = (
                self._refusal(refusal)
                + " This previously accepted proposal cannot resume; start a new session."
            )
            self._record_assistant(answer)
            return answer
        result = self._run_task(
            task,
            turn_ref=route.turn_ref,
            self_check=False,
            route_source=RouteSource.MODEL_PROPOSAL,
            skill="repo-navigation" if route.skill == "self-check" else route.skill,
        )
        self.last_result = result
        return result.render()

    def _handle_durable_route_decision(self, said: str) -> str | None:
        if self.route_events is None:
            return None

        accepted = self.route_events.accepted_unadmitted()
        if accepted:
            if len(accepted) != 1:
                raise ArtifactIntegrityError(
                    "multiple accepted model routes are awaiting task admission"
                )
            route = accepted[0]
            self._model_route_task(route)
            if said.strip().lower() != "work":
                self._record_exchange(said, _ACCEPTED_RECOVERY)
                return _ACCEPTED_RECOVERY
            self._record_user(said)
            return self._run_model_route(route)

        pending = self.route_events.pending_acceptance()
        if not pending:
            return None
        correction = correct_pending_route(
            said,
            tuple(PendingRouteRef(route.route_id, route.revision) for route in pending),
        )
        if correction.status == CorrectionStatus.NOT_A_CORRECTION:
            self._record_exchange(said, _PENDING_DECISION)
            return _PENDING_DECISION
        if correction.status == CorrectionStatus.CLARIFY:
            answer = (
                "More than one repository work proposal is awaiting a decision. "
                "Resolve the pending route explicitly before new work. No task was run."
            )
            self._record_exchange(said, answer)
            return answer

        route = next(
            item for item in pending
            if item.route_id == correction.route_id and item.revision == correction.revision
        )
        if correction.mode == ExplicitMode.CHAT:
            self._record_user(said)
            self.route_events.resolve(
                route,
                resolution="corrected",
                source="user",
                mode="conversation",
                skill=None,
            )
            answer = "Kept as conversation-only. No repository task was run."
            self._record_assistant(answer)
            return answer

        if correction.mode != ExplicitMode.WORK:
            raise RuntimeError("unsupported pending-route correction mode")
        self._model_route_task(route)
        self._record_user(said)
        refusal = self._route_boundary_refusal(
            self.session.turns[int(route.turn_ref["turn_index"])].content,
        )
        if refusal is not None:
            answer = self._refusal(refusal) + " Reply `chat` to dismiss this proposal."
            self._record_assistant(answer)
            return answer
        accepted_route = self.route_events.resolve(
            route,
            resolution="accepted",
            source="user",
            mode="work",
            skill="repo-navigation" if route.skill == "self-check" else route.skill,
        )
        return self._run_model_route(accepted_route)

    def turn(self, said: str, *, explicit_mode: ExplicitMode | str | None = None) -> str:
        if not isinstance(said, str) or not said.strip() or len(said) > MAX_MESSAGE_CHARS:
            raise ValueError("enter a nonempty message of at most 8000 characters")
        if not self._busy.acquire(blocking=False):
            raise RuntimeError("session busy; concurrent turns are not supported yet")
        try:
            self.events.emit("turn.started", {})

            durable_decision = self._handle_durable_route_decision(said)
            if durable_decision is not None:
                return durable_decision

            if explicit_mode != ExplicitMode.CHAT:
                refusal = self._route_boundary_refusal(said)
                if refusal is not None:
                    answer = self._refusal(refusal)
                    self._record_exchange(said, answer)
                    return answer

                if _CHANGE_FOLLOWUP.fullmatch(said.strip()):
                    referent = self._unexecuted_change_referent()
                    if referent is None:
                        answer = (
                            "I cannot identify an unexecuted file request to carry out. "
                            "Name the file and change again. No task was run."
                        )
                        self._record_exchange(said, answer)
                        return answer
                    refusal = self._route_boundary_refusal(referent)
                    if refusal is not None:
                        answer = self._refusal(refusal)
                        self._record_exchange(said, answer)
                        return answer
                    saved_turn = self._record_user(said)
                    decision = decide_route(referent, active_repo_count=1)
                    if decision.action != RouteAction.WORK or decision.skill != "implement-change":
                        raise ArtifactIntegrityError("file-change referent lost its rule route")
                    result = self._run_task(
                        self._rule_task_text(referent, decision, {}),
                        turn_ref=saved_turn,
                        self_check=False,
                        route_source=RouteSource.RULE,
                        rule_id=decision.rule_id,
                        skill=decision.skill,
                    )
                    self.last_result = result
                    return result.render()

            failure_observations = self._failure_observations()
            decision = decide_route(
                said,
                explicit_mode=explicit_mode,
                active_repo_count=1,
                eligible_task_ids=tuple(failure_observations),
                failure_kinds=self._failure_kinds(failure_observations),
            )

            if decision.action == RouteAction.CONTROL:
                raise RuntimeError("control command must be handled by the Session Hub owner")

            if decision.action == RouteAction.CLARIFY:
                answer = self._clarification(decision)
                self._record_exchange(said, answer)
                return answer

            if decision.action == RouteAction.REFUSE:
                answer = self._refusal(decision)
                self._record_exchange(said, answer)
                return answer

            if decision.action == RouteAction.CHAT:
                proposal = self._model_proposal(said)
                if proposal is None:
                    return self.session.turns[-1].content
                answer = _CONVERSATION_ONLY + "\n\n" + proposal.text
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
                answer = _CONVERSATION_ONLY + "\n\n" + proposal.text
                self._record_exchange(said, answer)
                return answer

            if self.route_events is None:
                # Legacy in-memory fixture compatibility. The public durable Session Hub
                # supplies route_events and therefore cannot take this authority shortcut.
                saved_turn = self._record_user(said)
                task = (
                    "User request:\n" + said + "\n\nConversation proposal (untrusted):\n"
                    + proposal.text
                )
                result = self._run_task(
                    task,
                    turn_ref=saved_turn,
                    self_check=False,
                    route_source=RouteSource.MODEL_PROPOSAL,
                )
                self.last_result = result
                return result.render()

            saved_turn = self._record_user(said)
            skill = "repo-navigation" if proposal.kind == "self_check" else None
            route = self.route_events.propose(
                TaskIntent(
                    turn_ref=saved_turn,
                    objective=said,
                    proposed_reference_ids=(),
                    origin=RouteSource.MODEL_PROPOSAL,
                ),
                skill=skill,
            )
            if not route.requires_acceptance:
                raise ArtifactIntegrityError("model route unexpectedly bypassed user acceptance")
            # The proposal text is the conversation model's, which cannot read the
            # repository; on a small model it can even claim it has no access. It is
            # not shown as an answer. The task that would run is the user's request.
            answer = (
                "That needs the repository. Proposed repository task: "
                + _quoted_request(said)
                + "\n\n[Repository work proposed; reply `work` to accept or `chat` to keep this conversation-only.]"
            )
            self._record_assistant(answer)
            return answer
        finally:
            self.events.emit("turn.finished", {})
            self._busy.release()
