# Brief for Fable: hostile review of an experiment

You get one pass. There is no follow-up round, no clarification turn, and no
budget for a second attempt. Everything you need is in this document. If
something you want is missing, state the assumption you are making and carry
on. Do not end your reply with questions.

## 0. Your role, and what you are not being asked to do

You are the chief architect reviewing an **experimental design and its first
dataset**. You are not reviewing code quality, naming, packaging, test
coverage, or Python style. Those have been through five rounds of adversarial
review with a separate reviewer and are not what this costs money to answer.

The single question you are being paid to answer is:

> **If this experiment is run at scale and the result is written up, what is
> the finding that a hostile expert would demolish, and is the design capable
> of supporting any honest claim at all?**

Rank everything you say by whether it changes what gets run next. A defect
that cannot change the measured difference between conditions is a footnote,
not a finding, and should be labelled as one.

## 1. What the instrument is

`local-code-agent` is a local C++ coding agent built as a **measurement
instrument**, not as a product. It runs a tool-calling loop against an
OpenAI-compatible endpoint (llama-server, OVMS, or a cloud model behind the
same interface: the client does not know which).

Relevant mechanics, stated once so you do not have to infer them:

- **Tools** are typed. Every `ToolResult` carries two independent axes:
  `ExecutionStatus` (OK / BLOCKED / ERROR) and `DomainStatus`
  (PASS / FAIL / UNKNOWN), plus a typed `Reason` with a derived `Locus`.
  `ran` means the tool executed. `observed` means it executed **and** produced
  a domain verdict. `DomainStatus` defaults to "not stated"; an explicit
  `UNKNOWN` survives inference and is never silently upgraded.
- **Verification is orchestrator-owned, not model-owned.** Only
  `build_target` and `run_test` can satisfy verification, only when the call
  was untargeted and unfiltered, and only when the result was `observed`. The
  model cannot talk its way into a verified state.
- **Mutation epoch**: a successful WRITE or DANGEROUS tool bumps an epoch,
  which resets `verified` and scopes the repeat guard. Proof obtained before
  an edit does not count after it.
- **Build freshness** is a stamp file written only by an untargeted successful
  build. A targeted build does not clear it. A profile stamp records what
  CMake was actually configured with, so the reported profile cannot lie about
  the cache.
- **Toolsets are enforced at execution.** The registry is consulted **before**
  narrowing, so an invented tool (`unknown_tool`) is typed apart from a real
  tool that was not offered (`tool_not_allowed`).
- ANSI is stripped before any deterministic log parsing.
- Clocks: connect 10 s, read 480 s, request deadline 900 s, stall detector
  1.0 tok/s over a 60 s window.

## 2. The research question

> What fraction of real, repetitive C++ engineering work can be completed
> **locally, with verified results**, without a premium cloud model, and how
> much of that can a low-power NPU tier carry on its own?

The hypothesis under test is an **interaction**, not a main effect:

> Deterministic skills (a fixed written procedure plus a narrowed toolset)
> help a small model (8B) **disproportionately more** than they help a large
> one (30B). The large model can improvise the procedure; the small one
> cannot.

If that holds, the engineering consequence is that procedure is a substitute
for parameters within a bounded task class, which is the only reason a
low-power tier is interesting at all.

## 3. The design

Three conditions, same tasks, same fixture, same model, same prompt:

| Condition | Procedure text | Toolset |
|---|---|---|
| `control` | none | full registry |
| `narrow`  | none | the skill's toolset |
| `skill`   | the skill's `SKILL.md` body | the skill's toolset |

The tool **catalogue** is off in all three, so no condition gets a free hint
about which tools exist beyond what it is offered.

Two contrasts fall out:

- `narrow − control` = the effect of **narrowing the action space**
- `skill − narrow`   = the effect of **the written procedure**

That decomposition is the load-bearing part of the design. A single
skill-versus-no-skill comparison conflates the two and is worth very little,
which is why the earlier exploratory run is not being used as a result.

## 4. Tasks and grading

Ten cases, each pinned to one of eight skills:

| Case | Skill | Expected claim |
|---|---|---|
| `clean-build` | build-and-test | success |
| `compile-error-locate` | diagnose-build-failure | diagnosis |
| `compile-error-fix` | fix-build-failure | success |
| `link-error` | diagnose-build-failure | diagnosis |
| `test-failure-diagnose` | diagnose-test-failure | diagnosis |
| `test-failure-fix` | fix-test-failure | success |
| `segfault` | diagnose-test-failure | diagnosis |
| `timeout` | diagnose-test-failure | diagnosis |
| `navigation` | repo-navigation | success |
| `review-restraint` | git-review | success |

Grading has three independent gates, all of which must pass:

1. **`required_checks`**: hard gates, never averaged. Example: a fix case
   requires a full untargeted build **and** the whole test suite to have
   passed **after** the last edit. A filtered `run_test`, a `rerun_failed`, a
   zero-match filter, or stale sources do not count.
2. **`expected_claim`**: exact set membership. A diagnosis case that returns
   `success` fails even if the prose is correct, and vice versa.
3. **`checks`**: weighted quality rubric, threshold 0.75. Never used to rescue
   a failed hard gate.

`verification_required`, `required_checks` and `expected_claim` all have **no
default**, deliberately: a defaulted `True` previously broke two read-only
cases in both cells and was only caught because one of them has a check
literally named "did not build".

No skill's `SKILL.md` mentions `submit_answer` or names a claim. The claim
vocabulary lives only in the shared prompt, identically for all three
conditions, so the skill text cannot coach the answer format.

## 5. Provenance discipline

