#!/usr/bin/env python3
"""Human-facing presenter for ``local-code-agent.ps1 run-task``.

The underlying developer CLI remains the execution contract. This script owns
presentation only: it prepares the already-supported public NPU server, streams
CLI observer events in user-facing language, and gives the final answer/result a
clear visual hierarchy.
"""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[2]
INTERNAL = ROOT / "internal"
if str(INTERNAL) not in sys.path:
    sys.path.insert(0, str(INTERNAL))

from terminal_ui import device_label, ui  # noqa: E402

_REPORT_MARKER = "--- run summary ---"
_MODEL_RE = re.compile(
    r"^~~ model (?P<total>[0-9.]+)s"
    r"(?: ttft (?P<ttft>[0-9.]+)s)?"
    r" in=(?P<input>\d+) out=(?P<output>\d+)"
    r"(?: cached=(?P<cached>\d+))?$"
)
_WALL_RE = re.compile(
    r"^(?P<total>[0-9.]+)s \(model (?P<model>[0-9.]+)s over "
    r"(?P<calls>\d+) call\(s\), tools (?P<tools>[0-9.]+)s, "
    r"overhead (?P<overhead>[0-9.]+)s\)$"
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="local-code-agent run-task")
    parser.add_argument("task")
    parser.add_argument("--skill")
    parser.add_argument("--interactive", action="store_true")
    parser.add_argument("--transcript")
    parser.add_argument("--quiet", action="store_true")
    return parser


def _powershell() -> str | None:
    for candidate in ("pwsh", "powershell.exe", "powershell"):
        resolved = shutil.which(candidate)
        if resolved:
            return resolved
    return None


def _server_command(shell: str) -> list[str]:
    """Prepare the managed server through the one public product launcher."""
    return [
        shell,
        "-NoProfile",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        str(ROOT / "local-code-agent.ps1"),
        "chat",
        "qwen3-8b-npu",
        "--ensure-only",
    ]


def _agent_command(args: argparse.Namespace) -> list[str]:
    command = [
        sys.executable,
        "-m",
        "local_agent.cli",
        "--profile",
        "ptl-npu-8b",
        "run",
        args.task,
    ]
    if args.skill:
        command += ["--skill", args.skill]
    if args.interactive:
        command.append("--interactive")
    if args.transcript:
        command += ["--transcript", args.transcript]
    if args.quiet:
        command.append("--quiet")
    return command


def _child_env() -> dict[str, str]:
    env = dict(os.environ)
    existing = env.get("PYTHONPATH")
    env["PYTHONPATH"] = str(INTERNAL) + (os.pathsep + existing if existing else "")
    env["PYTHONUNBUFFERED"] = "1"
    return env


def _split_report(output: str) -> tuple[str, list[str]]:
    lines = output.replace("\r\n", "\n").splitlines()
    try:
        marker = lines.index(_REPORT_MARKER)
    except ValueError:
        return "\n".join(lines).strip(), []
    answer = "\n".join(lines[:marker]).strip()
    return answer, lines[marker + 1 :]


def _parse_summary(lines: list[str]) -> tuple[dict[str, str], list[str], list[str]]:
    fields: dict[str, str] = {}
    warnings: list[str] = []
    evidence: list[str] = []
    bucket: list[str] | None = None
    for raw in lines:
        line = raw.rstrip()
        if line == "warnings:":
            bucket = warnings
            continue
        if line == "evidence:":
            bucket = evidence
            continue
        if line.startswith("  - ") and bucket is not None:
            bucket.append(line[4:])
            continue
        bucket = None
        if ":" in line:
            key, value = line.split(":", 1)
            fields[key.strip().lower()] = value.strip()
    return fields, warnings, evidence


def _route_display(detail: str) -> tuple[str, str | None]:
    """Translate internal tier names into language a first-time user understands."""
    if " -> " not in detail:
        return detail.strip(), None
    skill, tier = (part.strip() for part in detail.split(" -> ", 1))
    normalized = tier.lower()
    if normalized.endswith(" tier"):
        normalized = normalized[:-5].strip()
    labels = {
        "cheap": "Prefer local Qwen3-8B on NPU",
        "strong": "Use the higher-capability model route",
    }
    return skill, labels.get(normalized, tier)


def _parse_wall_timing(value: str) -> dict[str, str] | None:
    """Turn the developer wall-time tuple into operator-facing timing fields."""
    match = _WALL_RE.match(value.strip())
    if not match:
        return None
    calls = int(match.group("calls"))
    return {
        "total": f"{match.group('total')} s",
        "model": f"{match.group('model')} s across {calls} model call{'s' if calls != 1 else ''}",
        "tools": f"{match.group('tools')} s",
        "overhead": f"{match.group('overhead')} s",
    }


