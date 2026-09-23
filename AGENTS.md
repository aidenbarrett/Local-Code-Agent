# AGENTS.md

Local-Code-Agent is currently **product-first: close the P1 correctness gaps in the
Session Hub and self-engineering loop before adding more feature slices**. New
measurement campaigns are paused by the project owner's 2026-09-18 direction.
Historical evidence and measurement contracts remain protected. Operational regression
tests and honest verification are still required.

Read root `PROJECT_OVERVIEW.md` and `CURRENT_STATE.md` before substantial work.
For the first public release, read `TRICKS.md` for user journeys and acceptance gates;
for the longer product direction, read `internal/docs/product-roadmap.md`.
The destination is a dependable offline worker for finding, moving, Git and coding
work; deep-reasoning competition is not a goal. Roadmap items are not implemented
features and do not replace current source-derived product status.

For current product architecture, contract and implementation sequence, read
`internal/docs/session-hub-design.md`, `internal/docs/session-contract/README.md`,
`internal/docs/session-hub-file-layout.md` and `internal/docs/endpoint-scheduler-design.md`.
These supersede conflicting earlier model-first/web-client choices and the M0-M5 build
order. Interface-only primitives are not delivered features. Keep that distinction
explicit in reviews and user-facing claims.

Current client target: in-process Textual, mandatory activity and watch panes.
Do not change `internal/local_agent/agent/context.py` or move `base_prompt_sha256`
for this product stream. Evaluation must not import gateway/persona/chat context/UI.
If implementation requires violating those boundaries, stop for a generation decision.

The root is the product surface. Implementation, measurement, tests, research
documentation and frozen evidence live under `internal/`. `internal/README.md` maps that
tree and says which parts are the instrument; `internal/docs/README.md` says which
documents are normative, design intent, operational guidance and historical record.

This file is a router. Read only the sections that match your task.

## The rule that decides most arguments

Recorded evidence does not count merely because it exists. Every fact has to be
enforced at the next trust boundary before it can affect a result. If you find
yourself writing code that records something without checking or consuming it, stop.

One policy/state decision has one owner. Before adding a scheduler, controller, store,
app, queue, lease manager or verifier, search current source for the existing authority.
A second implementation beside an uncomposed first one is not progress.

## Where to look, by task

| Working on | Read |
|---|---|
| Current product status and open P1 gaps | `CURRENT_STATE.md`, then current GitHub source/PRs |
| First-release user journeys and public acceptance | `TRICKS.md`, then the actual launcher/composition tests |
| Session Hub routing/admission/results | `internal/local_agent/session/`, `internal/docs/session-hub-design.md` |
| Endpoint ownership | `internal/local_agent/session/endpoint_lease.py`, `endpoint_runtime.py`, `endpoint_call.py`, `internal/docs/endpoint-scheduler-design.md` |
| What counts as a correct experimental outcome, endpoints E1-E4, repeat rule | `internal/evaluation/endpoints.py`, then `internal/docs/next-experiment-preregistration.md` |
| Why an experimental run is valid, invalid, or tampered | `internal/evaluation/run_evaluation.py::run_case`, `internal/evaluation/oracle.py` |
| Task definitions, checks, forbidden tools | `internal/evaluation/task_contracts.py` |
| Tools the agent can call | `internal/local_agent/tools/` |
| Build and test proof, staleness, build stamp | `internal/local_agent/tools/testing_tools.py`, `internal/local_agent/tools/build.py`, `internal/docs/verification.md` |
| Model servers, devices, context budgets | `internal/local_agent/config.py` (`MODEL_PRESETS`), `internal/docs/bring-up.md` |
| NPU / GPU / CPU bring-up on the laptop | `install.ps1`, `internal/work-laptop-one-shot.ps1`, `internal/docs/work-laptop-bootstrap.md` |
| Hashes and generations | `internal/local_agent/provenance.py`, `internal/INSTRUMENT.json` |
| Running an experiment | `internal/docs/running-experiments.md` |
| Prior review findings | `internal/docs/review-history.md` |

`internal/docs/quality-hardening-roadmap.md` is a backlog, not a spec.

## Three identities, and what moving each one means

| Hash | Covers | Repository rule |
|---|---|---|
| `source_sha256` | behavioural source, canonical repository paths and raw file bytes | derived from the exact tree/run; **never commit a mutable live source declaration** |
| `base_prompt_sha256` | what the model is told | frozen declaration; moving it ends a generation once rows exist |
| `outcome_contract_sha256` | what the evaluator calls correct | frozen declaration; moving it ends a generation once rows exist |

