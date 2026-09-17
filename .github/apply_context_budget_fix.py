from pathlib import Path
import re


def replace_once(path: str, old: str, new: str) -> None:
    p = Path(path)
    text = p.read_text(encoding="utf-8")
    count = text.count(old)
    if count != 1:
        raise SystemExit(f"{path}: expected one occurrence, found {count}")
    p.write_text(text.replace(old, new), encoding="utf-8")


# Context budgeting must include schema/chat-template overhead, not only messages.
path = Path("internal/local_agent/agent/context.py")
text = path.read_text(encoding="utf-8")
replace_old = """    budget_tokens: int = 12_000
    keep_recent_tool_results: int = 6
    hysteresis: float = 0.55
    messages: list[dict[str, Any]] = field(default_factory=list)
    compactions: list[CompactionEvent] = field(default_factory=list)
"""
replace_new = """    # Total request budget, not just visible message text. Tool schemas and
    # chat-template framing consume prompt tokens too.
    budget_tokens: int = 12_000
    keep_recent_tool_results: int = 6
    hysteresis: float = 0.55
    request_overhead_tokens: int = 0
    calibration_margin_tokens: int = 128
    messages: list[dict[str, Any]] = field(default_factory=list)
    compactions: list[CompactionEvent] = field(default_factory=list)
"""
if text.count(replace_old) != 1:
    raise SystemExit("context fields did not match")
text = text.replace(replace_old, replace_new)

replace_old = """    @property
    def tokens(self) -> int:
        return approximate_tokens(self.messages)

    @property
    def stable_prefix_len(self) -> int:
"""
replace_new = """    @property
    def tokens(self) -> int:
        return approximate_tokens(self.messages)

    @property
    def estimated_request_tokens(self) -> int:
        return self.tokens + self.request_overhead_tokens

    def set_tool_schemas(self, schemas: list[dict[str, Any]]) -> None:
        \"\"\"Reserve prompt space for schemas before the first model call.\n\n        The server's tokenizer is authoritative once it reports usage. Until then\n        a conservative serialized-size estimate prevents us pretending schemas are\n        free context.\n        \"\"\"
        if not schemas:
            return
        serialized = json.dumps(schemas, sort_keys=True, default=str)
        estimate = max(1, len(serialized) // 4) + self.calibration_margin_tokens
        self.request_overhead_tokens = max(self.request_overhead_tokens, estimate)

    def observe_prompt_tokens(self, prompt_tokens: int, message_tokens: int) -> None:
        \"\"\"Calibrate fixed request overhead from a successful server call.\"\"\"
        if prompt_tokens <= 0:
            return
        measured = max(0, prompt_tokens - message_tokens)
        self.request_overhead_tokens = max(
            self.request_overhead_tokens, measured + self.calibration_margin_tokens
        )

    @property
    def stable_prefix_len(self) -> int:
"""
if text.count(replace_old) != 1:
    raise SystemExit("context token property did not match")
text = text.replace(replace_old, replace_new)

pattern = re.compile(
    r"    def needs_compaction\(self\) -> bool:\n.*?"
    r"    def for_request\(self\) -> list\[dict\[str, Any\]\]:\n",
    re.S,
)
replacement = """    def needs_compaction(self) -> bool:
        return self.estimated_request_tokens > self.budget_tokens

    def compact(self) -> CompactionEvent | None:
        \"\"\"Collapse tool payloads until the whole request is back in budget.\n\n        Prefer old results so recent evidence survives. If that is insufficient,\n        collapse recent results too rather than sending an oversized prompt. The\n        summary/evidence handle remains and the model can re-run a narrower call.\n        \"\"\"
        before = self.estimated_request_tokens
        if before <= self.budget_tokens:
            return None

        target_total = int(self.budget_tokens * self.hysteresis)
        target_messages = max(0, target_total - self.request_overhead_tokens)
        tool_indices = [i for i, m in enumerate(self.messages) if m.get("role") == "tool"]
        old = (
            tool_indices[: -self.keep_recent_tool_results]
            if self.keep_recent_tool_results
            else tool_indices
        )
        recent = [i for i in tool_indices if i not in old]

        first_touched = len(self.messages)
        collapsed = 0
        for idx in [*old, *recent]:
            try:
                already = json.loads(self.messages[idx].get("content") or "{}")
            except (ValueError, TypeError):
                already = {}
            if isinstance(already, dict) and already.get("collapsed"):
                continue
            self.messages[idx] = _collapse(self.messages[idx])
            first_touched = min(first_touched, idx)
            collapsed += 1
            if self.tokens <= target_messages:
                break

        if not collapsed:
            return None

        event = CompactionEvent(
            at_message_index=first_touched,
            tokens_before=before,
            tokens_after=self.estimated_request_tokens,
            collapsed_results=collapsed,
        )
        self.compactions.append(event)
        return event

    def for_request(self) -> list[dict[str, Any]]:
"""
text, count = pattern.subn(replacement, text, count=1)
if count != 1:
    raise SystemExit(f"context compact block replacement count={count}")
