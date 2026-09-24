# Product execution priorities

Adopted: 2026-09-24.

This document is the standing reference for **what product work should happen next,
what lessons from other agent projects we have chosen to adopt, and what attractive
ideas are deliberately not allowed to derail the current build**.

It does not replace live source, `CURRENT_STATE.md`, `TRICKS.md`, the standards repair
backlog, or the long-term `product-roadmap.md`:

- live GitHub `main` owns current implementation truth;
- `CURRENT_STATE.md` owns the immediate ordered work for the current checkout;
- `TRICKS.md` owns first-release public journeys and acceptance evidence;
- `standards-repair-backlog-2026-09-22.md` owns the current correctness repairs;
- `product-roadmap.md` owns the long-term product destination and milestone gates;
- this file owns the **adopted product learnings, anti-tangent rules and the priority
  ladder after/around the immediate correctness work**.

If these disagree about what is implemented, source and current-state evidence win.
If a new idea does not map to the priority ladder below, it is research/watch material,
not an implementation task.

## Direction remains unchanged

The comparative review did not identify a reason to replace LCA's architecture. The
core direction remains:

- the model is a replaceable dependency; the agent is the product;
- deterministic code owns admission, permissions, execution, verification, provenance
  and durable state;
- models may propose work but cannot grant themselves authority or certify success;
- uncertain effects fail closed; restart must not replay unknown side effects;
- local/offline operation is a product requirement for core supported workflows;
- conversation is the user surface, but conversation prose is not task truth;
- historical experiments remain frozen and are never silently reinterpreted by product
  instrumentation changes.

External projects are sources of ideas, not evidence that a design is better for LCA.
Every adopted idea below still needs LCA-specific behavioural acceptance before it is
considered successful.

## Product laws adopted from the review

### 1. The UI is a projection of truth

The Session Hub must never present a model, device, action, task state, verification
result or successful outcome unless an owning runtime/controller record supports it.
Configured, declared, observed and verified facts stay visibly distinct. Unknown stays
unknown.

This applies to status bars, activity rows, result cards, progress labels and future
editor clients. A decorative `RUNNING`, `STOPPED`, `NPU`, `VERIFIED` or similar label is
not acceptable if the underlying authority cannot prove it.

### 2. Users ask for outcomes, not internal vocabulary

A user should be able to say:

- `Inspect this repo.`
- `Where is X defined?`
- `What changed on my branch?`
- `Build it.`
- `Why did that fail?`

without learning route names, skill names, magic prefixes or controller vocabulary.
Deterministic routing and capability selection are product internals. If intent or
authority is genuinely ambiguous, ask one specific question rather than expose the
implementation model.

### 3. Important work is artifact-centric, not chat-centric

Conversation is how work is requested and discussed. The durable task/result is where
engineering truth lives. Significant work should surface bounded artifacts such as:

- requested scope;
- route/skill and tool authority;
- repository/tree identity;
- changed or inspected files;
- commands and exit status;
- verification scope and result;
- retained logs/evidence;
- task identity and provenance.

The default conversation stays readable. Detail is discoverable rather than dumped into
chat.

### 4. Recovery and intervention are first-class UX

A blocked task must tell the user what actually needs attention: permission, failed
verification, unavailable endpoint, ambiguous intent, unhealthy durable feed, cleanup
uncertainty or another typed reason.

Do not make the user reconstruct state from old chat. The Session Hub should converge on
one coherent **Attention** surface for actionable blockers and interventions.

### 5. Verification must answer the user's actual request

A proof record is valid only for its declared repository state, requested scope,
commands/checks and verification plan. A passing build cannot certify a requested test
run. A historical pass cannot certify a changed tree. Partial proof must remain partial.

### 6. Safe effects beat apparent autonomy

Mutation is added only after the read-only worker is useful and the permission,
verification, cancellation and recovery joins are trustworthy. Write authority is
scoped to intent, repository, operation, paths and preconditions. Commit, push and
history-changing operations remain separate capabilities.

## Priority ladder

### P0 — Finish the honest first-release worker

This work preempts every research-derived feature idea except a security defect or a
user-visible false claim.

