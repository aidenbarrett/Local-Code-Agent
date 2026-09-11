"""The two sweeps that stand between here and GO.

Sweep one: is the shared proof classifier ever a FALSE NEGATIVE, rejecting
work that was honestly done? A gate that never opens is not a strict gate, it
is a broken one, and it would depress every condition unequally depending on
how each one happens to sequence its calls.

Sweep two: are the three conditions byte-for-byte identical apart from the
intended difference? Not "designed to be", not "reviewed as being". Compared,
byte by byte, on the actual messages and tool schemas that reach the server.

Both are written to print evidence, not just to pass.
"""

from __future__ import annotations

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


def _case(name):
    from task_contracts import CASES
    return next(c for c in CASES if c.name == name)


def _finish_immediately():
    """A client that submits an answer on its first turn and calls nothing.

    The point is to capture the FIRST request: the system messages and the
    tool schemas exactly as they would reach the server, before any tool
    result has had a chance to make two conditions diverge for legitimate
    reasons.
    """
    return ScriptedClient([
        ChatResponse(tool_calls=[tool_call(
            "submit_answer",
            {"claim": "diagnosis", "summary": "captured the opening request"},
            "c0",
        )])
    ])


def _opening_request(case_name, condition, tmp_path):
    from run_evaluation import run_case
    client = _finish_immediately()
    row = run_case(_case(case_name), ModelConfig(), tmp_path / condition,
                   auto_approve=True, client=client, condition=condition,
                   catalogue=False)
    # `run_case` can return without the model ever being asked anything, on four
    # paths: prepare() raised, establish() raised, the tiered-client guard
    # exited, or orch.run() itself raised before its first request. All four
    # build an `_error_row` carrying the exception, its traceback and a
    # `validity` naming the stage, and this helper used to throw that row away
    # and report only "the client was never asked anything".
    #
    # On Linux the assertion never fired, so nobody noticed it was useless. On
    # Windows it fired and the diagnosis was already sitting in the row the
    # harness had just returned: a worktree path CMake could not build in.
    # Report the row.
    if not client.calls:
        raise AssertionError(
            f"{case_name}/{condition}: the client was never asked anything. "
            f"run_case returned before the model loop.\n"
            f"  validity: {row.get('validity')}\n"
            f"  error:    {row.get('error')}\n"
            f"  precondition: {row.get('precondition')}\n"
            f"  traceback:\n{row.get('traceback') or '(none recorded)'}"
        )
    return client.calls[0], client.tool_schemas[0]


# ---------------------------------------------------------------- sweep two

def test_control_and_narrow_receive_byte_identical_prompts(tmp_path):
    """Narrowing must be the ONLY difference between control and narrow.

    Not a smaller prompt, not a different word, not a reordered permissions
    blob. If any prose differs, `narrow - control` stops being the effect of
    taking tools away and becomes the effect of taking tools away plus
    whatever else changed.
    """
    control, control_tools = _opening_request("test-failure-diagnose", "control", tmp_path)
    narrow, narrow_tools = _opening_request("test-failure-diagnose", "narrow", tmp_path)

    assert len(control) == len(narrow), (
        f"different message counts: control {len(control)}, narrow {len(narrow)}"
    )
    for i, (a, b) in enumerate(zip(control, narrow)):
        assert a["role"] == b["role"], f"message {i} role differs"
        assert a["content"] == b["content"], (
            f"message {i} ({a['role']}) differs between control and narrow:\n"
            f"--- control ---\n{a['content']}\n--- narrow ---\n{b['content']}"
        )

    control_names = sorted(t["function"]["name"] for t in control_tools)
    narrow_names = sorted(t["function"]["name"] for t in narrow_tools)
    assert set(narrow_names) < set(control_names), \
        "narrow must be a strict subset of control; that is the whole treatment"
    assert len(narrow_names) < len(control_names)