text = text.replace(
    '            "approx_tokens": self.tokens,\n            "budget_tokens": self.budget_tokens,\n',
    '            "approx_tokens": self.tokens,\n'
    '            "request_overhead_tokens": self.request_overhead_tokens,\n'
    '            "estimated_request_tokens": self.estimated_request_tokens,\n'
    '            "budget_tokens": self.budget_tokens,\n',
)
path.write_text(text, encoding="utf-8")


# Prompt-length errors are context/controller failures, not dead servers.
path = Path("internal/local_agent/llm/client.py")
text = path.read_text(encoding="utf-8")
anchor = "def _http_stack() -> Any:\n"
helper = '''def _transport_kind(exc: Exception) -> str:
    \"\"\"Classify transport failures without calling a live server dead.\"\"\"
    name = type(exc).__name__
    message = str(exc).lower()
    if (
        "input length exceeds" in message
        or ("maximum allowed length" in message and "input" in message)
    ):
        return "context_overflow"
    if "timeout" in name.lower() or "timeout" in message:
        return "stalled"
    return "unavailable"


'''
if helper not in text:
    if anchor not in text:
        raise SystemExit("client helper anchor missing")
    text = text.replace(anchor, helper + anchor, 1)
text = text.replace(
    '                    f"{name}: {exc}", cause=name,\n'
    '                    kind="stalled" if "Timeout" in name else "unavailable",\n',
    '                    f"{name}: {exc}", cause=name, kind=_transport_kind(exc),\n',
)
text = text.replace(
    '                    f"{type(exc2).__name__}: {exc2}", cause=type(exc2).__name__\n',
    '                    f"{type(exc2).__name__}: {exc2}", cause=type(exc2).__name__,\n'
    '                    kind=_transport_kind(exc2)\n',
)
text = text.replace(
    '                    f"{type(exc).__name__}: {exc}", cause=type(exc).__name__\n'
    '                ) from exc\n'
    '            started = time.monotonic()\n',
    '                    f"{type(exc).__name__}: {exc}", cause=type(exc).__name__,\n'
    '                    kind=_transport_kind(exc)\n'
    '                ) from exc\n'
    '            started = time.monotonic()\n',
)
text = text.replace(
    '                    f"{type(exc2).__name__}: {exc2}", cause=type(exc2).__name__\n'
    '                ) from exc2\n\n'
    '        ttft: float | None = None\n',
    '                    f"{type(exc2).__name__}: {exc2}", cause=type(exc2).__name__,\n'
    '                    kind=_transport_kind(exc2)\n'
    '                ) from exc2\n\n'
    '        ttft: float | None = None\n',
)
path.write_text(text, encoding="utf-8")


replace_once(
    "internal/local_agent/agent/state.py",
    '    INFERENCE_STALLED = "inference_stalled"    # server alive, request made no progress\n',
    '    INFERENCE_STALLED = "inference_stalled"    # server alive, request made no progress\n'
    '    CONTEXT_BUDGET_EXHAUSTED = "context_budget_exhausted"  # request would exceed local model cap\n',
)


