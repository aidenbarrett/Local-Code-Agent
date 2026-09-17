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
    return [
        shell,
        "-NoProfile",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        str(ROOT / "chat.ps1"),
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


def _show_observer_line(term, raw: str) -> None:
    line = raw.strip()
    if not line:
        return
    if line.startswith("skill:"):
        detail = line[len("skill:") :].strip().replace(" -> ", " · ")
        term.status("active", f"Procedure selected: {detail}")
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
        bits = [f"Model {match.group('total')}s"]
        if match.group("ttft"):
            bits.append(f"TTFT {match.group('ttft')}s")
        bits.append(f"{int(match.group('input')):,} in / {int(match.group('output')):,} out")
        if match.group("cached"):
            bits.append(f"{int(match.group('cached')):,} cached")
        term.status("info", " · ".join(bits))
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

    term.section("LOCAL MODEL")
    server_rc = _prepare_server(term)
    if server_rc != 0:
        term.section("RESULT")
        term.status("fail", "Task did not start because the local model server is unavailable")
        return server_rc

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
    for label, key in (
        ("Procedure", "skill"),
        ("Phase", "phase"),
        ("Tool calls", "tool calls"),
        ("Wall time", "wall"),
    ):
        value = fields.get(key)
        if value:
            term.field(label, value)
    if evidence:
        term.status("info", f"Evidence recorded: {len(evidence)} item(s)")
    for warning in warnings:
        term.status("warn", warning)

    if args.transcript:
        term.status("info", f"Transcript: {args.transcript}")
    term.footer_note(
        "The model proposes actions. Local Code Agent owns policy, execution state and verification."
    )
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
