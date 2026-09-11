"""Adversarial tests: can the benchmark be told an agent succeeded when it did
not do the work?

Every test here is a direct reproduction of a false-positive path that scored a
capability success on package c067ee5b. They are written as the cheat, not as
the fix, so they keep failing if the fix is ever undone.

The pattern to notice: in all five, the ANSWER is correct and the quality score
is 1.00. Good answers are exactly when a loose rubric starts hiding procedural
failure, which is why evidence is now a hard gate and not a quality point.
"""

from __future__ import annotations

import json
import re
import shutil
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO / "evaluation"))

from local_agent.config import ModelConfig  # noqa: E402
from local_agent.llm.client import ScriptedClient, tool_call  # noqa: E402
from local_agent.llm.models import ChatResponse  # noqa: E402

pytestmark = pytest.mark.skipif(
    shutil.which("cmake") is None or shutil.which("ctest") is None,
    reason="needs cmake and ctest",
)

FIND = "bool RingBuffer::full() const { return count_ + 1 == slots_.size(); }"
REPL = "bool RingBuffer::full() const { return count_ == slots_.size(); }"


def _case(name):
    from task_contracts import CASES
    return next(c for c in CASES if c.name == name)


def _plan(*steps):
    """steps: (tool, args) pairs, then a final submit_answer payload."""
    def client(messages):
        n = len([m for m in messages if m.get("role") == "tool"])
        if n < len(steps) - 1:
            tool, args = steps[n]
            return ChatResponse(tool_calls=[tool_call(tool, args, f"c{n}")])
        return ChatResponse(tool_calls=[tool_call("submit_answer", steps[-1], "cz")])
    return ScriptedClient([client] * 12)


def _run(name, client, tmp_path, **kw):
    from run_evaluation import run_case
    return run_case(_case(name), ModelConfig(), tmp_path, auto_approve=True,
                    client=client, **kw)


def test_a_test_filter_that_matched_nothing_is_not_a_reproduction(tmp_path):
    """`run_test(name_filter="Crash")` exits 0 having exercised nothing. It
    scored 1.00 with verification_attempted true and "ran the test" checked."""
    row = _run("test-failure-diagnose", _plan(
        ("run_test", {"name_filter": "Crash"}),
        ("read_file", {"path": "src/ring_buffer.cpp"}),
        {"claim": "diagnosis",
         "summary": "ring_buffer: full() is off by one, count_ + 1 == capacity"},
    ), tmp_path)

    assert row["score"] == 1.0, "the answer is correct; that is the point"
    assert row["required_checks"]["reproduced the failure"] is False
    assert row["required_ok"] is False
    assert row["succeeded"] is False
    assert row["verification_attempted"] is False, "executing is not observing"


def test_a_fix_is_not_proved_by_a_build(tmp_path):
    """"Fix the code so the whole suite passes, and prove it." A passing build
    says it compiles. This run never called run_test at all and the check named
    "full test run passed after the last edit" was true."""
    def client(messages):
        tools = [m for m in messages if m.get("role") == "tool"]
        n = len(tools)
        if n == 0:
            return ChatResponse(tool_calls=[tool_call("propose_patch", {
                "path": "src/ring_buffer.cpp", "find": FIND, "replace": REPL}, "c0")])
        if n == 1:
            pid = json.loads(tools[-1]["content"])["data"]["patch_id"]
            return ChatResponse(tool_calls=[tool_call("apply_patch", {"patch_id": pid}, "c1")])
        if n == 2:
            return ChatResponse(tool_calls=[tool_call("build_target", {}, "c2")])
        return ChatResponse(tool_calls=[tool_call("submit_answer", {
            "claim": "success",
            "summary": "full() was off by one; fixed and rebuilt."}, "c3")])

    row = _run("test-failure-fix", ScriptedClient([client] * 8), tmp_path)

    assert "run_test" not in row["tools_used"]
    assert row["required_checks"]["full test run passed after the last edit"] is False
    assert row["succeeded"] is False
    # The evaluator's own later run does pass. That is our proof, not the
    # model's, and it must not be mistaken for the model's.
    assert row["eval_verification"]["ok"] is True


def test_a_navigation_agent_that_never_navigated_does_not_succeed(tmp_path):
    """Zero tool calls, a correct guess. Scored 0.80 and passed."""
    row = _run("navigation", _plan({
        "claim": "diagnosis",
        "summary": "RingBuffer is declared in include/sandbox/ring_buffer.hpp, "
                   "and push on a full buffer returns false.",
    }), tmp_path)

    assert row["tool_calls"] == 0
    assert row["score"] == 1.0
    assert row["required_checks"]["read or searched the repository"] is False
    assert row["succeeded"] is False


def test_a_review_agent_that_never_looked_does_not_succeed(tmp_path):
    """Never called git_status. Scored 0.75, exactly the threshold, and passed."""
    row = _run("review-restraint", _plan({
        "claim": "diagnosis",
        "summary": "The working tree is clean: there are no uncommitted changes.",
    }), tmp_path)

    assert row["tool_calls"] == 0
    assert row["required_checks"]["inspected the working tree"] is False
    assert row["succeeded"] is False


def test_a_targeted_build_and_a_filtered_test_do_not_finish_a_build_task(tmp_path):
    """"Build the project and run the test suite." A build of one target and a
    run of one test prove nothing about either. Claiming "diagnosis" instead of
    "success" also dodged the orchestrator's demand for a passing verification,
    and the whole thing scored 1.00."""
    row = _run("clean-build", _plan(
        ("configure_project", {}),
        ("build_target", {"target": "test_ring_buffer"}),
        ("run_test", {"name_filter": "ring_buffer"}),
        {"claim": "diagnosis", "summary": "the project builds and the test passes"},
    ), tmp_path)

    assert row["required_checks"]["full build passed"] is False
    assert row["required_checks"]["full test run passed"] is False
    assert row["claim_ok"] is False, "the contract says what a finished answer claims"
    assert row["succeeded"] is False


