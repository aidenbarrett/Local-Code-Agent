from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

from local_agent.session.self_check import run_self_check


def test_repo_name_cannot_spoof_local_code_agent_self_check(tmp_path):
    fake = tmp_path / "pretender"
    (fake / "internal" / "tests").mkdir(parents=True)
    repo = SimpleNamespace(
        name="local-code-agent",
        root=fake,
        policy=SimpleNamespace(allow_build=True, allow_test=True),
    )
    events = SimpleNamespace(emit=lambda *_args, **_kwargs: None)

    result = run_self_check(repo, str(uuid4()), events)

    assert result.outcome.value == "blocked"
    assert "checkout that supplied this running controller" in result.answer
