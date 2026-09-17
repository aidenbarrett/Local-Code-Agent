# AGENTS.md

Local-Code-Agent is a **measurement instrument first and an agent second**. It asks
one question: how much real, repetitive C++ engineering work can be completed
locally, with verified results, without a premium cloud model.

The root is the product surface. Implementation, measurement, tests, research
documentation and frozen evidence live under `internal/`.

This file is a router. Read only the sections that match your task.

## The rule that decides most arguments

Recorded evidence does not count merely because it exists. Every fact has to be
enforced at the next trust boundary before it can affect a result. If you find
yourself writing code that records something without checking it, stop.

## Where to look, by task

| Working on | Read |
|---|---|
| What counts as a correct outcome, endpoints E1-E4, the repeat rule | `internal/evaluation/endpoints.py`, then `internal/docs/next-experiment-preregistration.md` |
| Why a run is valid, invalid, or tampered | `internal/evaluation/run_evaluation.py::run_case`, `internal/evaluation/oracle.py` |
| Task definitions, checks, forbidden tools | `internal/evaluation/task_contracts.py` |
| Tools the agent can call | `internal/local_agent/tools/` |
| Build and test proof, staleness, the build stamp | `internal/local_agent/tools/testing.py`, `internal/local_agent/tools/build.py`, `internal/docs/verification.md` |
| Model servers, devices, context budgets | `internal/local_agent/config.py` (`MODEL_PRESETS`), `internal/docs/bring-up.md` |
| NPU / GPU / CPU bring-up on the laptop | `install.ps1`, `internal/work-laptop-one-shot.ps1`, `internal/docs/work-laptop-bootstrap.md` |
| Hashes and generations | `internal/local_agent/provenance.py`, `internal/INSTRUMENT.json` |
| Running an experiment | `internal/docs/running-experiments.md` |
| Prior review findings | `internal/docs/review-history.md` |

`internal/docs/quality-hardening-roadmap.md` is a backlog, not a spec.

## Three identities, and what moving each one means

| Hash | Covers | Moving it means |
|---|---|---|
| `source_sha256` | behavioural source, canonical repository paths and raw file bytes | update `internal/INSTRUMENT.json` in the same coherent change |
| `base_prompt_sha256` | what the model is told | **ends a generation once rows exist** |
| `outcome_contract_sha256` | what the evaluator calls correct | **ends a generation once rows exist** |

Generation 2 currently has **zero collected model rows**. That is the only reason
this branch may freeze the revised agent identity/anti-guessing prompt without
creating a post-data methodology change. The moment row one lands, both contract
axes freeze for confirmatory comparison.

Recompute and compare after the editable install:

```text
python -c "from local_agent import provenance as p; print(p.source_sha256()); print(p.base_prompt_sha256()); print(p.outcome_contract_sha256())"
```

## Do not change without saying so explicitly in the PR

- `internal/INSTRUMENT.json`, and anything that moves a hash
- `internal/experiments/` — frozen generation-1 evidence, never edited, never re-scored
- the native Windows CI job
- experiment semantics, condition purity, oracle isolation

Fixing a test by weakening it is not fixing it. If a test is wrong, say why it is
wrong before you change it.

## Commands

```text
python -m pytest -q
python internal/benchmark_fixture/generate_project.py
git diff --exit-code -- internal/benchmark_fixture/cpp_project

python internal/measurement/qualify_server.py --profile ptl-npu-8b
python internal/measurement/analyze_endpoints.py <rows.json>
python internal/measurement/capture_run_manifest.py --profile <p> --out <f>
```

`internal/measurement/run_test_suite.py` is a compatibility runner for offline
machines. It is **not authoritative**. Green there is not green if native pytest
fails.

Every test under `internal/tests/` is exercised by both real pytest and the
offline compatibility runner in pull-request CI. New tests must stay within the
compatibility runner's documented pytest subset — currently normal/user fixtures,
fixture dependencies and generator fixtures, `tmp_path`, `monkeypatch`,
`pytest.raises`, `pytest.mark.parametrize`, `pytest.mark.skipif`, `pytest.mark.skip`
and module-level `pytestmark` — or extend the runner and its regression coverage
in the same change. Do not introduce a pytest-only fixture such as `capsys` and
leave the compatibility job to discover the mismatch later.

## Product-facing contract

Normal demo/user instructions should stay on these surfaces:

```text
.\install.ps1
.\chat.ps1
.\local-code-agent.ps1
.\demo\...
```

Do not send a first-time user into `internal/` to repair our orchestration.

## Working agreement

One writer per branch, one coherent commit series, exact SHAs in the PR body.

State what you actually ran and on what, and state what you did not. "The tests
pass" is not evidence unless you say which tests, on which host, at which commit.

## Done means

A task is done when the change is merged and the thing it was meant to make
possible has actually happened, not when the patch is written. If the request
includes getting something running, running it is part of the task.