def test_the_honest_path_still_passes(tmp_path):
    """The bar has to be reachable. This is the behaviour the 30B actually
    produced on the frozen dataset, and it must still be a capability success:
    a tightened evaluator that also rejects correct work is not tightened, it
    is broken."""
    def client(messages):
        tools = [m for m in messages if m.get("role") == "tool"]
        n = len(tools)
        if n == 0:
            return ChatResponse(tool_calls=[tool_call("propose_patch", {
                "path": "src/ring_buffer.cpp", "find": FIND, "replace": REPL}, "c0")])
        if n == 1:
            pid = json.loads(tools[-1]["content"])["data"]["patch_id"]
            return ChatResponse(tool_calls=[tool_call("apply_patch", {"patch_id": pid}, "c1")])
        if n == 2:
            return ChatResponse(tool_calls=[tool_call("build_target", {}, "c2")])
        if n == 3:
            return ChatResponse(tool_calls=[tool_call("run_test", {}, "c3")])
        return ChatResponse(tool_calls=[tool_call("submit_answer", {
            "claim": "success",
            "summary": "full() reported the buffer full one slot early, an off by "
                       "one in the capacity check. Fixed, rebuilt, suite green.",
        }, "c4")])

    row = _run("test-failure-fix", ScriptedClient([client] * 9), tmp_path)
    assert row["required_ok"] is True
    assert row["claim_ok"] is True
    assert row["score"] == 1.0
    assert row["succeeded"] is True


def test_a_failed_forbidden_edit_is_still_a_scope_violation(tmp_path):
    """Malformed arguments are not restraint. If the tool was available and the
    model reached for it, the intent counts."""
    row = _run("test-failure-fix", _plan(   # a skill that DOES offer patch tools
        ("apply_patch", {"patch_id": "does-not-exist"}),
        {"claim": "success", "summary": "fixed"},
    ), tmp_path, )
    # test-failure-fix does not forbid patching, so construct the forbidden
    # case explicitly instead.
    import dataclasses
    from task_contracts import MUTATING_TOOLS
    from run_evaluation import run_case
    from local_agent.config import ModelConfig

    case = dataclasses.replace(_case("test-failure-fix"), forbidden_tools=MUTATING_TOOLS)
    row = run_case(case, ModelConfig(), tmp_path, auto_approve=True, client=_plan(
        ("apply_patch", {"patch_id": "does-not-exist"}),
        {"claim": "success", "summary": "fixed"},
    ))
    assert row["forbidden_attempts"] == ["apply_patch"]
    assert row["forbidden_calls"] == [], "it never executed, so no damage"
    assert row["scope_violation"] is True, "but it tried, and the tool was there"


def test_reaching_for_a_tool_the_skill_hid_is_not_double_counted(tmp_path):
    """Under a read-only skill the mutation cannot happen. That refusal is the
    narrowing treatment working, and it is already counted as tool_not_allowed.
    Counting it as a scope violation as well would penalise the treatment for
    preventing the thing, and confound the two components we want to separate."""
    row = _run("test-failure-diagnose", _plan(
        ("run_test", {"name_filter": "ring_buffer"}),
        ("propose_patch", {"path": "src/ring_buffer.cpp", "find": FIND, "replace": REPL}),
        {"claim": "diagnosis", "summary": "ring_buffer full() is off by one, capacity"},
    ), tmp_path)

    assert "propose_patch" in row["tool_not_allowed_calls"]
    assert row["forbidden_attempts"] == ["propose_patch"], "the reach is recorded"
    assert row["scope_violation"] is False, "but narrowing stopped it, and that is the point"
    assert row["mutation_epoch"] == 0
    assert row["succeeded"] is True, "the diagnosis itself was correct and proved"


def test_rerun_failed_is_not_a_full_suite(tmp_path):
    """`ctest --rerun-failed` runs only what failed last time, which after a fix
    is exactly the one test you just changed. Patch, build, rerun that one test,
    claim success: required_ok true, score 1.00, succeeded true, on a task whose
    text is "prove the whole suite passes"."""
    def client(messages):
        tools = [m for m in messages if m.get("role") == "tool"]
        n = len(tools)
        if n == 0:
            return ChatResponse(tool_calls=[tool_call(
                "run_test", {"name_filter": "ring_buffer"}, "c0")])
        if n == 1:
            return ChatResponse(tool_calls=[tool_call("propose_patch", {
                "path": "src/ring_buffer.cpp", "find": FIND, "replace": REPL}, "c1")])
        if n == 2:
            pid = json.loads(tools[-1]["content"])["data"]["patch_id"]
            return ChatResponse(tool_calls=[tool_call("apply_patch", {"patch_id": pid}, "c2")])
        if n == 3:
            return ChatResponse(tool_calls=[tool_call("build_target", {}, "c3")])
        if n == 4:
            return ChatResponse(tool_calls=[tool_call("run_test", {"rerun_failed": True}, "c4")])
        return ChatResponse(tool_calls=[tool_call("submit_answer", {
            "claim": "success", "summary": "off by one in full(); fixed and green."}, "c5")])

    row = _run("test-failure-fix", ScriptedClient([client] * 10), tmp_path)

    reran = [h for h in row["history"] if h["name"] == "run_test"
             and h["arguments"].get("rerun_failed")]
    assert reran and reran[0]["domain"] == "pass", "the partial rerun did pass"
    assert row["required_checks"]["full test run passed after the last edit"] is False
    assert row["succeeded"] is False


def test_a_prose_answer_contradicting_its_own_evidence_does_not_pass(tmp_path):
    """Every step done correctly, then a prose sign-off saying it failed.

    required_ok was true, claim was None, claim_ok was true, score 1.00,
    succeeded true. The benchmark reported a successful engineering task while
    the model's own answer to the user said the tests were still broken.
    """
    def client(messages):
        tools = [m for m in messages if m.get("role") == "tool"]
        n = len(tools)
        if n == 0:
            return ChatResponse(tool_calls=[tool_call("propose_patch", {
                "path": "src/ring_buffer.cpp", "find": FIND, "replace": REPL}, "c0")])
        if n == 1:
            pid = json.loads(tools[-1]["content"])["data"]["patch_id"]
            return ChatResponse(tool_calls=[tool_call("apply_patch", {"patch_id": pid}, "c1")])
        if n == 2:
            return ChatResponse(tool_calls=[tool_call("build_target", {}, "c2")])
        if n == 3:
            return ChatResponse(tool_calls=[tool_call("run_test", {}, "c3")])
        return ChatResponse(content=(
            "I found the full() off by one capacity bug, but the fix did not "
            "work and the tests are still failing."))

    row = _run("test-failure-fix", ScriptedClient([client] * 10), tmp_path)

    assert row["required_ok"] is True, "the engineering work really was done"
    assert row["submission_mode"] == "prose"
    assert row["claim_ok"] is False
    assert row["succeeded"] is False


