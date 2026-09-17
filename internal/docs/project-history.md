# Local Code Agent

A local engineering agent for C++ repositories, built as a **measurement
instrument first and an agent second**.

The question it exists to answer:

> How much real, repetitive C++ engineering work can be completed **locally,
> with verified results**, without a premium cloud model, and how much of that
> can a low-power tier carry on its own?

The orchestrator owns the workflow. The policy engine owns what may execute.
The build system and the test runner own verification. The model supplies
judgement where judgement is useful, and nothing else. It cannot talk its way
into a passing result: proof is decided from typed tool results by code the
model does not control.

---

# The result

Ten synthetic C++ engineering tasks. One 30B model running locally on CPU.
Three conditions over the same tasks with byte-identical prompts. The only
difference between the first two columns is which tools were on the table.

|                         | control  | narrow   | skill    |
|-------------------------|---------:|---------:|---------:|
| **verified completion** | **3/10** | **8/10** | **8/10** |
| answer quality          | 0.822    | 1.000    | 1.000    |
| tool calls              | 138      | 76       | 72       |
| wall clock              | 41.6 min | 24.9 min | 21.1 min |
| scope violations        | 4        | 0        | 0        |

- **control** — no written procedure, the full 20-tool registry
- **narrow** — no written procedure, only the tools the task's skill declares
- **skill** — the same narrowed tools, plus the skill's written procedure

Running three conditions rather than two is what makes the result readable:

```
narrow − control  =  the effect of restricting the action space   +5 tasks
skill  − narrow   =  the effect of the written procedure           0 tasks
```

## Analysis

**Restricting the tool set did all of the measurable work.** Taking the agent
from twenty tools down to the six or eight its task actually needs raised
verified completion from 3/10 to 8/10, eliminated every scope violation, and
cut tool calls by 45% and wall clock by 40%.

**The mechanism is not subtle.** In the control condition the agent held
`apply_patch` on read-only diagnosis tasks and used it. Asked to *explain* a
compile error, it fixed the compile error and then claimed success. Remove the
tool and the wrong behaviour is not available to be chosen.

**Adding the written procedure on top changed capability by nothing.** It was
marginally cheaper, 72 calls against 76, which is recorded as an efficiency
observation and never as capability evidence.

**Run the conventional way, this result would have been wrong.** A two-cell
skill-versus-no-skill comparison would have produced the same +5 and attributed
all of it to the written procedure. It is the toolset. Separating the two
mechanisms is the entire reason for a third condition, and it paid for itself
on the first dataset.

**What is capping the number.** Both treated cells sit at 8/10, and neither
remaining failure is an engineering failure. Answer quality is 1.000 in both.
The agent did the work correctly and then chose the wrong word for what it had
done: the claim vocabulary has no term for *"you asked a question, here is the
answer"*, so on the navigation task it answered `diagnosis` in **all three
conditions** while the contract expected `success`. That is a defect in the
measurement, not in the model, and it is corrected in the next generation.

## What this result does not show

Ten synthetic tasks, one run per cell, one model. No 8B, and therefore
**nothing whatsoever** about the small-model interaction the project exists to
test. No statistical claim: one run per cell is not a variance estimate.

An unplanned second run of the `skill` cell returned the same 8/10 with the
same eight tasks passing, and 72 calls falling to 67. On this fixture,
capability was stable and cost was not. That is one repeat, on the large model,
on a fixture it saturates.

Full analysis: [`experiments/2026-09-08-30b-three-conditions/findings.md`](experiments/2026-09-08-30b-three-conditions/findings.md)
Raw data: [`experiments/2026-09-08-30b-three-conditions/data/`](experiments/2026-09-08-30b-three-conditions/data/)

---

# Repository layout

Every directory, and what it is for.