def _show_observer_line(term, raw: str) -> None:
    line = raw.strip()
    if not line:
        return
    if line.startswith("skill:"):
        skill, route = _route_display(line[len("skill:") :].strip())
        term.status("active", f"Procedure selected: {skill}")
        if route:
            term.field("Routing", route)
        return
    if line.startswith("-> "):
        name = line[3:].split("(", 1)[0].strip()
        term.status("active", f"Tool → {name}")
        return
    if line.startswith("<- [ok ") or line.startswith("<- [ok]"):
        summary = line.split("]", 1)[1].strip() if "]" in line else line
        term.status("ok", summary)
        return
    if line.startswith("<- [ERR"):
        summary = line.split("]", 1)[1].strip() if "]" in line else line
        term.status("fail", summary)
        return
    match = _MODEL_RE.match(line)
    if match:
        term.line()
        term.status("info", "Model response")
        term.field("Model call time", f"{match.group('total')} s")
        if match.group("ttft"):
            term.field("First token", f"{match.group('ttft')} s")
        term.field(
            "Token usage",
            f"{int(match.group('input')):,} input · {int(match.group('output')):,} output",
        )
        if match.group("cached"):
            term.field("Prompt cache", f"{int(match.group('cached')):,} tokens reused")
        return
    if line.startswith("^^ escalating"):
        term.status("warn", line[3:].strip())
        return
    if line.startswith("!! context compacted"):
        term.status("warn", line[3:].strip())
        return
    term.status("info", line)


def _prepare_server(term) -> int:
    shell = _powershell()
    if not shell:
        term.status("fail", "PowerShell was not found; cannot prepare the local model server")
        return 2

    term.status("active", "Preparing Qwen3-8B (INT4) on the NPU")
    result = subprocess.run(
        _server_command(shell),
        cwd=ROOT,
        env=_child_env(),
        capture_output=True,
        text=True,
        errors="replace",
    )
    combined = "\n".join(part for part in (result.stdout, result.stderr) if part).strip()
    if result.returncode != 0:
        term.status("fail", "The local NPU model server could not be prepared")
        for line in combined.splitlines()[-12:]:
            term.status("info", line.strip())
        return result.returncode

    lowered = combined.lower()
    if "already ready" in lowered:
        term.status("ok", "Qwen3-8B already resident and ready on the NPU")
    else:
        term.status("ok", "Qwen3-8B ready on the NPU")
    return 0


def _run_agent(term, args: argparse.Namespace) -> tuple[int, str]:
    process = subprocess.Popen(
        _agent_command(args),
        cwd=ROOT,
        env=_child_env(),
        stdin=None,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        errors="replace",
        bufsize=1,
    )
    assert process.stderr is not None
    for line in process.stderr:
        _show_observer_line(term, line)
    assert process.stdout is not None
    stdout = process.stdout.read()
    return process.wait(), stdout


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    term = ui()

    term.banner(
        "CONTROLLED REPOSITORY TASK",
        "Local model · constrained tools · deterministic control · independent verification",
    )

    term.section("TASK")
    term.wrapped_field("Request", args.task)
    term.field("Procedure", args.skill or "automatic routing")
    term.field("Model", "Qwen3-8B · INT4")
    term.field("Device", device_label("NPU"))

    term.line()
    term.section("LOCAL MODEL")
    server_rc = _prepare_server(term)
    if server_rc != 0:
        term.line()
        term.section("RESULT")
        term.status("fail", "Task did not start because the local model server is unavailable")
        return server_rc

    term.line()
    term.section("AGENT WORK")
    rc, raw_report = _run_agent(term, args)
    answer, summary_lines = _split_report(raw_report)
    fields, warnings, evidence = _parse_summary(summary_lines)

    term.line()
    term.section("ANSWER")
    answer_text = answer or "No answer was produced."
    term.panel("LOCAL MODEL ANSWER", answer_text, border_role="magenta" if rc == 0 else "red")

    term.line()
    term.section("RESULT")
    outcome = fields.get("outcome", "pass" if rc == 0 else "fail")
    term.status("ok" if rc == 0 else "fail", f"Outcome: {outcome.upper()}")

    procedure = fields.get("skill")
    if procedure:
        term.field("Procedure", procedure)
    phase = fields.get("phase")
    if phase:
        phase_display = "Answer produced" if phase == "report" else phase.replace("_", " ").title()
        term.field("Final state", phase_display)
    tool_calls = fields.get("tool calls")
    if tool_calls:
        term.field("Tool calls", tool_calls)

    wall = fields.get("wall")
    if wall:
        timing = _parse_wall_timing(wall)
        if timing:
            term.line()
            term.field("Total task time", timing["total"], role="green" if rc == 0 else None)
            term.field("Model time", timing["model"])
            term.field("Tool time", timing["tools"])
            term.field("LCA overhead", timing["overhead"])
        else:
            term.field("Total task time", wall)

    if evidence:
        term.status("info", f"Evidence recorded: {len(evidence)} item(s)")
    for warning in warnings:
        term.status("warn", warning)

    if args.transcript:
        term.status("info", f"Transcript: {args.transcript}")
    term.footer_note(
        "CONTROL BOUNDARY · The model proposes answers and tool calls. "
        "Local Code Agent decides what may execute, tracks repository state, "
        "and decides what evidence is current enough to count as verified."
    )
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
