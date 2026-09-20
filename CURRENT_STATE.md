# Current state

Authoritative implementation: live GitHub `main`. This file describes what the tree can
do and what it cannot. It deliberately names no PR numbers: a current-state document
that tracks in-flight PRs is stale the day they merge, and nothing fails when it is.
Merge history lives in `internal/docs/review-history.md`.

## Product direction

The active direction is the **Session Hub**: one continuous conversation surface around a deterministic controller, with task admission, execution, evidence, verdict and recovery represented separately from model prose.

The earlier design-only state has been replaced by a real execution and session
foundation, including durable admission, durable sibling task artifacts and restart-safe
bounded follow-up context.

## What `main` implements now

- Direct chat persistence is wired. Existing conversations are opened under an exclusive lock before loading, preventing the stale-writer lost-update race.
- The public `session` command opens the canonical persisted conversation under the same ownership model and constructs the durable Session Hub event service for that conversation.
- Repository and self-check work from the public Session Hub is handed through durable task admission before controller effects run.
- The saved user `TurnRef` is the task origin. Direct-user and model-proposal origins remain typed separately; a future rule origin is refused until a real resolved rule identity exists.
- Durable gateway request identity is deterministic. Re-submitting the same saved turn cannot replay task effects.
- The admitted execution-contract digest covers current source identity plus the controller's effective repository policy, build/test profiles and context budget. Environment values affect that digest without being copied into durable events.
- Conversation-originated task admission atomically persists the exact `TurnRef` association and retained request bytes beside the task row and admitted event.
- Terminalization atomically persists bounded retained result bytes beside verdict, closure and the terminal task/result index.
- Retained artifact reads verify the recorded SHA-256, byte count and media type before returning data.
- Resumed conversations can reconstruct one bounded historical task observation from durable state with `last_result` absent. The gateway recomputes the associated `TurnRef` from canonical raw conversation first; corrupt, stale or unavailable task history is omitted before model context is composed.
- Historical task observations are labelled untrusted and are not current verification, assistant turns or deterministic referent authority.
- Databases created before retained task artifacts existed rebuild only the turn-to-task association already present in validated admission events. Missing historical bytes remain unavailable.
- Raw persisted load/save primitives are private. Owned contexts save explicitly; abandoning an edit does not auto-save it.
- Product-side task outcomes are a closed vocabulary with explicit `NO_VERDICT`.
- A success-shaped product result cannot be constructed unless verification was established.
- Product outcomes project explicitly into lifecycle/verdict state and CLI exit classes.
- Worker/controller exceptions produce unknown/no-verdict semantics rather than a normal finished-success shape.
- Current task routing records provenance (`model_proposal` or `user_direct`) before richer deterministic routing is added.
- Generation-2 evaluator success is executable-frozen as exactly `pass` and `escalated_pass`; the frozen outcome-contract hash is pinned in tests.
- Ledger reporting has an `unclassified` partition so an unfamiliar observed outcome cannot disappear silently.
- Session Hub acceptance gates execute real behavioural/structural checks, including retained artifact and TurnRef-association behaviour, rather than merely searching prose for desired words.

Source mutation, staging and commit behaviour remain disabled on the conversation product path.

## Durable event service and public task execution

`local_agent/session/` implements the executable `lca.session.events/1` path:

- JSON Schema validation before durable commit or publication, against the versioned
  contract under `internal/docs/session-contract/v1/`
- SQLite WAL session, task, association and retained-artifact storage
- one writer owning sequence assignment and persistence
- admission keyed by request identity and payload digest
- atomic admission + exact `TurnRef` association + retained request artifact
- atomic verdict + closure + terminal task/result indexing + retained bounded result
- stream-scoped crash recovery to unknown / `NO_VERDICT`, never automatic retry; recovery writes an inspectable bounded unknown result artifact
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
retained request artifact, typed origin, repository identity and effective execution-
contract digest. `DurableTaskExecutor` commits `task.admitted`, the association and request
bytes, advances the task to `running`, and only then invokes `TaskController` with the
durable task UUID. Verdict, closure, bounded result bytes and result indexing commit
atomically before the synchronous gateway returns the controller result. An identical
request retry returns the existing task identity and is refused rather than executing the
effect again.

