#!/usr/bin/env python3
"""Prepare the end-to-end walkthrough repository.

A disposable copy of a pinned real repository (cxxopts, from the qualification
cache) with one seeded library defect that its unchanged tests reveal, plus the
developer's own work in progress: a staged file, an unstaged edit and an untracked
note. Local Code Agent has to find and repair the defect without disturbing any of it.

Prints the repository path on the last line of stdout so the launcher's
``walkthrough`` command can open the Session Hub on it. Nothing is fetched: run
``acceptance --corpus cxxopts --fetch`` once while online.
"""
from __future__ import annotations

import argparse
from datetime import UTC, datetime
import subprocess
import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
INTERNAL = SCRIPTS.parent
if str(INTERNAL) not in sys.path:
    sys.path.insert(0, str(INTERNAL))

from scripts import acceptance_corpus as corpus  # noqa: E402
from serving.model_store import default_runtime_root  # noqa: E402

CORPUS_NAME = "cxxopts"
FAULT_ID = "source-defect"
STAGED = ("CHANGELOG-draft.md", "- Document the --verbose flag.\n")
UNSTAGED = ("README.md", "\nLocal note: benchmark parsing after the next release.\n")
UNTRACKED = ("scratch notes.txt", "ideas, not for commit\n")

STEPS = (
    "explain this repository",
    "where is ParseResult defined?",
    "run the tests",
    "why did that fail?",
    "fix it            (or select the failure and press Fix)",
    "Review, Apply, then run the tests again",
    "Commit (or Undo to restore exactly what was there)",
    "build it, then press Esc while it runs",
)


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)  # noqa: S603, S607


def prepare(output: Path, cache: Path) -> Path:
    """Create the walkthrough repository under ``output`` and return its path."""
    entry = corpus.load_manifest()[CORPUS_NAME]
    cached = corpus.cached_copy(entry, cache)
    if cached is None:
        raise corpus.CorpusError(
            f"{CORPUS_NAME} is not in {cache}; run once while online: "
            f"local-code-agent.ps1 acceptance --corpus {CORPUS_NAME} --fetch")
    if output.exists() and any(output.iterdir()):
        raise corpus.CorpusError(f"{output} is not empty; choose a new folder")
    repo = corpus.disposable_copy(entry, cached, output / CORPUS_NAME,
                                  fault=entry.fault(FAULT_ID))
    staged = repo / STAGED[0]
    staged.write_text(STAGED[1], encoding="utf-8")
    _git(repo, "add", "--", STAGED[0])
    unstaged = repo / UNSTAGED[0]
    unstaged.write_bytes(unstaged.read_bytes() + UNSTAGED[1].encode("utf-8"))
    (repo / UNTRACKED[0]).write_text(UNTRACKED[1], encoding="utf-8")
    return repo


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--output", type=Path, default=None,
                        help="a new or empty folder (default: a new folder under the "
                             "runtime root's walkthroughs/)")
    parser.add_argument("--corpus-cache", type=Path, default=None,
                        help="qualification cache (default: under the runtime root)")
    args = parser.parse_args(argv)
    runtime_root = default_runtime_root()
    cache = args.corpus_cache or corpus.cache_root(runtime_root)
    output = args.output or (runtime_root / "walkthroughs"
                             / datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ"))
    try:
        repo = prepare(output.resolve(), cache)
    except corpus.CorpusError as exc:
        sys.stderr.write(f"Walkthrough not prepared: {exc}\n")
        return 2
    lines = [
        f"Walkthrough repository: {repo}",
        f"Your work in progress, which must survive: staged {STAGED[0]}, "
        f"unstaged edit to {UNSTAGED[0]}, untracked '{UNTRACKED[0]}'.",
        "Suggested steps:",
        *(f"  {index}. {step}" for index, step in enumerate(STEPS, 1)),
        str(repo),
    ]
    sys.stdout.write("\n".join(lines) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