def test_narrow_and_skill_differ_by_exactly_one_message(tmp_path):
    """Procedure must be the ONLY difference between narrow and skill.

    Same tools, byte for byte, including the order they are serialised in,
    because tool order changes the prompt bytes and therefore the prefix cache
    as well as anything the model infers from ordering. The skill body arrives
    as one extra system message and nothing else moves.
    """
    narrow, narrow_tools = _opening_request("test-failure-diagnose", "narrow", tmp_path)
    skill, skill_tools = _opening_request("test-failure-diagnose", "skill", tmp_path)

    assert narrow_tools == skill_tools, \
        "narrow and skill must offer identical tool schemas in identical order"

    assert len(skill) == len(narrow) + 1, (
        f"skill should add exactly one message; narrow {len(narrow)}, "
        f"skill {len(skill)}"
    )
    added = [m for m in skill if m not in narrow]
    assert len(added) == 1, f"expected one added message, got {len(added)}"
    assert added[0]["role"] == "system"
    assert added[0]["content"].startswith("Active skill: diagnose-test-failure")

    # And every message narrow received is present in skill, unchanged.
    for m in narrow:
        assert m in skill, (
            f"skill did not receive narrow's {m['role']} message unchanged:\n"
            f"{m['content'][:400]}"
        )


def test_no_condition_is_shown_the_skill_catalogue(tmp_path):
    """A list of named procedures is itself a hint about how the work
    decomposes. It is off in all three, and this looks at the bytes rather
    than trusting the flag."""
    for condition in ("control", "narrow", "skill"):
        messages, _ = _opening_request("test-failure-diagnose", condition, tmp_path)
        blob = "\n".join(m["content"] for m in messages)
        assert "Available skills:" not in blob, f"{condition} was shown the catalogue"


def test_the_skill_body_never_reaches_narrow(tmp_path):
    """The one leak that would quietly destroy `skill - narrow`."""
    from local_agent.agent.skills import SkillLibrary

    library = SkillLibrary.discover_many([REPO / "skills"])
    body = library.get("diagnose-test-failure").body
    distinctive = [line.strip() for line in body.splitlines()
                   if len(line.strip()) > 40][:5]
    assert distinctive, "the skill body has no distinctive lines to look for"

    narrow, _ = _opening_request("test-failure-diagnose", "narrow", tmp_path)
    blob = "\n".join(m["content"] for m in narrow)
    for line in distinctive:
        assert line not in blob, f"narrow was shown a line of the skill body: {line!r}"


def test_a_read_only_case_is_read_only_in_narrow_and_skill_but_not_control(tmp_path):
    """The asymmetry that makes some cheats condition-dependent, stated as a
    fact rather than left implicit.

    Control holds the full registry on every case, so on a read-only diagnosis
    case it is the only arm that can mutate the tree at all. That is the
    correct control design: a control denied the tools has been denied the job,
    not the procedure. But it means any cheat that needs a write tool is
    reachable in exactly one arm, which is why writes into the build directory
    and the run journal are refused for every condition alike.
    """
    _, control_tools = _opening_request("test-failure-diagnose", "control", tmp_path)
    _, narrow_tools = _opening_request("test-failure-diagnose", "narrow", tmp_path)

    control_names = {t["function"]["name"] for t in control_tools}
    narrow_names = {t["function"]["name"] for t in narrow_tools}

    assert "apply_patch" in control_names
    assert "apply_patch" not in narrow_names
    # The guard that makes the asymmetry harmless for the build stamp is
    # tested in test_evaluator_cannot_be_fooled.py; this pins the asymmetry
    # itself so it cannot be forgotten.


def test_the_mechanism_conditions_refuse_a_tiered_client(tmp_path):
    """The tier is derived from the skill. Narrow and skill hold a skill
    object; control does not. With two tiers configured that is not a
    reporting difference, it is a different model serving one arm.

    Never reachable from the launcher, which passes no --cheap. Now not
    reachable at all."""
    from run_evaluation import run_case

    for condition in ("control", "narrow"):
        with pytest.raises(SystemExit) as excinfo:
            run_case(_case("test-failure-diagnose"), ModelConfig(),
                     tmp_path / f"tiered-{condition}", auto_approve=True,
                     cheap=ModelConfig(), condition=condition, catalogue=False)
        assert "--cheap" in str(excinfo.value)


