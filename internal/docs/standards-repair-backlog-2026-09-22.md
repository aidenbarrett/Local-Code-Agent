# Standards repair backlog, 2026-09-22

Status: active correctness programme derived from the adversarial review of `main` at
`9f244577f8222eef429d96cc2cf423dab591da33`. Recheck live `main` before implementing an
item. This is a repair backlog, not proof that a fix exists.

The review found that the foundations contain useful defensive engineering but the
product fails at several joins: recorded admission is not always execution authority,
durable storage can accept semantically contradictory completion, useful retained
answers are hidden, the Hub can accept work after feed failure, effective execution
identity is incomplete, endpoint ownership is duplicated/uncomposed and installed
product acceptance is weaker than the unit/structural suite suggests.

All fourteen review findings are P1 for the current hardening programme. Ordering below
is dependency/order-of-attack guidance, not a lower priority classification.

## Active P1 programme

### R01, startup/update preflight

One exact-interpreter preflight for public launchers. Verify supported Python, current
requirements and representative product imports before model/NPU startup. Keep
check-only read-only; make repair explicit and honest offline. Native Windows must
execute the real PowerShell path in CI.

### R14, contributor/current-status authority

Reconcile `AGENTS.md`, `CURRENT_STATE.md`, `PROJECT_OVERVIEW.md` and this docs index with
current provenance rules and public product reachability. Historical experiments/scores
remain historical. Contributors must be able to identify one component owner before
adding another scheduler/controller/store/app.

### R02, admitted skill is execution authority

Carry the validated selected skill from deterministic routing/admission through the
executor and durable controller adapter into `Orchestrator.run(skill_name=...)`. Resolve
it before effects. Unknown/missing deterministic skills fail closed. `task-diagnostic`
must be a real bounded supported procedure, not a provenance label that silently falls
back to heuristic routing.

### R03, durable completion invariants

Add one typed semantic validation boundary before terminal commit. Retained result,
status, verdict, verification facts, task identity, cleanup and evidence must agree.
Contradictory/fabricated proof fails atomically without sequence/index drift. Historical
missing evidence stays unavailable and old verdicts are never silently upgraded.

### R06, Hub input and feed health

A durable feed integrity failure disables task actions until an authoritative validated
resnapshot succeeds. Busy/closed/invalid/unhealthy submissions preserve the exact user
draft. Later ordinary status text cannot hide an unresolved feed-integrity fault.

### R04, truthful product result semantics

Distinguish successful observation, observed verification failure, refusal,
protocol/transport failure and incomplete verification. `NOT_REQUIRED` is allowed only
when the admitted verification plan genuinely permits it. Preserve typed reason codes
through retained result, events, CLI and UI.

### R05, retained task-result detail surface

Expose a discoverable retained result action in the Hub with three distinct things:
deterministic verdict, labelled worker answer/analysis and evidence/log detail. Worker
text is untrusted and cannot alter verdict styling/lifecycle. Results remain reachable
after resume and corrupt/missing artifacts stay explicitly unavailable.

### R07, effective execution contract

Build a typed immutable execution plan before admission. Bind effective resolved skill
bytes/references/tool scope, policy, command profile, relevant environment identity,
worker configuration and budgets. Repository/environment skill overrides must change the
identity. Hash or omit sensitive values. Drift before effects is refused or explicitly
re-admitted.

### R08, endpoint ownership and live client composition

There must be one endpoint queue/lease authority. Reconcile the duplicate scheduler
merged after the review with the existing `EndpointArbiter` / `EndpointRuntime` /
`EndpointCallAdapter` stack. Then wrap real conversation and worker calls with that same
authority, one endpoint identity vocabulary, truthful queue/lease/quarantine state and
explicit process-local versus cross-process support.

### R09, cancellation and bounded shutdown

Connect Session Hub input controls, cancellation runtime, task epoch fencing, endpoint
reconciliation and owned subprocess cleanup. Test queued cancellation, inference,
configured commands and terminal commit. Late results cannot produce a second terminal
record. Unconfirmed cleanup remains `NO_VERDICT`/unknown and is never replayed on restart.

### R13, installed-product acceptance and merge enforcement

Exercise the real public composition with deterministic fake inference and disposable
repositories/stores: clean install/upgrade, native Windows launcher, route-to-worker
skill selection, result retrieval, resume, feed failure and cancellation. Preserve
Linux/Windows authoritative pytest and frozen-contract guards. Repository-owner settings
must enforce required checks/current-base policy; a workflow file alone is not branch
protection.

### R10, incremental projections and resumable history

Advance projections only from new committed events, add close/unsubscribe lifecycle and
define bounded retention/snapshot/resume. Do not replace the current cap with an
unbounded list or silent truncation.

### R11, bounded repository discovery

Use one explicit ignore/discovery policy, exclude installed/generated trees before
traversal, bound work as well as rows and make large-file range reads actually supported
or refuse accurately.

### R12, explicit module/component names

After correctness boundaries settle enough to rename safely, run rename preflight, map
dependencies and retire unused/duplicate composition paths. One public orchestrator, one
public Session Hub composition, descriptive module names and no historical-PR naming.
This remains P1 even though it is intentionally sequenced after the higher-coupling
correctness repairs.

## Definition of done for every repair

1. State the user-visible failure and the invariant being restored.
2. Add a failing behavioural regression at the broken boundary before the repair.
3. Name one authority for every policy/state decision; no disconnected replacement
   implementation.
4. Run applicable native pytest plus compatibility coverage and require fresh
   direct-to-current-main CI. Never weaken a test to buy green.
5. Compute exact source provenance and verify frozen prompt/outcome identities. Frozen
   experiments are untouched.
6. Update current documentation in the same coherent change and record exact tested
   head/base/host/commands. Fake inference, real runtime and physical-device evidence are
   separate claims.
7. Merge only after the gate is satisfied, then verify the public workflow is reachable.
   A merged primitive is not a delivered feature.

Fewer completed paths beat another pile of disconnected foundations.
