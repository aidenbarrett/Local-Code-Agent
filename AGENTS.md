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
| Current status | `CURRENT_STATE.md`, then current source/PRs |
| Product priorities | `internal/docs/product-execution-priorities.md` |
| First-release journeys | `TRICKS.md` |
| Session Hub | `internal/local_agent/session/`, `internal/docs/session-hub-design.md` |
| Endpoint ownership | `internal/local_agent/session/endpoint_lease.py`, `endpoint_runtime.py`, `endpoint_call.py` |
| Local runtime ownership | `internal/serving/`, `internal/docs/serving.md` |
| Endpoint performance | `internal/perf/README.md`, `internal/perf/endpoint_harness.py` |
| Tools | `internal/local_agent/tools/` |
| Verification | `internal/local_agent/tools/testing_tools.py`, `internal/docs/verification.md` |
| Model profiles | `internal/local_agent/config.py` |
| Product source identity | `internal/local_agent/provenance.py` |

## Provenance

`source_sha256` is derived from the exact live product tree. It is evidence about the source that produced a result, not a mutable repository constant.

Historical research is archived off the active branch. Do not recreate experiment-era freeze machinery on `main` unless a new measured programme has a current owner, a current question and explicit methodology.

## Naming and structure

- A name says what the thing is, not merely where it sits.
- One word has one meaning repository-wide.
- Generic roles get qualifiers when ambiguity is possible.
- A filename beginning with `test_` is a test module.
- Product runtime code does not live under performance, demo or research directories.
- Backend-specific runtime comparison details belong in local harness profiles, not the generic harness.
- Duplicate authorities and compatibility shims with no active caller are defects.

## Before a rename or move

Run:

```text
python internal/devtools/check_rename_safety.py
```

Then audit the old path everywhere, not only Python imports: dynamic imports, PowerShell, workflows, docs, config, packaging and tests. Do not leave a shim merely to make stale callers green.

## Tests and CI

Native pytest is authoritative:

```text
python -m pytest -q
```

Linux and Windows CI are required before describing a code change as verified. New behaviour gets regression coverage. Bugs get the smallest test that would have caught them. Contract changes get positive and fail-closed negative cases.

Do not weaken a test to manufacture green.

## Performance measurement

There is one generic endpoint harness under `internal/perf/`.

Client latency belongs at the client boundary. Backend-reported values are separately labelled. Cancellation latency is only measured when endpoint-side observation proves the request was active and later proves it stopped. Socket closure alone is not proof.

The harness must remain backend-agnostic. Runtime/device-specific commands and observation URLs live in local profile configuration outside source control.

## Product-facing contract

Normal user instructions stay on supported root surfaces:

```text
.\install.ps1
.\local-code-agent.ps1
.\local-code-agent.ps1 chat
.\demo\...
```

Do not send a first-time user into `internal/` to repair orchestration.

## Working agreement

One writer per branch, one coherent commit series, exact SHAs in the PR body. State what actually ran and on what. Do not overwrite user edits, staging or history. Never silently reset a worktree, automatically push, rewrite history or self-upgrade.

## Done means

A task is done when the change is merged and the thing it was meant to make possible has actually happened, not when the patch exists.
