from __future__ import annotations

import pytest

from local_agent.tools.tool_primitives import Reason, ToolError
from local_agent.verification import ProofKind, classify_proof


@pytest.mark.parametrize("target", [None, "", " \t", "bad target"])
def test_invalid_explicit_build_targets_are_refused_before_effects(loaded, target):
    sandbox, _, registry, _, _ = loaded
    marker = sandbox.root / "build" / "must-survive"
    marker.parent.mkdir()
    marker.write_text("user data")

    with pytest.raises(ToolError) as caught:
        registry.get("build_target").handler(target=target)

    assert caught.value.reason is Reason.BAD_ARGUMENTS
    assert marker.read_text() == "user data"


@pytest.mark.parametrize(
    ("arguments", "expected"),
    [
        ({}, ProofKind.FULL_BUILD_PASS),
        ({"profile": "release"}, ProofKind.FULL_BUILD_PASS),
        ({"target": None}, ProofKind.NO_CURRENT_PROOF),
        ({"target": ""}, ProofKind.NO_CURRENT_PROOF),
        ({"target": " \t"}, ProofKind.NO_CURRENT_PROOF),
        ({"target": "sandbox"}, ProofKind.TARGETED_BUILD_PASS),
        ({"target": "test-ring_buffer.1"}, ProofKind.TARGETED_BUILD_PASS),
        ({"target": "bad target"}, ProofKind.NO_CURRENT_PROOF),
    ],
)
def test_build_proof_scope_never_promotes_an_explicit_empty_target(arguments, expected):
    assert classify_proof(
        name="build_target",
        arguments=arguments,
        execution="ok",
        domain="pass",
        evidence={},
    ) is expected
