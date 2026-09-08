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

# The result so far

Ten synthetic C++ tasks. One 30B model on CPU. Three conditions over the same
tasks with byte-identical prompts. The only difference between the first two
columns is which tools were on the table.

|                       | control | narrow | skill |
|-----------------------|--------:|-------:|------:|
| **verified completion** | **3/10** | **8/10** | **8/10** |
| answer quality          | 0.822   | 1.000  | 1.000 |
| tool calls              | 138     | 76     | 72    |
| wall clock              | 41.6 m  | 24.9 m | 21.1 m |
| scope violations        | 4       | 0      | 0     |

- **control** — no procedure, the full 20-tool registry
- **narrow** — no procedure, only the tools the task's skill declares
- **skill** — the same narrowed tools, plus the skill's written procedure

Two contrasts fall out, and they are the point of running three cells instead
of two:

```
narrow − control  =  the effect of restricting the action space   +5 tasks
skill  − narrow   =  the effect of the written procedure           0 tasks
```

## What that means

**Restricting the tool set did all the measurable work.** Taking the agent from
twenty tools down to the six or eight its task actually needs raised verified
completion from 3/10 to 8/10, removed every scope violation, and cut tool calls
by 45% and wall clock by 40%.

**The mechanism is not subtle.** In the control condition the agent held
`apply_patch` on read-only diagnosis tasks and used it. Asked to *explain* a
compile error, it fixed the compile error and then claimed success. Remove the
tool and the wrong behaviour is not available to be chosen.

**Adding the written procedure on top changed capability by nothing.** It was
slightly cheaper, 72 calls against 76, which is recorded as an efficiency
observation and not as capability.

**Run the usual way, this result would have been wrong.** A two-cell
skill-versus-no-skill comparison would have shown the same +5 and attributed
all of it to the written procedure. It is the toolset. Separating the two
mechanisms is why there are three conditions, and it paid for itself on the
first run.

**What is capping the number.** Both treated cells sit at 8/10 and neither
remaining failure is an engineering failure. Quality is 1.000 in both. The
agent did the work correctly and then chose the wrong word for what it had
done: the claim vocabulary has no term for "you asked me a question and here is
the answer", so on `navigation` it answered `diagnosis` in **all three
conditions** and the contract wanted `success`. That is a defect in the
measurement, not in the model, and it is fixed in the next generation.

## What this does not show

Ten synthetic tasks, one run per cell, one model. No 8B, so **nothing at all**
about the small-model interaction the project exists to test. No statistical
claim: one run per cell is not a variance estimate.

An unplanned second run of the `skill` cell returned the same 8/10 with the
same eight tasks passing and 72 calls falling to 67, so on this fixture
capability was stable and cost was not. That is one repeat, on the large model,
on a fixture it saturates.

Full analysis: [`experiments/2026-09-08-30b-three-conditions/findings.md`](experiments/2026-09-08-30b-three-conditions/findings.md)
Raw data: [`experiments/2026-09-08-30b-three-conditions/data/`](experiments/2026-09-08-30b-three-conditions/data/)

---

# How to read this repository

Every top-level directory, what it holds, and whether you can change it.

| Directory | What is in it |
|---|---|
| **`experiments/`** | **Frozen datasets and their analysis.** One directory per run, containing the raw evaluator output and the write-up of what it showed. Never edited after collection. Start here if you want the evidence. |
| **`docs/`** | **How the thing works and why.** Experiment design, what counts as proof, the measurement protocol, the adversarial review history, and how to run it on a machine. |
| **`src/local_agent/`** | **The agent.** Orchestrator, tools, policy, skills, the LLM client, the verification classifier, the CLI. |
| **`tests/`** | **The test suite, 270 tests.** Including the adversarial suite that reproduces every scoring cheat ever found against this instrument. |
| **`tests/evals/`** | **The evaluation harness.** Task contracts, grading, and the runner that produces everything in `experiments/`. |
| **`fixtures/`** | **The synthetic C++ repository** the evaluation runs against, and its seven fault scenarios. Not real work, not anybody's intellectual property. |
| **`devtools/`** | **Tools for running an experiment**, not for building the agent: server qualification, model benchmarking, offline re-scoring of frozen data, and the run launcher. |
| **`.github/skills/`** | **The eight skills.** Each is a markdown file containing a written procedure and the list of tools it declares. In the experiment these are the *treatment*. |
| **`scripts/`** | Repository chores. Building the distributable zip. |

## A warning about renaming things

Five of those paths are hashed into `source_sha256`, which is how every dataset
identifies the exact instrument that produced it:

```
src/local_agent/**.py        .github/skills/**/SKILL.md
tests/evals/**.py            fixtures/cpp_sandbox/**
devtools/*.py  devtools/*.sh          pyproject.toml
```