# ---------------------------------------------------------------- sweep one

def test_every_honest_workflow_is_accepted(tmp_path):
    """False-negative sweep, run against real cmake and real ctest.

    A gate that also rejects correct work is not strict, it is broken, and it
    would depress the conditions unequally: they sequence their calls
    differently, so a spurious rejection does not land evenly. Each of these
    is a legitimate way to finish, and each must produce a proof the evaluator
    accepts.
    """
    from run_evaluation import prepare
    from local_agent.config import load_repo_config
    from local_agent.tools import build_registry
    from local_agent.verification import ProofKind, classify_proof

    def kind(result, name, args=None):
        return classify_proof(
            name=name, arguments=args or {},
            execution=result.execution_status.value,
            domain=result.domain_status.value,
            evidence=result.data,
        )

    accepted: list[str] = []

    def fresh(scenario="clean"):
        import uuid
        root, _ = prepare(tmp_path / uuid.uuid4().hex[:8], scenario)
        registry, _, _ = build_registry(load_repo_config(root))
        return root, registry

    # 1. The plain path: configure, build, test, no profile named anywhere.
    root, reg = fresh()
    assert reg.get("configure_project").handler().ok
    b = reg.get("build_target").handler()
    t = reg.get("run_test").handler()
    assert kind(b, "build_target") is ProofKind.FULL_BUILD_PASS
    assert kind(t, "run_test") is ProofKind.FULL_TEST_PASS
    accepted.append("configure, build, test with no profile named")

    # 2. Build without configuring first. build_target configures on demand,
    #    and the stamp must still name the profile it actually produced.
    root, reg = fresh()
    b = reg.get("build_target").handler()
    t = reg.get("run_test").handler()
    assert kind(b, "build_target") is ProofKind.FULL_BUILD_PASS
    assert kind(t, "run_test") is ProofKind.FULL_TEST_PASS
    accepted.append("build without an explicit configure")

    # 3. Naming the default profile explicitly must be identical to omitting
    #    it. `debug` and None resolve to the same profile and a string
    #    comparison that got this wrong would fail every explicit run.
    root, reg = fresh()
    assert reg.get("build_target").handler(profile="debug").ok
    t = reg.get("run_test").handler(profile="debug")
    assert kind(t, "run_test", {"profile": "debug"}) is ProofKind.FULL_TEST_PASS
    accepted.append("naming the default profile explicitly")
    t = reg.get("run_test").handler()
    assert kind(t, "run_test") is ProofKind.FULL_TEST_PASS
    accepted.append("building with a named profile, testing without one")

    # 4. A targeted build FIRST, then a full build, then a test. A model that
    #    compiles one thing to check itself and then does the full job has
    #    done the full job.
    root, reg = fresh()
    assert reg.get("configure_project").handler().ok
    assert reg.get("build_target").handler(target="sandbox").ok
    b = reg.get("build_target").handler()
    t = reg.get("run_test").handler()
    assert kind(b, "build_target") is ProofKind.FULL_BUILD_PASS
    assert kind(t, "run_test") is ProofKind.FULL_TEST_PASS
    accepted.append("targeted build first, then the full build and test")

    # 5. A filtered run FIRST, then the whole suite. Reproducing one test
    #    before running everything is good practice, not a disqualification.
    root, reg = fresh()
    assert reg.get("build_target").handler().ok
    f = reg.get("run_test").handler(name_filter="ring_buffer")
    t = reg.get("run_test").handler()
    assert kind(f, "run_test", {"name_filter": "ring_buffer"}) is ProofKind.TARGETED_TEST_PASS
    assert kind(t, "run_test") is ProofKind.FULL_TEST_PASS
    accepted.append("filtered run first, then the whole suite")

    # 6. Edit, rebuild, test. The fix workflow, and the one the staleness rule
    #    could most easily break: the stamp is written after the build, so a
    #    source edited before it must not be newer than it.
    root, reg = fresh()
    assert reg.get("build_target").handler().ok
    src = root / "src" / "ring_buffer.cpp"
    src.write_text(src.read_text() + "\n// a real edit\n")
    assert reg.get("run_test").handler().data["stale_sources"], "should be stale here"
    b = reg.get("build_target").handler()
    t = reg.get("run_test").handler()
    assert t.data["stale_sources"] == [], "a rebuild must clear it"
    assert kind(b, "build_target") is ProofKind.FULL_BUILD_PASS
    assert kind(t, "run_test") is ProofKind.FULL_TEST_PASS
    accepted.append("edit, rebuild, test")

    # 7. Two rebuilds in a row, and a test after each. Nothing about running
    #    the build twice invalidates anything.
    b1 = reg.get("build_target").handler()
    b2 = reg.get("build_target").handler()
    t = reg.get("run_test").handler()
    assert kind(b1, "build_target") is ProofKind.FULL_BUILD_PASS
    assert kind(b2, "build_target") is ProofKind.FULL_BUILD_PASS
    assert kind(t, "run_test") is ProofKind.FULL_TEST_PASS
    accepted.append("consecutive full builds")

    # 8. Release, end to end. Configure, build and test one non-default
    #    profile with no debug build anywhere in the history.
    root, reg = fresh()
    assert reg.get("configure_project").handler(profile="release").ok
    b = reg.get("build_target").handler(profile="release")
    t = reg.get("run_test").handler(profile="release")
    assert kind(b, "build_target", {"profile": "release"}) is ProofKind.FULL_BUILD_PASS
    assert kind(t, "run_test", {"profile": "release"}) is ProofKind.FULL_TEST_PASS
    accepted.append("release configured, built and tested end to end")

    # 9. A profile switch done properly: debug, then release, both built.
    root, reg = fresh()
    assert reg.get("build_target").handler(profile="debug").ok
    assert reg.get("run_test").handler(profile="debug").ok
    assert reg.get("build_target").handler(profile="release").ok
    t = reg.get("run_test").handler(profile="release")
    assert kind(t, "run_test", {"profile": "release"}) is ProofKind.FULL_TEST_PASS
    accepted.append("debug then release, each actually built")

    # 10. A genuine failure is still a genuine observation, filtered or not.
    #     Diagnosis lives entirely on this, so a rule that swallowed it would
    #     fail every diagnosis case in every condition.
    root, reg = fresh("test_failure")
    assert reg.get("build_target").handler().ok
    whole = reg.get("run_test").handler()
    one = reg.get("run_test").handler(name_filter="ring_buffer")
    assert kind(whole, "run_test") is ProofKind.OBSERVED_TEST_FAIL
    assert kind(one, "run_test", {"name_filter": "ring_buffer"}) is ProofKind.OBSERVED_TEST_FAIL
    accepted.append("a real test failure, whole suite and filtered")

    # 11. A build that fails to compile is evidence too.
    root, reg = fresh("compile_error")
    b = reg.get("build_target").handler()
    assert kind(b, "build_target") is ProofKind.OBSERVED_BUILD_FAIL
    accepted.append("a real compile failure")

    print(f"\n  {len(accepted)} honest workflows accepted:")
    for line in accepted:
        print(f"    ok  {line}")
    assert len(accepted) == 12


