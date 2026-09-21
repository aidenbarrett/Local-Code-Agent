# Product roadmap: a dependable local worker

Direction agreed: 2026-09-20. This is the product destination, delivery order and
acceptance plan. It is not a claim that the planned capabilities are implemented.
Root [CURRENT_STATE.md](../../CURRENT_STATE.md) owns current reachability and gaps;
live GitHub `main` owns current code. The existing
[Session Hub implementation sequence](session-hub-implementation-plan.md) remains
the immediate work. The [quality backlog](quality-hardening-roadmap.md) supplies
supporting fixes, not a competing product programme.

## Final ambition

Build a smart, useful agent that can do our recurring work: find things, move
things safely, understand a repository, handle Git, make bounded code changes,
run builds and tests, diagnose failures, and carry a task through to an honest
result. It should require little supervision for supported work and ask a
specific question when intent or authority is missing.

The target is a capable local worker. Competing on deep reasoning, frontier-model
benchmarks or general intelligence is not a product goal. A workflow that saves
real effort counts; extra model calls, abstractions and PRs do not count by
themselves. Models still need enough competence for their assigned jobs, and
verification only establishes its declared scope, not universal correctness.

> The model is a replaceable dependency. The agent is the product.

Users must be able to choose independence from cloud AI. After provisioning the
models, runtimes, tools and repository dependencies, every core local workflow
must work with external networking unavailable. Cloud services may be explicit
optional integrations; they must never be required or silently used as fallback.
Using cloud assistants to develop LCA does not introduce a cloud dependency into
the delivered product.

This is feasible as an incremental engineering project for Aiden and the AI
collaborators already working on it. That is an engineering judgment, not a
measured result or a delivery-date promise. Aiden owns priorities and physical
acceptance; implementation and review can be shared across assistants. Their
agreement is not verification. Real workflows, independent checks, hardware
acceptance and ongoing maintenance decide whether the product is useful.

## The work the product must do

These are release acceptance scenarios, not claims about today's conversation
path. Each scenario needs a normal case and a meaningful failure or ambiguity
case. Test realistic repositories as well as controlled fixtures.

| Job | Required behaviour | Acceptance evidence |
|---|---|---|
| Find things | Locate files, symbols, callers, configuration, relevant history and failure logs; explain with precise source references | Expected items found within the declared scope; freshness and missing coverage visible; no invented paths or references |
| Move and rename things | Preview affected paths and references, perform approved changes, update imports/build references where supported, verify | Exact intended diff; no unrelated edits or overwritten destinations; collisions, symlinks, case-only renames and interrupted moves handled; recovery respects subsequent user edits |
| Handle Git | Inspect status, staging, diffs, history and branch divergence; prepare branches and changes; resolve supported conflict cases and verify | User edits and staging preserved; correct base/ours/theirs identified; ambiguous intent stops for input; destructive/history-changing or remote actions require their own explicit authority |
| Change code | Implement bounded fixes, small features, tests and understood refactors using repository conventions | Scoped diff and independent build/test or review evidence; no weakened tests to manufacture a pass; unsupported language/refactor cases disclosed |
| Build, test and diagnose | Use trusted repository profiles, identify failures from real logs, attempt bounded repairs when authorised, rerun required checks | Current-tree proof with command, environment and scope; timeout, missing tool and incomplete output cannot become success |
| Continue useful work | Resume a conversation, explain an earlier failure, inspect artifacts and run a fixed watch job | Durable task references survive restart; old evidence stays historical; no duplicated effects; watch compares compatible runs |
| Work across hosts | Use Windows inference with a Linux SSH repository and the same visible task semantics | Correct host/root on every action and artifact; connection loss cannot duplicate work or claim clean cancellation |
| Remain replaceable and offline | Change a qualified model/runtime/device and repeat supported jobs without changing controller policy | Qualification report, preserved session identity and verification rules, bounded resource use, and a disconnected acceptance run |

Git and code expertise means reliable behaviour on declared operations and task
families. It is not permission to guess the user's intended merge resolution or
claim every possible coding problem is supported. Initial mutation support is
deliberately bounded and expands only after its workflow passes acceptance.

