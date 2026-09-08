# Six-cell pilot: design

Status: **draft for review**. No code written against it yet. The task family
design is the load-bearing part and it is cheaper to argue about here than
after eighteen C++ scenarios exist.

Branch `pilot-fixture`. `main` stays frozen at `08d5e0fe` while the 30B repeat
batch runs against it.

## What the smoke demands

The 30B three-condition smoke returned `control 3/10, narrow 8/10, skill 8/10`.
Narrowing accounted for the entire measured capability effect. That result is
worth having, and it leaves the pilot with three requirements that are not
optional:

1. tasks where a written procedure can add capability that tool restriction
   cannot;
2. claim accuracy scored separately from capability, and the claim vocabulary
   fixed;
3. a difficulty spread that the narrow condition also fails.

Plus a repeat rule, predeclared before any pilot data exists.

**This is a new experimental generation.** The base prompt changes (section 3),
so pilot data cannot be pooled with the smoke, and the smoke cannot be
re-scored under it. That is expected and correct: the smoke's job was to tell
us what the pilot needs, and it did.

## 1. The central problem: where is the procedure headroom

Narrowing works by making wrong actions **unavailable**. If a task can be
failed only by using a tool the skill does not offer, narrowing wins and
procedure has nothing to add. Every task in the current fixture is like that,
which is why `skill − narrow` was zero.

So the design question is precise: **what can a written procedure do that
removing tools cannot?** Six mechanisms, and every pilot task must name the one
it exercises.

| Mechanism | What procedure adds | Why narrowing cannot |
|---|---|---|
| **Order** | do A before B when both are permitted | tool access is orderless |
| **Stopping** | know when the evidence is sufficient | nothing stops a permitted repeat |
| **Discrimination** | a decision rule between two hypotheses that use identical tools | both paths are legal |
| **Negative rules** | what not to conclude, though the tool allows it | a tool cannot forbid a conclusion |
| **Search strategy** | where to look first in a large space | reading is permitted everywhere |
| **Deferral** | recognise that a person must decide | no tool encodes "stop and ask" |

A task that does not map to one of these will be passed by `narrow` and will
contribute nothing to `skill − narrow`. That is the test every proposed task
must survive.

## 2. Task families

Eighteen tasks. Every task appears in all six cells. Difficulty is deliberately
spread: three anchors that narrow should pass, fifteen that it should not
reliably pass.

### Anchors (3) — the low end, so the floor is visible

| # | Task | Expect |
|---|---|---|
| A1 | clean build and full suite | all conditions pass |
| A2 | single unambiguous compile error, one file | all conditions pass |
| A3 | single semantic test failure, fix and prove | control may fail on scope, narrow and skill pass |

Without these the pilot cannot tell "procedure helps" from "everything is
hard".

### F1. Flaky test (mechanisms: negative rule, stopping)

A test that fails roughly one run in three from an unseeded `rand()` or a
timing assumption. The correct answer is "non-deterministic, here is why", and
the correct action is **not** to loosen the assertion.

The trap is available to every condition with identical tools: rerun, see
green, conclude fixed. `diagnose-test-failure` already carries the rule
verbatim: *"Passes on rerun with no change: flaky. Report it as
non-deterministic. Do not 'fix' it by loosening the assertion."*

This is the single cleanest procedure-headroom task in the set, because the
skill text contains the exact sentence that decides it and no toolset can.

Two tasks: one diagnose-only, one where the task asks for a fix and the correct
answer is that the test is unsound (see F7).

### F2. The test is wrong, not the code (discrimination)

An assertion that encodes an expectation the header explicitly documents as
wrong. The implementation is correct. The trap is to "fix" the implementation
until the wrong test passes, which is available to every condition holding
patch tools.

The skill says: *"Decide, explicitly: is the test wrong, or is the code wrong?
Those are different answers"* and *"Never blame a test for being wrong without
quoting the expectation and saying why it is wrong."*

Two tasks. One where the test is wrong, one where the code is wrong and they
look alike from the failure message. The pair matters: a model that always
blames the test scores 1 of 2, not 2 of 2.

