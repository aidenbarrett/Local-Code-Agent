# Current state

Reconciled against GitHub `main` at `064415b9d1136f072848f56cbf7f2593006d1fbe`
on 2026-09-25 after the retained-result integrity, Windows containment and public Stop
slices landed. Live source and current CI remain authoritative for implementation;
frozen artifacts remain authoritative for historical experiments.

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
self-certify success. Measurement collection remains paused; Generation-1 evidence is
frozen and product hardening must not rewrite historical methodology or denominators.

## What is implemented on current main

The public product path now includes:

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
- typed proof binding that ties accepted completion to the exact request, declared proof
  scope, current mutation epoch and current repository tree identity; targeted/partial
  proof cannot certify whole-tree success;
- deterministic failure follow-up for eligible retained failures, including explicit task
  UUID selection;
- retained-result projection that keeps the authoritative durable task/verdict visible if
  sibling retained worker prose fails integrity validation, while clearly marking that
  prose unavailable;
- Windows launcher containment of hostile ambient `PYTHONHOME`/`PYTHONPATH` state;
- one process-local endpoint ownership/arbitration path for live conversation and worker
  calls through the existing endpoint arbiter/runtime/call adapter; cross-process
  arbitration is not claimed;
- a public `stop`/`/stop` action that targets the exact active durable task ID and execution
  epoch, bypasses model dispatch and fences late completion authority;
- Textual activity/result projection with durable task identity, tool/verdict facts,
  retained answer detail, recent task IDs and fail-closed input behaviour when the durable
  feed is unhealthy.

Source mutation and commits remain disabled on the conversation product path.

## Boundaries that remain open

The remaining gaps must not be collapsed into stronger claims than current evidence
supports:

- **Physical Panther Lake acceptance is still open.** A configured `NPU` profile, prior
  accelerator demos and old physical smoke runs do not prove the current Session Hub
  first-release journeys. The required gate is a fresh disconnected run on the physical
  Windows Panther Lake machine with exact checkout, model/runtime/device identity,
  cold/warm latency and retained logs/screenshots/failures.
- **Stop is bounded, not complete cancellation.** The public action and epoch fencing are
  wired, but complete queued/inference interruption, descendant process-tree cleanup,
  endpoint quarantine/reconciliation and one fully reconciled terminal cancellation
  result still require evidence. Until cleanup is proven, the product says `Stop
  requested`, not `Stopped`.
- **Cross-process endpoint ownership is not claimed.** Current arbitration authority is
  intentionally process-local.
- **Repository-required-check enforcement is not established by workflow files alone.**
  Repository settings must be inspected/configured before claiming merge policy enforces
  those checks.
- **Broader mutation, commit/push, Watch creation and richer automation remain later
  product work.** Existing lower-level primitives are not public-journey acceptance.

## Immediate work, in order

1. Complete the physical offline Panther Lake acceptance described in `TRICKS.md` using
   the current public Session Hub. The capture procedure lives in
   `internal/docs/panther-lake-acceptance.md`; it records exact provenance and refuses to
   self-certify the hardware claim.
2. Verify/configure installed-product merge enforcement if repository ownership permits;
   keep native Linux/Windows and public-launcher acceptance authoritative.
3. After the physical first-release gate is satisfied, take P1 work from
   `internal/docs/product-execution-priorities.md`: evidence-backed lifecycle projection,
   clearer result semantics, result/evidence detail, one Attention surface and a
   deterministic whole-run/fault-injection harness.
4. Only after those trust joins, begin intent-scoped safe mutation. Preserve dirty
   worktrees, staging and unrelated user edits; keep commit/push/history changes as
   separate capabilities.

Security defects and user-visible false claims may interrupt this order. Novelty does not.
Every new slice needs a concrete public journey or trust-boundary failure, behavioural
regression evidence, fail-closed negatives, fresh exact-head CI and unchanged frozen
experimental axes.
