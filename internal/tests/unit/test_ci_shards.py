"""CI shards are a partition of the collected suite: every test runs exactly once."""
from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
SCRIPT = ROOT / "internal" / "devtools" / "ci_shards.py"
SPEC = importlib.util.spec_from_file_location("lca_ci_shards_under_test", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
shards = importlib.util.module_from_spec(SPEC)
sys.modules["lca_ci_shards_under_test"] = shards
SPEC.loader.exec_module(shards)


def test_the_plan_partitions_every_file_exactly_once_and_balances_by_time():
    files = [f"t{i}.py" for i in range(10)]
    weights = {"t0.py": 100.0, "t1.py": 60.0, "t2.py": 40.0}
    plan = shards.plan(files, weights, 3)
    flat = [f for shard in plan for f in shard]
    assert sorted(flat) == sorted(files) and len(flat) == len(set(flat))
    # LPT bound: no shard exceeds the lightest by more than the longest single file.
    median = 60.0
    loads = [sum(weights.get(f, median) for f in shard) for shard in plan]
    assert max(loads) - min(loads) <= max(weights.values())
    assert shards.plan(list(reversed(files)), weights, 3) == plan  # order-independent


def test_unknown_files_get_the_median_and_one_shard_is_everything():
    assert shards.plan(["a.py", "b.py"], {}, 1) == [["a.py", "b.py"]]
    plan = shards.plan(["new.py", "x.py", "y.py"], {"x.py": 10.0, "y.py": 30.0}, 2)
    assert sorted(f for s in plan for f in s) == ["new.py", "x.py", "y.py"]


@pytest.mark.parametrize("spec", ["0/2", "3/2", "a/b", "1", "1/0"])
def test_malformed_shard_specs_are_refused(spec):
    with pytest.raises(ValueError):
        shards.parse_shard(spec)


def test_junit_classnames_map_to_test_files():
    assert shards.file_of("internal.tests.unit.test_x") == "internal/tests/unit/test_x.py"
    assert shards.file_of("internal.tests.unit.test_x.TestY") == "internal/tests/unit/test_x.py"


def _collected(*extra: str) -> set[str]:
    targets = ["internal/tests/unit/test_ci_shards.py", "internal/tests/unit/test_annotate_test_failures.py",
               "internal/tests/unit/test_capture_ci_checkout.py"]
    out = subprocess.run(
        [sys.executable, "-m", "pytest", "-o", "addopts=", "--collect-only", "-q", "-p", "no:cacheprovider",
         *targets, *extra],
        cwd=ROOT, capture_output=True, text=True, check=True,
    ).stdout
    return {line for line in out.splitlines() if "::" in line}


def test_the_pytest_option_selects_disjoint_shards_whose_union_is_the_suite():
    everything = _collected()
    first, second = _collected("--shard", "1/2"), _collected("--shard", "2/2")
    assert first and second
    assert first.isdisjoint(second)
    assert first | second == everything
