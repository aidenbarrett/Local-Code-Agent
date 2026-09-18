# Current state

Date: 2026-09-18. Authoritative implementation: live GitHub `main`; active follow-up work is called out by PR number rather than silently treated as merged.

## Product direction

The active direction is the **Session Hub**: one continuous conversation surface around a deterministic controller, with task admission, execution, evidence, verdict and recovery represented separately from model prose.

PRs #46-#48 are merged. They replaced the earlier design-only state with a real execution/session foundation. PR #49 is open and implements the durable event-service slice; it is not part of `main` until merged.

## What `main` implements now

- Direct chat persistence is wired. Existing conversations are opened under an exclusive lock before loading, preventing the stale-writer lost-update race.
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

## Active PR #49: durable event service

The open durable-service slice is building the executable `lca.session.events/1` path:

- JSON Schema validation before durable commit/publication
- SQLite WAL session/task storage
- one writer owning sequence assignment and persistence
- idempotent admission keyed by request identity and payload digest
- replay from committed events
- bounded thread-safe subscriptions with explicit overflow/gap handling
- recovery of admitted-without-terminal tasks to `NO_VERDICT`/unknown effects, never automatic retry
- non-blocking submission so a future Textual event loop does not wait on controller/tool execution

Exact-head CI remains authoritative for that branch. None of the above open-PR behaviour should be described as merged until the PR lands.

## Known product gaps

- The existing gateway still needs full convergence onto the persisted conversation authority; controller task/verdict artifacts must remain outside model history.
- Deterministic-first Work/Chat/rule routing is not complete.
- Process-tree ownership and truthful cancellation are not complete. Current generic tool execution still uses blocking subprocess calls; cancellation cannot claim cleanup it did not prove.
- Endpoint lease/queue arbitration for shared OVMS use is not complete.
- Textual UI, watch/activity panes and fixed watch execution are later slices.
- No hidden source-mutation path exists behind these interfaces.

## Verification semantics

Controller verdicts are deterministic product claims, not model prose. `NO_VERDICT` means no reliable final verification can be established. Historical evidence never becomes current-tree proof merely because it was previously green.

The worker already tracks whether verification was attempted separately from whether it succeeded. Product plumbing is being hardened so those facts remain distinct at the Session Hub boundary.

## Experimental status

Measurement collection is paused. Frozen historical runs are not rescored in place.

Generation 1's published pooled table uses its historical weighted `succeeded` accounting: Control 9/30, Narrow 22/30, Skill 23/29. A newer typed `verified_completion` characterization of the same frozen rows gives 1/30, 12/30 and 9/29. That characterization is useful for understanding endpoint semantics but does not rewrite Generation 1.

Generation 2 currently has no collected model rows. Its success vocabulary and outcome-contract identity are pinned before collection begins, so accidental evaluator changes fail loudly rather than silently changing the generation.

## Next integration order

1. Finish and verify #49 durable event service and recovery.
2. Add deterministic Work/Chat/rule routing and fixed watch execution on top of recorded route provenance.
3. Add task/run process ownership, endpoint arbitration and truthful cancellation semantics where not already completed by the foundation.
4. Build the fixture-driven Textual shell against the durable event interface.
5. Wire the live controller/endpoint path and run physical-laptop acceptance.

Historical experiments and their artifacts remain frozen. New instrumentation or methodology belongs to a new experimental generation, never a silent reinterpretation of old evidence.