# Oversized list/search payloads must tell a small model how to retry narrowly.
path = Path("internal/local_agent/tools/base.py")
text = path.read_text(encoding="utf-8")
old = '''        trimmed = {
            "ok": self.ok,
            "summary": self.summary,
            "truncated": True,
            "note": (
                "Result exceeded the tool-result budget. Use read_log_chunk or "
                "read_file against the listed artifacts to page through it."
            ),
            "artifacts": self.artifacts,
        }
'''
new = '''        note = (
            "Result exceeded the tool-result budget. Use read_log_chunk or read_file "
            "against the listed artifacts to page through it."
            if self.artifacts
            else "Result exceeded the tool-result budget. Repeat the tool with a "
                 "smaller limit or a narrower path/pattern."
        )
        trimmed = {
            "ok": self.ok,
            "summary": self.summary,
            "truncated": True,
            "note": note,
            "artifacts": self.artifacts,
        }
'''
if text.count(old) != 1:
    raise SystemExit("base.py trim block did not match")
path.write_text(text.replace(old, new), encoding="utf-8")


# Wire model-specific result/context limits into the deterministic controller.
path = Path("internal/local_agent/agent/orchestrator.py")
text = path.read_text(encoding="utf-8")
old = '''        ctx = ContextManager(budget_tokens=budget)
        ctx.append(ctxmod.build_system_message(
'''
new = '''        ctx = ContextManager(budget_tokens=budget)
        active_config = (
            tiered.config_for() if tiered is not None
            else getattr(self.client, "config", None)
        )
        tool_result_max_bytes = self.repo.policy.max_tool_result_bytes
        if active_config is not None:
            token_cap = int(getattr(active_config, "max_tool_result_tokens", 0) or 0)
            if token_cap > 0:
                tool_result_max_bytes = min(
                    tool_result_max_bytes, max(512, token_cap * 4)
                )
        ctx.append(ctxmod.build_system_message(
'''
if text.count(old) != 1:
    raise SystemExit("orchestrator context init did not match")
text = text.replace(old, new)

old = '''        schemas = self.registry.schemas(toolset)
        self.observer("toolset", {"tools": toolset})
'''
new = '''        schemas = self.registry.schemas(toolset)
        ctx.set_tool_schemas(schemas)
        self.observer("toolset", {"tools": toolset})
'''
if text.count(old) != 1:
    raise SystemExit("orchestrator schemas block did not match")
text = text.replace(old, new)

old = '''            state.metrics.context_peak_tokens = max(
                state.metrics.context_peak_tokens, ctx.tokens
            )

            try:
                response = self.client.chat(ctx.for_request(), tools=schemas)
'''
new = '''            state.metrics.context_peak_tokens = max(
                state.metrics.context_peak_tokens, ctx.estimated_request_tokens
            )
            if ctx.estimated_request_tokens > budget:
                state.halt(
                    HaltCause.CONTEXT_BUDGET_EXHAUSTED,
                    f"context budget exhausted before inference: estimated request "
                    f"{ctx.estimated_request_tokens} tokens exceeds budget {budget}",
                )
                self.observer(
                    "context_budget_exhausted",
                    {"estimated": ctx.estimated_request_tokens, "budget": budget},
                )
                break

            message_tokens_before_request = ctx.tokens
            try:
                response = self.client.chat(ctx.for_request(), tools=schemas)
'''
if text.count(old) != 1:
    raise SystemExit("orchestrator pre-request block did not match")
text = text.replace(old, new)

old = '''                if getattr(exc, "kind", "unavailable") == "stalled":
                    state.halt(HaltCause.INFERENCE_STALLED, f"inference stalled: {exc}")
                    self.observer("server_stalled", {"error": str(exc), "cause": exc.cause})
                else:
'''
new = '''                kind = getattr(exc, "kind", "unavailable")
                if kind == "context_overflow":
                    state.halt(
                        HaltCause.CONTEXT_BUDGET_EXHAUSTED,
                        f"inference request exceeded the model context limit: {exc}",
                    )
                    self.observer(
                        "context_budget_exhausted",
                        {"error": str(exc), "cause": exc.cause},
                    )
                elif kind == "stalled":
                    state.halt(HaltCause.INFERENCE_STALLED, f"inference stalled: {exc}")
                    self.observer("server_stalled", {"error": str(exc), "cause": exc.cause})
                else:
'''
if text.count(old) != 1:
    raise SystemExit("orchestrator transport block did not match")