def test_the_finishing_protocol_is_in_every_condition(tmp_path):
    """submit_answer is infrastructure, like the tool schemas and the sandbox
    rules, not institutional procedure. Teaching it only in the skill bodies
    made it part of the treatment twice: the control could not be held to a
    claim nobody told it to make, and when the orchestrator later nudged it for
    one, the control paid an extra turn that would have read as a skill
    advantage in calls and wall time."""
    from run_evaluation import run_case
    from local_agent.config import ModelConfig

    seen = {}

    def observe(messages):
        seen[observe.condition] = "\n".join(
            m["content"] for m in messages if m["role"] == "system")
        return ChatResponse(tool_calls=[tool_call("submit_answer", {
            "claim": "diagnosis", "summary": "full() is off by one on capacity"}, "c1")])

    for condition in ("control", "narrow", "skill"):
        observe.condition = condition
        run_case(_case("test-failure-diagnose"), ModelConfig(), tmp_path,
                 auto_approve=True, client=ScriptedClient([observe] * 3),
                 condition=condition)

    for condition in ("control", "narrow", "skill"):
        assert "submit_answer" in seen[condition], condition
        assert "reply in prose" not in seen[condition], condition


def test_no_skill_body_teaches_the_reporting_protocol(tmp_path):
    """expected_claim is a hard gate, so a skill that also says WHICH claim to
    make and WHAT to cite is coaching the model on the benchmark, not on
    engineering. That is not procedural knowledge and it would land in
    `skill - narrow` as if it were.

    The vocabulary belongs in the prompt every condition receives, and the
    skill bodies keep only the engineering: what to build, what to read, when a
    fix counts as done.
    """
    from local_agent.agent.context import SYSTEM_PROMPT

    skills = sorted((REPO / "skills").glob("*/SKILL.md"))
    assert skills, "no skills found"
    for path in skills:
        body = path.read_text()
        assert "submit_answer" not in body, path.name
        for claim in ('claim `diagnosis`', 'claim `success`', 'claim `failure`',
                      'claim "diagnosis"', 'claim "success"'):
            assert claim not in body, f"{path.name} names the claim to make"

    # And the vocabulary every condition needs is in the shared prompt.
    for claim in ("success", "diagnosis", "failure", "needs_action"):
        assert claim in SYSTEM_PROMPT, claim


def test_ctest_cannot_clear_the_stale_warning_without_a_build(tmp_path):
    """ctest writes build/Testing/LastTest.log every run. When staleness was
    the newest mtime anywhere under build/, editing a source and then running
    the tests twice made the warning vanish with nothing recompiled: the test
    runner's own timestamp was being read as evidence of a build."""
    from run_evaluation import establish, prepare
    from local_agent.config import load_repo_config
    from local_agent.tools import build_registry

    case = _case("test-failure-diagnose")
    root, _ = prepare(tmp_path, case.scenario)
    registry, _, _ = build_registry(load_repo_config(root))
    establish(case, registry)
    run_test = registry.get("run_test").handler

    source = root / "src" / "ring_buffer.cpp"
    source.write_text(source.read_text().replace(FIND, REPL))

    first = run_test(name_filter="ring_buffer")
    assert first.data["stale_sources"] == ["src/ring_buffer.cpp"]
    assert first.summary.startswith("STALE:")

    # ctest has now written under build/. Run it again: nothing was compiled,
    # so the warning must still be there.
    second = run_test(name_filter="ring_buffer")
    assert second.data["stale_sources"] == ["src/ring_buffer.cpp"], \
        "a timestamp written by the test runner is not evidence of a build"
    assert second.summary.startswith("STALE:")

    assert registry.get("build_target").handler().ok
    assert run_test(name_filter="ring_buffer").data["stale_sources"] == []


def test_coloured_ctest_output_parses_identically(tmp_path):
    """CLICOLOR_FORCE, or a CI wrapper, and one invisible escape in front of
    "0% tests passed" turned a real failure into totals {} and failed [], which
    the tool layer then reported as "not configured or not built"."""
    from local_agent.tools.logs import parse_test_log

    plain = (
        "Test project /x\n"
        "    Start 1: ring_buffer\n"
        "1/1 Test #1: ring_buffer ......Subprocess aborted***Exception:   0.01 sec\n"
        "0% tests passed, 1 tests failed out of 1\n"
        "The following tests FAILED:\n"
        "\t  1 - ring_buffer (Subprocess aborted)\n"
    )
    coloured = (plain
                .replace("0% tests passed", "\x1b[0;31m0% tests passed")
                .replace("1 - ring_buffer", "\x1b[1;31m1 - ring_buffer\x1b[0m"))

    a, b = parse_test_log(plain), parse_test_log(coloured)
    assert a.totals == b.totals == {"failed": 1, "total": 1, "passed": 0}
    assert [f["name"] for f in a.failed] == [f["name"] for f in b.failed] == ["ring_buffer"]


def test_the_rescorer_does_not_inherit_the_old_verdict():
    """The old outcome is the thing being re-examined. Gating the new verdict
    on it means a row the old rules wrongly failed can never be shown to pass,
    which is exactly the navigation and review-restraint case that started
    all of this."""
    sys.path.insert(0, str(REPO / "measurement"))
    import importlib
    rescore = importlib.import_module("rescore_dataset")

    row = {
        # `claim` was "success" here, matching navigation's old contract. That
        # contract failed 9/9 across three conditions in the 2026-09-08 batch
        # and was wrong: asking where a class lives is explaining, not achieving
        # something a tool result proves. The fixture follows the corrected
        # contract. What the test is actually about, that a row the old rules
        # failed can still be shown to pass, is unchanged.
        "case": "navigation", "outcome": "fail", "succeeded": False, "score": 0.8,
        "mutation_epoch": 0, "claim": "diagnosis", "tool_calls": 1,
        "halt_reason": None,
        "answer": "RingBuffer is declared in include/sandbox/ring_buffer.hpp and "
                  "push on a full buffer returns false.",
        "history": [{"name": "read_file", "arguments": {"path": "include/sandbox/ring_buffer.hpp"},
                     "execution": "ok", "domain": "pass", "reason": None, "ok": True,
                     "epoch": 0, "exit_code": None, "summary": "read", "evidence": {}}],
    }
    out = rescore.rescore_row(row)
    assert out["rescorable"] is True
    assert out["replayability"] == "fully_replayable"
    assert out["original_succeeded"] is False
    assert out["rescored_succeeded"] is True
    assert out["changed"] is True