| Directory | Contents |
|---|---|
| **`experiments/`** | **Frozen datasets and their analysis.** One directory per run: the raw evaluator output, and the write-up of what it showed. Never edited after collection. Start here for the evidence. |
| **`local_agent/`** | **The agent.** Orchestrator, twenty typed tools, policy engine, skill loading, the LLM client, the proof classifier, the CLI and the JSON-RPC seam. |
| **`skills/`** | **The eight skills.** Each is one markdown file: a written procedure, plus the list of tools it declares. In the experiment these are the **treatment**. |
| **`evaluation/`** | **The evaluation harness.** `task_contracts.py` defines what each task must demonstrate to count as done; `run_evaluation.py` runs one condition and writes a dataset. |
| **`benchmark_fixture/`** | **The C++ project the tasks are set against**, and the generator that builds it, with seven fault scenarios. Not real work, and not anybody's intellectual property. |
| **`measurement/`** | **Everything that qualifies, runs or reports on an experiment.** Server qualification, the instrument's own test gate, the experiment launcher, model benchmarking, host telemetry, offline re-scoring and dataset comparison. |
| **`tests/`** | **The test suite, 270 tests.** Including the adversarial suite, which reproduces every scoring cheat ever found against this instrument, written as the cheat so it fails again if a fix is undone. |
| **`docs/`** | **How it works and why.** Experiment design, what counts as proof, the measurement protocol, the review history, and how to run it on a real machine. |
| **`scripts/`** | Repository chores: building the release archive, stamping package identity, running the suite as an unprivileged user. |

## Before renaming anything

Six of those paths are hashed into `source_sha256`, which is how every dataset
identifies the exact instrument that produced it:

```
local_agent/**/*.py            skills/**/SKILL.md
evaluation/**/*.py             benchmark_fixture/cpp_project/**
measurement/*.py               measurement/*.sh
pyproject.toml
```

**Renaming or editing any of those changes the hash**, and existing datasets
stop being reproducible from the current tree. That is sometimes the right
thing to do, and when it is, it is done deliberately, recorded, and the
previous state is tagged so the old hash can still be recomputed. It is never a
tidy-up.

`docs/`, `experiments/`, `scripts/`, `tests/`, this README and the CI
configuration are outside that set and can be changed freely.

The tag `instrument-08d5e0fe` marks the tree that produced the first dataset.

---

# How it works

```
                          you
                           │
                     CLI / stdio RPC
                           │
                 ┌─────────▼────────┐
                 │   orchestrator   │  state machine, context budget,
                 │                  │  policy, verification, halt causes
                 └────────┬─────────┘
            ┌─────────────┼─────────────┐
          skills        tools         state
            │             │             │
            │      files search git     │
            │      build test logs      │
            │        patch (2 step)     │
            └─────────────┼─────────────┘
                          ▼
                   C++ repository
                          │  selected context only
                          ▼
                         LLM
```

Four layers, and the seams are deliberate:

| Layer | Owns | Does not know about |
|---|---|---|
| Tools | executing things and typing the result | the model, the task |
| Policy | what may run, what needs approval | why it was asked for |
| Orchestrator | the loop, the budget, verification | which model is behind the client |
| Client | one HTTP contract | anything above it |

The model is reached through a single OpenAI-compatible interface, so the
runtime behind it is a deployment detail. Ten profiles ship today across
llama.cpp, OpenVINO, a model server on an NPU, a discrete GPU and a cloud
endpoint. The evaluation harness cannot tell them apart except by the profile
name.

## Verification is not the model's job

Every tool result carries two independent axes, because conflating them was a
real defect:

```
ExecutionStatus   OK | BLOCKED | ERROR      did the tool run
DomainStatus      PASS | FAIL | UNKNOWN     what did it find
```

A compile error is `OK` + `FAIL`: valid evidence. A missing cmake is `BLOCKED` +
`UNKNOWN`: not a model failure, and excluded from the capability denominator.
`UNKNOWN` is explicit and never silently upgraded.

One shared classifier in [`local_agent/verification.py`](local_agent/verification.py)
turns a call into a typed `ProofKind`, consumed by the orchestrator, the
evaluator and the offline re-scorer alike:

```
FULL_BUILD_PASS  FULL_TEST_PASS          prove the current tree
OBSERVED_BUILD_FAIL  OBSERVED_TEST_FAIL  real evidence; prove nothing is fixed
TARGETED_BUILD_PASS  TARGETED_TEST_PASS  passed, over part of the tree
NO_CURRENT_PROOF                         everything else
```

Only the first two can satisfy a verification contract. A filtered test run, a
`--rerun-failed`, a pass over binaries older than the source, a pass with no
recorded build, and a pass under a profile that was configured but never built
are all `NO_CURRENT_PROOF`. Proof does not survive a later edit: change the tree
after proving it and the proof dies with the epoch it belonged to.

Each of those rules exists because it was found letting something through.
[`docs/verification.md`](docs/verification.md) gives the detail;
[`docs/review-history.md`](docs/review-history.md) gives the defect log.

