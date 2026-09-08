"""LLM clients.

`OpenAICompatibleClient` speaks to anything exposing an OpenAI-compatible chat
completions endpoint, which includes OpenVINO Model Server. It streams by
default, not for the typing effect but because streaming is the only way to
measure time to first token from the client side, and on a bandwidth-starved
box time to first token is the number that decides whether the agent is usable.

`ScriptedClient` speaks to nothing at all and is what the test suite and the
eval harness use, so the whole orchestrator can be exercised without a model
loaded.
"""

from __future__ import annotations

import json
import re
import time
from typing import Any, Callable, Iterable, Protocol

from ..config import ModelConfig
from .models import CallStats, ChatResponse, LLMTransportError, ToolCall


class LLMClient(Protocol):
    def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
    ) -> ChatResponse: ...


# Qwen3 emits its reasoning inline. Different runtimes wrap it differently, and
# some put it in a separate `reasoning_content` delta instead. Either way it
# must never reach the orchestrator as the answer: `submit_answer` summaries and
# eval grading would both be polluted by it.
_THINK_BLOCK = re.compile(
    r"<think>.*?</think>\s*"
    r"|\[start thinking\].*?\[end thinking\]\s*",
    re.DOTALL | re.IGNORECASE,
)
# A leftover opening marker with no close means the model ran out of budget
# mid-thought: everything after it is reasoning and there is no answer.
_THINK_OPEN = re.compile(r"<think>|\[start thinking\]", re.IGNORECASE)


def split_reasoning(text: str) -> tuple[str, str]:
    """Return (answer, reasoning). Never raises, never loses the answer."""
    if not text:
        return "", ""
    reasoning_parts = _THINK_BLOCK.findall(text)
    answer = _THINK_BLOCK.sub("", text)
    # An unterminated <think> means the model ran out of budget mid-thought.
    # Everything after the tag is reasoning, and there is no answer.
    unterminated = _THINK_OPEN.search(answer)
    if unterminated:
        reasoning_parts.append(answer[unterminated.start():])
        answer = answer[: unterminated.start()]
    return answer.strip(), "".join(reasoning_parts)


def _usage_from(obj: Any) -> tuple[int, int, int | None]:
    """Pull prompt/completion/cached counts out of a usage object, tolerantly.

    Cached prompt tokens come back as None when the server did not report them,
    which is the expected case: OVMS documents only completion_tokens,
    prompt_tokens and total_tokens. Treating an absent field as a zero hit rate
    would make a perfectly good prefix cache look broken.
    """
    usage = getattr(obj, "usage", None)
    if usage is None:
        return 0, 0, None
    prompt = int(getattr(usage, "prompt_tokens", 0) or 0)
    completion = int(getattr(usage, "completion_tokens", 0) or 0)

    cached: int | None = None
    details = getattr(usage, "prompt_tokens_details", None)
    if details is not None and getattr(details, "cached_tokens", None) is not None:
        cached = int(details.cached_tokens)
    elif isinstance(usage, dict):
        detail_map = usage.get("prompt_tokens_details") or {}
        if "cached_tokens" in detail_map:
            cached = int(detail_map["cached_tokens"])
    return prompt, completion, cached


def _http_stack() -> Any:
    """The HTTP library the installed openai SDK is built on.

    openai>=3 moved from httpx to httpx2 and no longer installs httpx, so a
    fresh venv on a newer SDK has no `httpx` module at all. The SDK exports
    DefaultHttpx2Client exactly when it is on httpx2; that is the fact we key
    on, not a version number. Both libraries spell Timeout the same way.
    """
    import importlib
    import openai

    name = "httpx2" if hasattr(openai, "DefaultHttpx2Client") else "httpx"
    try:
        return importlib.import_module(name)
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError(
            f"the installed openai SDK expects {name}, which is not importable"
        ) from exc