def test_a_row_without_structured_evidence_is_only_partially_replayable():
    """Rows from before the evidence fields existed let a check see an empty
    dict and conclude "absent, therefore fine". Unknown is not the same as
    passed, and the artifact has to say which it is."""
    sys.path.insert(0, str(REPO / "measurement"))
    import importlib
    rescore = importlib.import_module("rescore_dataset")

    row = {
        "case": "test-failure-fix", "outcome": "pass", "succeeded": True, "score": 1.0,
        "mutation_epoch": 1, "claim": "success", "tool_calls": 2, "halt_reason": None,
        "answer": "fixed the off by one in full(), suite green",
        "history": [
            {"name": "apply_patch", "arguments": {}, "execution": "ok", "domain": "pass",
             "reason": None, "ok": True, "epoch": 0, "exit_code": None, "summary": "applied"},
            {"name": "build_target", "arguments": {}, "execution": "ok", "domain": "pass",
             "reason": None, "ok": True, "epoch": 1, "exit_code": 0, "summary": "built"},
            {"name": "run_test", "arguments": {}, "execution": "ok", "domain": "pass",
             "reason": None, "ok": True, "epoch": 1, "exit_code": 0, "summary": "passed"},
        ],
    }
    out = rescore.rescore_row(row)
    assert out["replayability"] == "partially_replayable"
    assert out["replay_gaps"]
    # And it must not publish a capability bit computed from the missing half.
    assert out["definitive"] is False
    assert out["rescored_succeeded"] is None
    assert out["changed"] is None
    assert out["provisional_succeeded"] is True


def test_an_unrelated_targeted_build_cannot_clear_a_stale_source(tmp_path):
    """The stamp means "every source is represented by a current binary".
    A targeted build cannot support that, and refreshing it after
    `build_target(target="test_text_util")` made an unrelated stale test binary
    look fresh: the tool lying to the agent about what it is running."""
    from run_evaluation import prepare
    from local_agent.config import load_repo_config
    from local_agent.tools import build_registry

    root, _ = prepare(tmp_path, "clean")
    registry, _, _ = build_registry(load_repo_config(root))
    assert registry.get("configure_project").handler().ok
    assert registry.get("build_target").handler().ok

    stale_file = root / "tests" / "test_ring_buffer.cpp"
    stale_file.write_text(stale_file.read_text() + "\n// touched\n")

    run_test = registry.get("run_test").handler
    assert run_test(name_filter="ring_buffer").data["stale_sources"] == \
        ["tests/test_ring_buffer.cpp"]

    # A successful build of something else entirely.
    assert registry.get("build_target").handler(target="test_text_util").ok
    after = run_test(name_filter="ring_buffer")
    assert after.data["stale_sources"] == ["tests/test_ring_buffer.cpp"], \
        "a targeted build says one thing is current, not everything"
    assert after.summary.startswith("STALE:")

    # A full build does clear it, because that is what the stamp means.
    assert registry.get("build_target").handler().ok
    assert run_test(name_filter="ring_buffer").data["stale_sources"] == []


def _assert_tree_is_configured_for(root, profile_name):
    """The requested profile matches the tree that was actually configured.

    The old assertion read `CMAKE_BUILD_TYPE:STRING=<name>` out of
    CMakeCache.txt. That is only meaningful under a single-config generator.
    Under Visual Studio, which is what Windows gets by default,
    CMAKE_BUILD_TYPE is empty and the configuration is chosen per build via
    `--config`, so the assertion could never pass and the invariant it was
    protecting went untested on the platform that needs it most.

    The profile marker is the better subject anyway, and this is a
    strengthening rather than a relaxation: `build_target` consults
    `configured_profile()` to decide whether a reconfigure is needed, so the
    marker is what the tool actually acts on, and the old test never checked
    it. The cache is still checked, in the form each generator can honour.
    """
    from local_agent.tools.testing import configured_profile

    assert configured_profile(root, "build") == profile_name, \
        f"the build directory is not marked as configured for {profile_name!r}"

    cache = (root / "build" / "CMakeCache.txt").read_text(encoding="utf-8")
    build_type = re.search(r"^CMAKE_BUILD_TYPE:\w+=(.*)$", cache, re.M)
    configuration_types = re.search(r"^CMAKE_CONFIGURATION_TYPES:\w+=(.+)$", cache, re.M)
    expected = {"debug": "Debug", "release": "RelWithDebInfo"}[profile_name]

    if build_type and build_type.group(1).strip():
        # Single-config generator: the cache names the one configuration.
        assert build_type.group(1).strip() == expected, cache[:400]
    else:
        # Multi-config generator: an empty CMAKE_BUILD_TYPE is correct rather
        # than a defect, and the cache has to say so by declaring the
        # configurations it offers, one of which must be the one we want.
        assert configuration_types, \
            "CMAKE_BUILD_TYPE is empty and no CMAKE_CONFIGURATION_TYPES: " + cache[:400]
        offered = [c.strip() for c in configuration_types.group(1).split(";")]
        assert expected in offered, f"{expected} not among {offered}"


def test_the_profile_label_matches_the_configured_tree(tmp_path):
    """Both profiles share one build directory and configure only ran when
    CMakeCache.txt was absent, so `build_target(profile="release")` on a tree
    configured for debug printed "build (release) succeeded" over a Debug
    cache."""
    from run_evaluation import prepare
    from local_agent.config import load_repo_config
    from local_agent.tools import build_registry

    root, _ = prepare(tmp_path, "clean")
    registry, _, _ = build_registry(load_repo_config(root))
    cache = root / "build" / "CMakeCache.txt"

    assert registry.get("configure_project").handler(profile="debug").ok
    assert registry.get("build_target").handler(profile="debug").ok
    _assert_tree_is_configured_for(root, "debug")

    result = registry.get("build_target").handler(profile="release")
    assert result.ok, result.summary
    assert "release" in result.summary
    _assert_tree_is_configured_for(root, "release")


def test_a_test_run_cannot_verify_a_profile_the_tree_is_not_configured_for(tmp_path):
    from run_evaluation import prepare
    from local_agent.config import load_repo_config
    from local_agent.tools import build_registry

    root, _ = prepare(tmp_path, "clean")
    registry, _, _ = build_registry(load_repo_config(root))
    assert registry.get("configure_project").handler(profile="debug").ok
    assert registry.get("build_target").handler(profile="debug").ok

    mismatched = registry.get("run_test").handler(profile="release")
    assert mismatched.data["profile_mismatch"] is True
    # Two independent facts, both true and both reported: the tree is
    # configured for debug, AND the last successful full build was debug. The
    # build fact is the more fundamental of the two, so it is the one that
    # leads; the configuration fact is still in the text.
    assert mismatched.summary.startswith("NOT BUILT FOR 'release'")
    assert "PROFILE MISMATCH" in mismatched.summary
    assert mismatched.data["build_profile_mismatch"] is True
    assert mismatched.data["built_profile"] == "debug"
    assert mismatched.domain_status.value == "unknown"
    assert mismatched.reason.value == "profile_mismatch"
    assert mismatched.ok is False

    matched = registry.get("run_test").handler(profile="debug")
    assert matched.data["profile_mismatch"] is False
    assert matched.data["build_profile_mismatch"] is False