def test_explicitly_false_arguments_do_not_narrow_anything(tmp_path):
    """The sharpest false-negative candidate in the classifier.

    A model that sends `rerun_failed: false` or `target: ""` has asked for the
    full thing. The tools agree: `if target:` and `if rerun_failed:` mean the
    command carries no `--target` and no `--rerun-failed`. So the classifier
    must read truthiness too. Testing membership instead (`"rerun_failed" in
    arguments`) would reject a full suite for the crime of mentioning the
    parameter, and Qwen-class models fill in defaults constantly.
    """
    from local_agent.verification import ProofKind, classify_proof

    full = {"totals": {"total": 4}}
    for args in ({}, {"rerun_failed": False}, {"name_filter": ""},
                 {"name_filter": None}, {"profile": "debug"},
                 {"rerun_failed": False, "profile": "debug"}):
        assert classify_proof(name="run_test", arguments=args, execution="ok",
                              domain="pass", evidence=full) is ProofKind.FULL_TEST_PASS, args

    for args in ({}, {"target": ""}, {"target": None}, {"profile": "release"}):
        assert classify_proof(name="build_target", arguments=args, execution="ok",
                              domain="pass", evidence={}) is ProofKind.FULL_BUILD_PASS, args

    # And the real narrowings still narrow.
    for args in ({"rerun_failed": True}, {"name_filter": "ring"}):
        assert classify_proof(name="run_test", arguments=args, execution="ok",
                              domain="pass", evidence=full) is ProofKind.TARGETED_TEST_PASS, args


