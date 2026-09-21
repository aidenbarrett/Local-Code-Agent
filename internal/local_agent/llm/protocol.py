"""Transport-neutral message types and per-call measurements.

The agent talks to these. Whether the bytes came from OVMS on a CPU under the
desk or an NPU on a Panther Lake box behind an SSH tunnel is not the agent's
business.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any


_QUALIFIED_FINISH_REASONS = frozenset({"stop", "tool_calls"})
_RESPONSE_VALIDATION_TOOL = "__lca_response_validation__"


class ToolName(str):
    """A tool name carrying controller-visible validation failure provenance.

    It remains a real ``str`` so transcript rendering and backend call association
    keep the exact model-provided name.  ``ToolRegistry.get`` inspects the marker
    before policy or handler dispatch, which lets an invalid response fail closed
    without inventing repaired JSON or turning the failure into a transport fault.
    """

    validation_error: str | None

    def __new__(cls, value: str, validation_error: str | None = None):
        obj = str.__new__(cls, value)
        obj.validation_error = validation_error
        return obj


def _flagged_name(value: str, reason: str) -> ToolName:
    return ToolName(value or _RESPONSE_VALIDATION_TOOL, reason)


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any]
    raw_arguments: str = ""
    parse_error: str | None = None

    @staticmethod
    def from_openai(call: Any) -> "ToolCall":
        raw = call.function.arguments or "{}"
        return ToolCall.from_parts(
            getattr(call, "id", "") or "call_0", call.function.name, raw
        )

    @staticmethod
    def from_parts(call_id: str, name: str, raw: Any) -> "ToolCall":
        parse_error: str | None = None
        if isinstance(raw, str):
            raw_text = raw
            try:
                args = json.loads(raw)
            except json.JSONDecodeError as exc:
                args = {}
                parse_error = f"invalid_json:{exc.msg}"
        else:
            try:
                args = dict(raw or {})
                raw_text = json.dumps(raw)
            except (TypeError, ValueError) as exc:
                args = {}
                raw_text = repr(raw)
                parse_error = f"invalid_arguments:{type(exc).__name__}"

        if parse_error is None and not isinstance(args, dict):
            args = {}
            parse_error = "arguments_not_object"
        if not isinstance(name, str) or not name:
            parse_error = parse_error or "missing_tool_name"
            name = ""

        dispatch_name: str = name
        if parse_error is not None:
            dispatch_name = _flagged_name(name, parse_error)
        return ToolCall(
            id=call_id or "call_0",
            name=dispatch_name,
            arguments=args,
            raw_arguments=raw_text,
            parse_error=parse_error,
        )

    def invalidate(self, reason: str) -> None:
        current = getattr(self.name, "validation_error", None)
        combined = reason if not current else f"{current};{reason}"
        self.name = _flagged_name(str(self.name), combined)

    def as_assistant_fragment(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "type": "function",
            "function": {
                "name": str(self.name),
                "arguments": self.raw_arguments or json.dumps(self.arguments),
            },
        }


@dataclass
class CallStats:
    """What one request to the model actually cost.

    `ttft_s` is only real when the call was streamed. Without streaming there is
    no way to separate prefill from decode from the client side, so it stays
    None rather than being guessed at, and `estimated` marks any derived split.
    """

    total_s: float = 0.0
    ttft_s: float | None = None
    prompt_tokens: int = 0
    completion_tokens: int = 0

    # OVMS chat completions documents completion_tokens, prompt_tokens and
    # total_tokens, and nothing about cached prompt tokens. So this is None,
    # meaning "the server did not tell us", not 0, meaning "no cache hit".
    # Reporting a missing field as a zero hit rate would make a working prefix
    # cache look broken, which is exactly the wrong conclusion to draw on DDR4.
    cached_tokens: int | None = None
    streamed: bool = False
    error: str | None = None

    # Qwen3 thinking, if it happened. Reasoning is stripped out of `content`
    # before the orchestrator ever sees it, but its cost is recorded, because a
    # cheap tier that narrates its way through a one-line instruction is not
    # cheap and the report has to say so.
    thinking_detected: bool = False
    thinking_chars: int = 0

    # Three separate facts, and collapsing them would poison every latency,
    # token and energy comparison six weeks from now:
    #   requested  what the profile asked for
    #   accepted   whether the server accepted the control at all
    #   effective  what was actually observed to happen
    # A silent compatibility retry is fine. Silently reporting the requested
    # policy as the effective one is not.
    thinking_requested: bool | None = None
    thinking_control_accepted: bool | None = None

    # Runtime provenance and server-side timings. Both are outside the OpenAI
    # schema, so they are read at the client edge and are None whenever a
    # backend does not send them. llama.cpp puts `system_fingerprint` on every
    # response and a `timings` block beside `usage`; OVMS may send neither.
    system_fingerprint: str | None = None
    server_prompt_ms: float | None = None
    server_predicted_ms: float | None = None
    server_cache_n: int | None = None

    @property
    def thinking_effective(self) -> str:
        """Observed, not assumed. 'on', 'off' or 'unknown'."""
        if self.thinking_detected:
            return "on"
        if self.completion_tokens or self.thinking_chars:
            return "off"
        return "unknown"

    @property
    def cache_reported(self) -> bool:
        return self.cached_tokens is not None

    @property
    def decode_s(self) -> float | None:
        if self.ttft_s is None:
            return None
        return max(0.0, self.total_s - self.ttft_s)

    @property
    def decode_tok_s(self) -> float | None:
        decode = self.decode_s
        if decode is None or decode <= 0 or self.completion_tokens <= 1:
            return None
        # The first token arrived at ttft, so the decode window produced the rest.
        return (self.completion_tokens - 1) / decode

    @property
    def prefill_tok_s(self) -> float | None:
        """Uncached prompt tokens per second of prefill.

        When the server reports no cache information we cannot know how much of
        the prompt was actually prefilled, so this is the whole-prompt rate and
        is a lower bound on the true prefill throughput.
        """
        if self.ttft_s is None or self.ttft_s <= 0 or self.prompt_tokens <= 0:
            return None
        uncached = max(0, self.prompt_tokens - (self.cached_tokens or 0))
        return uncached / self.ttft_s if uncached else None

    @property
    def cache_hit_ratio(self) -> float | None:
        """None when the server does not report it. Never guessed."""
        if self.cached_tokens is None or self.prompt_tokens <= 0:
            return None
        return self.cached_tokens / self.prompt_tokens

    def as_dict(self) -> dict[str, Any]:
        return {
            "total_s": round(self.total_s, 3),
            "ttft_s": round(self.ttft_s, 3) if self.ttft_s is not None else None,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "cached_tokens": self.cached_tokens,
            "cache_reported": self.cache_reported,
            "cache_hit_ratio": (
                round(self.cache_hit_ratio, 3) if self.cache_hit_ratio is not None else None
            ),
            "prefill_tok_s": (
                round(self.prefill_tok_s, 1) if self.prefill_tok_s is not None else None
            ),
            "decode_tok_s": (
                round(self.decode_tok_s, 1) if self.decode_tok_s is not None else None
            ),
            "streamed": self.streamed,
            "thinking_detected": self.thinking_detected,
            "thinking_chars": self.thinking_chars,
            "thinking_requested": self.thinking_requested,
            "thinking_control_accepted": self.thinking_control_accepted,
            "thinking_effective": self.thinking_effective,
            "system_fingerprint": self.system_fingerprint,
            "server_prompt_ms": self.server_prompt_ms,
            "server_predicted_ms": self.server_predicted_ms,
            "server_cache_n": self.server_cache_n,
            "error": self.error,
        }


class LLMTransportError(RuntimeError):
    """The inference call itself failed: refused, timed out, or the server broke.

    Raised by clients, caught by the orchestrator, which turns it into a typed
    halt. Deliberately not a tool error: no tool ran, and the evidence record
    must never say one did.
    """

    # kind distinguishes the two ways a server fails us, because they are
    # different facts and get different follow-up:
    #   unavailable  we could not reach it, or it errored, or it went silent
    #   stalled      it is alive and answering, but the request made no
    #                useful progress within our deadline or throughput floor
    def __init__(self, message: str, cause: str = "", kind: str = "unavailable") -> None:
        super().__init__(message)
        self.cause = cause  # exception class name from the transport, for the log
        self.kind = kind


@dataclass
class ChatResponse:
    content: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    finish_reason: str | None = "stop"
    stats: CallStats = field(default_factory=CallStats)

    def __post_init__(self) -> None:
        """Make incomplete/ambiguous tool output non-dispatchable before effects.

        We currently qualify one tool call at a time. Multiple-call output is
        deliberately rejected as a whole until a capability test proves batch
        semantics; this is safer than executing the first valid-looking call and
        discovering that a later call in the same model response was malformed.
        """
        reason: str | None = None
        if self.finish_reason not in _QUALIFIED_FINISH_REASONS:
            reason = f"unqualified_finish_reason:{self.finish_reason!r}"
        elif len(self.tool_calls) > 1:
            reason = "multiple_tool_calls_not_qualified"

        if reason is not None:
            if not self.tool_calls:
                self.tool_calls = [
                    ToolCall(
                        id="response_validation",
                        name=_flagged_name(_RESPONSE_VALIDATION_TOOL, reason),
                        arguments={},
                        raw_arguments="{}",
                        parse_error=reason,
                    )
                ]
            else:
                for call in self.tool_calls:
                    call.invalidate(reason)

    @property
    def wants_tools(self) -> bool:
        return bool(self.tool_calls)

    @property
    def validation_errors(self) -> list[str]:
        return [
            str(error)
            for call in self.tool_calls
            if (error := getattr(call.name, "validation_error", None))
        ]

    def as_assistant_message(self) -> dict[str, Any]:
        msg: dict[str, Any] = {"role": "assistant", "content": self.content or None}
        if self.tool_calls:
            msg["tool_calls"] = [c.as_assistant_fragment() for c in self.tool_calls]
        return msg
