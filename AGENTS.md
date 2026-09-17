# AGENTS.md

Local-Code-Agent is a **measurement instrument first and an agent second**. It asks
one question: how much real, repetitive C++ engineering work can be completed
locally, with verified results, without a premium cloud model.

This file is a router. Read the sections that match your task. Do not read the
`docs/` tree unless a section below sends you there.

## The rule that decides most arguments

Recorded evidence does not count merely because it exists. Every fact has to be
enforced at the next trust boundary before it can affect a result. If you find
yourself writing code that records something without checking it, stop.

## Where to look, by task

| Working on | Read |
|---|---|
| What counts as a correct outcome, endpoints E1-E4, the repeat rule | `evaluation/endpoints.py`, then `docs/next-experiment-preregistration.md` |
| Why a run is valid, invalid, or tampered | `evaluation/run_evaluation.py::run_case`, `evaluation/oracle.py` |
| Task definitions, checks, forbidden tools | `evaluation/task_contracts.py` |
| Tools the agent can call | `local_agent/tools/` |
| Build and test proof, staleness, the build stamp | `local_agent/tools/testing.py`, `local_agent/tools/build.py`, `docs/verification.md` |
| Model servers, devices, context budgets | `local_agent/config.py` (`MODEL_PRESETS`), `docs/bring-up.md` |
| NPU / GPU / CPU bring-up on the laptop | `scripts/work-laptop-one-shot.ps1`, `docs/work-laptop-bootstrap.md` |
| Hashes and generations | `local_agent/provenance.py`, `INSTRUMENT.json` |
| Running an experiment | `docs/running-experiments.md` |
| Prior review findings, so you do not rediscover them | `docs/review-history.md` |

`docs/quality-hardening-roadmap.md` is a backlog, not a spec. Read it only if you
are choosing what to work on next.

## Three identities, and what moving each one means

| Hash | Covers | Moving it means |
|---|---|---|
| `source_sha256` | behavioural source, raw file bytes | update `INSTRUMENT.json` in the same commit |
| `base_prompt_sha256` | what the model is told | **ends a generation** |
| `outcome_contract_sha256` | what the evaluator calls correct | **ends a generation** |

Generation 2 currently has **zero collected model rows**, which is the only reason
the outcome contract is still editable. The moment row one lands, both contract
axes freeze.

Recompute and compare:

```
python -c "import sys; sys.path.insert(0,'.'); from local_agent import provenance as p; print(p.source_sha256()); print(p.base_prompt_sha256()); print(p.outcome_contract_sha256())"
```

## Do not change without saying so explicitly in the PR

- `INSTRUMENT.json`, and anything that moves a hash
- `experiments/` — frozen generation-1 evidence, never edited, never re-scored
- the native Windows CI job — the last three defects were Windows-only
- experiment semantics, condition purity, oracle isolation

Fixing a test by weakening it is not fixing it. If a test is wrong, say why it is
wrong before you change it.

## Commands

```
python -m pytest -q                              # authoritative suite
python benchmark_fixture/generate_project.py     # regenerate the fixture
git diff --exit-code -- benchmark_fixture/cpp_project

python measurement/qualify_server.py --profile ptl-npu-8b     # protocol conformance
python measurement/analyze_endpoints.py <rows.json>           # endpoint analysis
python measurement/capture_run_manifest.py --profile <p> --out <f>   # pre-run sidecar
```

`measurement/run_test_suite.py` is a compatibility runner for offline machines. It
is **not authoritative** and it currently stubs some pytest behaviour silently.
Green there is not green.

## Working agreement

One writer per branch, one coherent commit series, exact SHAs in the PR body.

**Push and stop. Do not poll CI.** A human or a scheduled check reports the result.
Polling a workflow for twenty turns costs more than the change did.

State what you actually ran and on what, and state what you did not. "The tests
pass" is not evidence unless you say which tests, on which host, at which commit.

## Done means

A task is done when the change is merged and the thing it was meant to make
possible has actually happened, not when the patch is written. If the request
includes getting something running, running it is part of the task.
