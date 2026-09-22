# Current state

Reconciled against live GitHub `main` on 2026-09-22 at
`b24241ff40e91b0152693c2fd5abef7b6fd6a56c` (merge of the recent-task-ID Textual
slice). Recheck volatile fields and CI before making a new implementation claim.

Live GitHub `main` is authoritative for current code. Frozen artifacts/hashes are
authoritative for historical experiment claims. `PROJECT_OVERVIEW.md` owns durable
architecture, decisions and interpretation.

## Product direction

The active goal is a dependable local/offline engineering worker for finding, moving,
Git and coding/build/test work with replaceable model/runtime backends. Deep-reasoning
competition is not the goal. The deterministic controller owns routing authority,
verification, execution state and evidence; models propose work but do not certify it.

The active product surface is the **Session Hub**: one continuous Textual conversation
surface around a deterministic controller, with raw conversation, route decisions, task
execution, evidence/verdicts and watches represented as distinct state.

Measurement collection is paused while the product correctness programme is active.
Frozen historical experiments remain immutable.

## What the public product actually reaches today

- `local-code-agent.ps1 session` launches the in-process Textual Session Hub rather than
  the old blocking input/print loop. `--check` remains the deterministic headless path.
- Canonical raw conversation persistence is owned separately from task/result artifacts.
  Task result prose is not fabricated into assistant history.
- Model-proposed repository work is durably proposed and requires explicit `work`
  acceptance before execution; `chat` resolves it as conversation-only. Accepted work
  is executed from the canonical original user turn, not proposal prose.
- Deterministic rules run before model fallback. Explicit failed-task references of the
  supported form `why did task <durable-uuid> fail?` resolve only against eligible,
  integrity-checked durable failure candidates. Invalid/ineligible IDs fail closed.
- The Textual activity pane exposes the current durable task identity and a bounded set
  of recent terminal task UUIDs so deterministic follow-up syntax is usable.
- Conversation-originated repository/self-check work goes through durable task admission
  before controller effects. Admission, task lifecycle, retained request/result bytes and
  terminal task/result indexing are durable.
- Tool activity on the admitted controller path is durably wrapped. Canonical evidence
  identifiers are preserved; retained artifact bytes are hash/size/media-type checked.
- Fixed-watch work has durable watch admission and an execution lifecycle bridge using
  preallocated `(job_id, run_id)` identity and deterministic verification. This does not
  yet mean recurring watch scheduling is a complete public workflow.
- Task execution provenance distinguishes conversation routes from watch-origin work.
- The command runner has an execution-level cancellation probe: cancellation can be
  observed before spawn or while a configured command runs, and cleanup claims remain
  conservative. This is a primitive, not complete Session Hub cancellation.
- Source mutation/staging/commit authority remains disabled on the conversation product
  path.

## Correctness gaps currently treated as P1

The 2026-09-22 adversarial review found useful defensive primitives but material failures
at their joins. Until these are closed, do not describe the corresponding workflows as
dependable end-to-end product capabilities.

1. **Startup/update preflight.** Public launchers can select a stale editable Python
   environment and fail on current dependencies after a superficial package import
   check. Product startup needs one exact-interpreter preflight and explicit repair path.
2. **Admitted skill execution.** At this snapshot the selected skill is retained in
   admission but is dropped before the worker call, allowing heuristic rerouting. The
   audit record and actual tool authority can therefore disagree.
3. **Durable completion invariants.** Atomic storage validates bytes/sequence ownership
   but does not yet reject contradictory result/verdict/status/cleanup/evidence claims.
4. **Result semantics.** Successful observation, observed verification failure,
   protocol/transport failure and incomplete verification are still collapsed too
   aggressively into coarse outcomes.
5. **Result reachability.** Retained worker answers exist but the public Hub does not yet
   provide a proper task-result detail surface that separates worker analysis from the
   deterministic verdict/evidence.
6. **Feed/input health.** A broken durable feed can stop polling without disabling input
   dispatch, and rejected/busy input can lose the user's draft.
7. **Effective execution identity.** The execution digest does not yet bind the actual
   effective external/repository skill bytes and exact worker/runtime configuration.