## Starting point and evidence limits

The planning baseline was inspected at `main`
`5dc0024973c076aab8f7dd6c6e749f4d7a9aa110`. Its `tests` and `serving` workflows were
successful when checked. This identifies the starting tree, not a certification
of the future roadmap or of every physical hardware combination.

The foundation includes a common inference client and model profiles, existing
server qualification probes, controlled tools and independent verification,
persisted conversations, durable Session Hub events, admission before effects,
and crash recovery to explicit unknown state. The conversation product path
still disables source mutation and commits. Durable follow-up association, richer
routing, full cancellation, endpoint arbitration and Textual integration remain
gaps at that baseline.

The current setup target is Windows/Panther Lake with Qwen3-8B and OVMS/OpenVINO.
The historical 30B/llama.cpp CPU experiments are separate evidence. Neither that
history, a profile name, nor green CI qualifies every feature on every model or
device. The support matrix must cite the exact qualification evidence.

## Ten product objectives

### 1. Model capability contract

Extend the existing client/profile boundary rather than create another model
abstraction beside it. Describe model and artifact revision, quantisation,
runtime/version, device, tokenizer/chat-template/tool-parser identity, effective
context and output limits, tool-call modes, structured-output support, thinking
controls, streaming, usage reporting, cancellation and timeout behaviour.

Keep declared, observed and unverified capabilities distinct. A server accepting
a parameter does not prove it honoured it. Admission uses the qualified effective
capabilities for the operation; malformed/unknown contracts fail closed.
Optional feature absence may select a documented, tested reduced mode, such as
non-streaming chat. Missing required tool support must not become arbitrary
shell or unconstrained text parsing. Experimental profiles stay visibly unqualified.

**Gate:** two genuinely different qualified model configurations on at least two
runtime backends complete their declared workflow set through the same controller.
A profile/adapter change must not alter permissions, evidence or verdict semantics.
Unsupported required features are rejected before effects. A common HTTP shape
alone does not establish interchangeable behaviour.

### 2. Managed runtime lifecycle

Make install, model download/import, start, stop, readiness checks, recovery and
model/device switching ordinary product actions. Record component identities,
verify downloads, check disk/RAM/device requirements, handle interrupted installs,
and retain an understandable recovery path. Verify redistribution/licence terms
before bundling third-party runtimes or weights.

Own the processes LCA starts. Never stop a user's unrelated server. Reuse only a
compatible healthy endpoint; distinguish process liveness from successful model
inference. Switch at a safe task boundary, clear incompatible caches, preserve
conversation identity, and qualify the selected configuration. A failed switch
must leave a usable previous configuration or an explicit stopped/refused state.

**Gate:** clean install, interrupted download, occupied port, missing dependency,
low resources, server crash/hang, owned restart, safe switching and uninstall are
exercised on every configuration labelled supported. No silent CPU/cloud fallback
when a requested NPU/GPU cannot run the chosen model.

### 3. Qualification and honest support matrix

Build on existing server probes with a versioned product qualification suite for
each model/runtime/device/OS/topology combination. A matrix cell is identified by
its exact model/artifact revision, quantisation, runtime and version, device and
driver stack, operating system/topology, tokenizer/chat template/tool parser,
context/output limits and relevant controller/execution-contract identity. A model
name by itself is never a qualified configuration.

Treat qualification as three separate layers rather than one aggregate percentage:

1. **Platform conformance.** Check transport and template behaviour, tool calls,
   structured output, streaming where advertised, malformed-response handling,
   context/output limits, timeout and cancellation semantics, restart/reconnect,
   endpoint ownership/quarantine, policy boundaries and deterministic verifier
   interaction. Required conformance checks target 100% execution; execution
   coverage and pass rate are reported separately. A skipped required check is not
   a pass.
2. **Agent capability.** Run representative Git, repository navigation, coding,
   diagnosis and verification tasks. These fixtures should retain enough difficulty
   and headroom to expose differences, so 100% completion is not itself the target.
   Report verified completion, refusals/unknowns, scope violations, tool calls,
   latency, model usage when observable, retries, escalation and interventions.
