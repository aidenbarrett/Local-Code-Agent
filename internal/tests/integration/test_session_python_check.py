"""Exercise real compile/pytest child processes, not just report-parser mocks."""
import subprocess

import pytest

from local_agent.config import BuildProfile, Policy, RepoConfig
from local_agent.session import self_check as selfcheck
from local_agent.session.task_controller import TaskController
from local_agent.session.event_buffer import EventBuffer


@pytest.mark.parametrize("passing", [True, False])
def test_real_python_selfcheck(tmp_path, monkeypatch, passing):
    (tmp_path / "internal/tests").mkdir(parents=True)
    (tmp_path / "internal/local_agent").mkdir()
    (tmp_path / "internal/local_agent/__init__.py").write_text("")
    (tmp_path / "internal/tests/test_example.py").write_text(f"def test_example():\n    assert {passing}\n")
    (tmp_path / ".gitignore").write_text(".local-agent/\n__pycache__/\n.pytest_cache/\n")
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
    subprocess.run(["git", "-c", "user.name=test", "-c", "user.email=test@example.com",
                    "commit", "-qm", "baseline"], cwd=tmp_path, check=True)
    repo = RepoConfig(tmp_path, "local-code-agent", "build", ".local-agent/runs",
                      {"python": BuildProfile("python")}, "python", Policy())
    # This integration fixture deliberately stands in for the running LCA source
    # checkout. Make that trust assumption explicit rather than bypassing the
    # product identity rule inside run_self_check().
    monkeypatch.setattr(selfcheck, "_LCA_SOURCE_ROOT", tmp_path.resolve())
    controller = TaskController(repo, None, EventBuffer("s"), allow_execution=True)
    result = controller.run("check", self_check=True)
    assert (result.outcome == "pass") is passing
    assert result.verified_at_completion is passing
    assert result.metrics["tests"]["tests"] == 1
