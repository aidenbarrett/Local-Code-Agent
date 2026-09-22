# Fixed-watch task lifecycle

## Decision

Fixed watches remain deterministic procedures. The Session Hub task lifecycle wraps a fixed-watch run with durable admission, lifecycle state, verification/result evidence and projection; it does not replace the fixed-watch procedure with a model-driven agent loop.

This preserves the product boundary: models may help with conversational or bounded engineering tasks, but scheduled/repeated work whose procedure is already declared by a `FixedWatchJob` should execute the declared procedure directly.

## Current facts

The watch subsystem provides:

- durable watch-job configuration and immutable procedure revisions;
- preallocation of a UUID `run_id` before the runner factory, source observation or execution callback;
- same-job overlap refusal;
- durable watch-attempt history and comparison/delta handling;
- a Session Hub event schema whose `task.admitted.origin` explicitly supports `{kind: "watch", job_id, run_id}`;
- durable watch projection events for state changes and completed runs;
- an optional generic `WatchRunLifecycle` boundary around `FixedWatchService` that does not import Session Hub policy into the watch package;
- `DurableWatchTaskLifecycle`, which requires matching durable watch state, admits the exact preallocated run before runner construction/effects, commits running before `FixedWatchRunner.run`, requires execution-contract identity to agree with admission, consumes an explicit deterministic verifier, closes the durable task and projects the completed watch run using the same identities.

These are implementation facts for the reusable lifecycle components. They do **not** mean an OS scheduler or the public Session Hub entrypoint is already configured to construct and run watches automatically.

## Authority and ordering

For one fixed-watch run the implemented reusable ordering is:

1. `FixedWatchService` validates lifecycle policy, claims same-job ownership and preallocates `run_id`.
2. `DurableWatchTaskLifecycle.admit` requires matching durable `watch.state_changed` state and durably admits one task for the exact `(job_id, run_id)` before runner construction, source observation or command effects.
3. The runner is constructed and the admitted task transitions durably from `admitted` to `running` before `FixedWatchRunner.run` begins.
4. `FixedWatchRunner` observes the configured source and executes the fixed procedure.
5. The resulting attempt must carry the same execution-contract SHA-256 as durable task admission. A caller-supplied deterministic verifier returns the typed `TaskResult`; arbitrary observation prose is never interpreted as success.
6. The canonical durable task result/verdict is committed and the task is closed.
7. The completed watch attempt is projected through `watch.run_recorded` using the same `run_id` and durable `task_id`.

If the deterministic procedure raises before a `FixedWatchRun` exists, the already-admitted task closes as `NO_VERDICT`/`unknown`; no watch attempt or `watch.run_recorded` event is fabricated. If verification or execution-contract identity fails after the fixed attempt exists, that real attempt is retained, the durable task closes `NO_VERDICT`, and the watch run is projected with `NO_VERDICT` before the lifecycle error is surfaced.

A crash or exception must never cause the same `(job_id, run_id)` to replay effects silently. Re-admission of identical bytes is idempotent at the admission primitive; the composed execution lifecycle refuses an already-admitted run rather than replaying it. Reuse of the same run identity for changed request bytes fails closed.

## Provenance

Conversation `RouteSource` remains limited to conversation routing (`user_direct`, `rule`, `model_proposal`). A watch is not a synthetic user turn or routing rule. Controller execution provenance therefore uses the separate `TaskExecutionSource` vocabulary, which can represent `watch` without expanding conversation route authority.

The durable event authority is the existing schema-declared watch origin:

```json
{
  "kind": "watch",
  "job_id": "<uuid>",
  "run_id": "<uuid>"
}
```

The execution contract recorded by the fixed-watch attempt must equal the contract used for durable admission. A mismatch is a provenance failure, not a successful watch result.

## Non-goals

This lifecycle does not:

- let a model select or rewrite the fixed watch procedure;
- infer verification success from arbitrary watch observation strings;
- reinterpret historical/frozen experiment artifacts;
- weaken command allowlists, repository identity checks or verification requirements;
- claim cancellation/process cleanup that the runtime cannot prove;
- make the watch scheduler a second agent loop;
- configure an OS scheduler or silently enable recurring execution.

## Next composition boundary

The next product slice is public/runtime composition: construct the fixed-watch stores, deterministic procedure factory, `DurableWatchTaskLifecycle` and verifier from trusted configuration, expose explicit manual execution first, then attach scheduled triggering without creating a second authority path. Scheduler state must continue to flow through the same `FixedWatchService` overlap fence and durable Session Hub lifecycle.
