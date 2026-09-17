#!/usr/bin/env python3
r"""Show what Local Code Agent can and cannot do using the live implementation.

Use the root PowerShell entrypoint on Windows:

    .\local-code-agent.ps1 capabilities

Supported capabilities are read from the live implementation and installed
skills so this page cannot quietly drift away from the code. Unsupported items
are explicit design boundaries rather than missing documentation.
"""
from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from terminal_ui import ui  # noqa: E402

FIXTURE = REPO / "benchmark_fixture" / "cpp_project"

PLAIN_ENGLISH = {
    "repo_info": "Report what kind of repository this is and how it builds",
    "list_files": "List files and directories",
    "read_file": "Read a file, or a range of lines from one",
    "read_log_chunk": "Read part of a build or test log",
    "search_text": "Search the repository for text",
    "find_definition": "Find where a symbol is defined",
    "git_status": "Show the working tree status",
    "git_diff": "Show uncommitted changes",
    "git_log": "Show recent commits",
    "git_show": "Show a single commit",
    "git_branch_info": "Show branch and tracking information",
    "git_stage": "Stage changes for commit",
    "git_commit": "Commit staged changes",
    "configure_project": "Configure the build system",
    "build_target": "Build the project, or one target",
    "run_test": "Run tests and independently check that the results are current",
    "list_tests": "List the available tests",
    "propose_patch": "Propose a change without modifying files",
    "apply_patch": "Apply a previously proposed change",
    "submit_answer": "Submit a final answer backed by evidence from the run",
}

NOT_SUPPORTED = [
    ("Arbitrary shell access", "the model can call only the approved tools exposed by the controller"),
    ("Unrestricted filesystem access", "writes to protected locations such as .git and agent state are refused"),
    ("Model self-certification", "build and test evidence is checked independently, never accepted on the model's word"),
    ("Accepting stale test results", "a pass over out-of-date binaries is refused even when the test runner reports success"),
    ("Uncited claims", "a final answer citing evidence the run never produced is rejected"),
    ("Network access from tools", "repository tools do not fetch from the network; model serving is a separate connection"),
]


def main() -> int:
    from local_agent.agent.skills import SkillLibrary
    from local_agent.config import load_repo_config
    from local_agent.tools import build_registry

    term = ui()
    term.banner(
        "CONTROLLED CODING AGENT",
        "Approved tools, task procedures and independent verification.",
    )

    if not FIXTURE.is_dir():
        term.status("fail", "The local validation project is not present yet")
        term.line("  Run the root setup command:")
        term.line(r"    .\install.ps1")
        term.line()
        return 2

    registry, _, _ = build_registry(load_repo_config(FIXTURE))
    names = sorted(registry.names())

    term.section(f"Approved tools ({len(names)})")
    term.status("info", "This list is read from the implementation that is actually installed")
    term.line()
    for name in names:
        term.status("ok", PLAIN_ENGLISH.get(name, name))
        term.line(f"      {term.paint(name, 'dim')}")

    library = SkillLibrary.discover(REPO / "skills")
    names_of_skills = library.names()
    term.line()
    term.section(f"Task procedures / skills ({len(names_of_skills)})")
    term.line("  A skill gives the model a procedure for one kind of task and can reduce")
    term.line("  the tools available to it. The controller still owns policy and verification.")
    term.line()
    for skill_name in names_of_skills:
        skill = library.get(skill_name)
        allowed = len(skill.tools) if skill and skill.tools else len(names)
        note = f"{allowed} approved tools" if skill and skill.tools else "all approved tools"
        description = (skill.description if skill else "") or ""
        if len(description) > 52:
            description = description[:49].rstrip() + "..."
        term.status("ok", skill_name)
        if description:
            term.line(f"      {description}")
        term.line(f"      {term.paint(note, 'dim')}")

    term.line()
    term.section("Not supported, by design")
    for title, why in NOT_SUPPORTED:
        term.status("warn", title)
        term.line(f"      {why}")
        term.line()

    term.section("See verification in action")
    term.line("  See independent verification reject stale test results:")
    term.line(r"    .\local-code-agent.ps1 verification-demo")
    term.line()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