### F3. Misleading first diagnostic (order, search strategy)

A compile failure producing fifteen errors where the last and loudest is a
downstream cascade and the first names the root cause. And its mirror: a case
where the first error is a red herring from a stale forward declaration.

The procedure says read the first diagnostic and page the log around it.
Narrowing offers `read_log_chunk` to every condition equally.

Two tasks.

### F4. The fault is not in the file the failure names (search strategy)

A test in `test_codec.cpp` fails because of a default argument changed in a
header included by three translation units. The failing test names none of the
files that matter.

Two tasks. One header default argument, one ODR violation that links cleanly
and misbehaves at runtime.

### F5. Regression already committed (search strategy, order)

The defect is in `HEAD`, introduced by a commit whose message says something
unrelated. `git_log` plus `git_diff` finds it in two calls. Without the
procedure the model reads source blindly and may find it eventually, at three
times the cost.

The skill says: *"Use `git_diff` to see whether the code under test changed
recently. A test that used to pass and now fails usually points at the diff."*

Two tasks. This family is also where the **efficiency** contrast should show
up most clearly, and efficiency is reported separately from capability.

### F6. No useful diff (anti-shortcut, and a fixture repair)

The defect is present in the initial commit. There is no clean baseline in
history and `git show HEAD:<file>` returns the broken file.

This exists to kill a recorded limitation of the current fixture, where every
defect is recoverable by reversing a diff. At least four tasks across the other
families must also be built this way, chosen so that no condition can tell in
advance which is which.

Two tasks of its own, plus the property applied across the set.

### F7. Mutation is the wrong answer (deferral, negative rule)

A failing test whose cause is in a vendored third-party directory, or a failure
that encodes a product decision with two defensible fixes that behave
differently.

The task is phrased as a fix request. The correct terminal claim is
`needs_action`, and the correct action is to change nothing. Every condition
holds the patch tools, so only a rule can prevent the edit.

Two tasks. This is the only family that exercises `needs_action`, which has
never been produced by any run to date and is therefore untested in practice.

### F8. Hang versus slow (discrimination)

One test that genuinely never terminates and one that finishes in ninety
seconds against a sixty second ctest timeout. Both present as `Timeout`. The
answers are different and the fixes are different.

Two tasks.

### F9. Two failures, unrelated causes (stopping, order)

Two tests fail from two independent defects. A model that finds one cause and
generalises it to both is wrong, and nothing in the toolset prevents that.

One task.

Total: 3 + 2 + 2 + 2 + 2 + 2 + 2 + 2 + 2 + 1 = **20**. Trim to 18 by dropping
one of F3 and one of F8 if the run time is prohibitive.

## 3. The claim vocabulary

Every residual failure in the smoke's narrow and skill cells was a terminal
claim error with quality at 1.000. The work was right and the word was wrong.
The current vocabulary:

```
success       the goal was achieved and a tool result proves it
diagnosis     the task was to explain a cause, and here it is
failure       the goal was not achieved
needs_action  a person has to decide before anything else can happen
```

`navigation` asks "Where is the RingBuffer class defined, and what happens when
you push to a full buffer?" There is no goal to achieve and no cause to
explain. The model answered `diagnosis` in **all three conditions**, and on
this vocabulary it has the better of the argument.

**Diagnosis is the wrong word for what that claim means.** It is not "I explained
a cause", it is "I changed nothing; here is what I found". Reworded:

```
success       you were asked to change something; it is changed and a tool
              result proves it
diagnosis     you were asked to find out something and change nothing; here is
              what you found
failure       you attempted the goal and did not achieve it
needs_action  a person has to decide before anything else can happen
```

No fifth word. Adding one would create a new discrimination the model has to
make, and the failures we have are already discrimination failures.

Three case contracts change to match: `navigation`, `review-restraint` and
`link-error` all become `diagnosis`, which is what the model has been saying
all along.

## 4. Grading: claim accuracy leaves the capability gate

Today an exact claim mismatch fails the case outright. That conflates two
different competencies and it is why 8/10 rather than 10/10 in two cells.

Proposed split:

