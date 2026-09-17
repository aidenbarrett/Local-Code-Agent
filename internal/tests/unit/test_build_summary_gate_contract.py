import inspect

from local_agent.agent.orchestrator import Orchestrator


def test_repo_build_summary_is_deterministically_narrowed_after_repo_info():
    source = inspect.getsource(Orchestrator._run_once)
    assert "build_summary_mode" in source
    assert "submit_answer" in source
    assert "repo_info sufficient for build summary" in source
    assert "grounded in that repo_info result. Do not call another " in source
    assert "discovery tool." in source
