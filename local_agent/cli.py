"""Command line entry point.

    local-agent doctor
    local-agent skills
    local-agent tools [--skill build-and-test]
    local-agent run "reproduce the ring buffer failure and explain it"
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

from .agent import Orchestrator, SkillLibrary, cli_approval, dump_transcript, format_report
from .agent.skills import default_search_path
from .agent.policy import deny_all_approvals
from .config import MODEL_PRESETS, ModelConfig, find_repo_root, load_repo_config
from .llm.client import OpenAICompatibleClient
from .llm.router import CHEAP, STRONG, build_tiered_client
from .tools import TOOLSETS, build_registry


def _model_config(args: argparse.Namespace) -> ModelConfig:
    base = MODEL_PRESETS.get(args.profile, ModelConfig()) if args.profile else ModelConfig.from_env()
    if args.base_url:
        base = ModelConfig(**{**base.__dict__, "base_url": args.base_url})
    if args.model:
        base = ModelConfig(**{**base.__dict__, "model": args.model})
    return base


def _load(args: argparse.Namespace):
    root = find_repo_root(Path(args.repo or "."))
    repo = load_repo_config(root)
    registry, ctx, store = build_registry(repo)
    skills = SkillLibrary.discover_many(default_search_path(root, repo.skills_dir))
    return repo, registry, skills


def cmd_doctor(args: argparse.Namespace) -> int:
    root = find_repo_root(Path(args.repo or "."))
    print(f"repository root : {root}")
    try:
        repo, registry, skills = _load(args)
        print(f"config          : ok ({len(repo.profiles)} profile(s), "
              f"default {repo.default_profile})")
        print(f"tools           : {len(registry.names())} registered")
        print(f"skills          : {len(skills)} discovered -> {', '.join(skills.names()) or 'none'}")
    except Exception as exc:
        print(f"config          : FAILED - {exc}")
        return 2

    cfg = _model_config(args)
    print(f"model endpoint  : {cfg.base_url} ({cfg.device_note})")
    client = OpenAICompatibleClient(cfg)
    try:
        info = client.probe()
    except Exception as exc:
        print(f"model server    : UNREACHABLE - {type(exc).__name__}: {exc}")
        print("                  start OVMS, or use --base-url to point elsewhere")
        return 1
    print(f"served models   : {', '.join(info['served_models']) or 'none'}")
    print(f"configured model: {cfg.model} "
          f"{'(present)' if info['configured_model_present'] else '(NOT SERVED)'}")
    return 0 if info["configured_model_present"] else 1


def cmd_skills(args: argparse.Namespace) -> int:
    _, _, skills = _load(args)
    if not len(skills):
        print("no skills found")
        return 1
    for name in skills.names():
        skill = skills.get(name)
        assert skill is not None
        print(f"{name}")
        print(f"  {skill.description}")
        print(f"  body: {len(skill.body.splitlines())} lines, "
              f"references: {', '.join(skill.references) or 'none'}")
    return 0


def cmd_tools(args: argparse.Namespace) -> int:
    _, registry, skills = _load(args)
    names = registry.names()
    if args.skill:
        skill = skills.get(args.skill)
        names = (skill.tools if skill and skill.tools else None) or TOOLSETS.get(
            args.skill, registry.names()
        )
    for name in names:
        if name not in registry:
            continue
        tool = registry.get(name)
        print(f"{tool.name:20s} [{tool.risk.value}] {tool.description.splitlines()[0]}")
    return 0


def cmd_route(args: argparse.Namespace) -> int:
    _, _, skills = _load(args)
    for name, score in skills.rank(args.task, top=5):
        print(f"{score:6.3f}  {name}")
    return 0


def _client_for(args: argparse.Namespace, cfg):
    """One endpoint, or a cheap/strong pair when --cheap-profile is given."""
    if not getattr(args, "cheap_profile", None):
        return OpenAICompatibleClient(cfg), None
    cheap = MODEL_PRESETS[args.cheap_profile]
    tiered = build_tiered_client({STRONG: cfg, CHEAP: cheap}, default_tier=STRONG)
    return tiered, tiered


def cmd_run(args: argparse.Namespace) -> int:
    repo, registry, skills = _load(args)
    cfg = _model_config(args)
    client, tiered = _client_for(args, cfg)
    if tiered is not None:
        for tier, description in tiered.describe().items():
            print(f"  {tier:6s} {description}", file=sys.stderr)

    def observer(event: str, payload: dict) -> None:
        if args.quiet:
            return
        if event == "tool":
            print(f"  -> {payload['name']}({json.dumps(payload['arguments'])[:160]})",
                  file=sys.stderr)
        elif event == "observe":
            mark = "ok " if payload["ok"] else "ERR"
            print(f"  <- [{mark}] {payload['summary'][:200]}", file=sys.stderr)
        elif event == "route":
            print(f"  skill: {payload['skill']} -> {payload.get('tier', 'single')} tier",
                  file=sys.stderr)
        elif event == "escalate":
            print(f"  ^^ escalating {payload['from']} to {payload['to']}: "
                  f"{payload['reason']}", file=sys.stderr)
        elif event == "llm":
            ttft = payload.get("ttft_s")
            print(
                f"  ~~ model {payload['total_s']:.1f}s"
                + (f" ttft {ttft:.2f}s" if ttft is not None else "")
                + f" in={payload['prompt_tokens']} out={payload['completion_tokens']}"
                + (f" cached={payload['cached_tokens']}" if payload.get("cached_tokens") else ""),
                file=sys.stderr,
            )
        elif event == "compaction":
            print(
                f"  !! context compacted, reclaimed {payload['reclaimed']} tokens "
                "(prompt cache prefix lost)",
                file=sys.stderr,
            )

    orch = Orchestrator(
        repo=repo,
        registry=registry,
        client=client,
        skills=skills,
        approval=cli_approval if args.interactive else deny_all_approvals,
        observer=observer,
        context_budget_tokens=cfg.context_budget_tokens,
    )
    result = orch.run(args.task, skill_name=args.skill)
    print(format_report(result))

    if args.transcript:
        dump_transcript(result, Path(args.transcript))
        print(f"\ntranscript: {args.transcript}")
    return 0 if result.ok else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="local-agent")
    parser.add_argument("--repo", help="repository root (default: search upward)")
    parser.add_argument("--profile", choices=sorted(MODEL_PRESETS), help="model preset")
    parser.add_argument("--base-url", help="override the OpenAI-compatible endpoint")
    parser.add_argument("--model", help="override the model id")
    parser.add_argument(
        "--cheap-profile",
        choices=sorted(MODEL_PRESETS),
        help="second, cheaper endpoint. With this set, skills that declare "
             "tier: cheap are served by it and escalate to --profile only when "
             "they fail. This is how you measure what the NPU can carry.",
    )
    parser.add_argument("-v", "--verbose", action="store_true")

    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("doctor", help="check config, tools, skills and model server").set_defaults(func=cmd_doctor)
    sub.add_parser("skills", help="list discovered skills").set_defaults(func=cmd_skills)

    p_tools = sub.add_parser("tools", help="list tools, optionally for one skill")
    p_tools.add_argument("--skill")
    p_tools.set_defaults(func=cmd_tools)

    p_route = sub.add_parser("route", help="show deterministic skill ranking for a task")
    p_route.add_argument("task")
    p_route.set_defaults(func=cmd_route)

    p_run = sub.add_parser("run", help="run a task")
    p_run.add_argument("task")
    p_run.add_argument("--skill", help="force a skill instead of routing")
    p_run.add_argument("--interactive", action="store_true", help="prompt for approvals")
    p_run.add_argument("--transcript", help="write the full transcript to this path")
    p_run.add_argument("--quiet", action="store_true")
    p_run.set_defaults(func=cmd_run)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
    )
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
