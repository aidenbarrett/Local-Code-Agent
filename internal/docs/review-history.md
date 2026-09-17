# Adversarial review history

Eleven rounds. Every defect below was found by an adversarial reviewer or by a
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

## Round 7: the instrument caught itself

Not a reviewer this time. The batch wrapper's own integrity check failed one row
out of ninety, and the row was right to fail.

`repeat-02/03-control/link-error` recorded `verified = True`. The evaluator's
independent oracle re-verified the tree and returned `tests: fail, 1 test(s)
FAILED: ring_buffer`. The history explains it: the agent patched, built clean,
then ran the suite and got 1 failed of 4, and claimed success.

`state.verified` was set by a `FULL_BUILD_PASS` and the only thing that ever
cleared it was `note_mutation()`. So proof was retractable by editing the tree
and by nothing else, and an observed failure on the same tree left the flag
standing. `verified` meant "something built", not "the tree is proven".

Fixed by making proof retractable by contradiction:
`CONTRADICTS_CURRENT_TREE = {OBSERVED_BUILD_FAIL, OBSERVED_TEST_FAIL}` clears
`verified` on the current epoch. `verification_attempted` is deliberately not
cleared; the agent did try and the answer was no.

A targeted failure counts. Narrowing weakens a pass and never weakens a failure,
which is the same asymmetry `classify_proof` already applies when it types a
filtered failure as a real observation.

The point worth keeping: no reviewer found this. The cross-check between the
runtime's proof and the evaluator's independent verification found it, on real
data, on the seventh outing. That cross-check exists because round 4 built it.

## Round 8: three defects in the contracts layer

From the review of PR #1, plus two structural faults the three-repeat batch made
visible.

**Control was handed a tool that could only fail.** `read_skill_reference` was
registered unconditionally and control's toolset was a snapshot of the whole
registry, so control carried it on 9 of the 10 cases. With no active skill it
raises `TOOL_NOT_ALLOWED` every time, so control paid a tool call to discover
that a tool the treatments did not have was useless. That biases
`narrow - control`, the primary contrast, by an unbounded amount. Same defect
class as round 5, new location.

**`base_prompt_sha256` fingerprinted the system prompt only.** Tool result
payloads gained `evidence_id` and the answer contract narrowed to canonical ids,
both model-facing, both invisible to the hash. Same hole as the skills hashing
gap, new location. It now covers the prompt, the message builders, the tool
result shape and the answer contract, and explicitly not the tool schemas, which
are the independent variable and are recorded per row.

**`navigation` demanded a claim it refused to require evidence for.** It failed
9/9 in generation 1, every condition, every repeat, on the same line: the agent
claimed `diagnosis` and the contract wanted `success`. The taxonomy the model is
handed defines `success` as achieved and proved by a tool result, while the case
sets `verification_required=False`. The agents were right and the contract was
wrong. A structural test now refuses any case that pairs `SUCCESS_ONLY` with no
verification requirement.

`review-restraint` is exempt from that test on purpose, and named in it. Its
three arms disagreed with each other rather than converging, so the correct
claim type there is a live design question, not a settled defect. Leaving it
visible in an exemption set beats resolving it quietly.

## Round 9: one tree, two identities, and the number nobody could explain

Native Windows CI reported `source_sha256 = bd6ca03…` against a declared
`e16b2f01…`, on an identical commit. Four candidate explanations were computed
from the real bytes and **none matched**, which is what turned this from a
guess into an investigation. Reporting the miss rather than shipping the nearest
plausible story is the only reason the third cause was found.

There were three causes, not two, and all three had to close.

**The checkout.** Git for Windows ships `core.autocrlf=true` and rewrites LF to
CRLF for every text file. `source_sha256` hashes raw bytes deliberately, because
a hash that normalised line endings first would be a hash of what we decided to
look at rather than of what is on disk. So the tree has to be canonical instead:
root `.gitattributes` with `* text=auto eol=lf`, and frozen evidence pinned
`binary` rather than left to a NUL-byte heuristic.

**The generator.** `benchmark_fixture/generate_project.py` runs after checkout in
every CI job and undid the fix. `Path.write_text` opens in text mode, and text
mode on Windows translates `\n` to `\r\n` on the way out, so a generator run
there rewrote 24 of the 76 hashed files regardless of what the checkout had
produced. Every text write now goes through one helper pinning `newline="\n"`.

**The ordering.** `_files()` ended in `sorted(out)`, sorting `Path` objects.
`PurePath.__lt__` compares the host flavour's normcase form, which on Windows is
`str(path).lower()` with backslashes: case-insensitive. So
`cpp_project/README.md` sorted before `include/…` on Linux and after it on
Windows, and the identical tree hashed two different ways. The earlier
`.as_posix()` fix corrected the key each file contributes and never touched the
order they are contributed in. Now sorted on `_key`, so the sort key and the
hashed key are the same function and cannot drift apart again.

Computed on the tree at `5d729478`, applying each transformation to the real
bytes:

```
canonical order, LF     e16b2f01c4fd4ec4623b4588cffa064fe270ca5ee52b1e290762f8ae91e2566b
canonical order, CRLF   6ba842bd27204895d9511a04106a81079c82af846429e638d69995f4d64e657c
Windows order,   LF     6c0b0cde2c9a8fd5fe1eb4eb34cdf314ee4fe68d545124271920d75e367cc734
Windows order,   CRLF   bd6ca03ea18c0c12ddf3ab190aac50bb36d6fdf2d2eaafa3d4c296dc905f0dc6
```

