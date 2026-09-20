# Current state

Authoritative implementation: live GitHub `main`. This file describes what the tree can
do and what it cannot. It deliberately names no PR numbers: a current-state document
that tracks in-flight PRs is stale the day they merge, and nothing fails when it is.
Merge history lives in `internal/docs/review-history.md`.

## Product direction

The active direction is the **Session Hub**: one continuous conversation surface around a deterministic controller, with task admission, execution, evidence, verdict and recovery represented separately from model prose.

The earlier design-only state has been replaced by a real execution and session
foundation, including the durable event service and the public durable task hand-off
described below.

## What `main` implements now

- Direct chat persistence is wired. Existing conversations are opened under an exclusive lock before loading, preventing the stale-writer lost-update race.
- The public `session` command opens the canonical persisted conversation under the same ownership model and constructs the durable Session Hub event service for that conversation.
- Repository and self-check work from the public Session Hub is handed through durable task admission before controller effects run.
- The saved user `TurnRef` is the task origin. Direct-user and model-proposal origins remain typed separately; a future rule origin is refused until a real resolved rule identity exists.
- Conversation-originated task admission atomically indexes the trusted `TurnRef` beside the durable task. One turn can therefore resolve to zero or more durable task IDs without putting task artifacts into raw conversation history.
- Databases created before the turn/task sidecar existed rebuild that derived index from already-validated `task.admitted` events at startup.
- Bounded controller result text is staged as a sibling task artifact before terminalization and is reused for follow-up context only when the eventual durable status, verdict and reason match. A crash-recovered `NO_VERDICT/controller_crash` cannot inherit stale pre-crash result text as authority.
- Session Hub follow-up observations are reconstructed from durable task state on every turn and validate the stored `TurnRef` against the canonical raw conversation. Product follow-ups therefore survive restart and do not depend on the process-local `last_result` pointer.
- Durable gateway request identity is deterministic. Re-submitting the same saved turn cannot replay task effects.
- The admitted execution-contract digest covers current source identity plus the controller's effective repository policy, build/test profiles and context budget. Environment values affect that digest without being copied into durable events.
- Raw persisted load/save primitives are private. Owned contexts save explicitly; abandoning an edit does not auto-save it.
- Product-side task outcomes are a closed vocabulary with explicit `NO_VERDICT`.
- A success-shaped product result cannot be constructed unless verification was established.
- Product outcomes project explicitly into lifecycle/verdict state and CLI exit classes.
- Worker/controller exceptions produce unknown/no-verdict semantics rather than a normal finished-success shape.
- Current task routing records provenance (`model_proposal` or `user_direct`) before richer deterministic routing is added.
- Generation-2 evaluator success is executable-frozen as exactly `pass` and `escalated_pass`; the frozen outcome-contract hash is pinned in tests.
- Ledger reporting has an `unclassified` partition so an unfamiliar observed outcome cannot disappear silently.
- Session Hub acceptance gates execute real behavioural/structural checks rather than merely searching prose for desired words.

Source mutation, staging and commit behaviour remain disabled on the conversation product path.

## Durable event service and public task execution

`local_agent/session/` implements the executable `lca.session.events/1` path:

- JSON Schema validation before durable commit or publication, against the versioned
  contract under `internal/docs/session-contract/v1/`
- SQLite WAL session and task storage
- one writer owning sequence assignment and persistence
- admission keyed by request identity and payload digest
- atomic conversation-turn/task indexing at admission for origins that carry a `TurnRef`
- bounded sibling task summaries for restart-safe historical conversation context
- atomic verdict + closure + terminal task/result indexing
- stream-scoped crash recovery to unknown / `NO_VERDICT`, never automatic retry
- accepted-write/shutdown linearization so a returned receipt cannot be abandoned
  behind the shutdown sentinel
- durable non-terminal task state and execution-epoch fencing; stale state or stale
  epoch events fail without consuming a stream sequence
- replay from committed events
- bounded thread-safe subscriptions with explicit overflow/gap handling
- an explicit replay-to-live handoff boundary so a concurrent commit is either in the
  replay window or delivered live, without a silent gap or duplicate
- non-blocking task submission, so a future Textual event loop need not wait on controller
  or tool execution

