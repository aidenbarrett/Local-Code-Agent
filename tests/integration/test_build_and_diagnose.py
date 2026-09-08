"""End to end against a real CMake build, with a scripted model.

These are the tests that would have caught the interesting bugs: everything
here really compiles, really links and really runs ctest.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from local_agent.agent import Orchestrator, SkillLibrary, format_report
from local_agent.config import load_repo_config
from local_agent.llm.client import ScriptedClient, tool_call
from local_agent.llm.models import ChatResponse
from local_agent.tools import build_registry

REPO = Path(__file__).resolve().parent.parent.parent

pytestmark = pytest.mark.skipif(
    shutil.which("cmake") is None or shutil.which("ctest") is None,
    reason="cmake/ctest not available",
)


def _pieces(root: Path):
    repo = load_repo_config(root)
    registry, ctx, store = build_registry(repo)
    return repo, registry, store


def _run(root: Path, turns, skill: str, approval=None):
    repo, registry, _ = _pieces(root)
    skills = SkillLibrary.discover(REPO / ".github" / "skills")
    orch = Orchestrator(repo, registry, ScriptedClient(turns), skills,
                        approval=approval or (lambda *a: True))
    return orch.run("task under test", skill_name=skill)


def test_clean_build_and_test(loaded):
    sandbox, repo, registry, _, _ = loaded
    build = registry.get("build_target").handler()
    assert build.ok, build.summary
    assert build.data["error_count"] == 0
    assert build.data["warning_count"] == 0

    tests = registry.get("run_test").handler()
    assert tests.ok, tests.summary
    assert tests.data["totals"]["failed"] == 0


def test_compile_error_is_reduced_to_diagnostics(loaded):
    sandbox, repo, registry, _, _ = loaded
    sandbox.scenario("compile_error")
    result = registry.get("build_target").handler()
    assert not result.ok
    assert result.exit_code != 0
    assert result.data["error_count"] >= 1
    first = result.data["errors"][0]
    assert first["file"].endswith("ring_buffer.cpp")
    assert first["line"] == 13
    assert "count" in first["message"]
    # The raw log stayed on disk rather than in the context window.
    assert result.artifacts and (sandbox.root / result.artifacts[0]).is_file()


def test_link_error_is_identified_by_symbol(loaded):
    sandbox, repo, registry, _, _ = loaded
    sandbox.scenario("link_error")
    result = registry.get("build_target").handler()
    assert not result.ok
    assert result.data["link_errors"]
    assert "checksum" in result.data["link_errors"][0]["symbol"]


def test_failing_test_is_named(loaded):
    sandbox, repo, registry, _, _ = loaded
    sandbox.scenario("test_failure")
    assert registry.get("build_target").handler().ok
    result = registry.get("run_test").handler()
    assert not result.ok
    assert [f["name"] for f in result.data["failed"]] == ["ring_buffer"]
    assert result.data["assertions"], "the assertion text should be captured"


def test_segfault_is_classified(loaded):
    sandbox, repo, registry, _, _ = loaded
    sandbox.scenario("crash")
    assert registry.get("build_target").handler().ok
    result = registry.get("run_test").handler(name_filter="text_util")
    assert not result.ok
    assert result.data["failed"][0]["status"] in ("SEGFAULT", "Subprocess aborted")


def test_timeout_is_reported_as_a_timeout(loaded):
    sandbox, repo, registry, _, _ = loaded
    sandbox.scenario("timeout")
    assert registry.get("build_target").handler().ok
    result = registry.get("run_test").handler(name_filter="slow")
    assert not result.ok
    assert result.data["failed"][0]["status"] == "Timeout"


def test_warnings_do_not_fail_the_build(loaded):
    sandbox, repo, registry, _, _ = loaded
    sandbox.scenario("warning_storm")
    result = registry.get("build_target").handler()
    assert result.ok
    assert result.data["warning_count"] >= 2


def test_propose_then_apply_fixes_the_real_failure(loaded):
    sandbox, repo, registry, store, _ = loaded
    sandbox.scenario("test_failure")

    assert registry.get("build_target").handler().ok
    failing = registry.get("run_test").handler()
    assert not failing.ok
    assert [f["name"] for f in failing.data["failed"]] == ["ring_buffer"]

    proposal = registry.get("propose_patch").handler(
        path="src/ring_buffer.cpp",
        find="return count_ + 1 == slots_.size();",
        replace="return count_ == slots_.size();",
        rationale="off by one: the buffer reported full one slot early",
    )
    assert proposal.ok
    assert "-bool RingBuffer::full() const { return count_ + 1" in proposal.data["diff"]
    assert "+bool RingBuffer::full() const { return count_ ==" in proposal.data["diff"]
    # Nothing written yet.
    assert "count_ + 1" in (sandbox.root / "src" / "ring_buffer.cpp").read_text()

    applied = registry.get("apply_patch").handler(patch_id=proposal.data["patch_id"])
    assert applied.ok
    assert registry.get("build_target").handler().ok
    assert registry.get("run_test").handler().ok


def test_patch_refuses_when_the_file_moved_underneath_it(loaded):
    sandbox, repo, registry, store, _ = loaded
    from local_agent.tools.base import ToolError

    proposal = registry.get("propose_patch").handler(
        path="src/text_util.cpp", find="std::string out;", replace="std::string out;  // note"
    )
    target = sandbox.root / "src" / "text_util.cpp"
    target.write_text(target.read_text() + "\n// somebody else edited this\n")

    with pytest.raises(ToolError, match="changed since patch"):
        registry.get("apply_patch").handler(patch_id=proposal.data["patch_id"])


def test_non_unique_find_is_rejected(loaded):
    sandbox, repo, registry, _, _ = loaded
    from local_agent.tools.base import ToolError

    with pytest.raises(ToolError, match="appears"):
        registry.get("propose_patch").handler(
            path="src/ring_buffer.cpp", find="return", replace="return "
        )


def test_full_loop_diagnoses_and_fixes_a_build_break(sandbox):
    """The whole state machine, driven by a scripted model, on a real break."""
    sandbox.scenario("compile_error")

    def after_build(messages):
        return ChatResponse(
            tool_calls=[
                tool_call(
                    "propose_patch",
                    {
                        "path": "src/ring_buffer.cpp",
                        "find": "    ++count;",
                        "replace": "    ++count_;",
                    },
                    "c2",
                )
            ]
        )

    def after_proposal(messages):
        import json

        payload = json.loads(messages[-1]["content"])
        return ChatResponse(
            tool_calls=[
                tool_call("apply_patch", {"patch_id": payload["data"]["patch_id"]}, "c3")
            ]
        )

    turns = [
        ChatResponse(tool_calls=[tool_call("build_target", {}, "c1")]),
        after_build,
        after_proposal,
        ChatResponse(
            tool_calls=[
                tool_call(
                    "propose_patch",
                    {
                        "path": "src/ring_buffer.cpp",
                        "find": "return count_ == 0 }",
                        "replace": "return count_ == 0; }",
                    },
                    "c4",
                )
            ]
        ),
        after_proposal,
        ChatResponse(tool_calls=[tool_call("build_target", {}, "c6")]),
        ChatResponse(content="Two typos in ring_buffer.cpp. The build succeeded."),
    ]

    result = _run(sandbox.root, turns, "fix-build-failure")
    assert result.state.halt_reason is None
    assert result.state.verified is True
    assert result.state.changed_files == ["src/ring_buffer.cpp"]
    assert "build" in format_report(result).lower()
    assert (sandbox.root / "build").is_dir()