def sdk_identity() -> dict[str, Any]:
    """Which SDK and HTTP stack carried the requests. Quoted with every run.

    A fresh venv resolves `openai>=1.40` to whatever is newest that day. Two
    runs a week apart can sit on different SDK majors with different HTTP
    libraries and different default behaviour, so the version is part of the
    measured configuration, not an environment detail.
    """
    try:
        import openai
    except ImportError:  # pragma: no cover
        return {"sdk": "openai", "sdk_version": "not installed", "http_stack": None}
    stack = "httpx2" if hasattr(openai, "DefaultHttpx2Client") else "httpx"
    return {
        "sdk": "openai",
        "sdk_version": getattr(openai, "__version__", "unknown"),
        "http_stack": stack,
    }


def _provenance_from(obj: Any) -> dict[str, Any]:
    """system_fingerprint and the llama.cpp `timings` block, if present.

    Both sit outside the OpenAI schema. The SDK keeps unknown fields on the
    model object, so getattr works; a dict fallback covers raw JSON. Anything
    absent is simply absent: None, never zero.
    """
    out: dict[str, Any] = {
        "system_fingerprint": None,
        "server_prompt_ms": None,
        "server_predicted_ms": None,
        "server_cache_n": None,
    }
    if obj is None:
        return out
    get = (lambda k: obj.get(k)) if isinstance(obj, dict) else (lambda k: getattr(obj, k, None))
    fp = get("system_fingerprint")
    if fp:
        out["system_fingerprint"] = str(fp)
    timings = get("timings")
    if timings:
        tget = (lambda k: timings.get(k)) if isinstance(timings, dict) else (lambda k: getattr(timings, k, None))
        for src, dst, cast in (
            ("prompt_ms", "server_prompt_ms", float),
            ("predicted_ms", "server_predicted_ms", float),
            ("cache_n", "server_cache_n", int),
        ):
            value = tget(src)
            if value is not None:
                out[dst] = cast(value)
    return out