The exact match proved three things no argument would have: the Windows hashed
file **set** is identical, 76 files, killing the competing `rglob`/`_SKIP`
hypothesis; the CRLF was tree-wide rather than fixture-only, excluding two other
candidates by measurement; and `.gitattributes` plus the generator fix land on
`6c0b0cde`, necessary and demonstrably not sufficient.

Three regressions, each capable of failing on every host rather than only on the
one that had the bug. The ordering one additionally asserts that the tree still
contains a file pair the two orderings disagree on, so it says so rather than
passing vacuously if that ever stops being true.

Same defect class as round 5 and round 8: a fix that closed one instance of a
pattern and left another instance of the same pattern one layer up.

## Round 10: the trust boundary, and evidence that was never checked

Review of PR #7 at `f58f74a`. The endpoint separation work was sound; the
defects were all in what happened to evidence after it was recorded.

**Tampering was laundered into a replaceable missing observation.**
`counted = validity == "valid" and not tamper.tampered`, and every endpoint opens
`if row.get("counted") is not True: return None`. So a tampered row was not a
failure, it was a missing observation, in the same bucket as the inference server
falling over, and therefore replaceable inside the five-attempt bound. Executed:
two tampered draws followed by three clean ones produced a clean 2-of-3 pass on
every endpoint, and the analyser reported the interference as a 0.4 *deployment
flakiness rate*. Nothing in the output named it.

Worse, it was asymmetric. Tampering needs tools that reach the oracle, and
`control` holds the full registry on every case while `narrow` and `skill` do
not. So the free-retry mechanism was more available to one arm than the others,
exactly like the build-stamp forgery in round 5. Tampering is now a terminal,
counted, non-replaceable class that fails compliance and is named in reporting.

**Cherry-picking by deletion was undetectable.** The five-attempt bound stops
optional stopping only if every attempt is present, and nothing checked that.
Executed: an honest four-attempt cell (T, F, T, T) reported 2 successes and a
protocol violation; deleting the failing row from the file reported 3 successes,
clean, no violation. The doctored cell looked better than the honest one.
Attempt indices 17, 4 and 900 were also accepted without complaint. Declared
attempt counts are now persisted and checked against exact indices.

**The endpoints were tested only against hand-written rows.** Twenty-two good
negative tests, every one grading a dictionary built by a local `_row` helper.
No test graded a row `run_case` had actually produced. Every endpoint returns
`None` on a schema mismatch, `None` becomes `indeterminate`, and `analyse`
withholds every verdict when anything is indeterminate, so a single renamed key
would have turned the whole pilot into a run that produced no result while the
suite stayed green. Real rows now flow through `row_endpoints` in integration
tests.

**Known noncompliance returned Unknown.** A row with `claim_ok=False` could
return `None` rather than `False` when citation evidence was absent, converting a
known failure into an indeterminate one. Unknown is now reserved for genuinely
unavailable or malformed evidence.

The governing sentence that came out of this round and is worth keeping:
**recorded evidence does not count merely because it exists; every fact has to be
enforced at the next trust boundary before it can affect a result.**

## Round 11: build proof causality, and a fix that would have made it worse

The largest defect found so far, and the correction of a proposal of mine that
would have certified it.

`run_test` decided freshness by comparing source mtimes against the recorded
build. Four reproductions, all executed:

```
edit source, os.utime the original mtime back   ->  not stale
edit lands on exactly the stamp nanosecond      ->  not stale   (`>`, not `>=`)
delete a source file entirely                   ->  not stale
repo path contains a component named "build"    ->  detection off entirely
```

The fourth is the round 9 defect class again: the exclusion was computed on
absolute `path.parts`, so a repository living under any directory named `build`,
`.git` or `.local-agent` silently disabled staleness detection for the whole
tree.

The shipped interim fix, float seconds to integer nanoseconds, narrowed a
rounding error sitting on top of a much larger quantisation window. Windows file
times are stamped from a system clock that ticks at roughly 15.6 ms, so
nanosecond units do not mean nanosecond resolution.

**My proposed fix was wrong and would have been worse than the bug.** I proposed
hashing the build-relevant sources at the moment of a successful build and
comparing content thereafter. Astra reproduced the case that kills it, and it
reproduces on this fixture first go:

```
invalid C++ written into src/ring_buffer.cpp, original mtime restored
  incremental build   ->  "ninja: no work to do."  exit 0
  ctest               ->  100% tests passed, 4 of 4
  clean rebuild       ->  FAILS
```

The build tool's own decision about what to recompile is itself an mtime
comparison. We had layered our mtime oracle on top of ninja's mtime oracle, and
a content snapshot taken at the moment of that successful-but-empty build would
have recorded the invalid bytes as the bytes that were built, then certified
them with a hash, permanently.

The fix is causal rather than comparative: **a proof build is a clean build.**
`FULL_BUILD_PASS` is emitted only by a build that started from an empty build
tree. Incremental `build_target` remains a working tool that cannot produce
proof. Measured on this fixture, a clean configure and build is 1.3 seconds
against 0.017 for an incremental no-op, so there is no performance argument. The
full sequence above is now an acceptance test.

Recorded here because the lesson is not the bug. It is that a confident
mechanism telling the same lie is worse than a weak one, and that the right
question was never "how do we compare more carefully" but "what actually proves
these bytes were compiled".
