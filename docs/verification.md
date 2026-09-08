# What counts as proof

The model never decides whether its work is verified. That decision is made
from typed tool results by code the model cannot reach. This document is why
each rule exists, because every one of them was added after something got
through.

## Two axes, not one

```
ExecutionStatus   OK | BLOCKED | ERROR      did the tool run
DomainStatus      PASS | FAIL | UNKNOWN     what did it find
```

Conflating these was a real bug. Worked examples:

| Situation | Execution | Domain |
|---|---|---|
| gcc returns a compile error | OK | FAIL |
| cmake is not installed | BLOCKED | UNKNOWN |
| ctest reports a test TIMEOUT | OK | FAIL |
| our runner killed ctest after its own budget | ERROR | UNKNOWN |

A compile error is evidence. A missing toolchain is not a model failure and is
excluded from the capability denominator. Our own wall clock running out says
nothing about the code.

`DomainStatus` defaults to "not stated" and is inferred from `ok` for the many
tools that only set that. An explicit `UNKNOWN` is never silently upgraded: a
tool that ran cleanly and learned nothing has to be able to say so.

Two derived properties:

- `ran` — the tool executed cleanly, whatever it then found
- `observed` — it executed **and** produced a domain verdict

`run_test` with a filter matching no test exits 0 having exercised nothing. It
`ran`. It did not `observe`. That distinction stopped a null result counting as
a reproduction.

## One classifier, four consumers

`local_agent/verification.py` turns `(tool, arguments, execution, domain,
evidence)` into a `ProofKind`:

```
FULL_BUILD_PASS       an untargeted build that passed
FULL_TEST_PASS        the whole suite, current, passed
OBSERVED_BUILD_FAIL   a real compile or link failure
OBSERVED_TEST_FAIL    a real test failure, filtered or not
TARGETED_BUILD_PASS   one target built; proves that target
TARGETED_TEST_PASS    a subset passed; proves that subset
NO_CURRENT_PROOF      everything else
```

Only `FULL_BUILD_PASS` and `FULL_TEST_PASS` can satisfy a verification
contract.

It takes primitives, not objects, so four callers with incompatible types can
share it: the orchestrator's `ToolResult`, the evaluator's `ToolCallRecord`,
the offline re-scorer's JSON rows, and the tests.

It deliberately does **not** take the mutation epoch. Whether proof is current
is a question about when the call happened relative to the last edit, and only
the caller holds both numbers. Folding it in would let a caller that forgot the
epoch get a green answer.

**Why one classifier.** There used to be two, one in the orchestrator and one
in the evaluator. They agreed until they did not. They disagreed about
`--rerun-failed` and about stale passes, so a run could be recorded as verified
by the runtime and then scored as unproven by the evaluator, and the model saw
the permissive answer while the dataset recorded the strict one. Two answers to
"was this proved" is worse than either answer.

## The ways a pass proves nothing

Each of these is a reproduction that scored a false success before it was
closed.

**Narrowed.** `name_filter` runs a subset. `--rerun-failed` runs only what
failed last time, which after a fix is exactly the one test just changed. A
`target` builds one thing: under the link-error scenario `build_target(target=
"sandbox")` passes while the project does not build.

Narrowing is read by truthiness, not membership. A model sending
`rerun_failed: false` has asked for the full thing, and the tool agrees, so the
classifier must too. Testing `"rerun_failed" in arguments` would reject a
genuine full suite for mentioning the parameter, and these models fill in
defaults constantly.

**Stale.** ctest does not compile. A pass against binaries older than the source
is a fact about the old binaries. Both a stale pass and a stale failure are
`UNKNOWN`: a red suite from an old binary is no more informative than a green
one, and reporting it as a failure would send a diagnosis after a defect that
may already be fixed.

**Unbuilt.** No successful full build recorded means freshness is unknown, not
that nothing is stale. That distinction is the whole of it: reading "no record"
as "nothing wrong" let a tree nobody had built return a green suite.

**Wrong profile.** Configuring for a profile is not building it.
`configure_project(profile="release")` moves the marker without compiling, and
the leftover debug binaries would otherwise come back as a passing release run.
A profile is verified only when a successful **full build of that profile** is
recorded and newer than every source file.

**Empty.** ctest exits 0 having run nothing and reports "all tests passed (0
tests)". True, and it verified nothing.

**Superseded.** Proof does not survive a mutation. A successful WRITE or
DANGEROUS tool bumps the mutation epoch, which resets `verified` and scopes the
repeat guard. The tree that was proved is not the tree being submitted.

## The build record

`build/.local-agent-build-ok`, written only by an untargeted successful build,
carrying the profile it produced and the timestamp the filesystem assigned:

```json
{"profile": "debug", "at": 1788769725.49}
```

Three properties, each of which was learned the hard way.

**Written only by an untargeted build.** Refreshing it after
`build_target(target="test_text_util")` made an unrelated stale test binary
look fresh.

**Dated from its content, never its mtime.** The stamp is an ordinary file
inside the repository and `propose_patch` was happy to rewrite it. Three calls,
nothing compiled, and the staleness gate opened. Writes into the build
directory, the run journal and `.git` are now refused outright with a typed
`PROTECTED_PATH` reason, and even a write that bypasses the tools moves no
baseline.

**The timestamp comes from the filesystem, not `time.time()`.** Source
staleness compares filesystem mtimes. Two clocks agree on local disk and can
disagree on a network mount or a drifted guest clock, and a build recorded
later than it happened would hide a real stale source.

A stamp that cannot be parsed, or that carries no usable profile and timestamp,
is unknown provenance rather than a free pass.

## Verification is not task completion

`verified` and `required_ok` answer different questions and are allowed to
differ.

- `verified` — the current tree builds or passes, decided by the orchestrator
  from its own history
- `required_ok` — the task was done as the case contract asked

A control-condition run that was asked to *explain* a compile error, fixed it
instead, and then built successfully is `verified=true` and `required_ok=false`.
That is not a disagreement about proof. It is a correct proof of the wrong
thing.

The operational definition of a **verification disagreement** is narrower: a
row whose runtime proof contradicts the evaluator's *proof* check. Across all
three cells of the first dataset that count is zero, which is the strongest
single statement the smoke run makes about the instrument.

## Replay

`measurement/rescore_dataset.py` re-scores a frozen dataset without re-running it. It
reads only, and emits a derived artifact carrying the source dataset hash and
the evaluator identity.

It treats a **missing evidence key as unknown, never as False**. These keys
exist to say "this pass proves nothing", so their absence cannot honestly be
read as "nothing was wrong". The consequence is retrospective and uncomfortable
and it is the correct behaviour: every dataset collected before a new
invalidating key existed becomes *partially replayable* the moment that key is
added, and receives a provisional score rather than a definitive one.

Rows are marked `fully_replayable`, `partially_replayable` or
`not_replayable`, and only the first can produce a definitive verdict.
