# Quality Hardening Roadmap: target 9/10

Status: active hardening plan

Scope: Local Code Agent execution substrate, evaluation harness, measurement system,
portable runtime, skills, tooling, RPC surface, packaging and the future workload
scheduler interface.

This document is deliberately stricter than a normal project backlog. A green test
suite is necessary but not sufficient. The goal is a cross-platform agent substrate
that is boring to operate, difficult to fool, easy to audit, and safe to build a
self-improving workload scheduler on top of.

## Non-negotiable quality policy

1. No area may be rated below 7/10 on `main`. A score below 7 is a release blocker,
   not accepted technical debt.
2. The project target is 9/10 across every material area. A score of 9 means there
   is executable evidence for the claim, not merely good structure or documentation.
3. New functionality does not compensate for reliability debt. A new capability
   cannot raise the score of an area whose failure modes are untested.
4. Every bug that reaches a developer must, where practical, become a regression
   test that reproduces the failure before the fix.
5. Cross-platform claims require cross-platform CI. Manual confirmation is useful
   evidence, but it does not replace automated Linux and Windows gates.
6. Model-facing and experiment-facing behavior is treated as a contract. Changes
   must be deliberate, versioned and provenance-visible.
7. Telemetry used by future routing or prediction code must be semantically truthful.
   Unknown values remain unknown; estimates must be labelled as estimates; proxies
   must never be renamed as the quantity they approximate.
8. The dispatcher must remain a separate responsibility from the execution engine.
   The orchestrator executes a workload against a selected target. The scheduler
   selects the target. Telemetry is the bridge between them.

## Scoring rubric

A score is reassessed only when evidence changes.

- **9-10**: clear boundaries, adversarial tests, platform coverage, typed failures,
  reproducible behavior, documented invariants, no known correctness debt.
- **8-8.9**: strong production-quality design with limited, understood debt that is
  tested or constrained.
- **7-7.9**: acceptable but contains material maintainability, portability or
  observability debt. Allowed temporarily, never the target state.
- **<7**: release blocker under this policy.

The score is not derived from line coverage alone. Coverage, mutation score, CI
matrix, adversarial tests, code structure, reproducibility and known defect surface
all contribute.

## Baseline scorecard

This is the hardening baseline. It assumes the current Windows path-length defect is
fixed correctly and does not count that active repair twice.

| Area | Baseline | Target | Primary gap |
|---|---:|---:|---|
| Core architecture | 7.7 | 9.2 | dual Orchestrator definition and large central modules |
| Verification/correctness | 9.1 | 9.5 | preserve simplicity; add property/mutation tests |
| Adversarial testing | 9.2 | 9.5 | broaden fault, concurrency and parser attack surface |
| Linux/Windows portability | 6.8 | 9.2 | Windows absent from authoritative CI |
| Maintainability | 7.1 | 9.0 | several monolithic files and duplicated operational logic |
| Model/runtime abstraction | 8.0 | 9.2 | target identity and telemetry semantics need formalization |
| Telemetry/measurement | 7.5 | 9.3 | chunk/token ambiguity, reset semantics, schema versioning |
| Packaging/release | 5.8 | 9.0 | wheel does not carry built-in skills; no clean-install gate |
| Reproducibility/provenance | 8.6 | 9.4 | model-facing hash is not fully transitive/declarative |
| Dispatcher readiness | 7.5 | 9.0 | target contract, calibration, feedback-bias controls not formalized |
| RPC/editor boundary | 6.5 | 9.0 | synchronous prototype protocol, no version/cancel/schema |
| CI/release engineering | 6.8 | 9.2 | one OS, non-authoritative runner used as authority |

Scores below 7 are the first work items. The immediate objective is to remove all
red-zone scores before adding significant new features.

---

# Workstream A: authoritative cross-platform CI

Priority: **P0**

### A1. Make real pytest authoritative

Problem: CI installs pytest but uses `measurement/run_test_suite.py` as the primary
suite runner. The compatibility runner intentionally implements only a subset of
pytest semantics and gives `tmp_path` different behavior from real pytest. That
already hid a real Windows path-length defect.

Actions:

- Run `python -m pytest` as the authoritative CI suite.
- Keep `measurement/run_test_suite.py` as an offline/air-gapped compatibility path.
- Add a dedicated compatibility-runner CI job so it cannot silently rot.
- Document that a compatibility-runner pass does not override a real-pytest failure.

Acceptance evidence:

- Linux real-pytest job green.
- Windows real-pytest job green.
- Compatibility runner green on its supported subset.
- A regression test proves a semantic mismatch in the compatibility runner cannot
  make authoritative CI green.