1. **Natural read-only work.** Directly support repository inspection, symbol lookup,
   branch review and other declared read-only jobs through durable admission and fixed
   capability/tool authority. Remove unnecessary `work`-style second turns.
2. **Task-specific proof.** Bind build/test/diagnostic outcomes to the exact user
   request, complete verification scope and current tree identity. Improve deterministic
   follow-up selection without inventing referent authority.
3. **End-to-end Stop.** Compose user Stop, queued/inference cancellation, epoch fencing,
   owned process-tree cleanup, endpoint reconciliation and one durable terminal result.
   Unknown cleanup remains `NO_VERDICT`.
4. **Endpoint composition.** Use the single endpoint ownership/arbitration authority for
   actual conversation and worker calls; expose queue/lease/quarantine facts truthfully.
   Do not create another scheduler.
5. **Physical offline acceptance.** Run the public journeys on the Panther Lake machine
   with external networking unavailable, recording exact model/runtime/device identity,
   cold/warm latency, logs/screenshots and failures. Only observed evidence may support a
   hardware claim.
6. **Installed-product enforcement.** Keep native Linux/Windows and public-launcher
   acceptance authoritative and ensure merge policy actually enforces the checks claimed
   by the project.

Exit condition: the first-release public journeys work through the real installed
surface with honest results, no repeated effects, and evidence for the claimed local
configuration.

### P1 — Make Session Hub the truthful control surface

After the current correctness joins are closed, improve the product surface around the
facts LCA already owns.

1. **Evidence-backed lifecycle projection.** Project only durable/authoritative states;
   do not invent attractive intermediate states the event model cannot prove.
2. **Clear result semantics.** Make `VERIFIED`, `PARTIALLY VERIFIED`, `FAILED`,
   `REFUSED` and `NO_VERDICT` understandable at a glance while preserving typed reason
   detail underneath.
3. **Result/evidence view.** Expose requested scope, retained worker answer, evidence,
   logs, tool/command activity and provenance as separate labelled layers. Worker prose
   never controls verdict styling.
4. **Attention surface.** Collect actionable permission requests, failed verification,
   endpoint/runtime outages, cleanup uncertainty and other interventions in one place.
5. **Deterministic whole-run harness.** Provide fake model/runtime/tools/verifier/clock
   seams capable of exercising an entire Session Hub task lifecycle and asserting event
   order, durable state, UI projection, result semantics and restart behaviour. Add
   crash/fault injection at trust boundaries.

Success evidence must be behavioural, not screenshots alone: given an event/store state,
the UI projection and available actions must be deterministic and regression-tested.

### P2 — Safe worker mutation

Do not begin broad source mutation merely because lower-level write tools exist.

1. **Intent-scoped permission UX.** Ask for meaningful authority such as `modify these
   four files in this worktree once`, not permission for an internal tool name.
2. **Bounded file/Git mutation.** Preserve dirty worktrees, staging and unrelated user
   edits. Preview affected scope and verify the resulting diff.
3. **Operation-owned revert.** Where LCA can prove which changes it owns and that no
   conflicting outside edits occurred, offer a bounded revert of LCA-owned changes.
   Never implement generic blind time travel or reset user work.
4. **Worktree/isolation strategy where justified.** Prefer explicit, testable Git/worktree
   ownership for risky changes over model-managed repository state.

Exit condition: positive and adversarial mutation journeys show exact intended diffs,
no unrelated edits, safe interruption/recovery and independent verification.

### P2 — Reusable work and Watch

Turn proven successful work into a reviewed reusable specification rather than replaying
an old chat prompt.

The target flow is:

1. user asks naturally;
2. LCA completes and verifies the work;
3. any corrections/refinements are incorporated;
4. user chooses to make it reusable;
5. LCA proposes a structured task specification;
6. the user reviews it;
7. only then is a Task/Watch saved.

The reusable specification should explicitly carry goal, inputs, capability/tool scope,
selected procedure, verification requirements, delivery behaviour and schedule/trigger.
The controller remains the authority when the Watch runs.

### P2 — First-run and human usability acceptance

Build onboarding only when one safe real task is trustworthy enough to teach with.

Prefer one real, harmless first-run task over a slideshow or documentation dump. Let the
user see request, routing, runtime identity, work, verification and result using only
states LCA can prove.

