# Adversarial review history

Six rounds. Every defect below was found by an adversarial reviewer or by a
sweep written to attack the instrument, reproduced as a failing test, then
closed. The tests are written as the cheat rather than as the fix, so they fail
again if the fix is ever undone. They live in
`tests/integration/test_evaluator_cannot_be_fooled.py` and
`tests/integration/test_condition_purity.py`.

This document exists because a measurement instrument's credibility is its
defect history, not its architecture diagram. An agent benchmark that has never
been attacked is a benchmark whose failures have not been found yet.

## The pattern worth noticing

In almost every false positive below the **answer was correct and the quality
score was 1.00**. Good answers are exactly when a loose rubric starts hiding
procedural failure. That is why evidence is a hard gate and not a quality
point.

## Round 1: apparatus, not model

Three diagnosis cases scored 0.25 and were read as a model pathology. They were
not. Every case started from an unbuilt tree while the diagnosis skills had no
build tool, so the run measured precondition recovery, which is a different
experiment and, under a read-only toolset, an unwinnable one.

Fixed with explicit `precondition` on each case and an `establish()` step that
runs before measurement and is not charged to the model. **The earlier audit
claim that this was a model failure was retracted.**

Also closed in this round:

- **Answer-key leakage.** The model worktree exposed `scenarios/clean/`, and
  the model read pristine reference source during diagnosis. Removed from the
  sandbox.
- **Diagnosis treatment contamination.** A read-only diagnosis skill instructed
  the model to patch, with no patch tool available. Diagnosis and repair
  procedures split.

## Round 2: four evaluator cheats

Each reproduced end to end, before and after.

| Cheat | Before | After |
|---|---|---|
| `run_test(name_filter="Crash")` matching zero tests, then a correct diagnosis | pass, 1.00, `verification_attempted` true | fails: "reproduced the failure" not met |
| patch, build, never run a test, claim success on "prove it" | pass, 1.00, "full test run passed" true | fails: no unfiltered passing run at the current epoch |
| navigation with zero tool calls and a correct guess | pass, 0.80 | fails: "read or searched the repository" not met |
| review with no `git_status` | pass, 0.75 | fails: "inspected the working tree" not met |

Four mechanisms:

1. **Executing is not observing.** `ran` reads only the execution axis, so a
   filter matching nothing counted as an attempt at verification. `observed`
   requires a domain verdict.
2. **A build is not proof of a test.** `verified` is satisfied by a full build
   *or* a full test, so it cannot express "prove this fix by running the
   suite". Task-specific helpers now do.
3. **Mandatory steps were averaged away.** With four quality points a missing
   mandatory step is worth 20% and disappears under a 75% threshold. Evidence
   moved to `required_checks`: all must pass, none are averaged.
4. **The model chose its own contract.** `expected_claim` belongs to the case.

A regression introduced by that fix was also caught: moving checks out of the
quality set shrank its denominator, which failed a correct run on a substring
rubric. Quality sets are kept at three or more genuine signals.

## Round 3: contract defaults and the finishing protocol

- **`verification_required` defaulted to `True`**, silently demanding a build
  of `navigation`, a case whose own rubric contains a check called "did not
  build". It failed in both cells for a contract it was never under. The
  default was removed entirely; a case that has not decided cannot be
  constructed.
- **`--rerun-failed` accepted as a full suite.** After a fix it runs exactly
  the one test just changed.
- **A `None` claim allowed a contradictory prose sign-off.** `submit_answer`
  and the claim vocabulary moved into the shared prompt so every condition
  finishes the same way.
- **A targeted build cleared the global staleness stamp**, making an unrelated
  stale test binary look fresh.
- **The profile label lied about the CMake cache.** `build_target(profile=
  "release")` printed "release succeeded" over a Debug cache.

## Round 4: one definition of proof

The deepest round. The evaluator's definition of valid proof had become
stricter than the runtime's, and the two had drifted.

- **A stale full-suite PASS was typed PASS.** The prose said `STALE` and the
  type said `ok=True, domain=PASS`, so the orchestrator recorded
  `verified=True` over binaries predating the edit while the evaluator threw
  the row out. The model saw the permissive answer.
