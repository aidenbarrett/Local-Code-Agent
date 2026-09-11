"""Call run_case exactly as the purity sweep does, and print the row it returns.

Run from the repository root:

    python windows-runcase-probe.py

The sweep's helper throws that row away and asserts `client.calls`, which is why
Windows produced "the client was never asked anything" and nothing else. The row
carries the stage, the exception and the traceback. This prints them.

It also runs the same call twice: once under a deep pytest-shaped path and once
under a short one, because a build tree that fits in one and not the other is a
different bug from an exception that happens either way. No pytest, no model.
"""

import json
import shutil
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "evaluation"))

from run_evaluation import run_case                    # noqa: E402
from task_contracts import CASES                       # noqa: E402
from local_agent.config import ModelConfig             # noqa: E402
from local_agent.llm.client import ScriptedClient, tool_call   # noqa: E402
from local_agent.llm.models import ChatResponse        # noqa: E402

CASE = "test-failure-diagnose"
CONDITION = "control"


def _case(name):
    return next(c for c in CASES if c.name == name)


def _finish_immediately():
    """Identical to the sweep's client: answers on turn one, calls nothing."""
    return ScriptedClient([
        ChatResponse(tool_calls=[tool_call(
            "submit_answer",
            {"claim": "diagnosis", "summary": "captured the opening request"},
            "c0",
        )])
    ])


def attempt(label: str, workdir: Path):
    print("=" * 72)
    print(label)
    print("=" * 72)
    workdir.mkdir(parents=True, exist_ok=True)
    target = workdir / CONDITION
    print(f"  workdir       {target}")
    print(f"  path length   {len(str(target))} characters")
    print()

    client = _finish_immediately()
    row = run_case(_case(CASE), ModelConfig(), target,
                   auto_approve=True, client=client, condition=CONDITION,
                   catalogue=False)

    print(f"  client.calls  {len(client.calls)}")
    if client.calls:
        print("  THE MODEL WAS ASKED. This path works.")
    else:
        print("  THE MODEL WAS NEVER ASKED. run_case returned early.")
    print()
    print(f"  validity      {row.get('validity')}")
    print(f"  outcome       {row.get('outcome')}")
    print(f"  error         {row.get('error')}")
    print(f"  precondition  {json.dumps(row.get('precondition'), default=str)}")
    print()
    tb = row.get("traceback")
    print("  --- traceback ---")
    print(tb if tb else "  (none recorded, so nothing raised)")
    print()
    return row


print(f"case       {CASE}")
print(f"condition  {CONDITION}")
print(f"python     {sys.version.split()[0]}  on  {sys.platform}")
print()

base = Path(tempfile.mkdtemp(prefix="lca-runcase-"))

# 1. The shape pytest actually produces. tmp_path is already long, and the
#    sweep nests a per-case directory under it before run_case adds the
#    worktree and CMake adds its own tree underneath that.
deep = base / "pytest-of-user" / "pytest-0" / "test_control_is_never_offere0" / f"c-{CASE}"
row_deep = attempt("A: deep path, as pytest lays it out", deep)

# 2. The same call somewhere short. If A fails and B passes, the bug is the
#    path, not the code.
short = Path(tempfile.mkdtemp(prefix="s-", dir=tempfile.gettempdir()))
row_short = attempt("B: short path", short)

print("=" * 72)
print("VERDICT")
print("=" * 72)
deep_ok = row_deep.get("validity") == "valid"
short_ok = row_short.get("validity") == "valid"
if deep_ok and short_ok:
    print("  Both paths reached the model. The failure is not reproduced here,")
    print("  so it is something the sweep does that this probe does not.")
elif short_ok and not deep_ok:
    print("  The deep path failed and the short path worked.")
    print("  That is a path-length problem, not a logic problem.")
else:
    print("  Both failed the same way. Read the traceback above; it is the")
    print("  root cause and it has nothing to do with path length.")

for path in (base, short):
    shutil.rmtree(path, ignore_errors=True)
