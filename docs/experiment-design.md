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

The next dataset is a 15 to 20 task pilot across both models and all three
conditions, with three binding requirements taken from that result: tasks where
procedure can add something narrowing cannot, claim accuracy scored separately
from capability, and a difficulty spread that the narrow condition also fails.
