# Findings: 30B, three conditions, three repeats

Nine cells, 90 case rows, one instrument.

```
source_sha256       08d5e0fe6f2be291a0d62e14d664b4909932ee6041ecf5a78d4160af27a6641a
base_prompt_sha256  37f984092fed31eac321cf724946fcaffff590c1f624fb54777674e0c5cc4994
package_commit      c92a242b88c67bc0080018791df04fc9b69c0f3e
model               qwen3-coder-30b, UD-Q4_K_XL, CPU, llamacpp b10816-427291b5b
```

Identical to `2026-09-08-30b-three-conditions`, so the two datasets are formally
poolable. This one is reported on its own and the pooled figure is a footnote.

Order was balanced: control/narrow/skill, then narrow/skill/control, then
skill/control/narrow. No condition sat in the same slot twice.

## Headline

Verified completion, valid rows only:

| condition | per repeat | pooled | |
|---|---|---|---|
| control | 2/10, 4/10, 3/10 | **9/30** | 0.300 |
| narrow | 7/10, 7/10, 8/10 | **22/30** | 0.733 |
| skill | 8/10, 7/9, 8/10 | **23/29** | 0.793 |

```
narrowing  (narrow - control)  +0.433
procedure  (skill  - narrow)   +0.060
```

One row is missing from skill repeat 2: `compile-error-locate` came back
`INVALID_SERVER_UNAVAILABLE` and is excluded from its denominator rather than
counted as a failure, which is what that validity type is for.

This replicates the smoke run's shape. Restricting the tool set is the effect.
Nothing here changes that.

## What three repeats bought that one did not

### 1. The near-zero procedure aggregate is two effects cancelling

The smoke reported that adding the written procedure on top of narrowing
changed verified completion by zero. The observed `narrow -> skill` difference
here was not literally zero, but it stays small and unresolved, and three
repeats of the same ten tasks are not independent evidence of a population
effect. What the repeats bought is structure: the near-zero aggregate is an
average over two opposite per-case effects. Per case:

| case | control | narrow | skill | narrow − control | skill − narrow |
|---|---|---|---|---|---|
| clean-build | 3/3 | 3/3 | 3/3 | +0.00 | +0.00 |
| compile-error-fix | 3/3 | 3/3 | 3/3 | +0.00 | +0.00 |
| compile-error-locate | 0/3 | 2/3 | 2/2 | +0.67 | +0.33 |
| **link-error** | 0/3 | 0/3 | **3/3** | +0.00 | **+1.00** |
| navigation | 0/3 | 0/3 | 0/3 | +0.00 | +0.00 |
| **review-restraint** | 0/3 | **2/3** | **0/3** | +0.67 | **−0.67** |
| segfault | 1/3 | 3/3 | 3/3 | +0.67 | +0.00 |
| test-failure-diagnose | 0/3 | 3/3 | 3/3 | +1.00 | +0.00 |
| test-failure-fix | 0/3 | 3/3 | 3/3 | +1.00 | +0.00 |
| timeout | 2/3 | 3/3 | 3/3 | +0.33 | +0.00 |

The +0.06 aggregate is `link-error` at +3 rows, `compile-error-locate` at +1,
and `review-restraint` at −2. With one repeat these were indistinguishable from
noise, and the honest reading was "no measured effect". With three they are two
named, opposite, per-case effects. That is a statement about where the movement
sits in this fixture, not a claim that the underlying procedure effect is
non-zero. Ten tasks repeated three times cannot support that claim.

### 2. `link-error` is a clean, deterministic procedure effect

Narrow and skill were offered byte-identical tool payloads on this case (the
purity table confirms it, schema `c21935ce6e5cd069` for both). The only
difference between the arms is the written procedure. What changed:

| | claim | expected | calls |
|---|---|---|---|
| narrow r1/r2/r3 | `failure` | `diagnosis` | 13, 10, 11 |
| skill r1/r2/r3 | `diagnosis` | `diagnosis` | 5, 7, 5 |

Three for three, both ways. Narrow finds the link failure, reports it as a
failure, and is marked wrong because the task asked for a diagnosis. Skill
reports the same finding as a diagnosis and is marked right, in half the calls.

That is not the procedure teaching the model to link C++. It is the procedure
telling the model what kind of answer the task wants. Worth saying plainly,
because it is a smaller claim than "skills make the agent better at
engineering" and it is the one the data supports.

### 3. `review-restraint` moved the other way, and it stays in the write-up

| | claim | ok |
|---|---|---|
| control r1/r2/r3 | `failure`, `failure`, `failure` | 0/3 |
| narrow r1/r2/r3 | `success`, `None`, `success` | 2/3 |
| skill r1/r2/r3 | `None`, `None`, `failure` | 0/3 |

Under the skill the agent stopped answering: two of three runs finished with no
claim at all, in one and two tool calls. Small numbers, and it is the only case
that moved against the treatment, which is exactly why it is not getting
averaged away in a sentence about net effect.

