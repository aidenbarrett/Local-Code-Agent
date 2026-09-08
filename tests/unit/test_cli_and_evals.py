from __future__ import annotations

import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent.parent


def _cli(sandbox_root: Path, *args: str) -> subprocess.CompletedProcess:
    env = {"PYTHONPATH": str(REPO / "src"), "PATH": "/usr/bin:/bin:/usr/local/bin"}
    return subprocess.run(
        [sys.executable, "-m", "local_agent.cli", "--repo", str(sandbox_root), *args],
        capture_output=True,
        text=True,
        env=env,
        cwd=str(REPO),
    )


def test_cli_lists_skills(sandbox):
    # The sandbox has no skills of its own; point the loader at the agent repo's.
    proc = _cli(REPO, "skills")
    assert proc.returncode == 0, proc.stderr
    assert "diagnose-build-failure" in proc.stdout
    assert "build-and-test" in proc.stdout


def test_cli_lists_tools_for_one_skill(sandbox):
    proc = _cli(sandbox.root, "tools", "--skill", "git-review")
    assert proc.returncode == 0, proc.stderr
    assert "git_diff" in proc.stdout
    assert "build_target" not in proc.stdout
    assert "[read]" in proc.stdout


def test_cli_route_is_deterministic(sandbox):
    proc = _cli(sandbox.root, "route", "the build fails with an undefined reference")
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.splitlines()[0].strip().endswith("diagnose-build-failure")


def test_cli_doctor_reports_unreachable_server_without_crashing(sandbox):
    proc = _cli(sandbox.root, "--base-url", "http://127.0.0.1:9/v3", "doctor")
    assert proc.returncode != 0
    assert "UNREACHABLE" in proc.stdout or "NOT SERVED" in proc.stdout
    assert "tools" in proc.stdout


# ------------------------------------------------------------------- evals


def test_eval_checks_grade_a_synthetic_run():
    sys.path.insert(0, str(REPO / "tests" / "evals"))
    from eval_cases import CASES, answer_mentions, at_most_calls, succeeded

    from local_agent.agent.orchestrator import RunResult
    from local_agent.agent.state import AgentState, ToolCallRecord

    state = AgentState(task="t", repo_root=REPO)
    state.record(ToolCallRecord("build_target", {}, "auto", True, "build failed"))
    result = RunResult(answer="ring_buffer.cpp line 13 is wrong", state=state)

    assert succeeded("build_target")(result)
    assert not succeeded("run_test")(result)
    assert answer_mentions("ring_buffer.cpp", "13")(result)
    assert at_most_calls(2)(result)

    names = [c.name for c in CASES]
    assert len(names) == len(set(names))
    for case in CASES:
        assert case.checks, f"{case.name} has no checks"


def test_eval_harness_prepares_a_scenario(tmp_path):
    sys.path.insert(0, str(REPO / "tests" / "evals"))
    from run_evals import prepare

    root, oracle_dir = prepare(tmp_path, "test_failure")
    assert (root / "src" / "ring_buffer.cpp").read_text().count("count_ + 1") == 1
    assert (root / ".git").is_dir()
    # the oracle is taken after the scenario is applied and lives outside root
    assert (oracle_dir / "tests" / "test_ring_buffer.cpp").is_file()
    assert (oracle_dir / "CMakeLists.txt").is_file()
    assert oracle_dir.parent == root.parent