3. **Operational reliability.** Exercise long sessions, repeated inference,
   concurrent jobs, soak/memory-growth behaviour, mid-inference cancellation,
   subprocess/process-tree cleanup, endpoint quarantine and reconciliation,
   server crash/hang, runtime/model switching and restart recovery. Measure
   throughput, latency and peak memory; record power/energy per verified task only
   where trustworthy hardware telemetry and its provenance are available.

Separate deterministic adapter/negative tests from stochastic model task results.
Record every intended check as passed, failed, refused, unknown or not executed,
along with source, configuration, dependencies and artifact identities. Never let
an aggregate pass percentage hide missing execution. Known non-blocking failures
need an explicit disposition, affected surface, rationale, owner and exit condition.

Define blocker classes before release qualification. At minimum, controller or
policy fail-open behaviour, unsafe or unrelated mutation, stale proof accepted as
current proof, evidence/provenance corruption, false success, process/endpoint
ownership loss that permits further unsafe effects, hidden fallback, and recovery
that can duplicate effects block the affected supported capability. Model task
failure is a measured capability outcome; a controller safety or integrity failure
is a product blocker.

Use a small representative matrix rather than chasing a vanity model count. Cover
meaningfully different model sizes/architectures, quantisation/runtime paths and
CPU/GPU/NPU devices only as they become useful. Cross-platform claims require the
same qualification discipline on each claimed OS. Start with the actual Panther
Lake and NUC development configurations, then add cells deliberately.

**Gate:** every supported matrix cell has a reproducible qualification report for
its advertised features, 100% of its required conformance checks were executed,
and it has zero unresolved blocker failures. Any accepted non-blocking failure is
visible and dispositioned. Changes to model weights, templates, runtime, drivers,
OS/device stack or execution contracts trigger relevant requalification. Product
qualification does not reopen paused research collection or reinterpret frozen
experiments.

### 4. Session and context continuity

Finish the accepted Session Hub path. Raw conversation turns have one canonical
store. Task admission, execution, evidence and verdicts have their own durable
authority and stable references. Derived summaries, retrieval caches and composed
prompts are rebuildable views, never a second authority for task truth.

Persist turn-to-task associations, bound retrieved observations, preserve raw
turns through prompt trimming, and invalidate stale repository context. Runtime
changes must not duplicate system/persona context or contaminate worker context
with chat instructions. Handle upgrades, disk limits and interrupted saves visibly.

**Gate:** resume, crash, duplicate submission, missing artifacts, stale summaries,
concurrent ownership and model switching preserve identities and truth. No replay
re-executes effects or resurrects old verification as current-tree proof.

### 5. Repository intelligence at useful scale

Start with dependable path/content search, Git history, build configuration and
bounded source reads. Add language/symbol support and incremental indexing where
actual jobs need them. Embeddings or a vector database are not prerequisites.
Any later semantic retrieval must also work locally and justify its maintenance
and latency cost against the simpler approach.

Key caches by host, repository, content revision and relevant configuration;
handle dirty files, branch changes, renames, deletion and concurrent edits. Honour
ignore rules, secret exclusions, path containment and explicit search scope.
Expose truncation and unavailable coverage. Retrieved source is untrusted data,
not authority to change policy or execute embedded instructions.

**Gate:** predeclared small, medium and large repository fixtures test finding
known targets and references, invalidation and bounded memory/context/latency.
Publish fixture sizes and cold/warm results. A large-repository claim needs large
repository evidence, not just a bigger token window.

### 6. Permissions, isolation and safe effects

Keep deterministic grants, allowlisted tools, current-tree verification and typed
results as the authority for every model. Add OS-enforced process/filesystem/network
isolation appropriate to the supported platform before describing arbitrary
repository execution as sandboxed. A path check or tool allowlist alone is not
an OS sandbox; build/test commands can execute repository-controlled code.