def test_proof_does_not_survive_a_later_edit(tmp_path):
    """Documented strictness, recorded here so it is a decision and not a
    surprise in the data.

    Fix, build, prove, then touch one more file: the proof is at the previous
    epoch and the case fails. That is the intended rule, and it is the right
    one, because the tree that was proved is not the tree being submitted.

    It has a cost worth naming. Control carries patch tools on every case, so
    control is the arm most able to trip over it. If it ever fires in real
    data it is visible rather than mysterious: the row records
    `mutation_epoch` and every history entry records the epoch it ran at, so
    a run that proved at epoch 1 and submitted at epoch 2 can be read straight
    off the transcript.
    """
    import json as _json
    from run_evaluation import run_case

    FIND = "bool RingBuffer::full() const { return count_ + 1 == slots_.size(); }"
    REPL = "bool RingBuffer::full() const { return count_ == slots_.size(); }"

    def client(messages):
        tools = [m for m in messages if m.get("role") == "tool"]
        n = len(tools)
        if n == 0:
            return ChatResponse(tool_calls=[tool_call("propose_patch", {
                "path": "src/ring_buffer.cpp", "find": FIND, "replace": REPL}, "c0")])
        if n == 1:
            pid = _json.loads(tools[-1]["content"])["data"]["patch_id"]
            return ChatResponse(tool_calls=[tool_call("apply_patch", {"patch_id": pid}, "c1")])
        if n == 2:
            return ChatResponse(tool_calls=[tool_call("build_target", {}, "c2")])
        if n == 3:
            return ChatResponse(tool_calls=[tool_call("run_test", {}, "c3")])
        if n == 4:
            # Proved, and then edits again. A comment, but the tool cannot know
            # that and must not guess.
            return ChatResponse(tool_calls=[tool_call("propose_patch", {
                "path": "src/ring_buffer.cpp", "find": REPL,
                "replace": REPL + "  // tidied"}, "c4")])
        if n == 5:
            pid = _json.loads(tools[-1]["content"])["data"]["patch_id"]
            return ChatResponse(tool_calls=[tool_call("apply_patch", {"patch_id": pid}, "c5")])
        return ChatResponse(tool_calls=[tool_call("submit_answer", {
            "claim": "success", "summary": "fixed the off by one, suite green"}, "c6")])

    row = run_case(_case("test-failure-fix"), ModelConfig(), tmp_path / "epoch",
                   auto_approve=True, client=ScriptedClient([client] * 10),
                   condition="skill", catalogue=False)

    proving = [h for h in row["history"]
               if h["name"] == "run_test" and h["proof"] == "full_test_pass"]
    assert proving, "the run really did prove the suite green at the time"
    assert proving[0]["epoch"] < row["mutation_epoch"], \
        "and then mutated again, which is the point"
    assert row["required_checks"]["full test run passed after the last edit"] is False
    assert row["succeeded"] is False