---

# Quickstart

Requires Python 3.11+, cmake, ctest and a C++ compiler. **No model is required**
for anything in this section.

```bash
pip install -e ".[dev]"

# Build the C++ project the tasks are set against.
python benchmark_fixture/generate_project.py

# The whole instrument, with no model anywhere.
python measurement/run_test_suite.py tests          # 270 tests

# Look around a repository.
local-agent --repo benchmark_fixture/cpp_project doctor
local-agent --repo benchmark_fixture/cpp_project skills
```

With an OpenAI-compatible server running:

```bash
# Prove the server can do what the harness assumes, before trusting a number.
python measurement/qualify_server.py --profile nuc-llama-30b --json qualify.json

# One condition of the experiment.
python evaluation/run_evaluation.py --profile nuc-llama-30b \
    --condition narrow --out results.json
```

[`docs/running-experiments.md`](docs/running-experiments.md) is the operational
version, using the launcher that gates every step and refuses to run against a
stale package.

---

# Status

| Established | Not established |
|---|---|
| The architecture runs end to end against a local model | That skills work |
| Tool narrowing is worth +5/10 on this fixture, on a 30B | That written procedure works |
| The instrument survives six rounds of adversarial review | Anything about a smaller model |
| Runtime and evaluator agree on proof: measured, zero disagreements | Any statistical claim |
| Conditions are byte-identical apart from the intended difference | That any of it generalises past a synthetic fixture |

Next: a fifteen to twenty task pilot across two models and all three
conditions, designed so the written procedure has something to contribute that
removing tools cannot. The design, and its open questions, are in
[`docs/pilot-design.md`](docs/pilot-design.md).

---

# How this was built

The engineering question, the experimental design decisions, the hardware, the
runs and every judgement call are Aiden Barrett's. The implementation was
written with **Claude** (Anthropic), and the instrument was reviewed
adversarially by **ChatGPT** (OpenAI) across six rounds.

That arrangement turned out to matter more than it sounds, so it is worth
recording honestly rather than as a footnote.

The reviewer did not accept the implementer's reports. It read the source,
reproduced the claims, and repeatedly found that a defect described as fixed
was fixed in one layer and live in another. The `--rerun-failed` loophole was
recorded as closed for four consecutive packages while remaining open in the
runtime, and it was caught by a reviewer that refused to take "fixed" as
evidence of fixed. The implementer, for its part, twice had to retract things
it had stated confidently: once a claim that a dataset had been re-scored when
it had not, and once a proposed definition of "verification disagreement" that
would have reported four failures where there were none. Both retractions are
in [`docs/review-history.md`](docs/review-history.md), because a defect log
that only contains other people's mistakes is not a defect log.

The human contribution that mattered most was not code. It was deciding when
the arguing had to stop. Two capable reviewers will polish a ruler
indefinitely, and each round genuinely did find something, which is exactly
what makes the habit hard to break. The rule that ended it was written down in
advance: *can this defect move the three conditions by different amounts? If
yes, it blocks the run. If no, it is recorded as a limitation and fixed in the
next fixture.* Everything else was noted and left alone. The data was collected
the next day.

The result on this page is a direct consequence of that discipline. A
two-condition experiment would have been quicker to build, would have produced
a larger and more flattering headline number, and would have been wrong.

---

# Documentation

| | |
|---|---|
| [`docs/experiment-design.md`](docs/experiment-design.md) | conditions, contrasts, grading, and what would falsify the thesis |
| [`docs/verification.md`](docs/verification.md) | what counts as proof, and why each rule exists |
| [`docs/measurement-protocol.md`](docs/measurement-protocol.md) | how the performance numbers are taken, and why the usual ones are wrong |
| [`docs/review-history.md`](docs/review-history.md) | six rounds of adversarial review, every defect, and what closed it |
| [`docs/pilot-design.md`](docs/pilot-design.md) | the next fixture, in draft, with open questions |
| [`docs/running-experiments.md`](docs/running-experiments.md) | running one condition on a real machine |
| [`docs/bring-up.md`](docs/bring-up.md) | getting each backend serving |
| [`docs/external-review-brief.md`](docs/external-review-brief.md) | the brief for a one-shot hostile methods review |

# Licence

Not yet licensed. No rights are granted in the meantime.