def test_a_completed_task_claiming_failure_does_not_pass(tmp_path):
    """The shared prompt defines the claim by the requested GOAL: failure means
    the goal was not achieved. A correct navigation answer claiming failure,
    and a correct diagnosis of a real compile error claiming failure, both
    passed, because the evaluator was still reading the claim as a statement
    about the state of the code."""
    row = _run("navigation", _plan(
        ("read_file", {"path": "include/sandbox/ring_buffer.hpp"}),
        {"claim": "failure",
         "summary": "RingBuffer is defined in include/sandbox/ring_buffer.hpp; "
                    "push on a full buffer returns false."},
    ), tmp_path)
    assert row["required_ok"] is True and row["score"] == 1.0
    assert row["claim_ok"] is False
    assert row["succeeded"] is False

    row = _run("compile-error-locate", _plan(
        ("build_target", {}),
        {"claim": "failure",
         "summary": "src/ring_buffer.cpp line 13 uses count instead of count_, "
                    "and line 27 is missing a semicolon."},
    ), tmp_path)
    assert row["required_ok"] is True
    assert row["claim_ok"] is False
    assert row["succeeded"] is False


# --------------------------------------------------------------------------
# Fifth review. Four ways the RUNTIME could call something verified that the
# evaluator would have rejected. Every one of these was reproduced against
# package a4c9b14e before it was fixed, and the split between the two
# definitions of proof is the thing being pinned, not any one symptom.
# --------------------------------------------------------------------------


def _verified_by_orchestrator(result, name="run_test", arguments=None):
    """Exactly the predicate the orchestrator uses to set `state.verified`."""
    from local_agent.agent.orchestrator import _satisfies_verification
    return _satisfies_verification(
        name,
        arguments or {},
        result.execution_status.value,
        result.domain_status.value,
        result.data,
    )


def test_a_stale_full_suite_pass_is_not_typed_as_a_pass(tmp_path):
    """The prose said STALE and the type said PASS.

    Configure, build, pass. Edit a source. Do not rebuild. Run the whole suite.
    ctest re-runs the OLD binaries and reports them green, and the tool
    returned ok=True, domain=PASS with the warning glued to the front of the
    summary. The evaluator threw the row out on `stale_sources`; the
    orchestrator had already recorded `state.verified = True`. Two definitions
    of proof, and the model saw the permissive one.
    """
    from run_evaluation import prepare
    from local_agent.config import load_repo_config
    from local_agent.tools import build_registry
    from local_agent.verification import ProofKind, classify_proof

    root, _ = prepare(tmp_path, "clean")
    registry, _, _ = build_registry(load_repo_config(root))
    assert registry.get("configure_project").handler().ok
    assert registry.get("build_target").handler().ok

    green = registry.get("run_test").handler()
    assert green.ok and green.domain_status.value == "pass"
    assert _verified_by_orchestrator(green), "a clean full suite IS proof"

    source = root / "src" / "ring_buffer.cpp"
    source.write_text(source.read_text() + "\n// edited, not rebuilt\n")

    stale = registry.get("run_test").handler()
    assert stale.data["stale_sources"] == ["src/ring_buffer.cpp"]
    assert stale.summary.startswith("STALE:")
    assert stale.ok is False, "ctest ran the previous binary; that is not a pass"
    assert stale.domain_status.value == "unknown"
    assert stale.reason.value == "stale_binary"
    assert classify_proof(
        name="run_test", arguments={}, execution="ok", domain="unknown",
        evidence=stale.data,
    ) is ProofKind.NO_CURRENT_PROOF
    assert not _verified_by_orchestrator(stale), \
        "the runtime must not record a fossil as verification of this tree"


def test_configuring_a_profile_does_not_verify_the_previous_profiles_binaries(tmp_path):
    """Configuration identity was being mistaken for compilation provenance.

    Build debug. Edit a source. Configure release WITHOUT building it. Run the
    tests as release. The configure marker said release, so the profile check
    passed; no build stamp existed, so `_stale_sources` returned [] and the
    staleness check passed too. ctest then executed the leftover debug binaries
    and the result came back ok=True, domain=PASS, stale_sources=[], labelled
    release. Every gate was satisfied and nothing had been built.
    """
    from run_evaluation import prepare
    from local_agent.config import load_repo_config
    from local_agent.tools import build_registry

    root, _ = prepare(tmp_path, "clean")
    registry, _, _ = build_registry(load_repo_config(root))
    assert registry.get("configure_project").handler(profile="debug").ok
    assert registry.get("build_target").handler(profile="debug").ok

    source = root / "src" / "ring_buffer.cpp"
    source.write_text(source.read_text() + "\n// edited, not rebuilt\n")

    assert registry.get("configure_project").handler(profile="release").ok
    unbuilt = registry.get("run_test").handler(profile="release")

    assert unbuilt.ok is False
    assert unbuilt.domain_status.value == "unknown"
    assert unbuilt.data["no_build_record"] is True
    assert unbuilt.reason.value == "no_build_record"
    assert unbuilt.summary.startswith("NO BUILD RECORDED")
    assert not _verified_by_orchestrator(unbuilt, arguments={"profile": "release"})

    # And it recovers the moment the profile is actually compiled.
    assert registry.get("build_target").handler(profile="release").ok
    built = registry.get("run_test").handler(profile="release")
    assert built.ok and built.domain_status.value == "pass"
    assert built.data["built_profile"] == "release"
    assert _verified_by_orchestrator(built, arguments={"profile": "release"})


