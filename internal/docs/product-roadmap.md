# Product roadmap: dependable local engineering worker

This is the product destination, not an implementation claim. Live source owns implementation truth; `CURRENT_STATE.md` owns the immediate state and `TRICKS.md` owns first-release user journeys.

## Final ambition

Build a local-first engineering worker that can find things, understand repositories, handle Git, make bounded changes, build/test, diagnose failures and carry supported work through to independently verified results.

The model is a replaceable dependency. The deterministic controller owns authority, execution state, verification and recovery. Core supported workflows must work without cloud AI after the local runtime, model and repository dependencies are provisioned.

## Delivery order

### P1: finish the useful read-only Session Hub

Make normal repository inspection, symbol lookup, branch review, build/test and failure follow-up work through ordinary conversation. Runtime/model/device facts shown in the UI must come from owning records, not profile-name inference.

Exit gate: the public installed product completes the first-release journeys in `TRICKS.md`, including restart and failure cases, with truthful durable results.

### P2: close lifecycle and cancellation

Compose one endpoint ownership path, one task lifecycle authority and real cancellation. User Stop must fence further work immediately and only claim stopped when owned inference/process cleanup is observed. Unknown cleanup remains unknown.

Exit gate: queued work, active model requests and owned child processes are reconciled without duplicate effects or false completion.

### P3: safe mutation

Add scoped file/Git mutation only after the read-only worker is trustworthy. Preserve dirty worktrees, staging and unrelated edits. Preview scope, bind approval to exact preconditions, independently verify resulting state, and keep commit/push/history-changing actions as separate capabilities.

Exit gate: adversarial mutation journeys produce exactly the intended diff, survive interruption safely and never overwrite unrelated user work.

### P4: reusable tasks and Watch

Turn a proven successful task into a reviewed structured specification containing goal, inputs, tool/capability scope, procedure, verification and trigger. Do not replay old chat text as automation authority.

Exit gate: a user can create, inspect, run and stop a reusable task without weakening controller policy or recovery semantics.

### P5: second client and remote topology

After the Session Hub is solid, add VS Code/Remote SSH as another client of the same controller/state service, not another controller. Repository operations remain beside the repository; model inference may live on another explicitly configured host.

Exit gate: local and remote clients agree on task identity, authority, evidence and result semantics through reconnects and failures.

### P6: packaging and offline acceptance

Produce a complete installable package and integrity-checked offline provisioning path. No hidden network fallback, package download, telemetry upload or login may be required for core local workflows after provisioning.

Exit gate: a fresh installed product completes supported journeys with WAN access unavailable and attempted egress observed.

## Runtime and model qualification

Support is a property of an exact configuration: model/artifact revision, runtime/version, device, effective context/output limits and relevant parser/template behavior. A model name or HTTP-compatible endpoint alone is not enough.

Qualification should separate:

1. protocol/product conformance
2. useful agent capability on representative tasks
3. operational reliability such as restart, long-session behavior and cancellation

Runtime performance comparisons use `internal/perf/endpoint_harness.py`. The harness stays backend-agnostic; runtime-specific launch and observation hooks stay in local profile configuration. Compare JSON outputs, not anecdotes.

## Repository intelligence

Prefer simple bounded path/content/Git search first. Add symbol indexes, incremental parsing or semantic retrieval only when a named user journey benefits and the extra infrastructure earns its cost.

Retrieved repository content is untrusted data. It cannot alter policy or authorize effects.

## Product laws

- One state/policy decision has one owner.
- The model proposes; deterministic code controls effects and proof.
- Unknown stays unknown.
- Verification covers only its declared task, scope and repository state.
- Recovery must never replay uncertain effects automatically.
- User edits and Git state are protected by default.
- No arbitrary model-generated shell, hidden cloud fallback, automatic push or autonomous self-upgrade.
- UI status is a projection of authoritative state, never decorative optimism.

## Historical research

Earlier experiment generations, methodology documents and collected evidence are archived off the active branch. They do not participate in current CI or product claims. The exact pre-cleanup tree is preserved on `archive/legacy-experiments-2026-09-26` at `ad07af071a62acfa4d0168c7a89d7110a52088f6`.
