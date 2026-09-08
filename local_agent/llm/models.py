"""Transport-neutral message types and per-call measurements.

The agent talks to these. Whether the bytes came from OVMS on a CPU in
Falcarragh or an NPU on a Panther Lake box behind an SSH tunnel is not the
agent's business.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any]
    raw_arguments: str = ""

    @staticmethod
    def from_openai(call: Any) -> "ToolCall":
        raw = call.function.arguments or "{}"
        return ToolCall.from_parts(
            getattr(call, "id", "") or "call_0", call.function.name, raw
        )

    @staticmethod
    def from_parts(call_id: str, name: str, raw: Any) -> "ToolCall":
        try:
            args = json.loads(raw) if isinstance(raw, str) else dict(raw or {})
        except json.JSONDecodeError:
            args = {}
        if not isinstance(args, dict):
            args = {}
        return ToolCall(
            id=call_id or "call_0",
            name=name or "",
            arguments=args,
            raw_arguments=raw if isinstance(raw, str) else json.dumps(raw),
        )

    def as_assistant_fragment(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "type": "function",
            "function": {
                "name": self.name,
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
    finish_reason: str = "stop"
    stats: CallStats = field(default_factory=CallStats)

    @property
    def wants_tools(self) -> bool:
        return bool(self.tool_calls)

    def as_assistant_message(self) -> dict[str, Any]:
        msg: dict[str, Any] = {"role": "assistant", "content": self.content or None}
        if self.tool_calls:
            msg["tool_calls"] = [c.as_assistant_fragment() for c in self.tool_calls]
        return msg