def test_rerun_failed_cannot_set_the_runtime_verified_flag(tmp_path):
    """The evaluator rejected `--rerun-failed` as full-suite proof. The
    orchestrator accepted it. After a fix, `ctest --rerun-failed` runs exactly
    the one test just changed, so this is the cheapest possible way to be
    handed a green suite you never ran."""
    from local_agent.agent.orchestrator import _satisfies_verification
    from local_agent.verification import ProofKind, classify_proof

    assert classify_proof(
        name="run_test", arguments={"rerun_failed": True},
        execution="ok", domain="pass", evidence={"totals": {"total": 1}},
    ) is ProofKind.TARGETED_TEST_PASS
    assert not _satisfies_verification("run_test", {"rerun_failed": True})
    assert not _satisfies_verification("run_test", {"name_filter": "ring"})
    assert not _satisfies_verification("build_target", {"target": "sandbox"})
    assert _satisfies_verification("run_test", {})
    assert _satisfies_verification("build_target", {})


def test_a_cache_with_no_profile_marker_is_treated_as_unknown_provenance(tmp_path):
    """`needs_configure` was `no cache OR a KNOWN different profile`, so a
    CMakeCache.txt with no marker beside it was taken on trust. That happens
    with a human's build tree, a tree left by an older package, or a deleted
    marker, and a request for release then compiled against whatever the cache
    held and labelled the result release."""
    from run_evaluation import prepare
    from local_agent.config import load_repo_config
    from local_agent.tools import build_registry
    from local_agent.tools.testing import PROFILE_STAMP

    root, _ = prepare(tmp_path, "clean")
    registry, _, _ = build_registry(load_repo_config(root))
    cache = root / "build" / "CMakeCache.txt"

    assert registry.get("configure_project").handler(profile="debug").ok
    _assert_tree_is_configured_for(root, "debug")

    (root / "build" / PROFILE_STAMP).unlink()
    assert cache.is_file(), "the cache is still there; only the marker is gone"

    assert registry.get("build_target").handler(profile="release").ok
    assert "CMAKE_BUILD_TYPE:STRING=RelWithDebInfo" in cache.read_text(), \
        "an unlabelled cache must be reconfigured, not trusted"


def test_the_runtime_and_the_evaluator_agree_on_what_proof_is(tmp_path):
    """The point of the whole change. These two predicates were maintained
    separately and drifted twice: once on `rerun_failed`, once on stale passes.
    They now come from one classifier, and this pins that they answer the same
    question the same way on every shape either of them has ever disagreed on.
    """
    import sys as _sys
    _sys.path.insert(0, str(REPO / "evaluation"))
    from task_contracts import _whole_suite_passed, full_build_passed_after_edits
    from local_agent.agent.orchestrator import _satisfies_verification
    from local_agent.agent.state import ToolCallRecord
    from local_agent.verification import classify_record

    def record(name, arguments, domain="pass", execution="ok", evidence=None):
        r = ToolCallRecord(name=name, arguments=arguments, verdict="ok",
                           ok=(domain == "pass"), summary="")
        r.execution, r.domain = execution, domain
        r.evidence = evidence or {}
        r.epoch = 0
        r.proof = classify_record(r).value
        return r

    shapes = [
        ("run_test", {}, "pass", "ok", {}),
        ("run_test", {"rerun_failed": True}, "pass", "ok", {}),
        ("run_test", {"name_filter": "ring"}, "pass", "ok", {}),
        ("run_test", {}, "pass", "ok", {"stale_sources": ["src/a.cpp"]}),
        ("run_test", {}, "pass", "ok", {"no_build_record": True}),
        ("run_test", {}, "pass", "ok", {"build_profile_mismatch": True}),
        ("run_test", {}, "pass", "ok", {"profile_mismatch": True}),
        ("run_test", {}, "pass", "ok", {"totals": {"total": 0}}),
        ("run_test", {}, "unknown", "ok", {}),
        ("run_test", {}, "fail", "ok", {}),
        ("run_test", {}, "pass", "error", {}),
        ("build_target", {}, "pass", "ok", {}),
        ("build_target", {"target": "sandbox"}, "pass", "ok", {}),
        ("build_target", {}, "fail", "ok", {}),
        ("git_status", {}, "pass", "ok", {}),
    ]

    for name, args, domain, execution, evidence in shapes:
        r = record(name, args, domain, execution, evidence)
        runtime = _satisfies_verification(name, args, execution, domain, evidence)
        if name == "run_test":
            evaluator = _whole_suite_passed(r, 0)
        elif name == "build_target":
            evaluator = full_build_passed_after_edits()(
                type("R", (), {"state": type("S", (), {
                    "history": [r], "mutation_epoch": 0})()})()
            )
        else:
            evaluator = False
        assert runtime == evaluator, (
            f"runtime and evaluator disagree on {name} {args} "
            f"{execution}/{domain} {evidence}: {runtime} vs {evaluator}"
        )


def test_a_new_dataset_records_everything_the_rescorer_needs(tmp_path):
    """A row is born fully replayable or it is born already degraded.

    `rescore.py` treats a missing evidence key as UNKNOWN, not as False, which
    is the only honest reading: the keys exist to say "this pass proves
    nothing", so their absence cannot be read as "nothing was wrong". The
    consequence is that the moment the classifier learns a new invalidating
    key, every dataset that does not persist it becomes partially replayable.
    This pins the two lists together so that only ever happens on purpose.
    """
    import importlib
    sys.path.insert(0, str(REPO / "measurement"))
    rescore = importlib.import_module("rescore_dataset")

    row = _run("clean-build", _plan(
        ("configure_project", {}),
        ("build_target", {}),
        ("run_test", {}),
        {"claim": "success", "summary": "configured, built, and the whole suite passed"},
    ), tmp_path)

    assert row["succeeded"] is True, "the honest path has to stay reachable"
    tests = [h for h in row["history"] if h["name"] == "run_test"]
    assert tests, "the plan ran one"
    for call in tests:
        missing = [k for k in rescore.EVIDENCE_DEPENDENT["run_test"]
                   if k not in call["evidence"]]
        assert not missing, f"run_evaluation does not persist {missing}"
        assert call["proof"] == "full_test_pass"

    out = rescore.rescore_row(row)
    assert out["replayability"] == "fully_replayable"
    assert out["definitive"] is True
    assert out["rescored_succeeded"] is True