def test_a_case_that_cannot_start_reports_why(tmp_path, monkeypatch):
    """The purity sweep runs twenty cases through `run_case`. When one of them
    cannot reach the model, the sweep has to say which stage failed and what the
    exception was, not "the client was never asked anything".

    That message cost a ten minute Windows run to produce a fact the harness had
    already captured and handed back: CMake could not configure inside the deep
    worktree pytest had given it. `run_case` builds an `_error_row` with the
    exception, the traceback and a `validity` naming the stage; this pins that
    the helper reads it.

    Forced through a failing `establish` rather than a platform quirk, so it
    tests the reporting path on every OS.
    """
    import run_evaluation

    def refuse(case, registry):
        raise run_evaluation.PreconditionError("configure failed: configure (debug) FAILED in 4.9s")

    monkeypatch.setattr(run_evaluation, "establish", refuse)

    with pytest.raises(AssertionError) as excinfo:
        _opening_request("test-failure-diagnose", "control", tmp_path)

    message = str(excinfo.value)
    assert "test-failure-diagnose/control" in message
    assert "invalid_precondition_error" in message, message
    assert "configure failed" in message, message
    assert "PreconditionError" in message, message


def test_control_is_never_offered_a_tool_that_cannot_work_in_control(tmp_path):
    """The defect the fifth round closed, found again in a new place.

    `read_skill_reference` was registered unconditionally, and control's toolset
    was a snapshot of the whole registry, so control carried it on every case.
    With no active skill it raises TOOL_NOT_ALLOWED every time, so control was
    offered a tool the treatments lacked AND that could only fail, paying a call
    to find out. Measured on this case set before the fix: control 21 tools,
    narrow and skill 7 to 9, and `read_skill_reference` present in control on 9
    of the 10 cases.

    The general rule, which is what this test actually enforces: no tool may
    appear in control that is unavailable to narrow unless it is genuinely
    usable in control. Extra breadth in control is the design. Extra breadth
    that can only fail is a thumb on the primary contrast.
    """
    from local_agent.agent.contracts import REFERENCE_TOOL
    from task_contracts import CASES

    offending = []
    for index, case in enumerate(CASES):
        # Numbered rather than named. Twenty worktrees need twenty distinct
        # directories and nothing more; `c-test-failure-diagnose` spent
        # twenty-three characters of a Windows path budget on something the
        # failure message already carries.
        _, control_tools = _opening_request(case.name, "control", tmp_path / f"c{index}")
        _, narrow_tools = _opening_request(case.name, "narrow", tmp_path / f"n{index}")
        control_names = {t["function"]["name"] for t in control_tools}
        narrow_names = {t["function"]["name"] for t in narrow_tools}
        if REFERENCE_TOOL in control_names:
            offending.append((case.name, sorted(control_names - narrow_names)))

    assert not offending, (
        "control was offered the skill-reference tool, which can never succeed "
        f"without an active skill: {offending}"
    )


def test_a_success_claim_is_never_demanded_without_verification(tmp_path):
    """A structural rule, added after `navigation` failed 9/9 for a reason that
    had nothing to do with the model.

    The taxonomy the model is handed says `success` means "the goal was achieved
    and a tool result proves it". A case that sets `verification_required=False`
    has decided there is no proof to have, so demanding `success` asks for a
    claim the contract itself refuses to require evidence for. Every condition
    in the 2026-09-08 batch answered `diagnosis` to navigation, nine times out
    of nine, and every one of them was right.

    `review-restraint` is exempt on purpose and not because it is fine. Its
    three arms disagreed with each other rather than converging, so the correct
    claim type there is a live design question rather than a settled defect, and
    it is being left visible here instead of quietly resolved. Removing it from
    this set is a decision somebody has to make on the record.
    """
    from task_contracts import CASES

    open_questions = {"review-restraint"}
    wrong = [
        c.name for c in CASES
        if "success" in c.expected_claim
        and not c.verification_required
        and c.name not in open_questions
    ]
    assert not wrong, (
        "these cases demand a claim of success while requiring no proof of it: "
        f"{wrong}"
    )