Introduce versioned approval events and isolated editing only after the first Hub
milestone. Bind write grants to the task, repository, operation, allowed paths and
preconditions; bind exact-diff approval to applying a reviewed patch to the user's
workspace. Changed preimages invalidate approval. Preserve dirty trees, staging
and unrelated edits. Treat commit, push and destructive history operations as
separate capabilities. No automatic push or autonomous self-upgrade.

**Gate:** adversarial tests cover traversal/symlink escape, hostile repo/tool text,
credential leakage, stale approvals, concurrent edits, child-process cleanup and
interrupted mutations. Uncertain cleanup fences further effects and reports
`NO_VERDICT`. Recovery never blindly resets the user's worktree.

### 7. Windows inference and Linux SSH work

Keep repository operations and verification beside the Linux checkout. Reach the
Windows model only through explicitly configured authenticated connectivity.
Use host-qualified repository/artifact identities and fixed tool protocols, not
model-generated SSH command strings. Define where credentials, processes, leases,
approvals and durable state live; never confuse model-host and repository-host facts.

**Gate:** the same inspect/build/test and later approved-edit scenarios pass locally
and over SSH. Wrong-host paths, reconnect, lost responses, remote process survival,
timeouts and cancellation cannot broaden authority or duplicate effects. Repository
content stays within the user's declared trust boundary, including inference hosts.

### 8. A proper VS Code experience

Finish the in-process Textual client first. Then ship the
[VS Code client](vscode-client-design.md) as a second client of the same service,
not a second controller. Support local and Remote SSH workspaces, conversation,
visible activity/queue state, deterministic results, logs/diffs, scoped approvals,
cancellation and recovery. Keep setup and supported model selection understandable.

**Gate:** install a built VSIX, choose a workspace/model, complete the representative
jobs, reload the extension and reconnect without losing task truth or repeating
effects. UI actions cannot manufacture approval or verification. Both clients
must agree on task identity and result semantics.

### 9. Packaging and a tested offline guarantee

Ship complete versioned artifacts containing skills, schemas, UI resources and
dependency metadata. Support online provisioning and documented offline import
of an integrity-checked bundle. After provisioning, no account login, licence
heartbeat, telemetry upload, update check, cloud embedding or package/model
download may be required for core operation. Optional network actions must be
explicit and cannot block offline startup.

**Gate:** start fresh installed processes with external network interfaces disabled
on a single host, retain loopback for local inference, and complete chat, find,
Git inspection, approved move/edit, build/test, resume, watch and switching among
already-present models. Monitor attempted egress as well as observed traffic.
Repository toolchains and build dependencies must be provisioned for that test.

Test the remote topology separately on an isolated LAN with WAN access denied.
SSH requires connectivity to its host; it cannot work with every interface
disconnected. Fetch, push, missing downloads and other inherently networked
operations must report unavailable precisely. Verify install, upgrade, rollback
and uninstall preserve user data and never require edits inside `internal/`.

### 10. Optional local model routing

Make a single qualified model useful first. Later allow small/fast, coding and
larger-capacity models to coexist when hardware permits, with explicit unload/load
when it does not. A 30B model is a possible profile, not a required dependency.
Model size is not proof of suitability, and memory fit is not proof of good latency.

Use deterministic capability, resource, queue and task-budget rules before more
complex scheduling. Prefer no model call for a fully specified deterministic job.
Record selection and bounded escalation; the user can pin a model. No model can
award itself a passing verdict, widen its grants or silently call a cloud tier.

**Gate:** routing improves a declared user outcome such as task time or interventions
against a pinned single-model baseline on the same workflow set, without worse
correctness or policy behaviour. If it does not, keep it optional and retain the
simple default. Resource exhaustion and unavailable tiers have explicit outcomes.

## Delivery milestones

These are dependency gates, not calendar promises or renamed PR numbers. Supporting
work can proceed when useful, but shipping a workflow takes priority over building
all ten objectives in full before anyone can use the product.