Every row carries:

- `source_sha256`: hashes `src/local_agent`, `.github/skills`, `tests/evals`,
  `fixtures/cpp_sandbox`, `devtools/*.py`, `devtools/*.sh`, `pyproject.toml`.
  Current: `a4c9b14e52a50ef4bac71eb21e3c2f9b800851030a1556058e9edac33b3237a6`
- `base_prompt_sha256`: the model-facing contract, hashed separately, because
  the prompt can change without the evaluator changing and the two have
  different consequences. Current: `37f984092fed31ea...`
- `package_commit` from `PACKAGE.json`: `5061a6fa...`, built clean.

**Any dataset collected before prompt `37f98409` is historical and not
comparable to anything collected after it.** That includes the previously
reported 10/10 skill-condition run and the 6/10 exploratory no-skill run. The
6/10 was collected under an evaluator with four known scoring cheats since
fixed, so it is an **upper bound on the no-skill cell and nothing else**. It
has never been row-level re-scored. Treat any argument that leans on it as
unsupported.

`devtools/rescore.py` exists to re-score frozen datasets without re-running
them. It reads only, emits a derived artifact carrying `source_dataset_sha256`
and the evaluator identity, and marks each row `fully` / `partially` /
`not_replayable`. A partially replayable row yields a **provisional** score
that is explicitly not a result.

## 6. Known limitations, stated up front

Do not spend your pass rediscovering these. Do tell me if any of them is
**fatal** rather than merely ugly, and in particular whether any of them can
affect the three conditions **differently**, because that is the only kind of
defect that biases the contrast rather than the absolute score.

1. **Fixture comments describe the injected defects.** A model that reads the
   source can be told the answer by a comment. Affects all conditions, but not
   necessarily equally: conditions differ in how much file reading their
   toolset encourages.
2. **The clean baseline is recoverable** via `git show HEAD:<file>`. A model
   with `git_diff` can in principle reconstruct the pre-defect state rather
   than reason about it.
3. **Ten tasks, no repeats.** No variance estimate at all. Every number is a
   single sample.
4. **The fixture is small and synthetic.** It is deliberately not real work
   and not anyone's intellectual property.
5. **The skill cell has already scored 10/10** under a previous prompt. If
   `control` also scores near ceiling, the fixture cannot resolve the
   hypothesis regardless of how good the instrument is.
6. `re.compile` is not ctest's grammar, so the filter validator is
   approximate and rejects conservatively.
7. Separate build directories per profile are not used; profile transitions
   force a reconfigure.

## 7. Pre-committed falsification criteria

Written before the data exists, so that it cannot be adjusted afterwards.
Judge whether these are the right lines, and whether they are strict enough.

- **Ceiling.** If `control` scores ≥ 9/10, the fixture is saturated. The
  correct response is a harder fixture, not a claim. No thesis is reported
  from a saturated benchmark.
- **Floor.** If `control` scores ≤ 2/10, the tasks are testing basic tool-use
  competence rather than procedure, and the contrast measures the wrong thing.
- **Main effect.** If `skill − control` is ≤ 1 task on the 30B, the effect at
  this scale is indistinguishable from noise on n = 10 and is reported as
  "no measurable effect", not as a trend.
- **Interaction.** The thesis is about the small model. If the 8B gain is not
  **larger** than the 30B gain, the interaction hypothesis is falsified and
  will be reported as falsified. A gain that is merely equal does not support
  it.
- **Decomposition.** If essentially all of `skill − control` is carried by
  `narrow − control`, the finding is "tool narrowing works", not "skills
  work", and must be written that way.
- **Efficiency versus capability.** If the conditions tie on correctness but
  differ on call count or wall clock, the claim is about efficiency only.
  Correctness claims require correctness evidence.

## 8. The dataset

<!-- PASTE BEFORE SENDING. Fable must see real numbers, not a plan. -->

```
PACKAGE:        source_sha256 =
                base_prompt_sha256 =
                package_commit =
MODEL / HOST:
RESULTS:        control = __/10    narrow = __/10    skill = __/10
PER-CASE TABLE: (case, condition, required_ok, claim_ok, quality, calls, seconds)
ANOMALIES:      (any BLOCKED/ERROR, forbidden_tool attempts, halts, timeouts)
```

Attached: the three `results-*.jsonl` files and the transcripts.

## 9. What to answer, in this order

1. **Is the contrast valid?** Can any known or suspected defect move
   `control`, `narrow` and `skill` by different amounts? Name the mechanism,
   not the worry.
2. **Does the dataset support the claim being drawn from it, or a weaker
   one?** If weaker, write the strongest sentence the data actually supports.
3. **Is the falsification set in section 7 honest?** Name any line that is
   drawn where it is because it is convenient.
4. **Is the grading fooled?** Given the three gates, construct the cheapest
   behaviour that scores well without doing the work. If you cannot, say so.
5. **Is the fixture worth scaling?** The next step is a 15 to 20 task pilot
   fixture. Say what it must contain that this one does not, and what it must
   stop containing.
6. **What is the single most likely reason this whole line of work produces
   nothing publishable?** One paragraph, no hedging.

## 10. Output contract

- Verdict first, in one sentence: does this design survive, survive with
  changes, or not survive.
- Then findings, ranked, each as: **claim**, **mechanism**, **what it changes
  about the next run**. No finding without a mechanism.
- Then the strongest honest sentence the current data supports, written as it
  would appear in a write-up.
- Then the one thing to change before the pilot.
- No summary of this brief back at me. No praise. No questions. Assume I am
  hostile to my own result and want it broken.
