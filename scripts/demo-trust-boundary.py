#!/usr/bin/env python3
"""Demo: the model can propose actions. It cannot mark its own homework.

Runs entirely against a disposable copy of the benchmark fixture. No model, no
evaluator, no experiment path, nothing written inside the repository. Safe to
rehearse as often as you like.

    python scripts/demo-trust-boundary.py
    python scripts/demo-trust-boundary.py --keep      # leave the copy on disk

The point of the demo is beat 4. `ctest` genuinely reports 4/4 passed, and that
result is stale: the binaries predate the source edit. An agent reading it would
report success. Local Code Agent independently verifies the build evidence and
refuses the tree.

The demo asserts every claim it makes. If any beat does not behave as described
it aborts with a named reason rather than printing whatever happened, because a
demonstration of deterministic verification that narrates its own surprises
would be making exactly the mistake it exists to expose.
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

RULE = "=" * 78


class DemoInvariant(Exception):
    """A beat did not do what the demo exists to show."""


def require(condition: bool, message: str) -> None:
    if not condition:
        raise DemoInvariant(message)


def beat(number: int, title: str) -> None:
    print()
    print(RULE)
    print(f"  {number}.  {title}")
    print(RULE)


def say(label: str, value: str) -> None:
    print(f"      {label:<30} {value}")


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

    print()
    print("  LOCAL CODE AGENT  ·  INDEPENDENT VERIFICATION DEMO")
    print()
    print("  This shows why passing test output is not automatically accepted as proof.")
    print("  A disposable copy of the benchmark C++ project is used. No model is involved.")
    say("Disposable working copy", str(root))

    registry, _, _ = build_registry(load_repo_config(root))

    beat(1, "Build a clean copy of the project")
    started = time.time()
    require(registry.get("configure_project").handler().ok, "configure failed")
    require(registry.get("build_target").handler().ok,
            "the clean build failed; the fixture or the toolchain is wrong")
    say("Build result", f"PASS  ({time.time() - started:.1f}s)")

    beat(2, "Run the tests on the current source")
    honest = registry.get("run_test").handler()
    require(honest.ok,
            "verification refused an honest tree; nothing after this would mean anything")
    say("Raw ctest result", ctest_directly(root))
    say("Independent verification", "ACCEPTED")

    beat(3, "Change the source without rebuilding the binary")
    source = root / "src" / "ring_buffer.cpp"
    original_mtime = source.stat().st_mtime_ns
    source.write_text(
        source.read_text() + "\nthis line is not valid C++ and will not compile ;;;\n",
        encoding="utf-8", newline="\n",
    )
    os.utime(source, ns=(original_mtime, original_mtime))
    say("Source changed", "src/ring_buffer.cpp")
    say("Source timestamp", "restored to its original value")
    say("Compiled binary", "unchanged and now stale")

    beat(4, "Run the tests again without rebuilding")
    say("Raw ctest result", ctest_directly(root))
    print()
    print("      The tests genuinely passed, but they executed the OLD binary.")
    print("      Treating this output alone as proof would report a false success.")

    beat(5, "Ask Local Code Agent to verify the same result")
    verified = registry.get("run_test").handler()
    stale = verified.data.get("stale_sources") or []
    require(not verified.ok,
            "VERIFICATION ACCEPTED A STALE TREE. Do not show this demo; investigate.")
    require("src/ring_buffer.cpp" in stale,
            f"the edited file was not reported as stale; got {stale}")
    say("Independent verification", "REFUSED")
    for path in stale:
        say("Stale source detected", path)
    print()
    print("      " + verified.summary.split(" -- ")[0][:200])

    beat(6, "Rebuild honestly and expose the real source state")
    require(not registry.get("build_target").handler().ok,
            "the edited source compiled; the demo's premise is broken")
    say("Rebuild result", "FAILED TO COMPILE")
    print()
    print("      The source really was broken. The earlier passing tests were stale")
    print("      evidence, so Local Code Agent was correct to refuse them.")

    print()
    print(RULE)
    print("  The model can propose actions. It cannot mark its own homework.")
    print(RULE)
    print()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--keep", action="store_true",
                        help="leave the disposable copy on disk afterwards")
    parser.add_argument("--workdir", type=Path,
                        help="where to put the disposable copy")
    args = parser.parse_args(argv)

    if not FIXTURE.is_dir():
        print(f"fixture not found at {FIXTURE}", file=sys.stderr)
        print("run: python benchmark_fixture/generate_project.py", file=sys.stderr)
        return 2

    base = args.workdir or Path(tempfile.mkdtemp(prefix="lca-demo-"))
    try:
        root = base / "cpp_project"
        shutil.copytree(FIXTURE, root)
        demonstrate(root)
        return 0
    except DemoInvariant as exc:
        print()
        print(RULE)
        print(f"  DEMO ABORTED: {exc}")
        print(RULE)
        print()
        return 1
    finally:
        if args.keep:
            print(f"  copy left at {base}")
        else:
            shutil.rmtree(base, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