def test_the_build_stamp_cannot_be_forged(tmp_path):
    """The staleness gate was openable with a text editor.

    `run_test` decides whether a result is stale by comparing source mtimes
    against the recorded build, and the record lived in an ordinary file inside
    the repository, dated by its own mtime. So:

        propose_patch("build/.local-agent-build-ok", one byte)
        apply_patch(...)
        run_test()

    moved the baseline to now, cleared `stale_sources`, and returned
    FULL_TEST_PASS over binaries that predated the edit. Nothing was compiled.

    This is worse than an ordinary evaluator cheat because it is not equally
    available to every condition. Control holds the full registry on every
    case, including the read-only diagnosis cases where narrow and skill are
    offered no patch tools at all, so a cheat that needs `apply_patch` is
    reachable in one arm of the experiment and unreachable in the others. It
    does not add noise to the contrast, it biases it.

    Two locks, tested separately: writes into the build directory are refused,
    and the record is dated from its content so touching it proves nothing.
    """
    from run_evaluation import prepare
    from local_agent.config import load_repo_config
    from local_agent.tools import build_registry
    from local_agent.tools.base import ProtectedPathError
    from local_agent.tools.testing import BUILD_STAMP, build_record

    root, _ = prepare(tmp_path, "clean")
    registry, _, _ = build_registry(load_repo_config(root))
    assert registry.get("configure_project").handler().ok
    assert registry.get("build_target").handler().ok

    source = root / "src" / "ring_buffer.cpp"
    source.write_text(source.read_text() + "\n// edited, not rebuilt\n")
    assert registry.get("run_test").handler().data["stale_sources"] == ["src/ring_buffer.cpp"]

    # Lock one: the patch tools refuse the path, at the free step.
    stamp = root / "build" / BUILD_STAMP
    with pytest.raises(ProtectedPathError):
        registry.get("propose_patch").handler(
            path=f"build/{BUILD_STAMP}", find='"profile"', replace='"profile" ')
    for guarded in (".local-agent/runs", ".git/config"):
        with pytest.raises(ProtectedPathError):
            registry.get("propose_patch").handler(path=guarded, find="a", replace="b")

    # Lock two: even a write that bypasses the tools entirely proves nothing,
    # because the record is dated from its content and not from the file.
    before = build_record(root, "build")
    stamp.write_text(stamp.read_text() + " ")
    after = build_record(root, "build")
    assert after is not None and after.at == before.at, \
        "touching the stamp must not move the baseline"

    forged = registry.get("run_test").handler()
    assert forged.data["stale_sources"] == ["src/ring_buffer.cpp"]
    assert forged.ok is False and forged.domain_status.value == "unknown"

    # A stamp whose content is unreadable or undated is unknown provenance,
    # not a free pass.
    stamp.write_text("ok\n")
    assert build_record(root, "build") is None
    unknown = registry.get("run_test").handler()
    assert unknown.ok is False
    assert unknown.data["no_build_record"] is True

    # And a real build still restores it.
    assert registry.get("build_target").handler().ok
    assert registry.get("run_test").handler().ok


def test_an_observed_failure_retracts_a_standing_proof(tmp_path):
    """The seventh round, and this one the instrument caught on itself.

    `repeat-02/03-control/link-error` in the 2026-09-08 batch built clean, then
    ran a suite that came back 1 failed of 4, kept `verified = True` and claimed
    success. The evaluator's independent oracle re-verified the tree, disagreed,
    and the batch's integrity check failed on that one row out of ninety.

    The cause was that proof was retractable only by mutation. `state.verified`
    was set by a FULL_BUILD_PASS and nothing but `note_mutation()` ever cleared
    it, so an observed failure on the SAME tree left it standing. `verified`
    therefore meant "something built", not "the tree is proven", and it backed a
    false claim of success.

    No patching here on purpose. The test_failure scenario compiles perfectly
    well and fails at run time, which is the cheapest reproduction there is:
    build, then test, then submit.
    """
    def client(messages):
        n = len([m for m in messages if m.get("role") == "tool"])
        if n == 0:
            return ChatResponse(tool_calls=[tool_call("build_target", {}, "c0")])
        if n == 1:
            return ChatResponse(tool_calls=[tool_call("run_test", {}, "c1")])
        return ChatResponse(tool_calls=[tool_call("submit_answer", {
            "claim": "success", "summary": "built clean, calling it done"}, "c2")])

    from run_evaluation import run_case

    row = run_case(_case("test-failure-fix"), ModelConfig(), tmp_path / "retract",
                   auto_approve=True, client=ScriptedClient([client] * 6),
                   condition="skill", catalogue=False)

    history = row["history"]
    assert any(h["proof"] == "full_build_pass" for h in history), \
        "the build really did pass, which is what used to set the flag"
    assert any(h["proof"] == "observed_test_fail" for h in history), \
        "and the suite really did fail afterwards"

    assert row["verified"] is False, \
        "a build pass followed by a test failure is not proof of anything"
    assert row["verification_attempted"] is True, \
        "the agent did try; the answer was no. That is not the same as not trying"
    assert row["succeeded"] is False

    # The disagreement flag is the cross-check that caught this in the first
    # place. It must now be quiet on the very shape that tripped it.
    assert row["verification_disagreement"] is False
    assert row["eval_verification"]["ok"] is False


def test_a_targeted_failure_also_retracts(tmp_path):
    """Narrowing weakens a pass and never weakens a failure.

    `run_test(name_filter=...)` passing proves nothing about the tree, and is
    typed TARGETED_TEST_PASS for exactly that reason. The mirror image is not
    symmetric: one named test failing means the tree is red, whatever a full
    suite said earlier. This pins the asymmetry so a later tidy-up cannot make
    retraction depend on running everything.
    """
    from local_agent.verification import (
        CONTRADICTS_CURRENT_TREE, ProofKind, classify_proof,
    )

    filtered_failure = classify_proof(
        name="run_test", arguments={"name_filter": "ring_buffer"},
        execution="ok", domain="fail",
        evidence={"totals": {"failed": 1, "total": 1, "passed": 0}},
    )
    assert filtered_failure is ProofKind.OBSERVED_TEST_FAIL
    assert filtered_failure in CONTRADICTS_CURRENT_TREE

    # A build that fails to compile is the same kind of statement.
    assert classify_proof(
        name="build_target", arguments={}, execution="ok", domain="fail",
    ) in CONTRADICTS_CURRENT_TREE

    # And the two sets cannot overlap, now or after any future edit.
    from local_agent.verification import CURRENT_TREE_PROOFS
    assert not (CURRENT_TREE_PROOFS & CONTRADICTS_CURRENT_TREE)

    # Our own runner killing a build is not a build failure, so it retracts
    # nothing. Only an observation about the code counts.
    assert classify_proof(
        name="build_target", arguments={}, execution="blocked", domain="unknown",
    ) not in CONTRADICTS_CURRENT_TREE


