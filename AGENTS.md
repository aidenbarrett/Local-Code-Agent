# AGENTS.md

Local Code Agent is product-first. Close correctness gaps in the Session Hub and self-engineering loop before adding feature breadth.

Read `PROJECT_OVERVIEW.md` and `CURRENT_STATE.md` before substantial work. Use `TRICKS.md` for first-release journeys, `internal/docs/product-roadmap.md` for long-term direction and `internal/docs/product-execution-priorities.md` for the active priority ladder.

The destination is a dependable offline worker for finding, moving, Git and coding work. Deep-reasoning competition is not a goal. Roadmap items are not implemented features and do not replace source-derived product status.

## The rule that decides most arguments

Recorded evidence does not count merely because it exists. A fact has to be enforced or consumed at the next trust boundary before it can affect a result.

One policy/state decision has one owner. Before adding a scheduler, controller, store, app, queue, lease manager or verifier, search current source for the existing authority. A second implementation beside an uncomposed first one is not progress.

The Session Hub is a projection of authoritative state, not a place to invent status. Configured, declared, observed and verified facts remain distinct; unknown stays unknown. Users ask for outcomes, not route/skill vocabulary.

## Where to look

| Working on | Read |
|---|---|
| Current product status and open gaps | `CURRENT_STATE.md`, then current source/PRs |
| Product priorities | `internal/docs/product-execution-priorities.md` |
| First-release user journeys | `TRICKS.md` |
| Session Hub routing/admission/results | `internal/local_agent/session/`, `internal/docs/session-hub-design.md` |
| Endpoint ownership | `internal/local_agent/session/endpoint_lease.py`, `endpoint_runtime.py`, `endpoint_call.py` |
| Local runtime ownership/startup | `internal/serving/` |
| Endpoint performance comparison | `internal/perf/README.md`, `internal/perf/endpoint_harness.py` |
| Tools the agent can call | `internal/local_agent/tools/` |
| Build/test proof | `internal/local_agent/tools/testing_tools.py`, `internal/docs/verification.md` |
| Model profiles and context budgets | `internal/local_agent/config.py` |
| Windows local bring-up | `install.ps1`, `internal/work-laptop-one-shot.ps1`, `internal/docs/work-laptop-bootstrap.md` |
| Exact product source identity | `internal/local_agent/provenance.py` |
| Prior review findings | `internal/docs/review-history.md` |

`internal/docs/quality-hardening-roadmap.md` is a backlog, not a spec.

## Product provenance

`source_sha256` is derived from the exact live product tree. It is evidence about the source that produced a result, not a mutable repository constant.

Do not add shared source-hash stamp files that every PR has to edit. Package/release provenance may record the Git commit, dirty state and derived `source_sha256` in generated `PACKAGE.json`.

Historical research is archived off the active branch. Do not recreate experiment-era freeze machinery on `main` unless a new measured programme has a current owner, a current question and an explicit methodology.

## Naming and structure

- A name says what the thing is, not merely where it sits.
- One word has one meaning repository-wide.
- Generic roles get qualifiers when ambiguity is possible.
- A filename beginning with `test_` is a test module.
- Empty or documentation-only directories belong under `internal/docs/`.
- Product runtime code does not live under performance, demo or research directories.
- Backend-specific runtime comparison details belong in local harness profiles, not in the generic harness.

Bad or unintuitive naming, duplicate authorities and compatibility shims with no active caller are defects, not harmless tidiness issues.

## Before you rename or move anything

Run the rename pre-flight from the repository root:

```text
python internal/devtools/check_rename_safety.py
python internal/devtools/check_rename_safety.py --markdown > preflight.md
```

Then audit the old path everywhere, not just Python imports: dynamic imports, path manipulation, PowerShell, workflows, documentation, `pyproject.toml`, `.local-agent.toml`, packaging scripts, demo wrappers and tests.

A clean rename means the old path has no live callers or documentation references. Do not leave a shim simply to make stale code green unless there is a real supported compatibility contract.

## Tests and CI

Native pytest is authoritative:

```text
python -m pytest -q
```

Linux and Windows CI are both required before describing a code change as verified. Coverage is reported, not used as a substitute for behavioural acceptance.

New behaviour needs regression tests. Bugs get a test for the failing case. Contract changes get positive and fail-closed negative cases.

Do not weaken a test to make a change green without stating why the old assertion was wrong.

## Performance measurement

There is one generic endpoint harness under `internal/perf/`.

Client latency measurements belong at the client boundary. Backend-reported values are separately labelled and never replace client observations. Cancellation latency is only a measured number when endpoint-side observation proves the request was active and later proves it stopped. Socket closure alone is not cancellation proof.

The harness must remain backend-agnostic. Machine/runtime/device-specific commands and observation URLs live in local profile configuration outside source control.

## Product-facing contract

Normal user instructions stay on supported root surfaces:

```text
.\install.ps1
.\local-code-agent.ps1
.\local-code-agent.ps1 chat
.\demo\...
```

Do not send a first-time user into `internal/` to repair our orchestration.

## Working agreement

One writer per branch, one coherent commit series, exact SHAs in the PR body.

State what actually ran and on what. "The tests pass" is not evidence unless the exact tests and head are known.

Use direct-to-main integration candidates. Stacked PRs may be useful for early CI, but they must be reconciled onto current `main` and receive fresh authoritative CI before merge.

Do not overwrite user edits, staging or history. Never silently reset a worktree. Do not automatically push, rewrite history or self-upgrade.

## Done means

A task is done when the change is merged and the thing it was meant to make possible has actually happened, not when the patch exists. If the request includes getting something running, running it is part of the task.
