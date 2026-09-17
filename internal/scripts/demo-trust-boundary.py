#!/usr/bin/env python3
"""Demo: the model can propose actions. It cannot mark its own homework.

Runs entirely against a disposable copy of the benchmark project. No model, no
evaluator, no experiment path, nothing written inside the repository. Safe to
rehearse as often as you like.

The important moment is that the test runner genuinely reports success against
an old binary, while Local Code Agent independently refuses that stale result.
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
FIXTURE = REPO / "benchmark_fixture" / "cpp_project"
sys.path.insert(0, str(REPO))

from terminal_ui import ui  # noqa: E402


class DemoInvariant(Exception):
    """A beat did not do what the demo exists to show."""


def require(condition: bool, message: str) -> None:
    if not condition:
        raise DemoInvariant(message)


def ctest_directly(root: Path) -> str:
    """What a naive agent would run, and believe."""
    proc = subprocess.run(
        ["ctest", "--test-dir", str(root / "build"), "--output-on-failure"],
        capture_output=True, text=True, check=False,
    )
    for line in reversed((proc.stdout + proc.stderr).splitlines()):
        if "tests passed" in line or "tests failed" in line:
            return line.strip()
    return f"exit code {proc.returncode}"


def demonstrate(root: Path) -> None:
    from local_agent.config import load_repo_config
    from local_agent.tools import build_registry

    term = ui()
    term.banner(
        "INDEPENDENT VERIFICATION DEMO",
        "Passing test output is not automatically accepted as proof.",
    )
    term.status("info", "A disposable C++ project is used. No model is involved.")
    term.line()

    registry, _, _ = build_registry(load_repo_config(root))

    term.section("1 · BUILD A CLEAN PROJECT")
    started = time.time()
    require(registry.get("configure_project").handler().ok, "configure failed")
    require(registry.get("build_target").handler().ok,
            "the clean build failed; the project or toolchain is wrong")
    term.status("ok", f"Clean build verified ({time.time() - started:.1f}s)")

    term.line()
    term.section("2 · RUN TESTS ON THE CURRENT SOURCE")
    honest = registry.get("run_test").handler()
    require(honest.ok,
            "verification refused an honest tree; nothing after this would mean anything")
    term.field("Test runner", ctest_directly(root))
    term.status("ok", "Independent verification accepted the result")

    term.line()
    term.section("3 · CHANGE SOURCE WITHOUT REBUILDING")
    source = root / "src" / "ring_buffer.cpp"
    original_mtime = source.stat().st_mtime_ns
    source.write_text(
        source.read_text() + "\nthis line is not valid C++ and will not compile ;;;\n",
        encoding="utf-8", newline="\n",
    )
    os.utime(source, ns=(original_mtime, original_mtime))
    term.field("Source changed", "src/ring_buffer.cpp")
    term.field("Source timestamp", "restored to its original value")
    term.field("Compiled program", "unchanged and now out of date")
    term.line()
    term.status("warn", "The timestamp looks unchanged, but the source contents are different")

    term.line()
    term.section("4 · RUN THE TESTS AGAIN WITHOUT REBUILDING")
    term.field("Test runner", ctest_directly(root))
    term.line()
    term.status("warn", "The tests passed, but they executed the OLD compiled program")
    term.line("  Treating that output alone as proof would report a false success.")

    term.line()
    term.section("5 · ASK LOCAL CODE AGENT TO VERIFY THE SAME RESULT")
    verified = registry.get("run_test").handler()
    stale = verified.data.get("stale_sources") or []
    require(not verified.ok,
            "VERIFICATION ACCEPTED A STALE TREE. Do not show this demo; investigate.")
    require("src/ring_buffer.cpp" in stale,
            f"the edited file was not reported as stale; got {stale}")
    term.status("warn", "Independent verification REFUSED the passing test result")
    for path in stale:
        term.field("Changed source", path)
    term.line()
    term.line("  " + verified.summary.split(" -- ")[0][:200])

    term.line()
    term.section("6 · REBUILD THE CURRENT SOURCE")
    require(not registry.get("build_target").handler().ok,
            "the edited source compiled; the demo's premise is broken")
    term.status("fail", "Rebuild failed to compile")
    term.line()
    term.line("  The source really was broken. The earlier passing tests were stale evidence,")
    term.line("  so Local Code Agent was correct to refuse them.")

    term.line()
    term.section("RESULT")
    term.status("ok", "STALE TEST RESULT REJECTED")
    term.line()
    term.line("  The model can propose actions.")
    term.line("  It cannot mark its own homework.")
    term.line()
    term.rule("═", role="cyan")
    term.line()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--keep", action="store_true",
                        help="leave the disposable copy on disk afterwards")
    parser.add_argument("--workdir", type=Path,
                        help="where to put the disposable copy")
    args = parser.parse_args(argv)

    if not FIXTURE.is_dir():
        print(f"demo project not found at {FIXTURE}", file=sys.stderr)
        print("run the root installation / validation flow and try again", file=sys.stderr)
        return 2

    base = args.workdir or Path(tempfile.mkdtemp(prefix="lca-demo-"))
    try:
        root = base / "cpp_project"
        shutil.copytree(FIXTURE, root)
        demonstrate(root)
        return 0
    except DemoInvariant as exc:
        term = ui(stream=sys.stderr)
        term.line()
        term.section("DEMO ABORTED")
        term.status("fail", str(exc))
        term.line()
        return 1
    finally:
        if args.keep:
            print(f"  disposable copy left at {base}")
        else:
            shutil.rmtree(base, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