The public `local-code-agent.ps1 session` path composes that service beside the owned raw
conversation. A conversation deterministically maps to distinct stable UUID stream/session
identities, uses the shared Session Hub SQLite store, reconciles that stream's unfinished
durable tasks to unknown / `NO_VERDICT` on restart, and commits a validated
`session.opened` event before entering the interactive loop.

For repository or self-check work, the gateway first commits the user turn and keeps its
stable `TurnRef`. Hashed `task_admission.py` then derives the durable request identity,
request digest/reference, typed origin, repository identity and effective execution-
contract digest. `DurableTaskExecutor` commits `task.admitted`, advances the task to
`running`, and only then invokes `TaskController` with the durable task UUID. Verdict,
closure and result indexing commit atomically before the synchronous gateway returns the
controller result. An identical request retry returns the existing task identity and is
refused rather than executing the effect again.

The public composition uses a turn-indexed Session Hub store. Its admission commit writes
the `TurnRef` sidecar in the same SQLite transaction as `task.admitted`. Result text is
staged before terminalization but is not itself a verdict: the history reader requires its
expected terminal status, verdict and reason to match the committed `task.verdict` before
supplying it as labelled historical context. On restart, that reader reconstructs the
latest bounded task observations from SQLite and verifies each origin against the current
canonical conversation turn hash. Pre-sidecar databases are migrated from their existing
validated admission events.

The admission schema requires `deadline_utc`. The current adapter records a conservative
deadline envelope over existing bounded command/call configuration, but whole-task
cancellation is not implemented yet. That deadline is provenance, not proof that all
processes were stopped at expiry, and must not be rendered as such.

Important limits still remain:

- **Most declared event kinds are not yet emitted durably by the live product path.**
  Task admission/state/verdict/closure are durable. Controller/worker/tool activity still
  passes through the process-local `EventBuffer`; do not describe those transient records
  as durable activity history.
- **Deterministic-first routing is incomplete.** Current public task origins are direct
  `/check` or model proposal. Rule-origin admission fails closed until a real rule ID is
  available.

## Known product gaps

- Deterministic-first Work/Chat/rule routing is not complete.
- Full process-tree containment and truthful cancellation are not complete. The generic command runner owns the direct child, bounds timeout return and snapshots immutable public evidence, but POSIX process groups and Windows descendant enumeration are best-effort cleanup rather than proof of whole-tree containment. Session Hub cancel requests are not yet wired through to execution ownership.
- Endpoint lease/queue arbitration for shared OVMS use is not complete.
- Textual UI, watch/activity panes and fixed watch execution are later slices.
- No hidden source-mutation path exists behind these interfaces.

## Verification semantics

Controller verdicts are deterministic product claims, not model prose. `NO_VERDICT` means no reliable final verification can be established. Historical evidence never becomes current-tree proof merely because it was previously green.

The worker tracks whether verification was attempted separately from whether it succeeded.
The durable terminal boundary preserves that distinction and cannot turn missing proof into
a verified result.

## Experimental status

Measurement collection is paused. Frozen historical runs are not rescored in place.

Generation 1's published pooled table uses its historical weighted `succeeded` accounting: Control 9/30, Narrow 22/30, Skill 23/29. A newer typed `verified_completion` characterization of the same frozen rows gives 1/30, 12/30 and 9/29. That characterization is useful for understanding endpoint semantics but does not rewrite Generation 1.

Generation 2 currently has no collected model rows. Its success vocabulary and outcome-contract identity are pinned before collection begins, so accidental evaluator changes fail loudly rather than silently changing the generation.

## Next integration order

The durable correctness foundation, public composition path, admission-before-effects
handoff and restart-safe task follow-up history are in place. The next product integration
order is:

1. Add deterministic Work/Chat/rule routing and fixed watch execution on top of recorded route provenance.
2. Add task/run process ownership, endpoint arbitration and truthful cancellation semantics where not already completed by the foundation.
3. Build the fixture-driven Textual shell against the durable event interface.
4. Wire the live controller/endpoint path and run physical-laptop acceptance.

Historical experiments and their artifacts remain frozen. New instrumentation or methodology belongs to a new experimental generation, never a silent reinterpretation of old evidence.