### A2. Add Windows GitHub Actions

Actions:

- Add `windows-latest` with Python 3.12.
- Print CMake, CTest, compiler and Python versions in both OS jobs.
- Run the same source tree and same authoritative test command on Linux and Windows.
- Preserve the generator-idempotence and provenance guards on both platforms where
  practical; where a guard is platform-independent, one canonical job may own it.
- Cache only dependencies, never generated fixture state that could hide a defect.

Acceptance evidence:

- A PR cannot merge if either OS fails.
- At least one portability regression test is demonstrated to fail on one OS before
  its fix and pass afterward.

### A3. Add Python-version policy

Actions:

- State supported Python versions explicitly.
- Test the minimum supported Python and the primary development Python.
- If only 3.12 is truly supported during this phase, narrow the package declaration
  instead of claiming broader compatibility without evidence.

---

# Workstream B: one authoritative Orchestrator

Priority: **P0**

Problem: `local_agent.agent.orchestrator.Orchestrator` is a usable base class while
`local_agent.agent.contracts.Orchestrator` adds load-bearing hardening. The package
exports the hardened class, but direct imports can silently bypass it.

### B1. Collapse the dual implementation

Preferred end state:

- one public production `Orchestrator`, or
- an unmistakably private `_BaseOrchestrator` plus a single public hardened class.

Actions:

- Search all imports and instantiate only the authoritative production class.
- Make bypassing hardening difficult by construction rather than by convention.
- Preserve experiment semantics and base-prompt identity deliberately.
- Add an import-contract test asserting all supported public import paths resolve to
  the same hardened implementation.

Acceptance evidence:

- No supported import path can instantiate an unhardened orchestrator accidentally.
- Existing condition-purity, oracle-isolation, citation and fail-closed tests remain
  green unchanged in intent.

### B2. Decompose oversized agent modules after behavior is frozen

Candidate split for `orchestrator.py`:

- execution loop
- answer/evidence contract
- escalation/restoration
- reporting/transcript formatting

Do not refactor merely to reduce line count. Split only on stable responsibility
boundaries with characterization tests first.

---

# Workstream C: telemetry must be mathematically honest

Priority: **P0** because future scheduling decisions will learn from these values.

### C1. Stop treating stream chunks as tokens

Problem: streaming stall detection and fallback completion accounting use stream
chunk count as a token proxy. Chunk boundaries are backend-specific and can contain
text fragments, multiple tokens or partial tool-call JSON.

Actions:

- Introduce explicit measurement provenance for counts, e.g. `server`, `tokenizer`,
  `estimated`, `unavailable`.
- Do not name chunk-rate `tok/s`.
- If a backend supplies no usage, either tokenize with a pinned tokenizer or report
  token throughput as unavailable.
- Keep chunk-rate as a separate transport-health metric if it remains useful.
- Update stall logic to use a semantically valid progress metric.

Required tests:

- one chunk containing multiple textual tokens;
- many chunks containing one fragmented tool call;
- usage-present and usage-absent streams;
- mid-stream timeout;
- reasoning-only chunks;
- empty usage chunks.

### C2. Reset all tier telemetry state

Problem: `TieredClient.reset_stats()` resets tier counters but can leave discarded
cheap-call state from the prior run.

Actions:

- Reset every run-scoped metric, including discarded calls.
- Add a two-run regression test: first run escalates, reset, second run does not;
  second report must contain zero stale discard state.

### C3. Version telemetry schemas

Actions:

- Add a telemetry/result schema version.
- Separate observed, derived and estimated fields.
- Preserve raw observations needed to recompute derived metrics.
- Reject or explicitly migrate incompatible historical rows.
- Add round-trip serialization tests.

### C4. Define target identity structurally

Replace eventual reliance on free-form `device_note` for scheduler decisions with a
structured target identity containing at least:

- stable target id
- device class (`cpu`, `gpu`, `npu`, `cloud`)
- runtime and version
- model and quantization
- context limit
- tool parser
- thinking policy
- host/machine identity relevant to performance

Human-readable notes may remain, but routing logic must not parse them.

---

# Workstream D: configuration correctness

Priority: **P0/P1**

### D1. Complete ModelConfig environment semantics

Problem: `ModelConfig` exposes many behavior-changing fields, while `from_env()`
loads only a subset.

Actions:

- Decide field by field whether environment override is supported.
- Support every documented override or explicitly mark the field fixed by profile.
- Validate enums/ranges: tier, runtime, temperature, probabilities, timeouts, context
  budgets and token budgets.