def test_a_worktree_windows_cannot_build_in_is_refused_by_name(tmp_path, monkeypatch):
    """The Windows failure said `configure failed` and meant `path too long`.

    CMake nests about a hundred characters under the worktree and Windows caps
    the total at 260, so a long worktree leaves it nothing. The same run_case
    call was measured both ways on the work laptop: a 155 character root failed
    to configure, a 67 character one built and registered four tests.

    Simulated rather than skipped off Windows, because the people who hit this
    are not the people running the suite on Linux, and a guard nobody exercises
    is a guard nobody can trust.
    """
    import sys as _sys

    import run_evaluation

    short = tmp_path / "s"
    deep = tmp_path / ("d" * 200)

    # Off Windows it does nothing at all, whatever the length.
    monkeypatch.setattr(_sys, "platform", "linux")
    run_evaluation._refuse_a_path_windows_cannot_build_in(deep)

    monkeypatch.setattr(_sys, "platform", "win32")
    run_evaluation._refuse_a_path_windows_cannot_build_in(short)

    with pytest.raises(run_evaluation.PreconditionError) as excinfo:
        run_evaluation._refuse_a_path_windows_cannot_build_in(deep)

    message = str(excinfo.value)
    assert "characters" in message
    assert str(len(str(deep.resolve()))) in message, message
    # It has to say what to do, not just that something is wrong.
    assert "shorter" in message


def test_the_windows_basetemp_is_short_and_unique(tmp_path, monkeypatch):
    """Short fixes the path length. Unique stops two runs eating each other.

    pytest empties its basetemp on startup, so a fixed `<temp>/lca` shared by
    two concurrent invocations would have the second delete the first one's
    worktrees mid-run. `mkdtemp` hands each invocation its own directory
    atomically, under one parent that is the only thing anybody has to delete.
    """
    import importlib.util
    import sys as _sys
    import tempfile

    # Loaded by path. conftest.py is not importable as a module under either
    # runner, and the point of this test is the function, not the plumbing.
    spec = importlib.util.spec_from_file_location(
        "lca_conftest_under_test", REPO / "tests" / "conftest.py")
    conftest = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(conftest)

    class Config:
        """One per simulated invocation. A shared Option instance would make two
        runs look like one and hide the very collision this test is about."""

        def __init__(self, basetemp=None):
            self.option = type("Option", (), {"basetemp": basetemp})()

    # POSIX: untouched, whatever else is true.
    monkeypatch.setattr(_sys, "platform", "linux")
    posix_config = Config()
    conftest.pytest_configure(posix_config)
    assert posix_config.option.basetemp is None

    monkeypatch.setattr(_sys, "platform", "win32")
    first, second = Config(), Config()
    conftest.pytest_configure(first)
    conftest.pytest_configure(second)
    chosen = [first.option.basetemp, second.option.basetemp]

    try:
        assert chosen[0] != chosen[1], "two invocations must not share a basetemp"
        parent = str(Path(tempfile.gettempdir()) / "lca")
        for one in chosen:
            assert one.startswith(parent), one
            # Short enough that the worktree, the per-test directory and CMake's
            # own tree all still fit inside MAX_PATH.
            assert len(one) < 60, f"{one} is {len(one)} characters"
    finally:
        for one in chosen:
            shutil.rmtree(one, ignore_errors=True)

    # An explicit --basetemp wins, length and all.
    asked = Config(basetemp="D:/somewhere/the/user/chose")
    conftest.pytest_configure(asked)
    assert asked.option.basetemp == "D:/somewhere/the/user/chose"
