# Experiment design

## The question

> Can deterministic procedural skills and controlled tool access move useful,
> verified engineering work onto a smaller, cheaper local model, and eventually
> onto low-power hardware such as an NPU?

The hypothesis under test is an **interaction**, not a main effect:

> A fixed written procedure helps a small model disproportionately more than a
> large one, because the large one can improvise the workflow itself.

If that holds, procedure is a partial substitute for parameters within a
bounded task class, which is the only reason a cheap tier is interesting.

## Three conditions

A binary skill-versus-no-skill comparison bundles two mechanisms and cannot
separate them. So each cell is one of three:

| Condition | Procedure text | Toolset | Catalogue |
|---|---|---|---|
| `control` | none | full registry, 20 tools | off |
| `narrow` | none | the skill's declared toolset | off |
| `skill` | the skill's `SKILL.md` body | the same toolset as narrow | off |

Two contrasts fall out:

```
narrow − control    the effect of restricting the action space
skill  − narrow     the effect of the written procedure
```

The skill catalogue is off in all three. A list of named procedures is itself a
hint about how the work decomposes, so leaving it in the control would smuggle
part of the treatment into the baseline.

The control gets the **full** registry, not a read-only subset. An arm denied
the tools has been denied the job, not the procedure. The consequence is that
control is the only arm that can mutate the tree on a read-only diagnosis task,
which means any cheat requiring a write tool is reachable in exactly one
condition. That is why writes into the build directory, the run journal and
`.git` are refused identically everywhere.

## Purity is measured, not asserted

A scripted client captures the exact message list and tool schema array that
would reach the server, and the three conditions are diffed
(`tests/integration/test_condition_purity.py`):

- control and narrow receive **byte-identical** messages, same count, same roles
- narrow and skill receive **byte-identical tool schemas in identical order**
- skill adds exactly one system message and nothing else moves
- no condition's bytes contain the catalogue or any line of the skill body

Tool schema order is compared as well as content, because order changes the
prompt bytes and therefore the prefix cache and time to first token.

Every row records its own `offered_tools` and `tool_schema_hash`, so a dataset
proves the treatment rather than a document promising it. In the first dataset
narrow and skill matched on 10 of 10 cases and control differed on 10 of 10.

One confound was found this way and closed. The routing tier is derived from
the skill; narrow and skill hold a skill object and control does not, so with a
tiered client configured the control arm would have been served by a different
model. It was never reachable from the launcher, and it is now refused
outright. Every row records `tiered` and `escalated`.

## Grading

Three independent gates, all of which must pass:

**1. `required_checks`** — hard evidence, never averaged. A fix case requires
an untargeted build **and** the whole suite to have passed *after* the last
edit. A filtered run, a `--rerun-failed`, a zero-match filter or stale sources
do not count.

**2. `expected_claim`** — exact set membership. A diagnosis case that returns
`success` fails even when the prose is correct.

**3. `checks`** — weighted quality rubric, threshold 0.75, never used to rescue
a failed hard gate.

Averaging mandatory steps with quality points is how a navigation run that
called no tools at all scored 0.80 and was recorded as a capability success.
Evidence is a gate; prose quality is a score; they are not mixed.

`verification_required`, `required_checks` and `expected_claim` have **no
defaults**. A permissive default silently applied a build requirement to a
read-only case whose own rubric contains a check called "did not build". A
future case must not be able to inherit the wrong contract by saying nothing.

No `SKILL.md` mentions `submit_answer` or names a claim. The claim vocabulary
lives only in the shared prompt, identically for every condition, so the
procedure text cannot coach the answer format.

## Outcome accounting

Kept separate, because collapsing them hides the interesting cases:

```
capability success        did the model do the work
end-to-end success        did the task complete
BLOCKED (environment)     excluded from the capability denominator,
                          included in deployment reporting
invalid run               excluded from everything
scope violation           did work the task did not ask for
forbidden attempt         reached for a tool the condition withholds
tool_not_allowed          a real tool, not offered here
unknown_tool              a tool that does not exist; the model invented it
verification disagreement runtime and evaluator disagree about proof
```

`unknown_tool` and `tool_not_allowed` are typed apart because the registry is
consulted **before** narrowing. Asking the toolset first would report every
hallucinated name as a policy denial and the two failures could never be
separated, which matters most in exactly the condition that studies narrowing.

Run validity is orthogonal to task outcome. Only valid runs count.

## Provenance

Every row carries:

| Field | Fingerprints |
|---|---|
| `source_sha256` | the instrument: `local_agent`, `skills`, `evaluation`, `benchmark_fixture/cpp_project`, `measurement/*.py`, `measurement/*.sh`, `pyproject.toml` |
| `base_prompt_sha256` | the model-facing contract, hashed separately |
| `package_commit` | the commit, from `PACKAGE.json` |
| `tool_schema_hash`, `offered_tools` | what this condition was given |
| model, quant, runtime, version, sampler, context budget | the serving stack |

The two hashes are separate on purpose. **A change to the base prompt makes
prior data a different experiment** and it must not be pooled, whatever else
matches. A change to the instrument alone may be recoverable by re-scoring, but
only when the rows recorded enough evidence, which `measurement/rescore_dataset.py`
decides per row and refuses to guess.

### What `base_prompt_sha256` actually covers

