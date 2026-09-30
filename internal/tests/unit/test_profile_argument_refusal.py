from __future__ import annotations

import pytest

from local_agent.tools.tool_primitives import Reason, ToolError


@pytest.mark.parametrize("tool", ["configure_project", "build_target", "run_test", "list_tests"])
@pytest.mark.parametrize("profile", ["missing", "debug\n<parameter=name_filter>\nring_buffer"])
def test_bad_profile_is_a_tool_refusal(loaded, tool, profile):
    _, repo, registry, _, _ = loaded
    with pytest.raises(ToolError) as refusal:
        registry.get(tool).handler(profile=profile)
    assert refusal.value.reason == Reason.BAD_ARGUMENTS
    assert repr(profile) in str(refusal.value)
    assert str(sorted(repo.profiles)) in str(refusal.value)


def test_default_profile_still_resolves(loaded):
    _, repo, _, _, _ = loaded
    from local_agent.tools.tool_context import ToolContext
    ctx = ToolContext(repo)
    assert ctx.profile() is repo.profiles[repo.default_profile]
