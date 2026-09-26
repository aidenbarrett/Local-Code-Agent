# Current state

Live GitHub `main` is authoritative for implementation. This document records the product state around the legacy-research cleanup based on pre-cleanup `main` at `ad07af071a62acfa4d0168c7a89d7110a52088f6`.

Use `PROJECT_OVERVIEW.md` for durable architecture, `TRICKS.md` for first-release public journeys and `internal/docs/product-execution-priorities.md` for the active priority ladder.

## Direction and public path

The product target is a dependable local/offline worker for repository inspection, Git, bounded code changes, builds/tests and diagnosis with replaceable qualified model/runtime backends. The active surface is the in-process Textual Session Hub launched by `local-code-agent.ps1` with no argument or `session`.

The deterministic controller owns admission, permissions, execution facts, verification, provenance and durable state. Models may propose work but cannot grant authority or self-certify success.

Historical experiment machinery is no longer part of the active product tree or CI. The exact pre-cleanup state is preserved on `archive/legacy-experiments-2026-09-26`. Current runtime comparisons use the backend-agnostic endpoint harness under `internal/perf/`.

## Implemented on current product line

The public product path includes:

- exact-interpreter dependency preflight and deterministic Session Hub self-check
- managed primary-runtime startup with conservative ownership/reuse rules
- one persisted raw conversation plus separate durable event/task state
- admission before effects, retained request/result artifacts and restart reconciliation
- deterministic public read-only journeys for repository inspection, symbol lookup and branch review
- task-specific build/failure journeys through the public conversation gateway
- proof binding to the exact request, verification scope, mutation epoch and repository tree
- deterministic follow-up for explicitly referenced retained failures
- truthful retained-result projection when sibling worker prose fails integrity validation
- one process-local endpoint ownership/arbitration path for live conversation and worker calls
- public `stop`/`/stop` targeting the exact active durable task and execution epoch
- Textual activity/result projection with durable task identity, tool/verdict facts and retained detail
- exact product source identity covering live product/serving surfaces

Source mutation and commits remain disabled on the conversation product path.

## Performance harness

`internal/perf/endpoint_harness.py` is the single runtime-comparison harness. It talks to any OpenAI-compatible endpoint and emits JSON containing configured model/runtime/device identity plus optional endpoint-observed identity.

It measures client-boundary cold-start readiness, TTFT, decode rate and context-size behavior. Backend-reported compile/memory telemetry is kept separate from client-observed timing. Cancellation latency is only reported when an endpoint-side active-request hook proves the request existed before disconnect and later proves it stopped. Otherwise cancellation measurement is explicitly unsupported.

The soak path runs a fixed request loop for a chosen duration and records memory, errors and TTFT drift. Backend-specific commands and observation hooks live in local profile files, not in the harness source.

## Boundaries that remain open

- **Physical offline acceptance is still open.** A configured accelerator profile is not proof of the current Session Hub journeys on the target machine. A fresh disconnected acceptance run is still required.
- **Stop is bounded, not complete cancellation.** Public stop and epoch fencing exist, but queued/inference interruption, descendant cleanup, endpoint reconciliation and one fully reconciled terminal cancellation result still need evidence.
- **Cross-process endpoint ownership is not claimed.** Current arbitration is process-local.
- **Mutation, commit/push, Watch creation and broader automation remain later product work.** Lower-level primitives are not public acceptance.
- **Backend performance is deployment evidence, not an architecture claim.** Compare endpoints with the neutral harness and retain the identity alongside the numbers.

## Immediate work

1. Keep hardening the Session Hub around truthful lifecycle/result projection and intuitive interaction.
2. Complete the physical offline acceptance using the current public product path.
3. Close end-to-end cancellation and endpoint cleanup with direct evidence.
4. Use the neutral endpoint harness for runtime comparisons rather than carrying backend-specific benchmark logic in product code.
5. Only after the trust joins above, begin intent-scoped safe mutation while preserving dirty worktrees, staging and unrelated edits.

Security defects and user-visible false claims may interrupt this order. Novelty does not. Every new slice still needs behavioural regression coverage, fail-closed negatives and fresh exact-head CI.