- Fail clearly on malformed environment values.
- Include effective configuration in run identity.

Tests:

- table-driven test over every overridable field;
- malformed integer/float/bool;
- invalid probability/range;
- precedence: preset -> environment -> explicit CLI override where applicable;
- round-trip identity contains the effective value.

### D2. Strict repository config validation

Actions:

- reject unknown high-risk config keys rather than silently ignoring typos;
- validate empty commands and invalid profiles;
- validate run/build paths early;
- add Windows path-budget validation where needed;
- test relative, absolute, symlinked and malformed paths.

---

# Workstream E: tool contracts, sandboxing and process isolation

Priority: **P1**

### E1. Fail closed on unknown schema requests

`ToolRegistry.schemas(names)` should not silently omit a requested name. A requested
but unregistered tool indicates a configuration defect.

Tests:

- unknown requested schema raises a typed configuration error;
- duplicate registration remains rejected;
- skill declaring an unknown tool fails before model execution.

### E2. Preserve typed status when tool results are truncated

`ToolResult.to_json()` must preserve at least execution status, domain result and
reason in the truncated representation.

Tests:

- oversized PASS, FAIL, BLOCKED and ERROR results retain typed semantics;
- artifact handles survive truncation;
- truncation cannot turn UNKNOWN into PASS or FAIL.

### E3. Kill subprocess trees, not only parents

Actions:

- POSIX: use process groups/session management and terminate/kill the group.
- Windows: use an appropriate process-tree mechanism, ideally a Job Object or a
  tested equivalent.
- Ensure timeout cleanup cannot leave CMake/MSBuild/compiler/test children alive.

Tests:

- child process that spawns a grandchild;
- timeout leaves no descendant running;
- log files are finalized;
- next test run sees clean state.

### E4. Property-test path sandboxing

Use generated paths to test:

- `..` traversal
- absolute paths
- redundant separators
- `.` components
- symlink escapes
- Unicode names
- Windows separators and drive-letter forms
- very long paths
- protected subtrees

Invariant: no write-capable tool can resolve outside the intended source surface.

---

# Workstream F: evaluation harness decomposition and validity

Priority: **P1**

### F1. Split `run_evaluation.py` by stable responsibilities

Target modules, subject to characterization tests:

- `fixture.py`: prepare/copy/scenario setup
- `preconditions.py`: establish required initial state
- `execute.py`: construct client/orchestrator and run
- `scoring.py`: pure row scoring and validity
- `artifacts.py`: transcripts/checkpoints/serialization
- `run_evaluation.py`: CLI and high-level orchestration only

Acceptance evidence:

- frozen characterization fixtures produce byte-equivalent or deliberately migrated
  rows before and after the split;
- base prompt and source provenance moves are understood and recorded;
- no scoring rule is duplicated.

### F2. Pure scoring core

Make scoring as close as possible to a pure function over recorded facts. The live
run should collect facts; the scorer should determine success from those facts.

Benefits:

- offline rescoring;
- mutation testing;
- property testing;
- fewer accidental side effects;
- easier audit of scheduler training labels later.

### F3. Harness-invalid paths stay outside capability denominators

Keep and expand tests for:

- fixture failure
- precondition failure
- agent-internal crash before model call
- oracle failure
- server failure
- environment block

No invalid infrastructure run may silently become a model FAIL.

---

# Workstream G: skill system hardening

Priority: **P1**

### G1. Replace permissive metadata parsing with strict validated schema

The current minimal YAML-like parser is sufficient for controlled experiments but
is too permissive for organization-authored skills.

Actions:

- define a versioned skill metadata schema;
- reject unknown keys;
- validate `tier`, `escalation`, `tools`, `verification`, references and scripts;
- reject duplicate skill names at the same precedence level;
- fail if advertised references/scripts are missing;
- preserve deterministic override precedence.

### G2. Skill contract tests

For every built-in skill:

- all declared tools exist;
- only intended tools are exposed;
- verification requirement matches the task type;
- escalation policy is valid;
- references cannot escape skill directory;
- discovery is deterministic across Linux and Windows.

### G3. Keep deterministic router as baseline, not scheduler

The keyword router is useful as a cheap, inspectable routing baseline. Do not turn it
into the future efficiency engine by incremental conditionals. The future scheduler
gets its own package and contract.

---

# Workstream H: provenance and reproducibility

Priority: **P1**

### H1. Make model-facing contract hashing explicit

Problem: `base_prompt_sha256` hashes selected source functions. A transitive helper
could change model-visible behavior without the selected function's source changing.

Actions:

- define a declarative model-facing contract manifest or broaden the hashed surface;
- include serialization behavior that affects messages and tool results;
- add a test that mutating each known model-facing component moves the contract hash;
- add a test that non-model-facing refactors do not move it unnecessarily.

### H2. Dependency lock/constraints for reproducible measurement

Actions:

- retain broad package compatibility if desired, but provide a tested constraints or
  lock file for experimental runs;
- record OpenAI SDK and HTTP-stack versions;
- record CMake/compiler versions in experiment identity where they can affect output;
- build a clean environment from the lock in CI.

### H3. Release artifact verification

Every release artifact should prove:

- source hash matches declaration;
- packaged skills are present;
- CLI starts from a clean environment;
- no repository-layout dependency is required unless explicitly documented.

---

# Workstream I: RPC/editor boundary

Priority: **P0** to lift the current <7 score, then P1 for 9/10.

### I1. Version the stdio protocol

Add:

- protocol version handshake;
- request/response schema validation;
- stable typed error codes separate from prose;
- request ids guaranteed not to bleed between runs.

### I2. Approval protocol state machine

The current approval path consumes the next stdin line synchronously. Replace this
with explicit request correlation before concurrent editor use.

Tests:

- wrong request id;
- malformed approval;
- disconnect while waiting;
- cancellation while approval pending;
- two queued requests;
- unsolicited approval;
- EOF.

### I3. Cancellation and bounded execution

Editor/automation callers need a supported cancellation path that cleans up child
processes and leaves the worktree in a known state.

### I4. RPC must drive the same production Orchestrator

Keep this invariant. No extension-specific agent logic.

---

# Workstream J: packaging and installation

Priority: **P0** because baseline is below 7.

### J1. Make wheel/install artifact complete

Built-in skills must be packaged as package data or another explicit installed
resource.

Acceptance test from a clean environment:

1. build wheel;
2. create fresh virtual environment;
3. install only the wheel plus declared dependencies;
4. `local-agent skills` lists every built-in skill;
5. `local-agent doctor` works against a fixture repo;
6. no checkout-relative path is required.

### J2. Release archive and wheel parity

Test that both distribution forms expose the same built-in skill set and core
behavior.

### J3. Versioning policy

Introduce an explicit package/instrument versioning rule before external users depend
on the CLI/RPC/schema contracts.

---

# Workstream K: test-suite engineering

Priority: **P1**

### K1. Coverage is a diagnostic, not the objective

Add statement and branch coverage. Suggested initial gates:

- critical pure-contract modules (`verification`, policy/contract code): >=95% branch;
- core `local_agent`: >=90% branch;
- repository overall: >=85% branch.

Raise only when the missing branches are meaningful. Never add empty tests to chase a
percentage.

### K2. Mutation testing on critical pure logic

Run mutation testing on Linux for:

- verification classification
- policy decisions
- scoring/validity
- routing state and telemetry math
- config validation

Target initial mutation score >=80%, then >=90% for critical contract modules.
Surviving mutants become concrete test-backlog items.

### K3. Property-based tests

Introduce Hypothesis for functions with crisp invariants:

- path resolution/sandboxing
- serialization round trips
- proof classification
- configuration parsing
- skill metadata parsing
- task/tool schema handling
- citation/evidence ids
- telemetry arithmetic

Persist discovered counterexamples as regression examples when they reveal important
classes of defect.

### K4. Fuzz parsers and protocol edges

Fuzz:

- tool-call JSON
- streamed tool-call fragments
- skill metadata
- CTest/compiler log parsers
- stdio RPC messages
- result serialization

A malformed external input must never crash the process without a typed diagnostic.

### K5. Concurrency/reentrancy tests

Run tests and agent instances concurrently to expose hidden shared state:

- simultaneous pytest sessions;
- simultaneous run directories;
- multiple `TieredClient` instances;
- RPC request interleaving once supported;
- unique temp paths and cleanup.

### K6. Repeatability/flakiness gate

Critical deterministic suites should pass repeatedly, e.g. 20-100 repetitions in a
scheduled job, with zero flakes. A flaky test is a defect in the test or product until
shown otherwise.

### K7. Keep historical adversarial rationale

Large adversarial test files may be split by invariant, but preserve the comments
that explain the original false-positive mechanism. They are part of the safety case.

---

# Workstream L: operational scripts

Priority: **P1**

### L1. Deduplicate Windows bootstrap logic

Audit `bootstrap-work-laptop.ps1` and `work-laptop-one-shot.ps1` for duplicated:

- versions
- paths
- download URLs
- checksums
- environment setup
- prerequisite checks