**Renaming or editing any of those changes the hash**, and every existing
dataset stops being reproducible from this repository. That is sometimes the
right thing to do, and when it is, it happens deliberately as a new
experimental generation and is recorded. It is never a tidy-up.

`docs/`, `experiments/`, `scripts/`, `README.md` and the CI config are outside
that set and can be changed freely.

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

The model is reached through one OpenAI-compatible interface, so the runtime
behind it is a deployment detail. Ten profiles ship today across llama.cpp,
OpenVINO, OVMS on an NPU, a discrete GPU and a cloud endpoint. The evaluation
harness cannot tell them apart except by the profile name.

## Verification is not the model's job

Every tool result carries two independent axes, because conflating them was a
real bug:

```
ExecutionStatus   OK | BLOCKED | ERROR      did the tool run
DomainStatus      PASS | FAIL | UNKNOWN     what did it find
```

A compile error is `OK` + `FAIL`: valid evidence. A missing cmake is `BLOCKED` +
`UNKNOWN`: not a model failure, and excluded from the capability denominator.
`UNKNOWN` is explicit and never silently upgraded.

One shared classifier in [`src/local_agent/verification.py`](src/local_agent/verification.py)
turns a call into a typed `ProofKind`, and the orchestrator, the evaluator and
the offline re-scorer all consume it:

```
FULL_BUILD_PASS  FULL_TEST_PASS          prove the current tree
OBSERVED_BUILD_FAIL  OBSERVED_TEST_FAIL  real evidence, prove nothing is fixed
TARGETED_BUILD_PASS  TARGETED_TEST_PASS  passed, over part of the tree
NO_CURRENT_PROOF                         everything else
```

Only the first two can satisfy a verification contract. A filtered test run, a
`--rerun-failed`, a pass over binaries older than the source, a pass with no
recorded build, and a pass under a profile that was configured but never built
are all `NO_CURRENT_PROOF`. Proof does not survive a later edit: change the tree
after proving it and the proof dies with the epoch it belonged to.

Those rules exist because each one was found letting something through.
[`docs/verification.md`](docs/verification.md) has the detail and
[`docs/review-history.md`](docs/review-history.md) has the defect log.

---

# Quickstart

Needs Python 3.11+, cmake, ctest and a C++ compiler. **No model required** for
anything in this section.

```bash
pip install -e ".[dev]"

# Build the synthetic C++ repository the evaluation runs against.
python fixtures/generate_sandbox.py

# The whole instrument, with no model anywhere.
python devtools/minipytest.py tests          # 270 tests

# Look around a repository.
local-agent --repo fixtures/cpp_sandbox doctor
local-agent --repo fixtures/cpp_sandbox skills
```

With an OpenAI-compatible server running:

```bash
# Prove the server can do what the harness assumes, before trusting a number.
python devtools/qualify.py --profile nuc-llama-30b --json qualify.json

# One condition of the experiment.
python tests/evals/run_evals.py --profile nuc-llama-30b \
    --condition narrow --out results.json
```

[`docs/running-experiments.md`](docs/running-experiments.md) is the operational
version, with the launcher that gates every step and refuses to run on a stale
package.

---

# Status

| Established | Not established |
|---|---|
| The architecture runs end to end on a local model | That skills work |
| Tool narrowing is worth +5/10 on this fixture, 30B | That written procedure works |
| The instrument survives six rounds of adversarial review | Anything about a smaller model |
| Runtime and evaluator agree on proof, measured, zero disagreements | Any statistical claim |
| Conditions are byte-identical apart from the intended difference | That any of it generalises past a synthetic fixture |

Next: a 15 to 20 task pilot across two models and all three conditions, designed
so that the written procedure has something to contribute that removing tools
cannot. The design and its open questions are in
[`docs/pilot-design.md`](docs/pilot-design.md).

## Documentation

| | |
|---|---|
| [`docs/experiment-design.md`](docs/experiment-design.md) | conditions, contrasts, grading, and what would falsify the thesis |
| [`docs/verification.md`](docs/verification.md) | what counts as proof, and why each rule exists |
| [`docs/measurement-protocol.md`](docs/measurement-protocol.md) | how the performance numbers are taken, and why the usual ones are wrong |
| [`docs/review-history.md`](docs/review-history.md) | six rounds of adversarial review, every defect found, and what closed it |
| [`docs/pilot-design.md`](docs/pilot-design.md) | the next fixture, in draft |
| [`docs/running-experiments.md`](docs/running-experiments.md) | running a cell on a real machine |
| [`docs/bring-up.md`](docs/bring-up.md) | getting each backend serving |
| [`docs/external-review-brief.md`](docs/external-review-brief.md) | the brief for a one-shot hostile methods review |

## Licence

Not yet licensed. No rights are granted to anybody in the meantime.
