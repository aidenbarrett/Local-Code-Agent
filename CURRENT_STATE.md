# Current state

Authoritative implementation: live GitHub `main`. This file describes what the tree can
do and what it cannot. It deliberately names no PR numbers: a current-state document
that tracks in-flight PRs is stale the day they merge, and nothing fails when it is.
Merge history lives in `internal/docs/review-history.md`.

## Product direction

The active direction is the **Session Hub**: one continuous conversation surface around a deterministic controller, with task admission, execution, evidence, verdict and recovery represented separately from model prose.

The earlier design-only state has been replaced by a real execution and session
foundation, including the durable event service described below.

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

## Durable event service: correctness-gated, and not yet reachable

`local_agent/session/` implements the executable `lca.session.events/1` path:

- JSON Schema validation before durable commit or publication, against the versioned
  contract under `internal/docs/session-contract/v1/`
- SQLite WAL session and task storage
- one writer owning sequence assignment and persistence
- admission keyed by request identity and payload digest
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
- non-blocking task submission, so a Textual event loop will not wait on controller or
  tool execution

The current durable correctness gate is therefore closed. The adversarial tests cover
rollback of half-terminal writes, cross-stream recovery, shutdown/enqueue races,
stale-state/stale-epoch rejection and both sides of the replay/live handoff race.

Two limits that a feature list hides, both currently true of `main`:

- **No user-facing entry point constructs it.** The durable service is built by
  `internal/scripts/accept-session-hub.py` and by tests. Ordinary product use does not
  yet write task and event lifecycle records through `DurableSessionService`. Raw
  conversation persistence is a separate authority and does work: owned conversations
  load under an exclusive lock and save explicitly through the conversation store.
  Nothing that used to be kept is being lost; the durable event layer is simply not
  load-bearing yet.
- **Most declared event kinds are not yet emitted by the live product path.** Producer
  code exists for the task lifecycle kinds. For the rest, read the contract as a
  specification of where the hub is going rather than as a description of what a
  subscriber receives today. No exact count is given here on purpose: "has a producer
  implementation" and "is reachable from a product path" are different questions, and a
  number that does not say which one it answers is the sort of claim these documents
  exist to stop.

The durable layer now has the storage/writer/subscriber correctness fences required
before product wiring depends on it. Repository naming/ownership cleanup can resume
without pretending that the user-facing Session Hub already exists.

## Known product gaps

- The canonical raw-turn conversation store is wired, but the durable task/event service is not yet connected to the conversation gateway. Task artifacts remain separate from model history, and follow-up task observation is still process-local rather than reconstructed from durable task state.
- Deterministic-first Work/Chat/rule routing is not complete.
- Full process-tree containment and truthful cancellation are not complete. The generic command runner now owns the direct child, bounds timeout return and snapshots immutable public evidence, but POSIX process groups and Windows descendant enumeration are best-effort cleanup rather than proof of whole-tree containment. Session Hub cancel requests are not yet wired through to execution ownership.
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

The immediate repository work resumes the user-first naming/ownership cleanup now that
the durable correctness gate is closed. When product integration resumes, the order is:

1. Connect a user-facing Session Hub path to the durable task/event service without
   merging task artifacts into model chat history.
2. Add deterministic Work/Chat/rule routing and fixed watch execution on top of recorded route provenance.
3. Add task/run process ownership, endpoint arbitration and truthful cancellation semantics where not already completed by the foundation.
4. Build the fixture-driven Textual shell against the durable event interface.
5. Wire the live controller/endpoint path and run physical-laptop acceptance.

Historical experiments and their artifacts remain frozen. New instrumentation or methodology belongs to a new experimental generation, never a silent reinterpretation of old evidence.