`task_history.py` reads only terminal associated task state. A retained result must pass
artifact-integrity checks and strict typed result parsing. The gateway then recomputes the
referenced raw-turn hash from the canonical conversation before composing one bounded
system observation labelled historical, untrusted and not current verification. Old tasks
whose request/result bytes were never retained remain durable facts but do not acquire
invented text during migration.

The admission schema requires `deadline_utc`. The current adapter records a conservative
deadline envelope over existing bounded command/call configuration, but whole-task
cancellation is not implemented yet. That deadline is provenance, not proof that all
processes were stopped at expiry, and must not be rendered as such.

Important limits still remain:

- **Deterministic referent resolution and deterministic-first routing are incomplete.**
  The latest terminal associated task can be composed as bounded historical context, but
  selection among multiple candidate tasks is not yet deterministic. Current public task
  origins are direct `/check` or model proposal. Rule-origin admission fails closed until
  a real rule ID is available.
- **Most declared event kinds are not yet emitted durably by the live product path.**
  Task admission/state/verdict/closure are durable. Controller/worker/tool activity still
  passes through the process-local `EventBuffer`; do not describe those transient records
  as durable activity history.
- **Whole-task cancellation is not yet complete.** Admission records a required deadline
  derived conservatively from existing bounded configuration, but that timestamp is
  provenance rather than proof that every process was stopped at expiry.

## Known product gaps

- Deterministic-first Work/Chat/rule routing and deterministic referent selection are not complete.
- Fixed watch execution and its scheduler lifecycle are not complete.
- Full process-tree containment and truthful cancellation are not complete. The generic command runner owns the direct child, bounds timeout return and snapshots immutable public evidence, but POSIX process groups and Windows descendant enumeration are best-effort cleanup rather than proof of whole-tree containment. Session Hub cancel requests are not yet wired through to execution ownership.
- Endpoint lease/queue arbitration for shared OVMS use is not complete.
- Textual UI and durable activity/watch panes are later slices.
- No hidden source-mutation path exists behind these interfaces.

## Verification semantics

Controller verdicts are deterministic product claims, not model prose. `NO_VERDICT` means no reliable final verification can be established. Historical evidence never becomes current-tree proof merely because it was previously green.

The worker tracks whether verification was attempted separately from whether it succeeded.
The durable terminal boundary preserves that distinction and cannot turn missing proof into
a verified result. A retained historical result can explain what a previous task reported;
it cannot certify the present repository or resolve an ambiguous target by itself.

## Experimental status

Measurement collection is paused. Frozen historical runs are not rescored in place.

Generation 1's published pooled table uses its historical weighted `succeeded` accounting: Control 9/30, Narrow 22/30, Skill 23/29. A newer typed `verified_completion` characterization of the same frozen rows gives 1/30, 12/30 and 9/29. That characterization is useful for understanding endpoint semantics but does not rewrite Generation 1.

Generation 2 currently has no collected model rows. Its success vocabulary and outcome-contract identity are pinned before collection begins, so accidental evaluator changes fail loudly rather than silently changing the generation.

## Next integration order

The durable correctness foundation, public composition path, admission-before-effects
handoff and restart-safe task-history projection are in place. The next product integration
order is:

1. Add deterministic Work/Chat/rule routing, deterministic referent selection and fixed watch execution on top of recorded route provenance.
2. Add task/run process ownership, endpoint arbitration and truthful cancellation semantics where not already completed by the foundation.
3. Build the fixture-driven Textual shell against the durable event interface.
4. Wire the live controller/endpoint path and run physical-laptop acceptance.

Historical experiments and their artifacts remain frozen. New instrumentation or methodology belongs to a new experimental generation, never a silent reinterpretation of old evidence.
