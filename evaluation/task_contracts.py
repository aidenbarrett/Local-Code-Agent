"""Eval cases.

This is how you decide whether Qwen3-8B on the NPU is good enough, or whether
you need the 30B on CPU, without arguing about vibes. Same cases, same
repository, same scoring, different `--base-url`.

Each case is graded on behaviour the orchestrator can observe, not on prose
similarity. A model that produces a beautiful paragraph and never ran the build
scores zero.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

from local_agent.agent.orchestrator import RunResult
from local_agent.verification import ProofKind, classify_record, proves_current_tree

Check = Callable[[RunResult], bool]


def called(tool: str) -> Check:
    return lambda r: any(h.name == tool for h in r.state.history)


def not_called(tool: str) -> Check:
    return lambda r: not any(h.name == tool for h in r.state.history)


def succeeded(tool: str) -> Check:
    return lambda r: any(h.name == tool and h.ok for h in r.state.history)


# --------------------------------------------------------------- observation
#
# Calling a tool is not evidence. `run_test` with a filter matching no test
# executes cleanly, exercises nothing, and used to earn "ran the test". These
# helpers require an actual observation: execution OK and a domain verdict.

def _observed(h, tool: str, domain: str | None = None) -> bool:
    return (
        h.name == tool
        and h.execution == "ok"
        and h.domain in (("pass", "fail") if domain is None else (domain,))
    )


def observed(tool: str, domain: str | None = None) -> Check:
    return lambda r: any(_observed(h, tool, domain) for h in r.state.history)


def observed_any(*tools: str) -> Check:
    return lambda r: any(_observed(h, t) for h in r.state.history for t in tools)


def full_build_passed_after_edits() -> Check:
    """An untargeted build that passed against the current tree.

    `target=...` builds one thing and proves nothing about the rest, and a
    build from before the last edit describes a tree that no longer exists.
    """
    return lambda r: any(
        classify_record(h) is ProofKind.FULL_BUILD_PASS
        and h.epoch == r.state.mutation_epoch
        for h in r.state.history
    )


def _whole_suite_passed(h, epoch: int) -> bool:
    """One run_test call that actually exercised the entire suite and passed.

    Five ways to be handed a green result over less than the whole current
    tree, all of them now rejected by the shared classifier:

      name_filter    runs the subset that matches
      rerun_failed   `ctest --rerun-failed` runs only what failed last time,
                     which after a fix is exactly the one test you just
                     changed. A run that patched, built, and reran only the
                     previously failing test was recorded as having proved the
                     whole suite green
      stale sources  ctest does not compile, so a pass against a binary older
                     than the source is a fact about the old binary
      no build       nothing recorded a successful full build, so there is no
                     baseline to call anything stale against
      wrong profile  configured for, or last built as, something else

    Plus a run of zero tests, which is not a pass.

    This function used to implement those rules itself, in a second copy that
    the orchestrator did not share. The copies disagreed about `rerun_failed`
    and about stale passes, which meant a run could be recorded as verified and
    then scored as unproven. Now both call the same classifier and the only
    thing left here is the epoch, which is the evaluator's own question.
    """
    return proves_current_tree(h, epoch) and classify_record(h) is ProofKind.FULL_TEST_PASS


def full_test_passed_after_edits() -> Check:
    """An unfiltered test run that passed against the current tree.

    This is what "prove it" means on a fix task, and nothing else is. A passing
    build says the code compiles. The evaluator's own later run is not the
    model's proof, it is ours.
    """
    return lambda r: any(
        _whole_suite_passed(h, r.state.mutation_epoch) for h in r.state.history
    )


def did_not_mutate() -> Check:
    return lambda r: r.state.mutation_epoch == 0


def answer_mentions(*needles: str) -> Check:
    return lambda r: all(n.lower() in r.answer.lower() for n in needles)


def answer_mentions_any(*needles: str) -> Check:
    return lambda r: any(n.lower() in r.answer.lower() for n in needles)


def at_most_calls(n: int) -> Check:
    return lambda r: r.state.tool_calls <= n


def did_not_halt() -> Check:
    return lambda r: r.state.halt_reason is None


def verified() -> Check:
    """The orchestrator's own flag. Kept for reporting; not a task contract.

    It is satisfied by a full build OR a full test, so it cannot express "prove
    this fix with a test run". Use the specific helpers above for that.
    """
    return lambda r: r.state.verified


# The cases that decide whether this is a tool or a toy. A model can score well
# on "clean-build" by doing almost nothing; these four require it to read
# evidence and reach a specific conclusion.
DIAGNOSTIC_CASES = {
    "compile-error-locate",
    "compile-error-fix",
    "test-failure-diagnose",
    "segfault",
}

# Decided before any data was collected, on purpose. Below this on the
# diagnostic subset, the configuration is not a daily driver and no amount of
# prompt engineering will make it one.
KILL_THRESHOLD = 0.6

# The bar for the QUALITY checks only. Note it interacts with the SIZE of the
# quality set: with three checks the reachable scores are 0, .33, .67 and 1, so
# 0.75 means all three. Keep quality sets at three or more genuine signals, and
# do not let a set shrink to two, or "quality" silently becomes another hard
# gate. Moving mandatory steps out to required_checks did exactly that once and
# failed a clean-build run that had done everything right. It is a partial-credit average over
# things like "named the symbol", and averaging is right for those.
#
# It is wrong for evidence. Averaged with four quality points, a missing
# mandatory step is worth 20% and disappears under a 75% threshold: a
# navigation agent that called no tools at all and guessed correctly scored
# 0.80 and was recorded as a capability success, and a review agent that never
# ran git_status scored 0.75 and was too. Mandatory steps are now
# `required_checks`, every one of which must pass, and they are not averaged
# with anything.
SUCCESS_THRESHOLD = 0.75


@dataclass
class EvalCase:
    name: str
    scenario: str
    task: str
    # Whether a claim of success has to be backed by a passing full build or
    # test run. Declared on the CASE, not read off the skill, so the no-skill
    # control is held to exactly the bar the treatment is held to. A control
    # with a lower success bar is not a control.
    #
    # Deliberately has NO DEFAULT. A permissive default is how this went wrong
    # once already: defaulting it to True silently demanded a build of
    # `navigation`, a case whose own rubric contains a check called "did not
    # build", and of `review-restraint`, which is proved by git_status. Both
    # would have failed in BOTH cells for a contract they were never meant to
    # be under. A future read-only case must not be able to inherit the wrong
    # contract by saying nothing, so the dataclass refuses to construct one
    # that has not decided.
    verification_required: bool
    # Hard evidence requirements. EVERY one must pass for capability success.
    # Not averaged, not weighted, no partial credit: these are the difference
    # between doing the work and describing it. No default, for the same
    # reason verification_required has none.
    required_checks: dict[str, Check]
    # What the task contract says a finished answer claims. The model chooses
    # its own claim, and `claim == "success"` is what triggers the orchestrator's
    # demand for a passing verification, so a model that says "diagnosis" on a
    # build task dodges the requirement entirely. It scored 1.00 doing exactly
    # that. The contract is the experiment's to set, not the model's.
    # Exact. A finished answer that claims something the task did not ask for
    # has not finished it, and prose is not a finish at all: the protocol is in
    # the prompt every condition receives.
    expected_claim: frozenset
    skill: str | None = None
    # Quality points, averaged. "Did it name the right symbol", not "did it
    # look".
    checks: dict[str, Check] = field(default_factory=dict)
    weight: float = 1.0
    # Tools whose use is a scope violation for this task, not an inefficiency.
    # "Tell me what is wrong" followed by an edit to the tree is materially
    # wrong behaviour whatever the answer says, so a violation fails the case
    # outright rather than costing a check.
    forbidden_tools: tuple[str, ...] = ()
    # The state the task's question presupposes, established by the harness
    # before the model gets control, and asserted before the run starts.
    #
    # "built": configured, built, tests registered. A task that says "the
    # ring_buffer test is failing, work out why" is a lie if ctest cannot find
    # a build directory, and what gets measured then is precondition recovery,
    # not diagnosis. The first controlled probe measured exactly that by
    # accident: seven run_test calls, every one answered "not configured".
    #
    # "none": the task is about reaching that state, or about a build that is
    # supposed to be broken. Building it first would delete the subject.
    precondition: str = "none"


# Every mutating tool. A diagnosis or locate task may call none of them.
MUTATING_TOOLS: tuple[str, ...] = ("propose_patch", "apply_patch", "git_stage", "git_commit")


# What a finished answer must claim, per task. Exact, not a permissive set.
#
# The shared prompt defines the vocabulary in terms of the requested GOAL:
# success is "the goal was achieved and a tool result proves it", failure is
# "the goal was not achieved". The evaluator was still reading it as a
# statement about the state of the CODE, so a correct navigation answer could
# claim `failure` and pass, and so could a correct diagnosis of a real compile
# error. Under the shared definition both of those say the agent did not do
# what was asked, while the row shows it did. Two different meanings for one
# word, and the looser one was winning.
#
# These fixtures are constructed to be solvable and the evidence is guaranteed
# to be there, so `failure` and `needs_action` are never the correct terminal
# state on them. They stay in the protocol for real operation.
DIAGNOSIS_ONLY = frozenset({"diagnosis"})
SUCCESS_ONLY = frozenset({"success"})


CASES: list[EvalCase] = [
    EvalCase(
        name="clean-build",
        verification_required=True,
        # Report the result means produce the result. A targeted build and a
        # filtered test prove nothing about "the project" or "the suite".
        required_checks={
            "full build passed": full_build_passed_after_edits(),
            "full test run passed": full_test_passed_after_edits(),
        },
        expected_claim=SUCCESS_ONLY,
        scenario="clean",
        # Pinned like every other case. It was the only one without a skill, so
        # the router chose the treatment for it, which makes routing a variable
        # inside an experiment about narrowing and procedure. Routing quality
        # is a real question and it is a different one.
        skill="build-and-test",
        task="Build the project and run the test suite. Report the result.",
        checks={
            "did not halt": did_not_halt(),
            "was efficient": at_most_calls(6),
            # Substring rubrics are brittle. The 30B wrote "all tests (4
            # tests) pass" and scored zero here against a needle list of
            # "passed", "succeeded", "green".
            "reported success": answer_mentions_any(
                "pass", "succeed", "green", "no failing", "all tests"),
        },
    ),
    EvalCase(
        name="compile-error-locate",
        verification_required=True,
        required_checks={
            "observed the build fail": observed("build_target", "fail"),
            "did not modify the repository": did_not_mutate(),
        },
        expected_claim=DIAGNOSIS_ONLY,
        scenario="compile_error",
        task="The build is broken. Find the first compiler error and explain the cause.",
        skill="diagnose-build-failure",
        checks={
            "named the file": answer_mentions("ring_buffer.cpp"),
            "named the symbol": answer_mentions_any("count_", "count"),
            "cited the line": answer_mentions("13"),
            "did not halt": did_not_halt(),
            "was efficient": at_most_calls(8),
        },
        forbidden_tools=MUTATING_TOOLS,
    ),
    EvalCase(
        name="compile-error-fix",
        verification_required=True,
        required_checks={
            "applied a patch": succeeded("apply_patch"),
            "full build passed after the last edit": full_build_passed_after_edits(),
        },
        expected_claim=SUCCESS_ONLY,
        scenario="compile_error",
        task="Fix the compilation errors in this repository, then prove the build succeeds.",
        skill="fix-build-failure",
        checks={
            "proposed a patch": succeeded("propose_patch"),
            "named what was wrong": answer_mentions_any("count_", "semicolon", ";"),
            "did not halt": did_not_halt(),
            "was efficient": at_most_calls(20),
        },
        weight=2.0,
    ),
    EvalCase(
        name="link-error",
        verification_required=True,
        required_checks={
            "observed the build fail": observed("build_target", "fail"),
            "did not modify the repository": did_not_mutate(),
        },
        expected_claim=DIAGNOSIS_ONLY,
        scenario="link_error",
        task="The build fails at link time. What is missing and where should it be defined?",
        skill="diagnose-build-failure",
        checks={
            "named the symbol": answer_mentions("checksum"),
            "named the right file": answer_mentions_any("ring_buffer.cpp", "ring_buffer"),
            "did not halt": did_not_halt(),
        },
        forbidden_tools=MUTATING_TOOLS,
    ),
    EvalCase(
        name="test-failure-diagnose",
        verification_required=True,
        # Reproduce means reproduce. A filter that matched no test executes
        # cleanly, exercises nothing, and used to earn "ran the test".
        required_checks={
            "reproduced the failure": observed("run_test", "fail"),
            "did not modify the repository": did_not_mutate(),
        },
        expected_claim=DIAGNOSIS_ONLY,
        scenario="test_failure",
        task="The ring_buffer test is failing. Work out why and explain the defect.",
        checks={
            "named the test": answer_mentions("ring_buffer"),
            "identified the predicate": answer_mentions_any("full", "off by one", "capacity"),
            "did not halt": did_not_halt(),
        },
        weight=2.0,
        skill="diagnose-test-failure",
        forbidden_tools=MUTATING_TOOLS,
        precondition="built",
    ),
    EvalCase(
        name="test-failure-fix",
        verification_required=True,
        # "and prove it". A passing build says the code compiles. The only
        # proof that the suite passes is an unfiltered test run against the
        # tree as it stands after the last edit. The evaluator's own later run
        # is our proof, not the model's.
        required_checks={
            "applied a patch": succeeded("apply_patch"),
            "rebuilt after editing": full_build_passed_after_edits(),
            "full test run passed after the last edit": full_test_passed_after_edits(),
        },
        expected_claim=SUCCESS_ONLY,
        scenario="test_failure",
        task="The ring_buffer test is failing. Fix the code so the whole suite passes, and prove it.",
        skill="fix-test-failure",
        checks={
            "explained the fix": answer_mentions_any("full", "off by one", "capacity"),
            "did not halt": did_not_halt(),
            "was efficient": at_most_calls(12),
        },
        weight=2.0,
        precondition="built",
    ),
    EvalCase(
        name="segfault",
        verification_required=True,
        required_checks={
            "reproduced the crash": observed("run_test", "fail"),
            "did not modify the repository": did_not_mutate(),
        },
        expected_claim=DIAGNOSIS_ONLY,
        scenario="crash",
        task="A test is crashing. Identify which one and what causes the crash.",
        checks={
            "named the test": answer_mentions("text_util"),
            "identified the cause": answer_mentions_any("null", "nullptr", "dereference", "trim"),
            "did not halt": did_not_halt(),
        },
        skill="diagnose-test-failure",
        forbidden_tools=MUTATING_TOOLS,
        precondition="built",
    ),
    EvalCase(
        name="timeout",
        verification_required=True,
        required_checks={
            "reproduced the timeout": observed("run_test", "fail"),
            "did not modify the repository": did_not_mutate(),
        },
        expected_claim=DIAGNOSIS_ONLY,
        scenario="timeout",
        task="One test never finishes. Which one, and why does it not terminate?",
        checks={
            "named the test": answer_mentions("slow"),
            "identified the cause": answer_mentions_any("wrap", "unsigned char", "255", "never"),
            "did not halt": did_not_halt(),
        },
        skill="diagnose-test-failure",
        forbidden_tools=MUTATING_TOOLS,
        precondition="built",
    ),
    EvalCase(
        name="navigation",
        verification_required=False,
        # A navigation agent that never navigated scored 0.80 and was recorded
        # as a capability success. Looking is the task.
        required_checks={
            "read or searched the repository": observed_any(
                "search_text", "find_definition", "read_file"),
        },
        # Was SUCCESS_ONLY, and that was wrong. Over nine cells of the
        # 2026-09-08 batch this case failed 9/9: across all three conditions and
        # all three repeats, the same claim mismatch appeared every time, the
        # agent claiming `diagnosis` where the contract demanded `success`. That
        # points hard at a contract defect rather than a condition-specific
        # model failure.
        #
        # The taxonomy the model is given says `success` is "the goal was
        # achieved and a tool result proves it". This case sets
        # verification_required=False, so there is no proof to have, and asking
        # a question about the code is explaining rather than achieving. Under
        # the instrument's own definitions the agents were right and the
        # contract was wrong.
        #
        # This makes navigation pass its required gate in every condition, so it
        # discriminates only through the weighted checks below, where
        # `was efficient` still separates control from the treatments. That is a
        # ceiling where it used to be a floor. Whether the case earns its place
        # at all is a pilot-design question, deliberately not settled here.
        expected_claim=DIAGNOSIS_ONLY,
        scenario="clean",
        task="Where is the RingBuffer class defined, and what happens when you push to a full buffer?",
        skill="repo-navigation",
        checks={
            "named the header": answer_mentions("ring_buffer.hpp"),
            "described the behaviour": answer_mentions_any("false", "rejected", "refuses"),
            "did not build": not_called("build_target"),
            "was efficient": at_most_calls(6),
        },
    ),
    EvalCase(
        name="review-restraint",
        verification_required=False,
        # A review agent that never looked at the tree scored 0.75 and was
        # recorded as a capability success.
        required_checks={
            "inspected the working tree": observed_any("git_status", "git_diff"),
            "did not modify the repository": did_not_mutate(),
        },
        expected_claim=SUCCESS_ONLY,
        scenario="clean",
        task="Review my uncommitted changes.",
        skill="git-review",
        checks={
            "did not invent findings": answer_mentions_any(
                "no changes", "nothing", "clean", "no uncommitted"
            ),
            "did not halt": did_not_halt(),
            "was efficient": at_most_calls(4),
        },
    ),
]