8. **Endpoint ownership.** `EndpointArbiter`, `EndpointRuntime` and
   `EndpointCallAdapter` already exist, but the recently merged endpoint scheduler slice
   introduced a second queue/lease authority beside them. The live conversation/worker
   calls are not yet composed through one canonical owner. This must be reconciled before
   further endpoint work.
9. **Cancellation/reconciliation.** Cancellation primitives and command-level polling
   exist, but public Session Hub input, task epoch fencing, endpoint quarantine and
   bounded shutdown are not yet one complete stop-and-reconcile workflow.
10. **Installed-product acceptance/merge gates.** CI contains useful Linux/Windows and
    contract checks, but installed/update/public-entrypoint acceptance is incomplete.
    Repository `main` also had no enforced branch protection/required checks at the
    reviewed snapshot, so workflow files alone do not enforce merge policy.
11. **Contributor/current-status documentation.** Some root/docs instructions still
    describe superseded provenance rules and older product reachability. Documentation
    is an active coding-agent input and must match current source.

The P2 maintainability/scale backlog remains important but is deliberately behind these
correctness repairs: incremental history projections/retention, bounded repository
enumeration and explicit module/composition naming cleanup.

## Endpoint ownership at this snapshot

Current `main` contains two competing in-process endpoint arbitration concepts. The
established stack is:

- `session/endpoint_lease.py` -> `EndpointArbiter`, queue/lease/quarantine policy owner
- `session/endpoint_runtime.py` -> runtime composition
- `session/endpoint_call.py` -> call adapter

A later `session/scheduler.py` added another endpoint identity, queue and lease owner.
Do not build callers against both. Reconcile to one authority first. Neither in-process
implementation is proof of cross-process ownership, and the public inference calls are
not yet fully wrapped by the canonical owner.

## Verification and result semantics

Controller verdicts are deterministic product claims, not model prose. Historical
worker text is untrusted context and cannot change task lifecycle/verdict authority.
Build/test proof belongs to a repository state and becomes stale after relevant mutation.

`NO_VERDICT` means reliable final verification was not established. `NOT_REQUIRED`
should be used only when the admitted plan genuinely does not require build/test
verification, not as a fallback for failed/missing verification.

The current durable terminal transaction is atomic, but semantic consistency validation
across retained result bytes, terminal envelopes, evidence identities and cleanup state
is still a P1 repair. Do not equate atomic commit with verifier completeness.

## UI and feed truth

The Textual Hub is real and public now. It renders canonical conversation, durable task
activity, route state and watches and submits turns without blocking the Textual event
loop. Model-return prose is deliberately not injected into canonical conversation.

Remaining UI correctness work is not cosmetic: when the durable feed cannot establish
current task truth the Hub must refuse new task actions until an authoritative resnapshot
succeeds, and rejected submissions must preserve the draft. A separate retained
result-detail surface is also required so useful worker answers are visible without
making prose authoritative.

## Watch status

Fixed-watch primitives, durable admission and durable execution lifecycle composition are
implemented. Recurring public scheduling, complete Session Hub watch controls and the
full cancellation/recovery acceptance path are not yet delivered. A merged primitive is
not a delivered watch product until the public composition and physical acceptance path
exercise it.

## Experimental status

Historical Generation-1/smoke evidence remains frozen and must not be modified or
silently reinterpreted using current instrumentation. New claim vocabularies, routing,
verification or task contracts belong to a new generation.

Generation 2 has no collected model rows at this snapshot. Its frozen model-facing and
outcome-facing contract axes remain protected. `source_sha256` is derived from the exact
tree/run and is not a mutable live declaration in `internal/INSTRUMENT.json`.

## Active integration order

Until the P1 programme is closed, feature expansion is subordinate to correctness:

1. reconcile endpoint ownership to one implementation;
2. repair public startup/update preflight and contributor/current-status authority;
3. enforce admitted skill through the worker boundary;
4. enforce semantic durable-completion invariants;
5. repair feed/input health and truthful result semantics;
6. expose retained task answers/evidence without giving model prose status authority;
7. freeze/fingerprint effective execution inputs;
8. finish public cancellation + endpoint reconciliation;
9. assemble installed-product acceptance and enforce merge gates;
10. only then resume P2 scale/naming cleanup and new feature slices.

Every repair gets a failing behavioural regression at the broken boundary, exact-base
CI, preserved frozen experiment identities and current documentation in the same change.