`internal/INSTRUMENT.json` declares the frozen contract axes only. It must not contain a
live `source_sha256`. Source provenance is recomputed from the exact tree and recorded
with run/change evidence instead of forcing every source PR to edit one shared JSON line.

Generation 2 currently has **zero collected model rows**. Zero rows does not make the
two frozen contract axes casually editable; it makes an explicit generation change
cheap rather than expensive. Moving either axis is still a deliberate methodology
decision stated in the PR, never a side effect of a rename or tidy-up.

Once the first Generation-2 row exists, moving either frozen contract axis ends
Generation 2 for confirmatory comparison and requires an explicit new-generation
decision.

Canonical recomputation from the repository root, with no editable-install assumption:

```text
python -c "import sys; sys.path.insert(0,'internal'); from local_agent import provenance as p; print(p.source_sha256()); print(p.base_prompt_sha256()); print(p.outcome_contract_sha256())"
```

## Before every push

Every writer, human or model, must verify repository identity before pushing:

```text
python internal/devtools/finalize_change.py
```

The command computes exact `source_sha256` and verifies the two frozen contract axes.
It does **not** stamp source identity into `internal/INSTRUMENT.json`. The legacy
`--write-source` option is retained only as a compatibility no-op and must not be used
as evidence that a live source declaration was written.

If `base_prompt_sha256` or `outcome_contract_sha256` moved, stop for an explicit
generation/methodology decision. Do not auto-update those declarations to get green.
CI independently recomputes identities and remains the authoritative safety net.

Record the exact tested head/base identities and commands in the PR. A source hash is
provenance for that exact tree, not a mutable repository constant.

## Naming and structure

These are permanent rules, not the preferences of one refactor.

- **A name says what the thing is, not where it sits.** `session/storage.py` tells a
  reader nothing that the directory did not already say. `session_store.py` does.
- **One word, one meaning, repository-wide.** `endpoint` means an inference server
  everywhere except `evaluation/endpoints.py`, where it means a success metric. Do not
  add a second such collision.
- **A module named for a generic role gets a qualifier.** `base.py`, `context.py`,
  `client.py`, `runner.py`, `service.py` and `testing.py` are ambiguous the moment two
  of them are open at once.
- **A filename beginning with `test_` is a test module.** A helper for tests is
  `testing_tools.py`, not `test_execution.py`.
- **A directory means something.** Every directory under `internal/` is either part of
  the instrument or not, and `test_hashed_surface_membership.py` fails until a new one
  declares which.
- **An empty or documentation-only directory must not look like source.** A directory
  containing only a README belongs under `internal/docs/`.

## Before you rename or move anything

Run the pre-flight first from the repository root:

```text
python internal/devtools/check_rename_safety.py
python internal/devtools/check_rename_safety.py --markdown > preflight.md
```

Then audit the old path everywhere, not just Python imports: bare-name imports made
possible by `sys.path.insert`, path manipulation, PowerShell, shell scripts, workflows,
documentation, `pyproject.toml`, `.local-agent.toml`, `conftest.py`, `INSTRUMENT.json`,
provenance globs, packaging scripts, demo wrappers and tests.

**Prove the transformation on the real tree.** Before claiming a rename does or does
not move an identity, perform it on a scratch copy of the current tree, recompute all
three hashes and import the package. Static reasoning is not evidence.

## Do not change without saying so explicitly in the PR

- `internal/INSTRUMENT.json`, or either frozen contract axis
- `internal/experiments/` — frozen generation-1 evidence, never edited, never re-scored
- the native Windows CI job
- experiment semantics, condition purity, oracle isolation

Fixing a test by weakening it is not fixing it. If a test is wrong, say why before you
change it.

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

Every test under `internal/tests/` is exercised by both real pytest and the offline
compatibility runner in pull-request CI. New tests must stay within that runner's
supported pytest subset or extend the runner and its regression coverage in the same
change.

## Product-facing contract

Normal demo/user instructions stay on these surfaces:

```text
.\install.ps1
.\local-code-agent.ps1
.\local-code-agent.ps1 chat
.\demo\...
```

Do not send a first-time user into `internal/` to repair our orchestration.

## Working agreement

One writer per branch, one coherent commit series, exact SHAs in the PR body.

State what you actually ran and on what, and state what you did not. "The tests pass"
is not evidence unless you say which tests, on which host, at which commit.

Use direct-to-main integration candidates. Stacked PRs may be useful for early CI, but
must be reconciled onto current `main` and receive fresh authoritative CI before merge.
Do not merge a reviewed child into an obsolete feature branch and call it delivered.

## Done means

A task is done when the change is merged and the thing it was meant to make possible
has actually happened, not when the patch is written. If the request includes getting
something running, running it is part of the task.
