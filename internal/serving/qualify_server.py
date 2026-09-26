#!/usr/bin/env python3
"""Deterministic protocol conformance checks for a configured model endpoint.

This is product bring-up, not a benchmark. It checks transport/model identity,
streaming, a schema-constrained tool call and bounded context probes. Results are
PASS/FAIL/SKIP observations written as JSON when requested.
"""
from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass, field, replace
import json
from pathlib import Path
import sys
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from local_agent.config import MODEL_PRESETS  # noqa: E402
from local_agent.llm.client import OpenAICompatibleClient  # noqa: E402

PASS = "PASS"
FAIL = "FAIL"
SKIP = "SKIP"


@dataclass
class Check:
    name: str
    status: str
    detail: str
    data: dict[str, Any] = field(default_factory=dict)


@dataclass
class Report:
    profile: str
    model: str
    base_url: str
    checks: list[Check] = field(default_factory=list)

    def add(self, name: str, status: str, detail: str, **data: Any) -> None:
        self.checks.append(Check(name, status, detail, data))
        print(f"{status:4s} {name}: {detail}")

    @property
    def failed(self) -> bool:
        return any(check.status == FAIL for check in self.checks)


def _probe_context(client: OpenAICompatibleClient, tokens: int) -> tuple[str, str, dict[str, Any]]:
    # This is deliberately an approximate input-size probe. The endpoint's usage
    # object is retained as the observed token count when it is available.
    prompt = "x " * tokens
    try:
        reply = client.chat(
            [{"role": "user", "content": prompt}],
            tools=None,
            max_tokens=1,
        )
    except Exception as exc:
        return FAIL, f"request rejected: {type(exc).__name__}: {exc}", {}
    observed = reply.stats.prompt_tokens
    return PASS, "request accepted", {"requested_approx_tokens": tokens, "observed_prompt_tokens": observed}


def run(profile: str, base_url: str | None, model: str | None, probes: list[int]) -> Report:
    if profile not in MODEL_PRESETS:
        raise ValueError(f"unknown profile: {profile}")
    config = MODEL_PRESETS[profile]
    if base_url:
        config = replace(config, base_url=base_url.rstrip("/") + "/")
    if model:
        config = replace(config, model=model)

    report = Report(profile=profile, model=config.model, base_url=config.base_url)
    client = OpenAICompatibleClient(config)

    try:
        info = client.probe()
        served = list(info.get("served_models") or [])
        present = bool(info.get("configured_model_present"))
        report.add(
            "configured model present",
            PASS if present else FAIL,
            ", ".join(served) if served else "endpoint returned no model ids",
            served_models=served,
        )
    except Exception as exc:
        report.add("endpoint reachable", FAIL, f"{type(exc).__name__}: {exc}")
        return report

    try:
        reply = client.chat(
            [{"role": "user", "content": "Reply with exactly READY"}],
            tools=None,
            max_tokens=16,
        )
        report.add(
            "plain chat",
            PASS if (reply.content or "").strip() else FAIL,
            f"ttft={reply.stats.ttft_s!r}, decode_tps={reply.stats.decode_tok_s!r}",
            ttft_s=reply.stats.ttft_s,
            decode_tokens_per_second=reply.stats.decode_tok_s,
        )
    except Exception as exc:
        report.add("plain chat", FAIL, f"{type(exc).__name__}: {exc}")

    tool = {
        "type": "function",
        "function": {
            "name": "echo_value",
            "description": "Return one supplied string.",
            "parameters": {
                "type": "object",
                "properties": {"value": {"type": "string"}},
                "required": ["value"],
                "additionalProperties": False,
            },
        },
    }
    try:
        reply = client.chat(
            [{"role": "user", "content": "Call echo_value with value READY. Do not answer directly."}],
            tools=[tool],
            max_tokens=128,
        )
        calls = list(reply.tool_calls or [])
        valid = any(str(call.name) == "echo_value" and call.arguments.get("value") == "READY" for call in calls)
        report.add(
            "schema constrained tool call",
            PASS if valid else FAIL,
            f"observed {len(calls)} tool call(s)",
        )
    except Exception as exc:
        report.add("schema constrained tool call", FAIL, f"{type(exc).__name__}: {exc}")

    for target in probes:
        status, detail, data = _probe_context(client, target)
        report.add(f"context probe ~{target}", status, detail, **data)
        if status == FAIL:
            break

    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", required=True, choices=sorted(MODEL_PRESETS))
    parser.add_argument("--base-url")
    parser.add_argument("--model")
    parser.add_argument("--context-probes", default="1000,4000,7000")
    parser.add_argument("--json", dest="json_path", type=Path)
    parser.add_argument("--dump-dir", type=Path, help="retained for command compatibility; failures are reported in JSON")
    args = parser.parse_args(argv)

    probes = [int(value.strip()) for value in args.context_probes.split(",") if value.strip()]
    if any(value <= 0 for value in probes):
        parser.error("context probes must be positive")

    report = run(args.profile, args.base_url, args.model, probes)
    if args.json_path:
        args.json_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "schema_version": 1,
            "profile": report.profile,
            "model": report.model,
            "base_url": report.base_url,
            "checks": [asdict(check) for check in report.checks],
            "failed": report.failed,
        }
        args.json_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return 1 if report.failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
