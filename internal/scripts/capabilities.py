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

    if not FIXTURE.is_dir():
        print("\nThe benchmark fixture is not present yet.", file=sys.stderr)
        print("Run the root setup command:", file=sys.stderr)
        print(r"  .\install.ps1", file=sys.stderr)
        print(file=sys.stderr)
        return 2

    registry, _, _ = build_registry(load_repo_config(FIXTURE))
    names = sorted(registry.names())

    print()
    print("Local Code Agent capabilities")
    print()
    print("These capabilities are read from the implementation that is actually installed.")
    print()
    print(f"Approved tools ({len(names)})")
    print()
    for name in names:
        print(f"  [x] {PLAIN_ENGLISH.get(name, name):<66} {name}")

    library = SkillLibrary.discover(REPO / "skills")
    names_of_skills = library.names()
    print()
    print(f"Task procedures / skills ({len(names_of_skills)})")
    print()
    print("A skill gives the model a procedure for one kind of task and can reduce the")
    print("tools available to it. The controller still owns policy and verification.")
    print()
    for skill_name in names_of_skills:
        skill = library.get(skill_name)
        allowed = len(skill.tools) if skill and skill.tools else len(names)
        note = f"{allowed} approved tools" if skill and skill.tools else "all approved tools"
        description = (skill.description if skill else "") or ""
        if len(description) > 52:
            description = description[:49].rstrip() + "..."
        print(f"  [x] {skill_name:<26} {description:<52} {note}")

    print()
    print("Not supported, by design")
    print()
    for title, why in NOT_SUPPORTED:
        print(f"  [ ] {title}")
        print(f"      {why}")

    print()
    print("See independent verification reject stale test results:")
    print(r"  .\local-code-agent.ps1 verification-demo")
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
