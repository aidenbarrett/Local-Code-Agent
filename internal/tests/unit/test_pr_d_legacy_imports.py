from __future__ import annotations

from pathlib import Path

INTERNAL = Path(__file__).resolve().parents[2]
RETIRED = (
    INTERNAL / "local_agent/tools/base.py",
    INTERNAL / "local_agent/tools/context.py",
    INTERNAL / "local_agent/tools/runner.py",
    INTERNAL / "local_agent/tools/testing.py",
    INTERNAL / "local_agent/llm/models.py",
    INTERNAL / "local_agent/llm/router.py",
)


def test_pr_d_retired_module_paths_stay_absent() -> None:
    present = [str(path.relative_to(INTERNAL)) for path in RETIRED if path.exists()]
    assert not present, "retired PR-D module paths reappeared:\n" + "\n".join(present)