### 4. `navigation` is broken and is eating a tenth of the resolution

0/9. Every condition, every repeat, the same failure: claim `diagnosis`,
contract expects `success`.

The same claim mismatch appearing 9/9 times, across all three conditions and
all three repeats, points at a case contract that does not match the task it
ships with rather than at condition-specific model behaviour.
Right now `navigation` cannot distinguish anything, it just removes 0.1 from
every arm equally. Fix the contract or drop the case before the pilot.

### 5. Control's failure mode is now named and stable

Scope violations, by cell:

```
control   4, 3, 4     11 of 30 rows
narrow    0, 0, 0      0 of 30 rows
skill     0, 0, 0      0 of 29 rows
```

On `compile-error-locate`, `link-error` and `test-failure-diagnose`, control
patched the repository on all three repeats and then claimed `success` on a
task that asked only for a diagnosis. It also did it on `segfault` twice.

So the 0.300 is not the model failing to understand C++. Control solves plenty
of these. It fails because, given the full registry, it does work it was not
asked to do and then misreports what it did. That is a much more specific
finding than "the small model needs help", and it is the mechanism the whole
narrowing contrast was built to isolate.

## Cost

| | tool calls | model calls | median task | wall, three repeats |
|---|---|---|---|---|
| control | 138, 125, 132 | 139, 129, 136 | 281s, 205s, 295s | 138.3 min |
| narrow | 82, 74, 72 | 91, 84, 82 | 135s, 132s, 110s | 67.3 min |
| skill | 68, 67, 70 | 78, 76, 80 | 109s, 127s, 118s | 61.0 min |

Control costs roughly twice the wall clock for less than half the verified
completions. The procedure buys a further 10 percent off the call count on top
of narrowing, consistently across all three repeats, which is a smaller and
steadier effect than its effect on correctness.

## Instrument integrity

The wrapper's own check ran and returned **FAIL** on one item. Everything else
passed:

- 9/9 cells complete, 90/90 case rows
- narrow and skill tool payloads identical per case, 10/10
- control payload distinct per case, 10/10, and stable across all three repeats
- identity fields stable across all nine cells
- 0 invented tools, 0 reaches for a real tool the condition did not offer
- `tiered` false and `escalated` zero on every cell

The failing item is one proof-level verification disagreement, and it is a real
defect in the instrument rather than a definitional artefact.

### The defect

`repeat-02 / 03-control / link-error`. The runtime recorded `verified = True`.
The evaluator's independent oracle re-verified the tree and returned:

```json
{"configure": "pass", "build": "pass", "tests": "fail", "ok": false,
 "detail": "1 test(s) FAILED: ring_buffer"}
```

The history explains it. The agent patched, built (pass), then ran tests three
times: one that matched nothing, one filtered run that failed, and a full suite
that came back 1 failed of 4. It then claimed `success`.

`state.verified` is set when a call classifies as `FULL_BUILD_PASS`, and the
only thing that ever clears it is `note_mutation()`. A build pass followed by an
observed test failure on the same mutation epoch leaves `verified` standing. So
`verified` currently means "something built" rather than "the tree is proven",
and on this row it stood behind a false claim of success.

**Impact on the numbers: none.** That row already fails on two required checks,
"did not modify the repository" and the wrong claim type. No headline figure
moves. The cross-check existing and firing is the machinery working.

**The fix.** An `OBSERVED_BUILD_FAIL` or `OBSERVED_TEST_FAIL` on the current
epoch must clear `state.verified`. Proof should be retractable by contradiction,
not only by mutation. One line at the assignment site plus a test in
`test_evaluator_cannot_be_fooled.py` that walks build-pass then test-fail and
asserts `verified` is False.

That fix moves `source_sha256`, so it is a new generation and it does not
happen to this dataset. Do it before the pilot, with the four Intel scrubs and
the PR #1 control-condition fix, in one deliberate cut.

## What this does not show

No 8B ran. The interaction hypothesis, that deterministic procedure helps a
small model disproportionately more than a large one, is exactly as untested as
it was this morning. This dataset establishes the 30B baseline and the
per-case structure the 8B will be compared against. It is a control arm for an
experiment that has not been run.

Three repeats is also still three. `link-error` at 3/3 versus 0/3 is a strong
signal for nine observations; `review-restraint` at 0/3 versus 2/3 is five
observations pretending to be a finding. Treat the first as real and the second
as a flag.

## Pooled with the smoke run

Same `source_sha256` and `base_prompt_sha256`, so the two may be put in one
table:

| condition | cells | pooled |
|---|---|---|
| control | 4 | 12/40 = 0.300 |
| narrow | 4 | 30/40 = 0.750 |
| skill | 5 | 39/49 = 0.796 |

Narrowing +0.450, procedure +0.046. The shape does not move.
