"""One definition of what a tool call proves. Used by everything.

There used to be three: the tool decided a domain verdict, the orchestrator
decided `state.verified`, and the evaluator decided whether a required check
passed. They agreed until they did not, and every time they drifted the
instrument reported a run as verified that the evaluator then scored as
unproven. Two answers to "was this proved" is worse than either answer.

So the rules live here, once, in a function that takes primitives. The tool
layer has `ToolResult`, the orchestrator has `ToolResult`, the evaluator has
`ToolCallRecord` rows off a finished run, and `measurement/rescore_dataset.py` has JSON
read back off disk. None of those types can see each other, and all four can
call this.

What it deliberately does NOT take is the mutation epoch. Whether proof is
CURRENT is a question about when the call happened relative to the last edit,
and only the caller holds both numbers. `classify_proof` answers "what kind of
proof would this call be", and the caller checks the epoch. Folding the two
together here would let a caller that forgot the epoch get a green answer.
"""

from __future__ import annotations

from enum import Enum
from typing import Any, Mapping

VERIFYING_TOOLS = frozenset({"build_target", "run_test"})


class ProofKind(str, Enum):
    """What a single tool call establishes about the tree it ran against."""

    # Proves the whole current tree. These are the only two that can satisfy a
    # verification contract.
    FULL_BUILD_PASS = "full_build_pass"
    FULL_TEST_PASS = "full_test_pass"

    # Real observations, and exactly what a diagnosis task needs. A build that
    # fails to compile is evidence; so is a test that fails. Neither proves
    # anything is fixed, and neither is meant to.
    OBSERVED_BUILD_FAIL = "observed_build_fail"
    OBSERVED_TEST_FAIL = "observed_test_fail"

    # Passed, but only over part of the tree. `build_target(target="sandbox")`
    # passes under the link_error scenario while the project does not build,
    # and `run_test(name_filter="ring")` passes while three other tests are
    # red. Useful to the model, worthless as proof.
    TARGETED_BUILD_PASS = "targeted_build_pass"
    TARGETED_TEST_PASS = "targeted_test_pass"

    # Everything else: the wrong tool, an execution that did not complete, a
    # domain the tool refused to state, a pass over stale or unbuilt binaries.
    NO_CURRENT_PROOF = "no_current_proof"


#: The proof kinds that entitle anything to say the current tree is good.
CURRENT_TREE_PROOFS = frozenset({ProofKind.FULL_BUILD_PASS, ProofKind.FULL_TEST_PASS})

#: The proof kinds that say the current tree is NOT good, and therefore retract
#: any proof standing over the same tree.
#:
#: Without this, proof was retractable only by mutation, and a build that passed
#: followed by a suite that failed left `verified` standing. That is not a
#: hypothetical: repeat-02/03-control/link-error in the 2026-09-08 batch built
#: clean, then ran a suite that came back 1 failed of 4, kept `verified = True`
#: and backed a claim of success on a red tree. The evaluator's independent
#: oracle caught it and the batch's integrity check failed on that one row.
#:
#: A targeted failure counts. `run_test(name_filter="ring_buffer")` failing means
#: the tree is red whatever an earlier full pass said, so narrowing weakens a
#: pass but never weakens a failure. That asymmetry is the same one
#: `classify_proof` already applies when it types a filtered failure as a real
#: observation.
CONTRADICTS_CURRENT_TREE = frozenset(
    {ProofKind.OBSERVED_BUILD_FAIL, ProofKind.OBSERVED_TEST_FAIL}
)

#: Evidence keys that invalidate a PASS. Each is set by `run_test` alongside a
#: domain of UNKNOWN, so this is belt and braces: a future tool that forgets to
#: downgrade the domain still cannot produce a proof through this function.
_INVALIDATING_EVIDENCE = (
    "stale_sources",        # sources newer than the last successful full build
    "profile_mismatch",     # configured for a different profile than requested
    "build_profile_mismatch",  # last full build was for a different profile
    "no_build_record",      # no successful full build recorded at all
)


def _narrowed_test(arguments: Mapping[str, Any]) -> bool:
    """Did this test call run less than the whole suite?

    `rerun_failed` belongs here and was missed for a while. `ctest
    --rerun-failed` runs only what failed last time, which after a fix is
    exactly the one test just changed, and it was being accepted as proof that
    the whole suite was green.
    """
    return bool(arguments.get("name_filter")) or bool(arguments.get("rerun_failed"))


def classify_proof(
    *,
    name: str,
    arguments: Mapping[str, Any] | None,
    execution: str,
    domain: str,
    evidence: Mapping[str, Any] | None = None,
) -> ProofKind:
    """Classify one tool call. Pure, total, and takes only primitives.

    `execution` and `domain` are the string values off `ExecutionStatus` and
    `DomainStatus` ("ok"/"blocked"/"error", "pass"/"fail"/"unknown"), because
    that is the form the records and the JSON rows are in.
    """
    args = arguments or {}
    ev = evidence or {}

    if name not in VERIFYING_TOOLS:
        return ProofKind.NO_CURRENT_PROOF
    # A blocked or errored execution says nothing about the code. Our own
    # runner killing a build is not a build failure.
    if execution != "ok":
        return ProofKind.NO_CURRENT_PROOF

    if name == "build_target":
        if domain == "fail":
            return ProofKind.OBSERVED_BUILD_FAIL
        if domain != "pass":
            return ProofKind.NO_CURRENT_PROOF
        if args.get("target"):
            return ProofKind.TARGETED_BUILD_PASS
        return ProofKind.FULL_BUILD_PASS

    # run_test
    if domain == "fail":
        # A filtered failing test is still a genuine reproduction, which is the
        # whole of a diagnosis task. Narrowing does not weaken a failure: it
        # only weakens a pass.
        return ProofKind.OBSERVED_TEST_FAIL
    if domain != "pass":
        return ProofKind.NO_CURRENT_PROOF
    if any(ev.get(key) for key in _INVALIDATING_EVIDENCE):
        return ProofKind.NO_CURRENT_PROOF
    totals = ev.get("totals") or {}
    if "total" in totals and not totals["total"]:
        # ctest exits 0 having run nothing. True, and it verified nothing.
        return ProofKind.NO_CURRENT_PROOF
    if _narrowed_test(args):
        return ProofKind.TARGETED_TEST_PASS
    return ProofKind.FULL_TEST_PASS


def _getter(record: Any):
    """Read a record whether it is an object or a mapping."""
    if isinstance(record, Mapping):
        return record.get

    def get(key: str, default: Any = None) -> Any:
        return getattr(record, key, default)

    return get


def classify_record(record: Any) -> ProofKind:
    """Classify a `ToolCallRecord`, or anything shaped like one.

    Accepts objects with attributes (the live run) and mappings (a JSONL row
    read back by the re-scorer), so the same rules apply to replay.
    """
    get = _getter(record)
    return classify_proof(
        name=str(get("name", "") or ""),
        arguments=get("arguments") or {},
        execution=str(get("execution", "") or ""),
        domain=str(get("domain", "") or ""),
        evidence=get("evidence") or {},
    )


def proves_current_tree(record: Any, epoch: int | None = None) -> bool:
    """Did this call prove the whole tree, and is that proof still current?

    `epoch` is the mutation epoch to compare against. Passing it is the caller
    saying "and it must not predate the last edit". Omitting it asks only about
    the shape of the call.
    """
    if epoch is not None and _getter(record)("epoch") != epoch:
        return False
    return classify_record(record) in CURRENT_TREE_PROOFS
