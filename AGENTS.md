# AGENTS.md

Local-Code-Agent is currently **product-first: build the conversation gateway and
the self-engineering loop**. New measurement campaigns are paused by the project
owner's 2026-09-18 direction. Historical evidence and measurement contracts remain
protected. Operational regression tests and honest verification are still required.

Read root `PROJECT_OVERVIEW.md` and `CURRENT_STATE.md` before substantial work.
For current product architecture, contract and implementation sequence, read
`internal/docs/session-hub-design.md`, `internal/docs/session-contract/README.md`
and `internal/docs/session-hub-file-layout.md`. These supersede conflicting earlier
model-first/web-client choices and the M0-M5 build order. Interface-only skeletons are not
working features. Keep that distinction explicit in reviews and user-facing claims.

Current client target: in-process Textual, mandatory activity and watch panes.
Do not change `internal/local_agent/agent/context.py` or move base_prompt_sha256
for this product stream. Evaluation must not import gateway/persona/chat context/UI.
If implementation requires violating those boundaries, stop for a generation decision.

The root is the product surface. Implementation, measurement, tests, research
documentation and frozen evidence live under `internal/`. `internal/README.md` maps that
tree and says which parts are the instrument; `internal/docs/README.md` says which
documents are normative and which are aspirational.

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

Generation 2 currently has **zero collected model rows**, and both contract axes are
already deliberately pinned. Zero rows does not make them casually editable; it makes an
explicit generation change cheap rather than expensive, which is what allowed the revised
agent identity and anti-guessing prompt to be frozen without a post-data methodology
change. Moving either axis is still a decision taken on purpose and stated in the PR,
never a side effect of a rename or a tidy-up.

Once the first Generation-2 row exists, moving either contract axis ends Generation 2
for confirmatory comparison and requires an explicit new-generation decision.

Canonical recomputation from the repository root, with no editable-install
assumption:

```text
python -c "import sys; sys.path.insert(0,'internal'); from local_agent import provenance as p; print(p.source_sha256()); print(p.base_prompt_sha256()); print(p.outcome_contract_sha256())"
```

The same command is recorded in `internal/INSTRUMENT.json`; keep the two in sync.

## Naming and structure

These are permanent rules, not the preferences of one refactor.

- **A name says what the thing is, not where it sits.** `session/storage.py` tells a
  reader nothing that the directory did not already say. `session_store.py` does. The
  test is whether the name still makes sense pasted into an import line halfway down
  another file.
- **One word, one meaning, repository-wide.** `endpoint` means an inference server
  everywhere except `evaluation/endpoints.py`, where it means a success metric. Do not
  add a second such collision. If a word is already taken, pick another.
- **A module named for a generic role gets a qualifier.** `base.py`, `context.py`,
  `client.py`, `runner.py`, `service.py` and `testing.py` are all ambiguous the moment
  two of them are open at once.
- **A filename beginning with `test_` is a test module.** Pytest collects it. A helper
  for tests is `testing_tools.py`, not `test_execution.py`.
- **A directory means something.** Every directory under `internal/` is either part of
  the instrument or not, and `test_hashed_surface_membership.py` fails until a new one
  declares which. Adding a folder is a decision, so make it out loud.
- **An empty or documentation-only directory must not look like source.** A directory
  containing only a README belongs under `internal/docs/`.

## Before you rename or move anything

Run the pre-flight first. Windows PowerShell or WSL, from the repository root:

```text
python internal/devtools/check_rename_safety.py
python internal/devtools/check_rename_safety.py --markdown > preflight.md
```

Then audit the old path everywhere, not just in Python imports: bare-name imports made
possible by `sys.path.insert`, every path manipulation, PowerShell, shell scripts,
GitHub workflows, documentation, `pyproject.toml`, `.local-agent.toml`, `conftest.py`,
`INSTRUMENT.json`, provenance globs, packaging scripts, demo wrappers and tests. A move
is incomplete while an authoritative old reference remains. The highest-consequence one
is `.local-agent.toml`, which is how the agent tests its own checkout.

**Prove the transformation on the real tree.** This rule exists because reasoning about
labels was wrong twice in a row on this repository. Renames were assessed as unsafe by
reading which names appear inside hashed inputs; executing the renames and recomputing
the hashes gave a different answer both times, in both directions. Before claiming a
rename does or does not move an identity: perform it on a scratch copy of the current
tree, recompute all three hashes, and import the package. Static "looks safe" reasoning
is not evidence, and neither is a green hash on a tree that no longer imports.

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
fails. With no path argument it runs `internal/tests`; a missing path or an empty
collection is an error, never a green zero-test run.

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
