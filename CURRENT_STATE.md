# Current state

Reconciled against GitHub `main` at `ad07af071a62acfa4d0168c7a89d7110a52088f6`
on 2026-09-26 after the retained-result, Windows containment, public Stop, Panther Lake
capture, Stop terminal-reconciliation and merge-enforcement truth slices landed. Live
source and current CI remain authoritative for implementation; frozen artifacts remain
authoritative for historical experiments.

Use [PROJECT_OVERVIEW.md](PROJECT_OVERVIEW.md) for durable architecture/rationale,
[TRICKS.md](TRICKS.md) for first-release public journeys and acceptance evidence, and
[product-execution-priorities.md](internal/docs/product-execution-priorities.md) for the
priority ladder after the immediate first-release gate.

## Direction and public path

The product target is a dependable local/offline worker for repository inspection, Git,
bounded code changes, builds/tests and diagnosis with replaceable qualified model/runtime
backends. The active surface is the in-process Textual Session Hub launched by
`local-code-agent.ps1` with no argument or `session`.

The deterministic controller owns admission, permissions, execution facts, verification,
provenance and durable state. Models may propose work but cannot grant authority or
self-certify success. Historical experiment machinery is no longer part of the active
product tree or CI; the exact pre-cleanup state is preserved on
`archive/legacy-experiments-2026-09-26`. Current runtime comparisons use the
backend-agnostic endpoint harness under `internal/perf/`.

## What is implemented on current main

The public/product path now includes:

- exact-interpreter dependency preflight, native Windows installed-checkout launcher
  acceptance, deterministic help and Session Hub `--check`;
- managed primary-runtime startup with conservative ownership/reuse rules and truthful
  configured-versus-observed runtime facts;
- one persisted raw conversation plus separate durable event/task state, admission before
  effects, retained request/result artifacts and restart reconciliation that never replays
  unknown effects;
- deterministic public read-only journeys for repository inspection, symbol lookup and
  branch review, with model fallback remaining advice rather than permission;
- task-specific build/failure journeys through the public conversation gateway;
- typed proof binding tying accepted completion to the exact request, proof scope, current
  mutation epoch and current repository tree identity; targeted/partial proof cannot
  certify whole-tree success;
- deterministic failure follow-up for eligible retained failures, including explicit task
  UUID selection;
- retained-result projection that preserves authoritative durable task/verdict truth when
  sibling retained worker prose fails integrity validation and marks that prose unavailable;
- Windows launcher containment of hostile ambient `PYTHONHOME`/`PYTHONPATH` state;
- one process-local endpoint ownership/arbitration path for live conversation and worker
  calls through the endpoint arbiter/runtime/call adapter; cross-process arbitration is
  not claimed;
- a public `stop`/`/stop` action targeting the exact active durable task ID and execution
  epoch, bypassing model dispatch and fencing late completion authority;
- revoked Stop epochs terminalize durably as `UNKNOWN` / `NO_VERDICT` rather than leaving
  a task hanging non-terminal; late controller results cannot regain commit authority;
- Textual activity/result projection with durable task identity, tool/verdict facts,
  retained answer detail, recent task IDs and fail-closed input behaviour when the durable
  feed is unhealthy;
- a Panther Lake acceptance-capture path that records exact checkout, offline preconditions,
  runtime/model/device provenance, public Session Hub preflight, cold/warm run evidence and
  hashes, while explicitly refusing to self-certify physical NPU acceptance;
- a fail-closed merge-enforcement checker that distinguishes actual GitHub branch/ruleset
  required-check policy from merely having workflow files;
- exact product-source identity covering live product, serving, scripts, skills and
  session-schema surfaces after removal of the retired research tree;
- a backend-agnostic OpenAI-compatible endpoint harness under `internal/perf/` for
  client-observed cold readiness, TTFT, decode rate, context behavior and soak evidence,
  with backend telemetry kept separately labelled and cancellation reported only when an
  endpoint-side lifetime hook can prove it.

Source mutation and commits remain disabled on the conversation product path.

## Boundaries that remain open

The remaining gaps must not be collapsed into stronger claims than current evidence
supports:

- **Physical Panther Lake acceptance is still open.** The capture path exists, but the
  required evidence is still a fresh disconnected run on the physical Windows Panther
  Lake machine using the current Session Hub with exact checkout, model/runtime/device
  identity, cold/warm latency and retained logs/screenshots/failures. No hardware claim is
  complete until that run exists.
- **Stop remains bounded, not complete cancellation.** Public Stop, epoch fencing and
  durable `UNKNOWN` / `NO_VERDICT` terminal reconciliation exist. Complete queued and
  inference interruption, owned descendant process-tree cleanup, endpoint quarantine/
  reconciliation and mutation reconciliation still require effectful proof. Until cleanup
  is proven, the product says `Stop requested`, not `Stopped`.
- **Cross-process endpoint ownership is not claimed.** Current arbitration authority is
  intentionally process-local.
- **Merge enforcement is now machine-checkable but not configured by the repository.** At
  the last verified GitHub read, `main` was unprotected and no active required-status-check
  ruleset existed. The checker can prove configuration after an owner applies it; it does
  not possess repository-admin authority itself.
- **Broader mutation, commit/push, Watch creation and richer automation remain later
  product work.** Existing lower-level primitives are not public-journey acceptance.
- **Backend performance is deployment evidence, not an architecture claim.** Runtime
  comparisons use the neutral endpoint harness and retain configured/observed identity
  alongside measurements.

## Immediate work, in order

1. Complete the physical offline Panther Lake acceptance described in `TRICKS.md` using
   the current public Session Hub and the capture procedure in
   `internal/docs/panther-lake-acceptance.md`.
2. Configure GitHub required-check enforcement for `main` through repository settings,
   then verify it with `internal/devtools/check_merge_enforcement.py`. Until configuration
   is observed, green CI remains convention rather than enforced merge policy.
3. Continue P1 Session Hub trust work from
   `internal/docs/product-execution-priorities.md`: evidence-backed lifecycle projection,
   clearer result semantics, result/evidence detail, one Attention surface and a
   deterministic whole-run/fault-injection harness.
4. Continue Stop cleanup only through bounded authority-specific joins: endpoint
   interruption/quarantine, owned process-tree termination and mutation reconciliation.
   Do not promote `UNKNOWN` / `NO_VERDICT` into `CANCELLED` without cleanup evidence.
5. Only after those trust joins, begin intent-scoped safe mutation. Preserve dirty
   worktrees, staging and unrelated user edits; keep commit/push/history changes as
   separate capabilities.

Security defects and user-visible false claims may interrupt this order. Novelty does not.
Every new slice needs a concrete user journey or trust-boundary failure, behavioural
regression evidence, fail-closed negatives, fresh exact-head CI and unchanged frozen
experimental axes.