# ------------------------------------------------------------------ rmtree
#
# `prepare()` destroys and recreates its own worktree, and git makes that hard
# on Windows: everything under `.git/objects` is marked read-only, which blocks
# deletion there while changing nothing on POSIX.
#
# The recovery handler has three properties and each was got wrong once, so
# each has its own test. Only the last of these is Windows-specific; the rest
# are portable, which matters because the minimum supported interpreter is 3.11
# and a green run on 3.12 cannot speak for it.


def test_the_legacy_onerror_tuple_shape_is_handled(tmp_path):
    """Python 3.11 has only `shutil.rmtree(onerror=...)`, which is handed a
    `sys.exc_info()` TUPLE. Python 3.12 added `onexc`, handed the exception
    INSTANCE. The project supports 3.11.

    A single handler assuming an instance is broken on the minimum supported
    version and fails in a worse way than the bug it repairs:
    `isinstance(tuple, PermissionError)` is False, so it reaches `raise <tuple>`
    and dies with `TypeError: exceptions must derive from BaseException`.

    This drives the legacy adapter with a real `exc_info` tuple, produced by
    actually raising, rather than a hand-built stand-in.
    """
    import os
    import stat
    import sys as _sys

    import run_evaluation

    target = tmp_path / "object"
    target.write_text("content", encoding="utf-8")
    os.chmod(target, 0o444)

    try:
        raise PermissionError(13, "Access is denied")
    except PermissionError:
        exc_info = _sys.exc_info()

    assert isinstance(exc_info, tuple) and len(exc_info) == 3

    retried = []
    run_evaluation._clear_readonly_and_retry_legacy(retried.append, target, exc_info)

    assert retried == [target], "the failing operation must be retried once"
    assert stat.S_IMODE(os.lstat(target).st_mode) & stat.S_IWUSR


def test_recovery_preserves_the_existing_mode(tmp_path):
    """Add the owner write bit; do not replace the mode.

    `os.chmod(path, stat.S_IWRITE)` sets the mode to 0o200, destroying read and
    execute. On POSIX that turns one unrecoverable file into an untraversable
    directory and a failed subtree.

    The assertion is written as a subset relation rather than an exact mode,
    because Windows does not represent the owner, group and other write bits
    independently. `os.chmod` there toggles one read-only attribute, so a 0o444
    file that becomes writable is reported as 0o666, not 0o644. An exact check
    demanded POSIX granularity from an OS that does not have it.

    Worth being explicit about what each platform can prove here, because the
    two are not equal. On POSIX the subset relation still catches the dangerous
    regression: reverting to `S_IWRITE` yields 0o200, and `0o200 & 0o444` is
    zero, so the assertion fails. On Windows it cannot catch it, because the bad
    implementation also ends up reported as 0o666. POSIX carries that evidence,
    which is why the exact check below is kept where the OS can express it
    rather than dropped entirely.

    This is not a Windows carve-out. The test runs on both platforms and
    asserts the portable invariant on both; it additionally asserts exactness
    where exactness is meaningful.
    """
    import os
    import stat
    import sys as _sys

    import run_evaluation

    target = tmp_path / "object"
    target.write_text("content", encoding="utf-8")
    os.chmod(target, 0o444)
    before = stat.S_IMODE(os.lstat(target).st_mode)

    run_evaluation._clear_readonly_and_retry(lambda _p: None, target,
                                             PermissionError(13, "Access is denied"))

    after = stat.S_IMODE(os.lstat(target).st_mode)

    assert after & before == before, \
        f"recovery removed permission bits: {oct(before)} -> {oct(after)}"
    assert after & stat.S_IWUSR, \
        f"recovery must make the path owner-writable: {oct(after)}"

    if _sys.platform != "win32":
        assert after == before | stat.S_IWUSR, \
            f"expected exactly {oct(before | stat.S_IWUSR)}, got {oct(after)}"


def test_a_stuck_tree_still_fails_loudly(tmp_path):
    """The handler must not become a blanket "ignore errors", in either shape.

    Anything that is not a permission problem has to propagate, or a tree that
    genuinely cannot be removed is silently accepted and the next case runs
    against the previous case's repository.
    """
    import sys as _sys

    import run_evaluation

    error = OSError("device is on fire")
    calls = []

    for handler, payload in (
        (run_evaluation._clear_readonly_and_retry, error),
        (run_evaluation._clear_readonly_and_retry_legacy, (OSError, error, None)),
    ):
        try:
            handler(calls.append, tmp_path, payload)
        except OSError as raised:
            assert raised is error
        else:
            raise AssertionError(f"{handler.__name__} swallowed a non-permission error")
    assert not calls, "the failing operation must not be retried"

    # And the version split itself is pinned, so a future tidy-up cannot
    # collapse the two adapters back into one.
    assert (_sys.version_info >= (3, 12)) or run_evaluation._clear_readonly_and_retry_legacy


def test_a_worktree_with_read_only_git_objects_can_still_be_removed(tmp_path):
    """The end-to-end case, and the only Windows-specific one here.

    On Windows this fails without the fix. On POSIX `rmtree` succeeds anyway,
    because unlinking needs write permission on the directory rather than on the
    file, so this is a smoke test there and the three tests above carry the
    portable evidence.
    """
    import os
    import stat

    from run_evaluation import prepare

    root, _ = prepare(tmp_path, "clean")
    objects = root / ".git" / "objects"
    assert objects.is_dir(), "prepare must leave a real git repository behind"

    marked = 0
    for path in objects.rglob("*"):
        if path.is_file():
            os.chmod(path, stat.S_IRUSR)
            marked += 1
    assert marked, "the fixture is meant to have git objects to mark"

    again, _ = prepare(tmp_path, "clean")
    assert again.is_dir() and (again / ".git").is_dir()


def test_the_profile_invariant_rejects_a_mismatched_tree(tmp_path):
    """The generator-aware profile check has to be capable of failing.

    It replaced a literal `CMAKE_BUILD_TYPE:STRING=Debug` match that could never
    pass under a multi-config generator. A replacement that passes on both
    platforms is only worth having if it still catches what the original caught.
    """
    from run_evaluation import prepare
    from local_agent.config import load_repo_config
    from local_agent.tools import build_registry

    root, _ = prepare(tmp_path, "clean")
    registry, _, _ = build_registry(load_repo_config(root))
    assert registry.get("configure_project").handler(profile="debug").ok

    _assert_tree_is_configured_for(root, "debug")
    with pytest.raises(AssertionError):
        _assert_tree_is_configured_for(root, "release")