It hashed `SYSTEM_PROMPT` and nothing else, and that was too narrow. Two changes
landed that altered what the model sees and how its answer is judged while the
number sat still: every tool result payload gained an `evidence_id` field, and
the answer contract went from resolving several citation schemes to accepting
canonical `name:index` ids only. A dataset collected from that tree would have
advertised comparability with the 2026-09-08 run and not had it.

It now covers `SYSTEM_PROMPT`, the functions that build the system and skill
messages, the shape of every tool result the model reads back, and the answer
contract that decides whether a submission is accepted.

Tool names, descriptions and JSON schemas are model-facing and are deliberately
**outside** this number. They are the independent variable: they differ by
condition on purpose, so one per-run value cannot describe them without lying
about one of the arms. Every row carries `tool_schema_hash` over the exact
toolset that row was offered.

## Generations

A generation is a span over which datasets may be pooled. It ends when the
model-facing contract changes. Data does not cross a generation boundary,
forwards or backwards.

| Generation | `source_sha256` | `base_prompt_sha256` | Datasets |
|---|---|---|---|
| 1 | `08d5e0fe...` | `37f98409...` | `2026-09-08-30b-three-conditions`, `2026-09-08-30b-three-conditions-x3` |
| 2 | see the tag | see the tag | none yet |

**Generation 2 opens with four changes, and three of them are model-facing.**

1. **The answer contract narrowed to canonical evidence ids**, and every tool
   result now carries the `evidence_id` the model is expected to cite. That is a
   coherent design, because a canonical id is now one the model has actually
   been given, and it is a contract change rather than a bug fix. It is declared
   here rather than absorbed. The two audit tests that encoded the previous
   contract were re-based deliberately, not deleted as stale.
2. **The control condition no longer receives `read_skill_reference`.** With no
   active skill the tool can only fail, so control was carrying a tool the
   treatments lacked that cost a call to discover. It reached 9 of the 10 cases
   and moved `narrow - control`, the primary contrast, by an amount nobody can
   bound.
3. **`navigation` expects `diagnosis` rather than `success`.** Across all three
   conditions and all three repeats of generation 1, the same claim mismatch
   appeared 9/9 times, which points hard at a contract defect rather than a
   condition-specific model failure. The case now
   passes its required gate everywhere and discriminates only through its
   weighted checks, which is a ceiling where it used to be a floor; whether it
   earns its place in the pilot is still open.
4. **An observed build or test failure retracts a standing proof.** Not
   model-facing, so it does not by itself end a generation, but it is in this
   cut. Proof was retractable only by mutation, so a build that passed followed
   by a suite that failed left `verified` standing over a red tree.

Generation 1 data stays exactly where it is and is not re-scored under these
rules. The 30B has to be re-run on generation 2 before any 8B result can be
compared with it.

## Pre-committed falsification

Written before the data existed. These adjudicate the **main matrix**, not the
pilot: an interaction is a difference of differences over four cells, and a
15 to 20 task pilot cannot resolve a 10 percentage point effect. The pilot
calibrates difficulty and estimates power; it does not decide the thesis.

- **Ceiling.** A critical cell at or near ceiling means the fixture cannot
  resolve the contrast. The response is a harder fixture, not a claim.
- **Floor.** A critical cell at floor means the tasks test basic tool use
  rather than procedure.
- **Interaction.** The thesis requires the smaller model to gain **more** from
  procedure than the larger one. Equal gains do not support it and will be
  reported as falsifying it.
- **Decomposition.** If nearly all of `skill − control` is already present in
  `narrow − control`, the finding is tool narrowing, not procedural knowledge,
  and must be written that way.
- **Efficiency versus capability.** If capability ties and calls or wall clock
  differ, the conclusion is efficiency only.

The threshold is not moved after seeing data.

## Where this stands

The 30B three-condition smoke has been run. Narrowing accounted for the entire
measured capability effect; procedure added nothing this fixture could measure,
because narrow already sits at 8/10 and the two remaining failures are terminal
claim errors rather than engineering. See
[`experiments/2026-09-08-30b-three-conditions/findings.md`](../experiments/2026-09-08-30b-three-conditions/findings.md).

Three repeats of the same three conditions followed, and changed the reading of
the procedure contrast. The observed `narrow -> skill` difference was not
literally zero in the repeated batch, but it remains small and unresolved, and
three repeats of the same ten tasks are not independent evidence of a population
effect. What the repeats did buy is structure: the near-zero aggregate is
`link-error` at +3 rows against `review-restraint` at -2, and with one repeat
those two were indistinguishable from noise. See
[`experiments/2026-09-08-30b-three-conditions-x3/findings.md`](../experiments/2026-09-08-30b-three-conditions-x3/findings.md).

`link-error` is worth stating precisely, because it is the clearest thing in the
data and it is a smaller claim than it looks. Narrow and skill were offered
byte-identical tool payloads. Narrow answered `failure` three times out of
three; skill answered `diagnosis` three times out of three, in half the calls.
The procedure was not teaching the model to link C++. It was telling the model
what kind of answer the task wanted.

The next dataset is a 15 to 20 task pilot across both models and all three
conditions, with three binding requirements taken from that result: tasks where
procedure can add something narrowing cannot, claim accuracy scored separately
from capability, and a difficulty spread that the narrow condition also fails.

Before any of that, generation 2 has to be baselined: the 30B re-run on the new
instrument, because generation 1 data cannot be compared across the boundary.