| Milestone | Deliverable | Exit gate | Objectives advanced |
|---|---|---|---|
| P1: useful Session Hub | Durable follow-ups, deterministic routing, fixed watch, cancellation/endpoint ownership, Textual and live wiring in the current accepted order | Physical-laptop inspect/check/watch/resume/cancel flow; activity and verdicts are honest; no repeated effects | 4; foundations of 2, 3, 6, 8 |
| P2: dependable daily worker | Find/Git inspection, scoped move/rename, isolated bounded code fixes, review/apply and verification | Real daily jobs completed end to end; negative cases preserve user work; versioned write approvals and containment gates before writes | 5, 6; continued 3, 4 |
| P3: easy setup and replacement | Managed runtime lifecycle, effective capability contracts, first supported matrix, complete install/import artifacts | Clean setup to useful work; a second model/backend substituted; offline single-host scenarios pass | 1, 2, 3, 9 |
| P4: remote and editor workflow | Windows inference with Linux SSH execution; VS Code client over the same service | Real Remote SSH workflow, built VSIX and reconnect/cancel acceptance on both hosts | 7, 8; continued 6 |
| P5: release quality and scale | Large-repo qualification, soak/stress/failure-injection coverage, resource/performance budgets, recovery/upgrade support, full offline release checks | Published supported matrix and release acceptance pack; 100% required conformance execution, zero unresolved blockers, and every advertised workflow satisfies its declared gate | 3, 5, 9; regression of 1-8 |
| P6: useful multi-model choice | Optional local routing and resource-aware model residency | Same-task comparison demonstrates practical benefit; pinned single-model path remains supported | 10; regression of 1-9 |

Offline behaviour, host identity, replaceable models and safe effects are design
constraints from P1, even where full acceptance arrives later. Basic runtime
readiness belongs with the first usable workflow; broader lifecycle management
must not block finishing that workflow. P6 is part of the final ambition but is
not a prerequisite for releasing a useful single-model product.

The immediate next work remains the order in `CURRENT_STATE.md`: durable
turn-to-task follow-ups, deterministic routing/fixed watch, process ownership and
endpoint/cancellation semantics, fixture-driven Textual, then live physical
acceptance. Do not replace those slices with a new generic agent framework.

## Quality, efficiency and the definition of finished

For each shipped workflow, record verified completion within its scope, failure,
refusal and unknown outcomes; human interventions; time to a useful result; model
calls/tokens where actually reported; and peak memory/disk use. Where trustworthy
device telemetry exists, also record power/energy with enough provenance to make
comparisons reproducible. Deterministic operations should bypass inference when
the user's request is already explicit. Optimise avoidable calls, repeated reads
and invalid caches before adding machinery.

Keep release/product exit separate from capability benchmarking. Release exit is
about the integrity of the platform and its advertised workflows: required checks
must execute, blockers must be zero, and known non-blocking failures must be
explicitly dispositioned. Capability benchmarks should remain difficult enough to
measure useful differences between models, skills and routing; forcing those tasks
toward 100% would make the benchmark less informative rather than the product safer.

Before a milestone's acceptance run, specify its task set, supported configuration,
minimum successful completion and maximum intervention counts, repeat policy,
latency/resource budgets and required checks. Report both execution coverage and
pass rate. A refused supported positive case does not count as a completed job. An
expected refusal in a negative case may pass that safety check. Never hide failure
behind a green aggregate or treat a few successful runs as a population-wide
reliability estimate.

Use Aiden's actual recurring work to set those budgets. Publish observations and
remaining gaps separately from targets. Research comparisons, if resumed later,
need their own approved generation and methodology; operational acceptance must
not silently become a causal model/skills claim.

Keep one policy authority, one verification authority and one durable authority
for each kind of state. Prefer an explicit adapter to a forked orchestrator. Add
contracts at real boundaries, remove obsolete paths when replacements land, and
record migration/rollback for persistent state. Known correctness defects block
the affected capability; bounded debt needs an owner and an exit condition.

A milestone is complete only when its implementation is merged, required CI has
passed for the relevant revision, installed/public-path acceptance has happened
on the claimed configurations, and current-state/support documentation cites the
evidence. Interface skeletons, model prose, reviewer agreement and PR counts are
not completed features. This roadmap edit changes no code, frozen evidence,
experimental prompt or outcome contract.