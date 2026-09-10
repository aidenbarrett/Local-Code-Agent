"""Isolate `establish` on Windows in about a minute instead of ten.

Run from the repository root:

    python windows-establish-probe.py

It does exactly what the purity sweep does before it asks the model anything,
for the first case that has a `built` precondition, and prints every command,
its exit code and its raw output. No model, no network, no pytest.
"""

import sys
import tempfile
import traceback
from pathlib import Path

REPO = Path(__file__).resolve().parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "evaluation"))

from run_evaluation import establish, prepare          # noqa: E402
from task_contracts import CASES                       # noqa: E402
from local_agent.config import load_repo_config        # noqa: E402
from local_agent.tools import build_registry           # noqa: E402

case = next(c for c in CASES if c.name == "test-failure-diagnose")
print(f"case        {case.name}")
print(f"scenario    {case.scenario}")
print(f"precondition{case.precondition!r}")
print()

work = Path(tempfile.mkdtemp(prefix="lca-probe-"))
root, _ = prepare(work, case.scenario)
print(f"worktree    {root}")

repo = load_repo_config(root)
for name in ("debug",):
    prof = repo.profile(name)
    print(f"profile {name}:")
    print(f"  configure {prof.configure}")
    print(f"  build     {prof.build}")
    print(f"  test      {prof.test}")
print()

registry, _, _ = build_registry(repo)

# The three steps establish() takes, run one at a time so the failing one is
# obvious, with the raw log rather than the summary.
for step, kwargs in (("configure_project", {}), ("build_target", {}), ("list_tests", {})):
    print("=" * 70)
    print(f"STEP {step}")
    print("=" * 70)
    result = registry.get(step).handler(**kwargs)
    print(f"  ran        {result.ran}")
    print(f"  ok         {result.ok}")
    print(f"  exit_code  {result.exit_code}")
    print(f"  summary    {result.summary}")
    print(f"  data keys  {sorted((result.data or {}).keys())}")
    if step == "list_tests":
        print(f"  tests      {(result.data or {}).get('tests')}")
    for artifact in getattr(result, "artifacts", []) or []:
        path = Path(artifact)
        if path.is_file():
            print(f"  --- raw log {path.name} ---")
            print(path.read_text(errors="replace")[-4000:])
    print()

print("=" * 70)
print("establish() as the harness calls it")
print("=" * 70)
try:
    detail = establish(case, registry)
    print("OK:", detail)
except Exception:
    traceback.print_exc()

print()
print(f"worktree left at {root} for inspection")