text = text.replace(old, new)

old = '''            state.metrics.observe_call(response.stats)
            self.observer("llm", response.stats.as_dict())
'''
new = '''            state.metrics.observe_call(response.stats)
            ctx.observe_prompt_tokens(
                response.stats.prompt_tokens, message_tokens_before_request
            )
            self.observer("llm", response.stats.as_dict())
'''
if text.count(old) != 1:
    raise SystemExit("orchestrator call metrics block did not match")
text = text.replace(old, new)

old = '                        outcome.to_json(self.repo.policy.max_tool_result_bytes),\n'
new = '                        outcome.to_json(tool_result_max_bytes),\n'
if text.count(old) != 1:
    raise SystemExit("orchestrator tool result serialization did not match")
text = text.replace(old, new)

old = '            state.metrics.context_peak_tokens, ctx.tokens\n'
new = '            state.metrics.context_peak_tokens, ctx.estimated_request_tokens\n'
if text.count(old) != 1:
    raise SystemExit("orchestrator final peak block did not match")
text = text.replace(old, new)
path.write_text(text, encoding="utf-8")


Path("internal/tests/unit/test_context_budget_guard.py").write_text(
    '''import json\n\nfrom local_agent.agent.context import ContextManager, tool_result_message\nfrom local_agent.llm.client import _transport_kind\nfrom local_agent.tools.base import ToolResult\n\n\ndef test_request_budget_includes_tool_schema_overhead_and_server_calibration():\n    ctx = ContextManager(budget_tokens=7500)\n    ctx.append({"role": "system", "content": "x" * 4000})\n    visible = ctx.tokens\n    ctx.set_tool_schemas([\n        {"type": "function", "function": {"name": "list_files", "parameters": {"type": "object", "description": "y" * 2400}}}\n    ])\n    assert ctx.estimated_request_tokens > visible\n\n    before = ctx.tokens\n    ctx.observe_prompt_tokens(before + 1700, before)\n    assert ctx.request_overhead_tokens >= 1828\n    assert ctx.estimated_request_tokens == ctx.tokens + ctx.request_overhead_tokens\n\n\ndef test_compaction_can_collapse_recent_large_tool_result_to_avoid_overflow():\n    ctx = ContextManager(budget_tokens=1800, keep_recent_tool_results=6, hysteresis=0.7)\n    ctx.set_tool_schemas([{"name": "list_files", "description": "schema" * 80}])\n    ctx.append({"role": "system", "content": "rules" * 100})\n    ctx.append({"role": "user", "content": "summarize repo"})\n    payload = json.dumps({"ok": True, "summary": "100 files", "data": {"files": [f"internal/path/{i}/file.cpp" for i in range(400)]}})\n    ctx.append(tool_result_message("call_1", "list_files", payload))\n    assert ctx.needs_compaction()\n    event = ctx.compact()\n    assert event is not None\n    assert event.collapsed_results == 1\n    assert json.loads(ctx.messages[-1]["content"])["collapsed"] is True\n    assert ctx.estimated_request_tokens < event.tokens_before\n\n\ndef test_model_tool_result_token_budget_can_drop_oversized_payload_without_artifact():\n    result = ToolResult(True, "100 files", data={"files": ["x" * 100 for _ in range(100)]})\n    rendered = json.loads(result.to_json(max_bytes=800))\n    assert rendered["truncated"] is True\n    assert rendered["artifacts"] == []\n    assert "smaller limit" in rendered["note"]\n\n\ndef test_prompt_length_bad_request_is_not_classified_as_server_unavailable():\n    exc = RuntimeError("400: Input length exceeds the maximum allowed length")\n    assert _transport_kind(exc) == "context_overflow"\n''',
    encoding="utf-8",
)