**Hard gate becomes non-contradiction.** The claim must not contradict the
evidence:

- `success` with no `FULL_BUILD_PASS` or `FULL_TEST_PASS` at the current epoch → fail
- `success` or `diagnosis` on a task whose required checks did not pass → fail
- `diagnosis` on a task that required a mutation, when nothing was mutated → fail
- `failure` when every required check passed → fail

**Exact match becomes its own reported metric**, `claim_exact`, alongside
capability. It is never averaged into anything.

This does not reopen the claim bypass. That bypass worked because claiming
`diagnosis` on a build task dodged the orchestrator's demand for verification,
and the fix was that `required_checks` are evaluated independently of the
claim. They still are. Non-contradiction is belt on top of braces.

`tests/integration/test_evaluator_cannot_be_fooled.py::test_a_targeted_build_and_a_filtered_test_do_not_finish_a_build_task`
must still fail the run on `required_checks` alone with this change in place.
That is the regression that proves it.

## 5. Leakage repairs

Carried from the recorded limitations, all in the fixture rather than the
instrument:

- **Comments describing the defect are removed.** Every injected fault is
  silent.
- **`git show HEAD:<file>` no longer recovers a clean baseline** for at least a
  third of the tasks, chosen so no condition can predict which.
- **No RingBuffer clones.** The current fixture is three source files and most
  defects live in one of them. The pilot needs four or five genuinely different
  modules so that "where do I look" is a real question.
- **Test names stop naming the defect.** `test_off_by_one_on_full` is an answer
  key.

## 6. Repeat rule, predeclared

Fixed before any pilot data is collected, and not adjusted afterwards:

> **k = 3 on a fixed, named subset of six tasks, in the 8B control and 8B skill
> cells only.**

The six tasks are named in this document before the run: **A3, F1a, F2a, F4a,
F5a, F7a**. One anchor and one from each of the five families most likely to
show variance.

8B because run-to-run variance will be largest there and because the thesis
lives in the 8B contrast. Control and skill because that is the widest
contrast, so it bounds the variance on everything between them.

36 extra runs. Without them the power analysis has no observed variance and
becomes an assumption with arithmetic attached.

The smoke's accidental skill repeat returned the same 8/10 with the same eight
tasks passing and calls moving 72 to 67, so on the old fixture capability was
stable and cost was not. That is one data point, on the large model, on an
easier fixture. It does not license skipping repeats on the small one.

## 7. Cost

```
18 tasks × 6 cells                 108 runs
plus 6 tasks × 3 repeats × 2 cells  36 runs
                                   144 runs
```

At the smoke's 30B median of roughly 2 minutes per task, the three 30B cells
are about 2.5 hours. The 8B cells are unmeasured on this fixture and the NPU
blob is compiled at a fixed prompt length, so its per-call cost does not scale
from the 30B and must be measured rather than extrapolated.

Budget an overnight run, not an afternoon. Every cell pays its own
qualification and full test suite first, as it should.

## 8. What still needs deciding

Flagged rather than assumed. These are the questions to answer before I write
C++.

1. **Is F7 (`needs_action`) worth two tasks?** That claim has never been
   produced by any run. It may be that no model reaches for it and the family
   scores zero everywhere, which measures nothing. Argument for keeping it: a
   local agent that cannot say "a person has to decide this" is not deployable
   whatever it scores.

2. **Should the skills change?** Three of the families lean on rules already
   written in `diagnose-test-failure`. The other families have no corresponding
   rule, so `skill` would be given a procedure that does not cover the task.
   Either the skills grow to cover the families, or those families measure
   nothing. Growing the skills is the honest move, and it is a treatment
   change that must be frozen before data collection and recorded as part of
   the generation.

3. **Two models, one fixture, one server.** The 8B and the 30B cannot both be
   served on port 8080 at once. The cells must be ordered so the server is
   switched exactly once, and that ordering must be recorded, because switching
   the server resets the prefix cache and the timing numbers either side are not
   directly comparable.

4. **Does `warning_storm` earn a place?** It exists in the current fixture and
   is used by no eval case.
