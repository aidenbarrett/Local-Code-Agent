#!/usr/bin/env python3
"""Protocol qualification: the first thing to run once a model server exists.

Not the eval suite. A nasty little conformance pass that checks every
assumption this project makes about the server, one at a time, so that when the
real suite misbehaves you already know which assumptions held.

    python devtools/qualify.py --profile ptl-npu-8b
    python devtools/qualify.py --profile ptl-gpu-30b --json qualify-gpu.json

Each check reports PASS, FAIL or SKIP with the observed value. Nothing here is
graded on model intelligence. It is all protocol and plumbing.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import random
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))

from local_agent.config import MODEL_PRESETS, ModelConfig, load_repo_config  # noqa: E402
from local_agent.llm.client import OpenAICompatibleClient, sdk_identity  # noqa: E402
from local_agent.provenance import package_identity  # noqa: E402
from local_agent.tools import build_registry  # noqa: E402

PASS, FAIL, SKIP, WARN = "PASS", "FAIL", "SKIP", "WARN"


@dataclass
class Check:
    name: str
    status: str
    detail: str
    data: dict[str, Any] = field(default_factory=dict)


@dataclass
class Qualification:
    checks: list[Check] = field(default_factory=list)

    def add(self, name: str, status: str, detail: str, **data: Any) -> Check:
        check = Check(name, status, detail, data)
        self.checks.append(check)
        mark = {PASS: "  ok ", FAIL: "FAIL ", SKIP: "skip ", WARN: "warn "}[status]
        print(f"{mark} {name:38s} {detail}")
        return check

    @property
    def failed(self) -> list[Check]:
        return [c for c in self.checks if c.status == FAIL]


def _sha(obj: Any) -> str:
    return hashlib.sha256(
        json.dumps(obj, sort_keys=True, default=str).encode()
    ).hexdigest()[:16]


def _filler(rng: random.Random, chars: int) -> str:
    words = "void int const auto template typename struct class buffer ring push".split()
    out: list[str] = []
    length = 0
    while length < chars:
        w = rng.choice(words)
        out.append(w)
        length += len(w) + 1
    return " ".join(out)


def _cache_verdict(cold: Any, hot: Any, hard: bool,
                   shared_prefix_tokens: int = 0) -> tuple[str, str, dict[str, Any]]:
    """Did the second, identical request reuse the first one's prefix?

    Primary evidence is the server's own cached_tokens when it reports them:
    the cold request should have reused nothing BEYOND the header it shares
    with everything else, the warm one almost everything (the ceiling is
    prompt_tokens - 1; the server always evaluates at least one token).
    Wall-clock speedup is supporting evidence only. An early version used
    speedup alone and passed at 1.6x on a prompt that was already warm from
    the previous qualification run.

    `shared_prefix_tokens` is the length of the chat template plus the tool
    schemas, measured immediately before, and it is the honest floor for the
    cold leg. Demanding zero there was wrong: with tools rendered into the
    system block, every request in the run shares those ~740 tokens, so on a
    server that has already answered one tool call the "cold" probe is
    partially warm by construction and the check fails for a reason that says
    nothing about the server. What must be cold is the part under test, the
    filler, and that is what is asserted here.
    """
    fail = FAIL if hard else WARN
    data: dict[str, Any] = {}
    speedup = None
    if cold.stats.ttft_s and hot.stats.ttft_s:
        speedup = cold.stats.ttft_s / hot.stats.ttft_s
        data["speedup"] = round(speedup, 2)
        timing = f"{cold.stats.ttft_s:.2f}s cold vs {hot.stats.ttft_s:.2f}s warm ({speedup:.1f}x)"
    else:
        timing = "no TTFT available"

    if cold.stats.cache_reported and hot.stats.cache_reported and hot.stats.prompt_tokens:
        hot_frac = (hot.stats.cached_tokens or 0) / hot.stats.prompt_tokens
        # Anything the cold leg may legitimately have cached: the shared header,
        # plus a tokenisation margin, plus a floor for servers with no tools in
        # play at all.
        allowance = max(shared_prefix_tokens + 64,
                        int(0.10 * max(1, cold.stats.prompt_tokens)))
        cold_beyond = (cold.stats.cached_tokens or 0) - shared_prefix_tokens
        data.update(cold_cached=cold.stats.cached_tokens, hot_cached=hot.stats.cached_tokens,
                    prompt_tokens=hot.stats.prompt_tokens,
                    shared_prefix_tokens=shared_prefix_tokens,
                    cold_cached_beyond_prefix=max(0, cold_beyond))
        counts = (f"cached {cold.stats.cached_tokens}/{cold.stats.prompt_tokens} cold "
                  f"(header {shared_prefix_tokens}), "
                  f"{hot.stats.cached_tokens}/{hot.stats.prompt_tokens} warm")
        if (cold.stats.cached_tokens or 0) > allowance:
            # The body under test was already cached. That is a probe defect,
            # and the timing number beside it is meaningless.
            return fail, f"cold probe body was already cached ({counts}); {timing}", data
        if hot_frac < 0.90:
            return fail, f"prefix not reused ({counts}); {timing}", data
        status = PASS if (speedup is None or speedup > 1.5) else WARN
        return status, f"{counts}; {timing}", data

    # No cached_tokens from this server: timing is all we have, and we say so.
    if speedup is None:
        return SKIP, "server reports neither cached_tokens nor TTFT", data
    return (PASS if speedup > 1.5 else fail), f"{timing}; cached_tokens not reported, timing only", data


def run_qualification(
    client: Any,
    cfg: ModelConfig,
    tool_schemas: list[dict[str, Any]],
    context_probe_tokens: list[int],
) -> Qualification:
    q = Qualification()
    rng = random.Random(20260905)
    # Every probe that must be cold carries this, so a server that kept the
    # previous qualification run's prompts in its cache cannot serve them warm.
    # The repeated (warm) request in each pair uses the identical string.
    nonce = f"[qualification {time.time_ns():x}] "

    # 1. reachability -------------------------------------------------------
    try:
        info = client.probe()
        served = ", ".join(info["served_models"]) or "none"
        q.add(
            "server reachable",
            PASS if info["configured_model_present"] else WARN,
            f"serves {served}",
            served=info["served_models"],
        )
    except Exception as exc:
        q.add("server reachable", FAIL, f"{type(exc).__name__}: {exc}")
        return q

    # 2. unary, then streaming ---------------------------------------------
    warm = client.chat(
        [{"role": "user", "content": "Reply with the single word: ready."}],
        tools=None, max_tokens=8,
    )
    q.add("warm-up call", PASS, f"{warm.stats.total_s:.1f}s (discarded)")

    # Unary first: if this fails, streaming will too and the cause is simpler.
    unary_client = getattr(client, "as_unary", None)
    if callable(unary_client):
        try:
            unary = unary_client().chat(
                [{"role": "user", "content": "Reply with the single word: ready."}],
                tools=None, max_tokens=16,
            )
            q.add("unary chat", PASS if unary.content else FAIL,
                  f"{unary.stats.total_s:.1f}s, {unary.content.strip()[:30]!r}")
        except Exception as exc:
            q.add("unary chat", FAIL, f"{type(exc).__name__}: {exc}")
    else:
        q.add("unary chat", SKIP, "client does not expose a non-streaming mode")

    plain = client.chat(
        [{"role": "user", "content": "Count from one to twenty, comma separated."}],
        tools=None, max_tokens=96,
    )
    if plain.stats.streamed and plain.stats.ttft_s is not None:
        q.add(
            "streaming gives TTFT", PASS,
            f"ttft {plain.stats.ttft_s:.2f}s, decode "
            f"{plain.stats.decode_tok_s or 0:.1f} tok/s",
            ttft_s=plain.stats.ttft_s,
        )
    else:
        q.add(
            "streaming gives TTFT", FAIL,
            "no time to first token; prefill and decode cannot be separated",
        )

    # 2b. thinking mode ----------------------------------------------------
    # A cheap tier that narrates its way through a one-line instruction is not
    # cheap. This is part of configuration identity, not an incidental detail:
    # comparing an 8B with thinking on against a 30B with thinking off and
    # blaming the model would be a rotten mistake.
    terse = client.chat(
        [{"role": "user", "content": "Reply with exactly: READY"}],
        tools=None, max_tokens=256,
    )
    requested = terse.stats.thinking_requested
    accepted = terse.stats.thinking_control_accepted
    effective = terse.stats.thinking_effective

    # Whether the server took the control at all is a separate fact from what
    # the model then did. A silent compatibility retry is fine operationally;
    # reporting the requested policy as the effective one would poison every
    # latency, token and energy comparison downstream.
    if requested is None:
        q.add("thinking control accepted", SKIP,
              "profile requests no thinking policy; server default in force")
    elif accepted is False:
        q.add(
            "thinking control accepted", WARN,
            "server rejected chat_template_kwargs; the request was retried "
            f"without it, so thinking={requested} was never actually applied",
            thinking_requested=requested, thinking_control_accepted=False,
        )
    elif accepted is True:
        q.add("thinking control accepted", PASS,
              f"server accepted chat_template_kwargs (enable_thinking={requested})",
              thinking_requested=requested, thinking_control_accepted=True)
    else:
        q.add("thinking control accepted", SKIP, "control never sent")

    if requested is not None and effective != "unknown" and (
        effective == ("on" if requested else "off")
    ):
        q.add(
            "thinking policy honoured", PASS,
            f"requested {'on' if requested else 'off'}, observed {effective}",
            thinking_requested=requested, thinking_effective=effective,
        )
    elif requested is not None and effective == "unknown":
        q.add(
            "thinking policy honoured", SKIP,
            "nothing observable came back; cannot say whether the policy held",
            thinking_requested=requested, thinking_effective=effective,
        )
    elif requested is not None:
        # Asked for a policy, observably did not get it. Every number produced
        # under this configuration is measuring something other than what the
        # identity block claims.
        q.add(
            "thinking policy honoured", FAIL,
            f"requested {'on' if requested else 'off'}, observed {effective}"
            + (f" ({terse.stats.thinking_chars} chars of reasoning)"
               if terse.stats.thinking_detected else "")
            + ("; the server rejected the control" if accepted is False
               else "; the server accepted the control and ignored it"),
            thinking_requested=requested,
            thinking_control_accepted=accepted,
            thinking_effective=effective,
            thinking_chars=terse.stats.thinking_chars,
        )
    else:
        q.add(
            "thinking policy honoured", SKIP,
            f"no policy requested; observed {effective}",
            thinking_effective=effective,
        )

    if terse.stats.thinking_detected:
        q.add(
            "thinking mode", WARN,
            f"model reasoned for {terse.stats.thinking_chars} chars before a "
            "one-word answer; cheap-tier latency will carry that cost",
            thinking_chars=terse.stats.thinking_chars,
        )
    else:
        q.add("thinking mode", PASS,
              f"no reasoning emitted (config: thinking={cfg.thinking})")
    q.add(
        "terse instruction followed",
        PASS if "ready" in (terse.content or "").lower() else WARN,
        f"answered {terse.content.strip()[:40]!r}",
    )

    # 2c. runtime provenance and sampler --------------------------------------
    # Every response from llama.cpp carries system_fingerprint; a profile that
    # names a runtime_version gets it checked on every call from now on. Here we
    # check once and say what we saw.
    observed_fp = plain.stats.system_fingerprint
    claimed = cfg.runtime_version or None
    if observed_fp and claimed:
        q.add(
            "runtime fingerprint matches profile",
            PASS if observed_fp == claimed else FAIL,
            f"server {observed_fp}, profile {claimed}",
            observed=observed_fp, claimed=claimed,
        )
    elif observed_fp:
        q.add("runtime fingerprint matches profile", WARN,
              f"server reports {observed_fp}; profile records no runtime_version, "
              "so nothing can be compared",
              observed=observed_fp)
    else:
        q.add("runtime fingerprint matches profile", SKIP,
              "server does not report system_fingerprint"
              + (f"; profile claims {claimed}" if claimed else ""))

    if plain.stats.server_prompt_ms is not None:
        q.add("server-side timings reported", PASS,
              f"prompt {plain.stats.server_prompt_ms:.0f} ms, "
              f"predicted {plain.stats.server_predicted_ms or 0:.0f} ms, "
              f"cache_n {plain.stats.server_cache_n}")
    else:
        q.add("server-side timings reported", SKIP,
              "no timings block; prefill/decode come from client-side TTFT only")

    # The sampler travels in extra_body on every request. If the server had
    # rejected top_k or min_p the warm-up would have been a transport error, so
    # reaching this line is the acceptance; record what was sent.
    q.add("sampler sent explicitly", PASS,
          f"temperature {cfg.temperature}, top_k {cfg.top_k}, top_p {cfg.top_p}, "
          f"min_p {cfg.min_p} (in identity)",
          temperature=cfg.temperature, top_k=cfg.top_k, top_p=cfg.top_p, min_p=cfg.min_p)

    # 3. usage fields -------------------------------------------------------
    if plain.stats.prompt_tokens and plain.stats.completion_tokens:
        q.add(
            "usage reported", PASS,
            f"prompt {plain.stats.prompt_tokens}, completion "
            f"{plain.stats.completion_tokens}",
        )
    else:
        q.add(
            "usage reported", FAIL,
            "no prompt/completion token counts; all token-derived rates are dead",
        )

    if plain.stats.cache_reported:
        q.add("cached_tokens reported", PASS,
              f"{plain.stats.cached_tokens} cached", cached=plain.stats.cached_tokens)
    else:
        q.add(
            "cached_tokens reported", SKIP,
            "server does not report it; cache measured by cold/warm TTFT instead",
        )

    # 4. one tool call round trip ------------------------------------------
    if tool_schemas:
        tool_reply = client.chat(
            [
                {"role": "system", "content": "Use a tool. Do not answer in prose."},
                {"role": "user", "content": "What is the current git status of this repository?"},
            ],
            tools=tool_schemas, max_tokens=256,
        )
        if tool_reply.tool_calls:
            call = tool_reply.tool_calls[0]
            valid = isinstance(call.arguments, dict)
            q.add(
                "tool call round trip (streaming)",
                PASS if valid else FAIL,
                f"called {call.name} with {call.arguments}",
                tool=call.name,
            )
        else:
            # An agent-capable profile that cannot call a tool is a failure, not
            # an unsupported optional feature. Keep the raw material so it can be
            # reproduced with curl, outside this code, before anything is blamed.
            q.add(
                "tool call round trip (streaming)", FAIL,
                "model answered in prose with tools available; check the tool "
                "parser and the chat template",
                raw_request={
                    "messages": [
                        {"role": "system", "content": "Use a tool. Do not answer in prose."},
                        {"role": "user", "content":
                         "What is the current git status of this repository?"},
                    ],
                    "tools": tool_schemas,
                },
                raw_response={
                    "content": tool_reply.content,
                    "finish_reason": tool_reply.finish_reason,
                },
            )

        # 5. structured finish ---------------------------------------------
        finish = client.chat(
            [
                {"role": "system", "content":
                 "You have finished. Call submit_answer with claim=diagnosis and a "
                 "one sentence summary. Cite no evidence."},
                {"role": "user", "content": "Report your conclusion."},
            ],
            tools=[s for s in tool_schemas if s["function"]["name"] == "submit_answer"],
            max_tokens=256,
        )
        submitted = [c for c in finish.tool_calls if c.name == "submit_answer"]
        if submitted and submitted[0].arguments.get("claim"):
            q.add("structured finish", PASS,
                  f"claim={submitted[0].arguments.get('claim')}")
        else:
            q.add(
                "structured finish", WARN,
                "did not call submit_answer; the run will fall back to prose and "
                "claims will not be checkable",
            )

    # 6. prefix cache, without and with tools ------------------------------
    def _header_tokens(tools: Any) -> int:
        """How long the part every request shares is, right now.

        The chat template and, when tools are sent, the rendered tool schemas
        sit in front of every prompt. A cold probe cannot avoid sharing them
        with whatever ran before it, so measure them instead of pretending
        they are not there. The probe is unique, so its own prompt length is
        an upper bound on what the next request can share with it.
        """
        probe = client.chat(
            [{"role": "user", "content": f"{nonce}-header-{rng.random()}"}],
            tools=tools, max_tokens=1,
        )
        return int(probe.stats.prompt_tokens or 0)

    shared = nonce + _filler(rng, 12_000) + "\n\nAnswer with the single word: ok."
    header = _header_tokens(None)
    cold = client.chat([{"role": "user", "content": shared}], tools=None, max_tokens=8)
    hot = client.chat([{"role": "user", "content": shared}], tools=None, max_tokens=8)
    status, detail, data = _cache_verdict(cold, hot, hard=False,
                                          shared_prefix_tokens=header)
    q.add("prefix cache, no tools", status, detail, **data)

    if tool_schemas:
        # The real question: does re-sending the tool schema break the prefix?
        # A fresh filler, so this pair cannot borrow the previous pair's prefix.
        shared_t = nonce + _filler(rng, 12_000) + "\n\nAnswer with the single word: ok."
        header_t = _header_tokens(tool_schemas)
        cold_t = client.chat([{"role": "user", "content": shared_t}],
                             tools=tool_schemas, max_tokens=8)
        hot_t = client.chat([{"role": "user", "content": shared_t}],
                            tools=tool_schemas, max_tokens=8)
        status, detail, data = _cache_verdict(cold_t, hot_t, hard=True,
                                              shared_prefix_tokens=header_t)
        q.add("prefix cache, with tools", status, detail, **data)

        # And is our own serialisation stable? If this ever varies, the prefix
        # breaks every turn and it is our bug, not the server's.
        hashes = {_sha(tool_schemas) for _ in range(5)}
        q.add(
            "tool schema serialisation stable",
            PASS if len(hashes) == 1 else FAIL,
            f"sha {next(iter(hashes))}" if len(hashes) == 1
            else f"{len(hashes)} different serialisations",
        )

    # 7. context validity sweep --------------------------------------------
    # The NPU pipeline is static shape and overrunning gives garbage, not an
    # error, so ask a question with a known answer at each size and check it.
    for target in context_probe_tokens:
        body = _filler(rng, target * 4)
        prompt = (
            f"{nonce}{body}\n\nIgnore everything above. It is filler. "
            "Reply with exactly the word: pomegranate"
        )
        try:
            reply = client.chat([{"role": "user", "content": prompt}],
                                tools=None, max_tokens=16)
        except Exception as exc:
            q.add(f"context {target} tokens", FAIL, f"{type(exc).__name__}: {exc}")
            continue
        text = (reply.content or "").strip().lower()
        ok = "pomegranate" in text
        q.add(
            f"context {target} tokens",
            PASS if ok else FAIL,
            (f"coherent ({reply.stats.prompt_tokens} prompt tokens)" if ok
             else f"GARBLED: {text[:60]!r} at {reply.stats.prompt_tokens} prompt tokens"),
            prompt_tokens=reply.stats.prompt_tokens,
        )
        if not ok:
            break  # no point probing further once it has started producing rubbish

    return q


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--profile", choices=sorted(MODEL_PRESETS))
    parser.add_argument("--base-url")
    parser.add_argument("--model")
    parser.add_argument("--repo", default=str(REPO / "fixtures" / "cpp_sandbox"))
    parser.add_argument("--context-probes", default="1000,4000,8000,12000,15000")
    parser.add_argument("--json", dest="json_out")
    parser.add_argument(
        "--dump-dir",
        help="write the raw request and response of any failed check here, so it "
             "can be reproduced with curl before anything gets blamed",
    )
    args = parser.parse_args()

    cfg = MODEL_PRESETS.get(args.profile, ModelConfig()) if args.profile else ModelConfig.from_env()
    if args.base_url:
        cfg = ModelConfig(**{**cfg.__dict__, "base_url": args.base_url})
    if args.model:
        cfg = ModelConfig(**{**cfg.__dict__, "model": args.model})
    cfg = ModelConfig(**{**cfg.__dict__, "stream": True})

    registry, _, _ = build_registry(load_repo_config(Path(args.repo)))
    schemas = registry.schemas(
        ["git_status", "read_file", "build_target", "submit_answer"]
    )

    probes = [int(x) for x in args.context_probes.split(",") if x.strip()]
    probes = [p for p in probes if p <= cfg.context_budget_tokens + 2000]

    print(f"qualifying {cfg.model} on {cfg.device_note} via {cfg.base_url}")
    for key, value in {**cfg.identity(), **sdk_identity(), **package_identity()}.items():
        print(f"  {key:24s} {value}")
    print(f"  context probes           {probes}\n")

    q = run_qualification(OpenAICompatibleClient(cfg), cfg, schemas, probes)

    print()
    if q.failed:
        print(f"{len(q.failed)} check(s) FAILED. Fix these before running the suite:")
        for check in q.failed:
            print(f"  - {check.name}: {check.detail}")
    else:
        print("all checks passed. run_suite.py is safe to trust.")

    if args.dump_dir:
        dump = Path(args.dump_dir)
        dump.mkdir(parents=True, exist_ok=True)
        for check in q.failed:
            if not check.data:
                continue
            slug = check.name.replace(" ", "-").replace("/", "-")
            (dump / f"{slug}.json").write_text(
                json.dumps({"check": check.name, "detail": check.detail, **check.data},
                           indent=2, default=str),
                encoding="utf-8",
            )
        print(f"raw material for failed checks: {dump}")

    if args.json_out:
        Path(args.json_out).write_text(
            json.dumps(
                {
                    "endpoint": cfg.base_url,
                    "identity": {**cfg.identity(), **sdk_identity(), **package_identity()},
                    "checks": [
                        {"name": c.name, "status": c.status, "detail": c.detail, **c.data}
                        for c in q.checks
                    ],
                },
                indent=2,
            ),
            encoding="utf-8",
        )
        print(f"written: {args.json_out}")

    return 1 if q.failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