Extract shared data/helpers before they drift.

### L2. Script tests

Where possible:

- parse PowerShell in CI;
- test check-only mode on Windows runners;
- mock/download-independent checks for path and version logic;
- verify scripts never silently require elevation for normal setup.

---

# Workstream M: future dispatcher readiness

Priority: **P1 now for contracts, later P0 when scheduler implementation starts.**

The scheduler must not leak into the Orchestrator. Define an interface first.

### M1. Workload descriptor

Create a versioned structure containing facts available before dispatch, for example:

- task/skill class
- required tool capabilities
- context-size estimate
- mutation allowed/forbidden
- verification requirement
- privacy/locality requirement
- latency objective
- historical feature version

Do not include post-outcome information in pre-dispatch features.

### M2. Candidate target descriptor

For each candidate model/device/runtime expose measurable capabilities and constraints,
not prose labels.

### M3. Scheduler decision record

Every decision should log:

- candidate set;
- features/version used;
- predicted success probability per candidate;
- predicted latency/cost/energy where available;
- selected candidate;
- decision rule/model version;
- confidence/uncertainty;
- reason for abstention/escalation.

### M4. Feedback-loop bias protection

This is load-bearing. If the scheduler always chooses its current favorite target,
telemetry becomes selection-biased: the unchosen targets receive no comparable tasks,
so the scheduler cannot know whether its belief is wrong.

Before online learning:

- keep periodic fixed benchmark matrices across all targets;
- use safe controlled exploration only on eligible low-risk tasks;
- log selection propensity if stochastic routing is used;
- consider shadow/offline replay where execution cost permits;
- separate policy-generated telemetry from randomized/benchmark evidence.

Never train an optimizer blindly on its own selectively observed outcomes.

### M5. Calibrated probability, not arbitrary score

If the scheduler says a route has 90% success probability, that number must be
calibrated. Track at least:

- Brier score or log loss;
- reliability/calibration curves;
- coverage vs risk for abstention;
- success/latency/cost regret versus a fixed benchmark oracle where measurable.

### M6. Distribution shift

Track task-family and environment drift. A model that was excellent on repetitive
CMake failures may be poor on a new codebase or toolchain. Historical success is not a
static capability constant.

### M7. Offline-first deployment sequence

1. replay historical telemetry;
2. frozen holdout evaluation;
3. shadow recommendations without control;
4. bounded canary routing;
5. wider rollout only after calibration and failure-class review;
6. automatic rollback on predefined guardrail breach.

---

# Definition of 9/10 for this repository

The project is not rated 9/10 merely because all tests are green. The target state
requires all of the following:

- authoritative Linux and Windows CI;
- real pytest is authoritative;
- no public unhardened execution path;
- typed failure semantics preserved end to end;
- process-tree cleanup on timeout/cancel;
- strict skill/config validation;
- complete clean-install package;
- versioned RPC, telemetry and result contracts;
- property tests on pure invariants;
- mutation testing on critical decision logic;
- fuzzing on parsers/protocol boundaries;
- high branch coverage on critical modules;
- repeated flake testing;
- provenance guards and generator idempotence;
- no known score below 7;
- no unresolved P0 correctness item;
- scheduler contracts designed so telemetry can be learned from without hidden
  target-selection bias.

## PR discipline

For each hardening PR:

1. state the invariant being strengthened;
2. include a failing regression test or characterization test where practical;
3. make the smallest behavior change that fixes it;
4. run Linux and Windows authoritative suites when platform-relevant;
5. report provenance movement;
6. explicitly state whether model-facing behavior changed;
7. do not combine unrelated feature work;
8. record the new score only after evidence lands.

## Suggested execution order

1. Finish current Windows portability repair.
2. A1/A2: real pytest + Linux/Windows CI.
3. J1: complete packaging enough to clear the <7 floor.
4. I1-I3: bring RPC above the <7 floor without overbuilding the editor.
5. B1: collapse dual Orchestrator.
6. C1-C3: telemetry truthfulness and schema versioning.
7. D1/D2: complete config semantics and validation.
8. E1-E4: fail-closed tools, process trees, property-tested sandbox.
9. K1-K6: coverage, mutation, property, fuzz, concurrency and flake gates.
10. F1/F2: evaluation decomposition under characterization tests.
11. G1/G2: strict skill schema.
12. H1-H3: provenance and release reproducibility hardening.
13. L1/L2: operational script deduplication/tests.
14. M1-M7: formal dispatcher contract and ML feedback discipline.

This ordering intentionally raises the weakest scores first and protects the quality
floor before chasing the final 9/10 target.
