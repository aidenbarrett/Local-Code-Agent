#!/usr/bin/env python3
"""Measure compile wall time while a separate agent workload runs concurrently."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import subprocess
import time

from local_agent.persistence import sanitize_for_persistence


def parse_command_json(value: str) -> list[str]:
    try:
        command = json.loads(value)
    except json.JSONDecodeError as exc:
        raise ValueError(f"command is not valid JSON: {exc}") from exc
    if not isinstance(command, list) or not command or not all(isinstance(x, str) and x for x in command):
        raise ValueError("command JSON must be a non-empty array of non-empty strings")
    return command


def run_contention(agent_command, compile_command, *, settle_seconds=0.0, agent_timeout_seconds=600.0):
    if settle_seconds < 0 or agent_timeout_seconds <= 0:
        raise ValueError("settle must be non-negative and timeout must be positive")
    agent_started = time.monotonic()
    try:
        agent = subprocess.Popen(agent_command)
    except OSError as exc:
        return {
            "measurement_quality": "unobserved",
            "reason": f"agent command failed to start: {exc}",
            "agent_exit_code": None,
            "compile_exit_code": None,
            "compile_wall_seconds": None,
            "overlap_seconds": 0.0,
            "agent_timed_out": False,
        }
    if settle_seconds:
        time.sleep(settle_seconds)
    compile_started = time.monotonic()
    try:
        compiled = subprocess.run(compile_command, check=False)
        compile_exit = compiled.returncode
        compile_error = None
    except OSError as exc:
        compile_exit = 127
        compile_error = str(exc)
    compile_ended = time.monotonic()

    timed_out = False
    remaining = max(0.0, agent_timeout_seconds - (time.monotonic() - agent_started))
    try:
        agent_exit = agent.wait(timeout=remaining)
    except subprocess.TimeoutExpired:
        timed_out = True
        agent.terminate()
        try:
            agent_exit = agent.wait(timeout=5)
        except subprocess.TimeoutExpired:
            agent.kill()
            agent_exit = agent.wait(timeout=5)
    agent_ended = time.monotonic()
    overlap = max(0.0, min(compile_ended, agent_ended) - max(compile_started, agent_started))
    quality = (
        "observed"
        if compile_error is None
        and compile_exit == 0
        and agent_exit == 0
        and overlap > 0
        and not timed_out
        else "invalid"
    )
    return {
        "measurement_quality": quality,
        "reason": compile_error,
        "agent_exit_code": agent_exit,
        "compile_exit_code": compile_exit,
        "compile_wall_seconds": compile_ended - compile_started,
        "overlap_seconds": overlap,
        "agent_timed_out": timed_out,
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--agent-command-json", required=True)
    parser.add_argument("--compile-command-json", required=True)
    parser.add_argument("--settle-seconds", type=float, default=0.0)
    parser.add_argument("--agent-timeout-seconds", type=float, default=600.0)
    args = parser.parse_args(argv)
    try:
        agent_command = parse_command_json(args.agent_command_json)
        compile_command = parse_command_json(args.compile_command_json)
        observation = run_contention(
            agent_command,
            compile_command,
            settle_seconds=args.settle_seconds,
            agent_timeout_seconds=args.agent_timeout_seconds,
        )
    except ValueError as exc:
        parser.error(str(exc))

    manifest = sanitize_for_persistence({
        "kind": "compile_contention_completion",
        "agent_command": agent_command,
        "compile_command": compile_command,
        **observation,
    })
    args.out.parent.mkdir(parents=True, exist_ok=True)
    with args.out.open("x", encoding="utf-8") as fh:
        json.dump(manifest, fh, indent=2)
        fh.write("\n")
    print(json.dumps(manifest, indent=2))
    return 0 if observation["measurement_quality"] == "observed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
