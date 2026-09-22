# Fixed-watch task lifecycle

## Decision

Fixed watches remain deterministic procedures. The Session Hub task lifecycle wraps a fixed-watch run with durable admission, lifecycle state, verification/result evidence and projection; it does not replace the fixed-watch procedure with a model-driven agent loop.

This preserves the product boundary: models may help with conversational or bounded engineering tasks, but scheduled/repeated work whose procedure is already declared by a `FixedWatchJob` should execute the declared procedure directly.

## Current facts

The current watch subsystem already provides:

- durable watch-job configuration and immutable procedure revisions;
- preallocation of a UUID `run_id` before the runner factory, source observation or execution callback;
- same-job overlap refusal;
- durable watch-attempt history and comparison/delta handling;
- a Session Hub event schema whose `task.admitted.origin` explicitly supports `{kind: "watch", job_id, run_id}`;
- durable watch projection events for state changes and completed runs.

These are existing implementation facts. They do **not** mean watch runs are already composed through the Session Hub task lifecycle.

## Authority and ordering

For one fixed-watch run the intended order is:

1. `FixedWatchService` validates lifecycle policy and preallocates `run_id`.
2. The Session Hub durably admits one task for the exact `(job_id, run_id)` before source observation or command effects.
3. The admitted task transitions to running.
4. `FixedWatchRunner` observes the configured source and executes the fixed procedure.
5. Deterministic verification/result conversion produces the task verdict and closes the durable task.
6. The completed watch attempt is projected through `watch.run_recorded` using the same `run_id` and durable `task_id`.

A crash or exception must never cause the same `(job_id, run_id)` to replay effects silently. Re-admission of identical bytes is idempotent; reuse of the same run identity for changed request bytes fails closed.

## Provenance

Conversation `RouteSource` remains limited to conversation routing (`user_direct`, `rule`, `model_proposal`). A watch is not a synthetic user turn or routing rule. Controller execution provenance therefore needs a separate vocabulary that can represent `watch` without expanding conversation route authority.

The durable event authority is the existing schema-declared watch origin:

```json
{
  "kind": "watch",
  "job_id": "<uuid>",
  "run_id": "<uuid>"
}
```

## Non-goals

This lifecycle does not:

- let a model select or rewrite the fixed watch procedure;
- reinterpret historical/frozen experiment artifacts;
- weaken command allowlists, repository identity checks or verification requirements;
- claim cancellation/process cleanup that the runtime cannot prove;
- make the watch scheduler a second agent loop.

## Next composition boundary

The next implementation slice after durable admission and explicit execution provenance is a small adapter that starts the already-admitted watch task, runs the deterministic fixed-watch procedure, commits a typed task result/verdict, and records the watch run with the same task/run identities. That adapter should own failure cleanup so a factory/admission failure cannot leave effects running or fabricate a completed watch result.
