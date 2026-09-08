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
        "case": "navigation", "outcome": "fail", "succeeded": False, "score": 0.8,
        "mutation_epoch": 0, "claim": "success", "tool_calls": 1,
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
    assert "CMAKE_BUILD_TYPE:STRING=Debug" in cache.read_text()

    result = registry.get("build_target").handler(profile="release")
    assert result.ok, result.summary
    assert "release" in result.summary
    assert "CMAKE_BUILD_TYPE:STRING=RelWithDebInfo" in cache.read_text(), \
        "the label has to describe the tree that was actually built"


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
    assert "CMAKE_BUILD_TYPE:STRING=Debug" in cache.read_text()

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