Add cold-user comprehension checks alongside deterministic UI tests. A small beginner
study should answer whether a new user understands:

- what LCA can do;
- which model/runtime/device facts are known versus configured;
- whether a result was actually verified;
- where evidence lives;
- how to stop work;
- when permission is required.

Passing automated tests is not evidence that the interface is understandable.

## Experiments worth prototyping later

These are not roadmap commitments. Prototype them only after the earlier gates and keep
them only if measured user outcomes improve.

- **Selective resumability/checkpoints.** Resume only steps with proven idempotence or a
  safe continuation contract. Unknown side effects still recover to `NO_VERDICT` rather
  than automatic replay.
- **Observability export.** OpenTelemetry-style traces/metrics may be useful for debugging
  and qualification, provided they are derived from existing authorities, remain
  optional/offline-safe and do not become a second task-state system.
- **Parallel/multi-worker execution.** Consider only for tasks with genuinely independent
  bounded subproblems and a deterministic join/verifier. Measure elapsed time,
  interventions and correctness against the simpler single-worker path.
- **Richer repository intelligence.** Symbol indexes, incremental parsing or semantic
  retrieval must beat bounded path/content/Git search on declared tasks before becoming
  required infrastructure.
- **Richer IDE presentation.** VS Code remains a second client over the same service and
  state authorities, never a second controller.

## Ideas to watch, not chase

Useful ideas may stay parked until a concrete LCA journey needs them. Examples include
browser/desktop control, elaborate workflow builders, general-purpose agent ecosystems,
large plugin marketplaces, deep multi-agent role systems and broad remote orchestration.

Research may continue, but implementation requires a named user problem and acceptance
gate.

## Explicit anti-tangent list

Do not interrupt the current programme to build any of the following without a new
project-level decision and concrete acceptance case:

- a generic planner/coder/reviewer multi-agent framework;
- a graphical node workflow editor;
- gamified/pixel-world/animated-agent UI;
- arbitrary model-generated shell execution;
- autonomous push/publish or self-upgrade;
- hidden cloud fallback;
- automatic replay of uncertain side effects after crash/restart;
- a second scheduler, controller, durable store, verifier or permission authority;
- a giant telemetry/dashboard stack before the underlying states are complete;
- chain-of-thought/tool-noise dumped into the main conversation;
- broad browser/desktop automation merely because another agent project has it.

The burden of proof is on new complexity.

## Gate for accepting new work

Before a feature, refactor or research idea enters active implementation, answer all of
these:

1. Which current user journey or priority-ladder item does it advance?
2. What concrete user problem or failure does it fix?
3. Which existing authority owns the relevant policy/state decision?
4. Does the proposal compose that authority rather than create a competing one?
5. What behavioural regression or acceptance evidence will prove success?
6. What negative/failure case must fail closed?
7. Does it preserve frozen experimental contracts and historical artifacts?
8. What simpler implementation was considered first?

If those answers are weak, park the idea. Do not create implementation momentum and
rationalise the product value afterward.

## How external learnings are treated

Comparative GitHub research is directional input, not measurement of LCA. A clever
pattern elsewhere can inspire a hypothesis such as `an Attention surface will reduce
recovery friction`; it does not prove that hypothesis here.

For each adopted pattern, keep the evidence classes separate:

- **External observation:** another project exposes or documents the pattern.
- **LCA interpretation:** why the pattern appears relevant to an LCA problem.
- **LCA implementation hypothesis:** the smallest compatible design worth trying.
- **LCA evidence:** behavioural tests, physical acceptance or user comprehension results
  collected after implementation.

Do not collapse those into `other projects do this, therefore LCA should`.

## Current ordering rule

At any moment, start with `CURRENT_STATE.md` and the live open PRs. Complete that
ordered work before pulling the next item from this document. Security defects and
truthfulness bugs may interrupt the sequence; novelty does not.

The intended progression is:

`first-release correctness -> truthful Session Hub -> safe mutation -> reusable work ->
real-task onboarding -> measured experiments/IDE expansion`.

That ordering is deliberately boring. It is how LCA becomes a worker people can trust
instead of an impressive pile of agent features.