class OpenAICompatibleClient:
    def __init__(self, config: ModelConfig) -> None:
        self.config = config
        self._client: Any = None
        self._usage_in_stream_supported = True
        # None until we have actually asked. False once a server has rejected
        # the field, which is a fact the run has to carry, not swallow.
        self._chat_template_kwargs_supported: bool | None = None

    def _ensure(self) -> Any:
        if self._client is None:
            try:
                from openai import OpenAI
            except ImportError as exc:  # pragma: no cover
                raise RuntimeError(
                    "the openai package is required for the HTTP client: "
                    "pip install openai"
                ) from exc
            http = _http_stack()
            self._client = OpenAI(
                base_url=self.config.base_url,
                api_key=self.config.api_key,
                # Explicit per-phase timeouts. A bare float would apply one value
                # to connect, read, write and pool alike.
                timeout=http.Timeout(
                    connect=self.config.connect_timeout_s,
                    read=self.config.read_timeout_s,
                    write=self.config.read_timeout_s,
                    pool=self.config.connect_timeout_s,
                ),
                # Zero. One logical request is one measured attempt. The SDK's
                # retry would silently turn a timeout into two attempts and a
                # 5xx into a second try, and the run would never know. An
                # earlier version set this to 1.
                max_retries=0,
            )
        return self._client

    def _kwargs(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None,
        max_tokens: int | None = None,
    ) -> dict[str, Any]:
        kwargs: dict[str, Any] = {
            "model": self.config.model,
            "messages": messages,
            "temperature": self.config.temperature,
            "top_p": self.config.top_p,
            "max_tokens": max_tokens or self.config.max_tokens,
            # Not OpenAI fields, so the SDK only lets them through extra_body,
            # which it merges into the top level of the JSON body. llama.cpp and
            # OVMS both read them from there.
            "extra_body": {"top_k": self.config.top_k, "min_p": self.config.min_p},
        }
        if tools:
            kwargs["tools"] = tools
            kwargs["tool_choice"] = "auto"
        if self.config.thinking is not None and self._chat_template_kwargs_supported is not False:
            # llama.cpp and vLLM honour this for Qwen3. A server that rejects it
            # is detected once and never asked again.
            #
            # It goes in extra_body, not as a keyword: the SDK's create() has an
            # explicit keyword-only signature and raises TypeError on anything
            # it does not know. An earlier version passed this at the top level,
            # which meant every real run would have tripped the "server rejected
            # it" fallback on the first call and silently run with thinking left
            # at the server default. The fakes in the test suite accept **kw and
            # never noticed. See test_request_uses_only_sdk_keywords.
            kwargs["extra_body"]["chat_template_kwargs"] = {
                "enable_thinking": self.config.thinking
            }
            if self._chat_template_kwargs_supported is None:
                self._chat_template_kwargs_supported = True
        return kwargs

    # Keyword parameters the openai SDK's chat.completions.create() accepts.
    # Anything else must ride in extra_body. Pinned by a test so the mistake
    # described above cannot come back.
    SDK_KEYWORDS = frozenset({
        "model", "messages", "temperature", "top_p", "max_tokens", "tools",
        "tool_choice", "stream", "stream_options", "extra_body", "extra_headers",
        "extra_query", "timeout", "seed", "stop", "n", "user", "response_format",
        "presence_penalty", "frequency_penalty", "logit_bias", "logprobs",
        "top_logprobs", "parallel_tool_calls", "max_completion_tokens",
    })

    # ------------------------------------------------------------------ chat

    def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        max_tokens: int | None = None,
    ) -> ChatResponse:
        if self.config.stream:
            return self._chat_streaming(messages, tools, max_tokens)
        return self._chat_blocking(messages, tools, max_tokens)

    def _chat_blocking(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None,
        max_tokens: int | None,
    ) -> ChatResponse:
        kwargs = self._kwargs(messages, tools, max_tokens)
        started = time.monotonic()
        try:
            completion = self._ensure().chat.completions.create(**kwargs)
        except Exception as exc:
            if self._chat_template_kwargs_supported is not False and "chat_template_kwargs" in str(exc):
                # Same compatibility retry as the streaming path, recorded the
                # same way: the run learns the control was rejected.
                self._chat_template_kwargs_supported = False
                kwargs["extra_body"].pop("chat_template_kwargs", None)
                started = time.monotonic()
                try:
                    completion = self._ensure().chat.completions.create(**kwargs)
                except Exception as exc2:
                    raise LLMTransportError(
                        f"{type(exc2).__name__}: {exc2}", cause=type(exc2).__name__
                    ) from exc2
            else:
                name = type(exc).__name__
                raise LLMTransportError(
                    f"{name}: {exc}", cause=name,
                    kind="stalled" if "Timeout" in name else "unavailable",
                ) from exc
        elapsed = time.monotonic() - started
        provenance = _provenance_from(completion)

        choice = completion.choices[0]
        message = choice.message
        prompt, completion_tokens, cached = _usage_from(completion)
        answer, reasoning = split_reasoning(message.content or "")
        reasoning += getattr(message, "reasoning_content", None) or ""

        return ChatResponse(
            content=answer,
            tool_calls=[ToolCall.from_openai(c) for c in (message.tool_calls or [])],
            finish_reason=choice.finish_reason or "stop",
            stats=CallStats(
                total_s=elapsed,
                ttft_s=None,
                prompt_tokens=prompt,
                completion_tokens=completion_tokens,
                cached_tokens=cached,
                streamed=False,
                thinking_detected=bool(reasoning),
                thinking_chars=len(reasoning),
                thinking_requested=self.config.thinking,
                thinking_control_accepted=self._chat_template_kwargs_supported,
                **provenance,
            ),
        )

    def _chat_streaming(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None,
        max_tokens: int | None,
    ) -> ChatResponse:
        kwargs = self._kwargs(messages, tools, max_tokens)
        kwargs["stream"] = True
        if self._usage_in_stream_supported:
            kwargs["stream_options"] = {"include_usage": True}

        started = time.monotonic()
        try:
            stream = self._ensure().chat.completions.create(**kwargs)
        except Exception as exc:
            message = str(exc)
            retried = False
            if self._usage_in_stream_supported and "stream_options" in message:
                # Older servers reject the field outright. Remember and retry once.
                self._usage_in_stream_supported = False
                kwargs.pop("stream_options", None)
                retried = True
            if self._chat_template_kwargs_supported is not False and "chat_template_kwargs" in message:
                # Retry without it, but never pretend the request was honoured.
                self._chat_template_kwargs_supported = False
                kwargs["extra_body"].pop("chat_template_kwargs", None)
                retried = True
            if not retried:
                raise LLMTransportError(
                    f"{type(exc).__name__}: {exc}", cause=type(exc).__name__
                ) from exc
            started = time.monotonic()
            try:
                stream = self._ensure().chat.completions.create(**kwargs)
            except Exception as exc2:
                raise LLMTransportError(
                    f"{type(exc2).__name__}: {exc2}", cause=type(exc2).__name__
                ) from exc2

        ttft: float | None = None
        cached: int | None = None
        content_parts: list[str] = []
        reasoning_parts: list[str] = []
        # Tool calls arrive as deltas keyed by index, with arguments in fragments.
        partial: dict[int, dict[str, str]] = {}
        finish_reason = "stop"
        prompt = completion_tokens = 0
        chunk_count = 0

        provenance = _provenance_from(None)
        try:
            chunks = iter(stream)
        except Exception as exc:  # pragma: no cover - defensive
            raise LLMTransportError(f"{type(exc).__name__}: {exc}", cause=type(exc).__name__) from exc

        deadline = self.config.request_deadline_s
        stall_tok_s = self.config.stall_tok_s
        stall_window = self.config.stall_window_s

        def _abort_stalled(reason: str) -> None:
            # Close the response so the server sees the disconnect and cancels
            # the task, then report it as a stall, not as unavailability.
            try:
                close = getattr(stream, "close", None)
                if callable(close):
                    close()
            except Exception:  # pragma: no cover - best effort
                pass
            raise LLMTransportError(reason, cause="stall", kind="stalled")

        while True:
            now = time.monotonic()
            if now - started > deadline:
                _abort_stalled(
                    f"request exceeded deadline of {deadline:.0f}s "
                    f"({chunk_count} chunks received)"
                )
            if ttft is not None and chunk_count > 1:
                decoding_for = now - started - ttft
                if decoding_for >= stall_window:
                    rate = (chunk_count - 1) / decoding_for
                    if rate < stall_tok_s:
                        _abort_stalled(
                            f"decode stalled: {rate:.2f} tok/s over {decoding_for:.0f}s "
                            f"(floor {stall_tok_s} tok/s over {stall_window:.0f}s)"
                        )
            try:
                chunk = next(chunks)
            except StopIteration:
                break
            except Exception as exc:
                # The server died mid-stream, or went silent past read_timeout.
                # What we have so far is not an answer, and pretending otherwise
                # would grade a half-response.
                name = type(exc).__name__
                kind = "stalled" if "Timeout" in name else "unavailable"
                raise LLMTransportError(
                    f"stream aborted: {name}: {exc}", cause=name, kind=kind
                ) from exc

            found = _provenance_from(chunk)
            for key, value in found.items():
                if value is not None:
                    provenance[key] = value

            usage_prompt, usage_completion, usage_cached = _usage_from(chunk)
            if usage_prompt or usage_completion:
                prompt = usage_prompt or prompt
                completion_tokens = usage_completion or completion_tokens
                if usage_cached is not None:
                    cached = usage_cached

            if not chunk.choices:
                continue
            choice = chunk.choices[0]
            delta = getattr(choice, "delta", None)
            if choice.finish_reason:
                finish_reason = choice.finish_reason
            if delta is None:
                continue

            produced = False
            if getattr(delta, "reasoning_content", None):
                reasoning_parts.append(delta.reasoning_content)
                produced = True
            if getattr(delta, "content", None):
                content_parts.append(delta.content)
                produced = True
            for call in getattr(delta, "tool_calls", None) or []:
                index = getattr(call, "index", 0) or 0
                slot = partial.setdefault(index, {"id": "", "name": "", "arguments": ""})
                if getattr(call, "id", None):
                    slot["id"] = call.id
                fn = getattr(call, "function", None)
                if fn is not None:
                    if getattr(fn, "name", None):
                        slot["name"] = fn.name
                    if getattr(fn, "arguments", None):
                        slot["arguments"] += fn.arguments
                produced = True

            if produced:
                chunk_count += 1
                if ttft is None:
                    ttft = time.monotonic() - started

        elapsed = time.monotonic() - started
        content, inline_reasoning = split_reasoning("".join(content_parts))
        reasoning = "".join(reasoning_parts) + inline_reasoning

        if not completion_tokens:
            # No usage reported. Chunk count is a floor, not a measurement, so
            # mark it rather than pretending it is a token count.
            completion_tokens = chunk_count

        calls = [
            ToolCall.from_parts(
                slot["id"] or f"call_{index}", slot["name"], slot["arguments"] or "{}"
            )
            for index, slot in sorted(partial.items())
            if slot["name"]
        ]

        return ChatResponse(
            content=content,
            tool_calls=calls,
            finish_reason=finish_reason,
            stats=CallStats(
                total_s=elapsed,
                ttft_s=ttft,
                prompt_tokens=prompt,
                completion_tokens=completion_tokens,
                cached_tokens=cached,
                streamed=True,
                thinking_detected=bool(reasoning),
                thinking_chars=len(reasoning),
                thinking_requested=self.config.thinking,
                thinking_control_accepted=self._chat_template_kwargs_supported,
                **provenance,
            ),
        )

    def as_unary(self) -> "OpenAICompatibleClient":
        """A twin of this client with streaming off, for qualification only."""
        from dataclasses import replace

        twin = OpenAICompatibleClient(replace(self.config, stream=False))
        twin._client = self._client
        return twin

    def identity(self) -> dict[str, Any]:
        """What is behind this endpoint. Telemetry only, never orchestration."""
        from ..provenance import package_identity
        return {**self.config.identity(), **sdk_identity(), **package_identity()}

    def probe(self) -> dict[str, Any]:
        """Confirm the server is up and report which models it serves."""
        client = self._ensure()
        models = [m.id for m in client.models.list().data]
        return {
            "base_url": self.config.base_url,
            "device_note": self.config.device_note,
            "configured_model": self.config.model,
            "served_models": models,
            "configured_model_present": self.config.model in models,
        }


Turn = Callable[[list[dict[str, Any]]], ChatResponse] | ChatResponse


class ScriptedClient:
    """Replays a fixed sequence of responses. No network, no model, no excuses."""

    def __init__(self, turns: Iterable[Turn]) -> None:
        self._turns = list(turns)
        self.calls: list[list[dict[str, Any]]] = []
        self.tool_schemas: list[list[dict[str, Any]]] = []

    def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        max_tokens: int | None = None,
    ) -> ChatResponse:
        self.calls.append([dict(m) for m in messages])
        self.tool_schemas.append(list(tools or []))
        if not self._turns:
            return ChatResponse(content="(scripted client exhausted)")
        turn = self._turns.pop(0)
        return turn(messages) if callable(turn) else turn


def tool_call(name: str, arguments: dict[str, Any], call_id: str = "call_1") -> ToolCall:
    return ToolCall(
        id=call_id, name=name, arguments=arguments, raw_arguments=json.dumps(arguments)
    )


def build_client(config: ModelConfig) -> LLMClient:
    return OpenAICompatibleClient(config)