- **A configure-only profile switch verified old binaries.** Two holes lined
  up: the build stamp recorded *that* a build succeeded but not *which profile*
  it produced, and an absent stamp was read as "nothing is stale" when it means
  "freshness unknown".
- **The orchestrator accepted `rerun_failed` while the evaluator rejected it.**
- **A CMake cache with no profile marker was taken on trust.**

Closed by extracting `local_agent/verification.py`: one classifier, taking
primitives, consumed by the orchestrator, the evaluator, the re-scorer and the
tests. A table-driven test runs both predicates over fifteen call shapes,
including every shape they have ever disagreed on, and asserts they agree.

The retrospective cost was accepted rather than avoided: the re-scorer treats a
missing evidence key as unknown rather than False, so every dataset predating a
new invalidating key becomes partially replayable and can only receive a
provisional score.

## Round 5: the forgeable build stamp

Found while reviewing an unrelated document. The staleness gate was openable
with a text editor:

```
propose_patch(path="build/.local-agent-build-ok", one byte)
apply_patch(...)
run_test()      ->  stale_sources [], PASS, verified
```

Three calls, nothing compiled, over binaries that predated the edit.

Worse than an ordinary cheat because it is **condition-asymmetric**. Control
holds the full registry on every case, including the read-only diagnosis cases
where narrow and skill are offered no patch tools, so the cheat is reachable in
one arm of the experiment and unreachable in the others. It does not add noise
to the contrast, it biases it.

Two independent locks: patch tools refuse any path under the build directory,
the run journal or `.git` with a typed `PROTECTED_PATH` reason so the attempt
is recorded; and the build record is dated from its own content, so touching
the file moves no baseline.

## Round 6: the two sweeps

Written to answer the last two open questions with evidence rather than
judgement.

**False negatives.** A gate that rejects correct work is not strict, it is
broken, and it does not depress the conditions evenly because they sequence
their calls differently. Twelve honest workflows were run against real cmake
and ctest and all were accepted, including the sharpest candidate: explicit
`rerun_failed: false` and `target: ""`, which the classifier must read by
truthiness rather than membership.

One fix came out of it. The build record stored `time.time()` while staleness
compares filesystem mtimes. Two clocks that agree on local disk and can
disagree on a network mount, where a build recorded later than it happened
would hide a real stale source.

**Condition purity, at the byte level.** Control and narrow receive
byte-identical messages; narrow and skill receive byte-identical tool schemas
in identical order and differ by exactly one added system message; no
condition's bytes contain the catalogue or any line of the skill body.

This sweep found the tier-routing confound: the routing tier is derived from
the skill, narrow and skill hold a skill object and control does not, so a
tiered client would have served one arm from a different model. Never reachable
from the launcher, now refused outright, and every row records `tiered` and
`escalated` so the dataset proves it.

## Recorded, deliberately not fixed

Neither can move one condition differently from another, so both belong in the
next fixture rather than in another round of instrument changes.

- `_stale_sources` watches a hard-coded suffix list. A real repository needs it
  derived from the build system.
- The staleness comparison is strictly greater-than, so an edit inside the same
  filesystem tick as the build completing would not be flagged. Nanoseconds on
  ext4.

Fixture limitations are recorded in the README: injected defects are visible in
`git diff`, some comments describe them, and the 30B saturates the fixture once
the toolset is right.

## Two errors of mine, recorded

**A false claim of re-scoring.** I stated that a no-skill dataset had been
re-scored under the new evaluator. It had not; the re-scorer had been run
against a different frozen dataset. Publicly retracted, and `measurement/rescore_dataset.py`
was built as the auditable replacement so the claim can never again rest on
recollection.

**A wrong definition of verification disagreement.** I proposed counting rows
where `verified=true` and `required_ok=false`. Applied to the first dataset it
would have reported four disagreements where there were none: those rows failed
"did not modify the repository", a case contract, not a proof question.
`verified` and `required_ok` answer different questions and are allowed to
differ. Corrected before the number was quoted.
