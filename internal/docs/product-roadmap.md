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


## Engineering expertise pillars

Routine engineering expertise belongs in the product, not in the luck of what the current
model happens to remember. A qualified small local model should receive the mechanical
facts, bounded specialist guidance and safe capabilities needed to behave systematically.
The model is used for judgement; deterministic code owns state discovery, effects, safety
policy and proof.

### Git Expert

Git is a first-class engineering subsystem, not arbitrary shell access and not a bag of
prompt recipes. The target user experience is outcome-oriented: requests such as
`sort out this branch`, `where did my commit go?`, `resolve these conflicts` or
`get me safely onto current main` should begin with deterministic inspection rather than
requiring the user to name Git commands.

The Git authority should provide a structured repository-state model covering, where
relevant:

- HEAD, branch, upstream, ahead/behind and merge-base identity;
- committed, staged, unstaged, untracked and ignored state without conflating them;
- merge, rebase, cherry-pick, revert, bisect and other interrupted-operation state;
- conflicts with base/ours/theirs identity where Git can establish it;
- refs, reflog, stash, worktrees, submodules and remote-tracking facts needed by the task;
- recoverability facts before any destructive or history-changing proposal.

Problem classification selects bounded procedure and reference knowledge; it does not grant
authority. The model may explain ambiguity, choose among semantically valid conflict
resolutions and propose a recovery plan. Deterministic policy still separates read-only,
worktree mutation, index mutation, commit, ref/history mutation and push authority.

Initial expert acceptance families include:

1. ordinary and add/add merge conflicts;
2. rebase/cherry-pick conflicts and interrupted operations;
3. branch divergence and upstream/tracking mistakes;
4. detached HEAD and worktree confusion;
5. lost/deleted commits recovered through reflog or reachable refs;
6. stash conflicts and staged-versus-working-tree confusion;
7. ref namespace/lock failures and stale lock diagnosis;
8. submodule/worktree state where the repository actually uses them;
9. safe branch cleanup with recoverability established first;
10. refusal of destructive recovery when preconditions or authorization are absent.

Every recovery ends by re-inspecting Git state and, when source changed, invoking the
appropriate independent build/test verification. A plausible model explanation is never
proof that Git was repaired.

### Build -> Run -> Debug -> Repair -> Verify

Build/test support grows into one reusable engineering loop rather than repository-specific
scripts embedded in prompts.

1. **Discover.** Establish the repository's declared/configured build, test and run
   capabilities, environment/toolchain requirements and relevant targets.
2. **Execute.** Deterministic controller actions run configured mechanical operations.
   A model is not asked to remember to call a build or test tool.
3. **Observe.** Capture command identity, exit status, bounded logs, artifacts and exact
   repository/tree identity.
4. **Classify.** Deterministic signatures identify known classes such as configure,
   compile, link, test, launch, timeout, dependency, environment and infrastructure
   failures. Unknown remains a valid class.
5. **Retrieve.** Supply only relevant repository instructions, specialist references and
   known failure guidance. Retrieved text is untrusted data and cannot authorize effects.
6. **Diagnose.** The model reasons over observed facts and retrieved guidance, requesting
   further bounded inspection when necessary.
7. **Repair.** Prepare source/configuration changes through the existing isolated-candidate
   authority. Environment or repository-state changes use their own explicit capability.
8. **Verify.** Re-run the exact failed operation and the task-appropriate wider checks.
   Proof is bound to the resulting tree and declared scope.
9. **Present.** State what failed, what changed, what passed, what remains unknown and what
   authority is needed next.

This loop must handle compiler/linker diagnostics, failing tests, launch/runtime failures,
logs and hung/failed processes without becoming an unrestricted shell agent.

### Project knowledge and progressive disclosure

Repository-specific know-how is a layer over the generic engines. A project may describe
how to configure targets, build firmware, run tests/simulators, locate useful logs and
recognise project-specific success/failure evidence. The controller exposes only the
relevant slice for the current task.

The NPU firmware repository is a proving ground for this design, not a special-case product
dependency. Lessons that generalise become generic Git/build/run/debug capabilities.
Intel-proprietary source, logs and artifacts remain on the authorized machine; product
improvements are driven by non-sensitive behavioural findings rather than copied IP.

Do not create one skill per command or failure signature. Prefer a small number of owning
engines with typed observations, progressively disclosed knowledge and explicit
capabilities.

### Qualification

Expertise is measured adversarially. Maintain synthetic/public torture fixtures for dirty
worktrees, conflicts, interrupted operations, lost commits, broken configure/build/link/test
runs, hung processes, stale evidence and failed repairs. Score at least:

- diagnosis/classification correctness;
- preservation of unrelated user state;
- authorization correctness;
- successful recovery where a supported recovery exists;
- refusal/UNKNOWN correctness where proof or authority is absent;
- independent post-repair verification;
- unnecessary model/tool work and elapsed time.

A larger model getting lucky on a fixture does not replace a deterministic product
capability. A smaller model passing because LCA supplied better facts and procedure is the
intended architecture.


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
