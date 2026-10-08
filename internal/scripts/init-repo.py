#!/usr/bin/env python3
"""`.\\local-code-agent.ps1 init`: declare how Local Code Agent builds and tests a repository.

Without --write it only looks: it shows the canonical repository root and either the
existing declaration, a proposed one, or why it will not propose one. With --write it
writes the proposal, never replacing an existing file. It runs no discovered command.
"""
from __future__ import annotations

import argparse
from pathlib import Path
import sys

INTERNAL = Path(__file__).resolve().parents[1]
if str(INTERNAL) not in sys.path:
    sys.path.insert(0, str(INTERNAL))

from local_agent.repo_setup import (  # noqa: E402
    RepoInspection,
    RepoSetupError,
    init_command,
    inspect_repository,
    write_proposal,
)

EXIT_OK = 0
EXIT_REFUSED = 2


def _command(root: Path, *extra: str) -> str:
    return " ".join((init_command(root), *extra))


def render(inspection: RepoInspection) -> list[str]:
    lines = [f"Repository root: {inspection.root}"]
    if inspection.opened_from != inspection.root:
        lines.append(f"                 (opened from {inspection.opened_from})")
    if inspection.declared is not None:
        repo = inspection.declared
        lines += [
            f"Declared:        {inspection.config_path}",
            f"                 profiles {', '.join(sorted(repo.profiles))} "
            f"(default {repo.default_profile})",
            "It is valid and init leaves it as it is.",
            r"Next: .\local-code-agent.ps1 doctor",
        ]
        return lines
    if inspection.invalid is not None:
        lines += [
            f"Declared:        {inspection.config_path} is invalid: {inspection.invalid}",
            "init never replaces a declaration.",
            f"Next: correct {inspection.config_path}, then rerun {_command(inspection.root)}",
        ]
        return lines
    found = ", ".join(inspection.build_systems) or "none"
    lines.append(f"Build systems:   {found}")
    if inspection.refusal is not None:
        lines += [
            f"No proposal: {inspection.refusal}.",
            f"Next: write {inspection.config_path} by hand with a [profiles.<name>] table "
            "of configure, build and test argv lists.",
        ]
        return lines
    proposal = inspection.proposal or ""
    lines += [
        "",
        f"Proposed {inspection.config_path} (nothing has been written or run):",
        "",
        *proposal.rstrip("\n").splitlines(),
        "",
        f"To write it: {_command(inspection.root, '--write')}",
    ]
    return lines


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="local-code-agent init", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--repo", type=Path, default=Path("."),
                        help="repository root or a path inside it")
    parser.add_argument("--write", action="store_true",
                        help="write the proposed declaration; never replaces an existing one")
    args = parser.parse_args(argv)
    try:
        inspection = inspect_repository(args.repo)
        lines = render(inspection)
        if args.write:
            if inspection.proposal is None:
                lines.append("Nothing written.")
                sys.stdout.write("\n".join(lines) + "\n")
                return EXIT_REFUSED if inspection.declared is None else EXIT_OK
            written = write_proposal(inspection)
            lines = [f"Repository root: {inspection.root}", f"Wrote {written}.",
                     r"Next: .\local-code-agent.ps1 doctor"]
    except (RepoSetupError, OSError) as exc:
        sys.stderr.write(f"REFUSED: {exc}\n")
        return EXIT_REFUSED
    sys.stdout.write("\n".join(lines) + "\n")
    no_outcome = inspection.declared is None and inspection.proposal is None
    return EXIT_REFUSED if no_outcome else EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
