# Testing, Evaluation, Verification and Validation Framework

This document defines the testing methods Local Code Agent should use as it moves
from research instrument to a durable local-agent execution substrate and eventually
an AI workload scheduler for engineering work.

The aim is not to collect fashionable test techniques. Each method below protects a
specific failure surface in this repository.

## 1. Test taxonomy

Use several independent layers. No single layer is considered sufficient evidence.

### 1.1 Unit tests

Use for deterministic local logic:

- proof classification;
- policy decisions;
- config validation;
- routing state;
- telemetry arithmetic;
- serialization;
- parser helpers;
- path containment;
- skill metadata validation.

Rule: pure decision logic should be testable without CMake, Git, a model server or
network access.

### 1.2 Contract tests

Use where two modules meet:

- LLM client -> orchestrator;
- orchestrator -> tool registry;
- tool -> ToolResult;
- Orchestrator -> evaluator;
- CLI/RPC -> Orchestrator;
- scheduler -> execution target in the future.

A contract test should assert the shape and semantics crossing the boundary, not
private implementation details.

### 1.3 Integration tests

Use real implementations together:

- real Git repository;
- real CMake/CTest;
- real compiler/linker;
- real subprocess handling;
- real path handling on Linux and Windows.

Mocks are inappropriate when the behavior under test is the behavior of the external
toolchain itself.

### 1.4 End-to-end tests

Exercise:

`task -> client -> orchestrator -> tools -> worktree -> verification -> outcome`

Use ScriptedClient for deterministic end-to-end protocol tests and a small controlled
live-model smoke suite separately.

### 1.5 Characterization tests

Before refactoring load-bearing code, freeze current valid behavior as data and prove
the refactor preserves it. Particularly important for:

- `run_evaluation.py` decomposition;
- Orchestrator consolidation;
- telemetry schema migrations;
- parser rewrites.

### 1.6 Regression tests

Every reproduced defect should receive the smallest test that would have caught it.
Examples already represented in the project include stale proof, oracle leakage,
platform-specific CTest behavior and Windows path depth.

---

# 2. Property-based testing

Use Hypothesis for invariants where example-based tests leave large input spaces
unexplored. Property-based testing generates edge cases automatically and shrinks a
failure to a small reproducible counterexample.

High-value Local Code Agent properties:

### Path safety

For arbitrary path strings:

- a resolved writable path is either inside the allowed source surface or rejected;
- `..`, separators, Unicode and redundant components never escape;
- equivalent path spellings resolve to the same allowed target;
- symlink escape attempts are rejected.

### Proof classification

For arbitrary combinations of tool name, execution status, domain status, arguments
and evidence:

- blocked/error execution never yields current-tree proof;
- targeted PASS never becomes full-tree proof;
- any genuine build/test FAIL never remains a current-tree PASS;
- invalidating stale/profile evidence prevents PASS proof.

### Serialization

For arbitrary valid records:

`object -> JSON -> object`

must preserve every load-bearing semantic field.

### Configuration

Generated malformed config values must either produce a valid typed configuration or
a clear ConfigError. Never silently reinterpret malformed values.

### Stream/tool-call parsing

Fragment a known tool call into arbitrary chunk boundaries. Reassembly must produce
the same ToolCall independent of chunk segmentation.

Reference: Hypothesis documentation recommends properties such as round trips and
equivalence between a simple reference implementation and an optimized one.
https://hypothesis.readthedocs.io/

---

# 3. Mutation testing

Mutation testing asks a stronger question than coverage:

> If a plausible bug is injected into production logic, does the suite actually fail?

Use a Python mutation tool on Linux CI or scheduled CI for critical pure modules.

Priority targets:

- `local_agent/verification.py`;
- policy and outcome logic;
- scoring/validity logic;
- config validation;
- routing/telemetry arithmetic;
- evidence/citation contracts.

Examples of mutations the suite should kill:

- `execution != "ok"` changed to `execution == "ok"`;
- targeted build accepted as full build;
- stale evidence ignored;
- `and` changed to `or` in success gates;
- reset omits a state field;
- router threshold comparison inverted.

Targets:

- initial critical-module mutation score >=80%;
- mature critical-module target >=90%;
- every surviving mutant is reviewed, not merely averaged away.

Mutation score must not become a vanity metric. Equivalent mutants may be documented
and excluded when proven equivalent.

Reference implementation considered: mutmut.
https://mutmut.readthedocs.io/

---

# 4. Fuzz testing

Fuzz untrusted or semi-trusted boundaries rather than ordinary business logic.

Targets:

- streamed OpenAI-compatible response fragments;
- tool-call JSON;
- stdio RPC JSON;
- CMake/CTest/compiler log parsers;
- skill metadata;
- result/transcript readers;
- patch arguments and file paths.

Required properties:

- malformed input does not crash the process without a typed diagnostic;
- no malformed input escapes sandbox boundaries;
- parser resource use is bounded;
- a crash input is persisted as a deterministic regression case.

For Python, Hypothesis can cover structured fuzz-style exploration. For lower-level or
future native components, libFuzzer/AFL++/ClusterFuzzLite-style continuous fuzzing is
appropriate. Google's OSS-Fuzz documentation describes PR-time continuous fuzzing and
reproducible crash artifacts.
https://google.github.io/oss-fuzz/

---

# 5. Differential testing

Run equivalent behavior through two implementations and compare invariants.

Useful pairs:

- streaming vs blocking client parsing;
- real pytest vs the compatibility test runner for the explicitly supported subset;
- Linux vs Windows for platform-neutral fixture outcomes;
- two runtime backends serving the same deterministic scripted responses;
- old vs refactored evaluator during decomposition;
- scheduler policy vs a fixed reference policy in offline replay.

Do not require byte-identical logs where platforms legitimately differ. Compare typed
semantics: outcome, proof kind, failure locus, test identity and verification state.

---

# 6. Metamorphic testing

Metamorphic testing is valuable where exact expected output is difficult to state but
relationships between executions should hold.

Examples for this repository:

- moving a fixture between two safe path depths must not change task semantics;
- changing path separators must not change the resolved in-repo resource;
- splitting the same streamed tool call at different chunk boundaries must not change
  the reconstructed ToolCall;
- regenerating the fixture twice must yield no diff;
- reordering independent JSON object keys must not change identity or parsing;
- running a pure scoring function twice on the same row must return the same result;
- moving from CPU to GPU/NPU may change latency but must not change the meaning of a
  deterministic scripted-client test.

Metamorphic tests are particularly useful for local-agent backends because many
runtime differences are legitimate while contract differences are not.

---

# 7. Fault injection and chaos-style tests

The agent must fail correctly, not only succeed correctly.

Inject:

- model server unavailable before request;
- server 500/rejection;
- server disconnect mid-stream;
- server emits bytes too slowly;
- malformed tool arguments;
- missing executable;
- compiler spawn failure;
- command timeout;
- child/grandchild process surviving parent;
- disk write failure where practical;
- oracle file inaccessible;
- Git command failure;
- worktree changed between proof and submission;
- RPC disconnect during approval;
- cancellation during build/test.

For every injection assert both:

1. the operation fails safely; and
2. the failure is attributed to the correct locus (model/server/tool/environment/
   policy/user), so future scheduler training data is not poisoned.

---

# 8. Concurrency and isolation testing

A local workload scheduler will eventually run more than one thing. Shared mutable
state must be exposed now rather than later.

Tests should include:

- two pytest sessions in parallel;
- two agent runs against separate fixture copies;
- two TieredClient instances with independent telemetry;
- unique run directories under concurrent creation;
- no shared temporary-path collision;
- simultaneous log writes remain isolated;
- future RPC request correlation under interleaving.

Use pytest-xdist as one way to make tests run in different orders and processes. A
suite that only passes serially has hidden coupling until proven otherwise.
https://pytest-xdist.readthedocs.io/

---

# 9. Coverage strategy

Coverage identifies unexercised code; it does not prove assertions are good.

Use branch coverage, not only line coverage, because decision-heavy code is where the
important bugs live.

Suggested gates after an initial baseline is measured:

- `verification.py` and other critical contract logic: >=95% branch coverage;
- `local_agent/` core: >=90% branch coverage;
- total repository Python behavior under test: >=85% branch coverage.

Every exclusion must have a reason. Never add trivial tests solely to raise the
percentage.

Coverage.py supports branch coverage and CI fail-under thresholds.
https://coverage.readthedocs.io/

---

# 10. Static quality gates

Add gradually, fixing findings rather than blanket-ignoring them.

Recommended categories:

- formatter;
- linter;
- static type checker;
- dependency vulnerability scan;
- secret scan;
- import-cycle detection where useful;
- PowerShell syntax/static analysis for Windows scripts.

The objective is deterministic feedback, not tool count.

---

# 11. Cross-platform test matrix

The supported platform contract requires native testing.

Minimum authoritative matrix:

| OS | Python | Purpose |
|---|---|---|
| Ubuntu latest | primary | GCC/CMake/CTest and Linux semantics |
| Windows latest | primary | MSVC/CMake/CTest and Windows semantics |
| minimum Python | minimum supported | package language compatibility |

Manual Panther Lake testing remains necessary for NPU/GPU hardware behavior, but it
must sit above a portable software CI foundation.

Platform-specific expected text should be normalized only where semantics are truly
identical. Never normalize away a meaningful difference merely to make tests green.

---

# 12. Flake and order-dependence testing

Run deterministic critical tests repeatedly and in varying order.

Scheduled checks:

- repeat critical suite 20-100 times;
- randomized test order where safe;
- parallel execution;
- fresh temp roots;
- long and short path roots;
- clean process between selected runs.

A reproducible deterministic suite should have a 0% accepted flake rate. A flaky test
or flaky product behavior remains an open defect.

---

# 13. ML/AI-specific TEVV

The project eventually contains an ML prediction system (the dispatcher) even if the
execution substrate itself is largely deterministic. The scheduler therefore needs a
proper Test, Evaluation, Verification and Validation discipline.

NIST's AI Risk Management Framework emphasizes documented, repeatable TEVV and
measurement under conditions similar to deployment. NIST's TEVV-Athlon draft also
explicitly covers agentic systems. These are useful frameworks for structure, not a
claim that this project is formally NIST-certified.

References:

- NIST AI RMF / AI Resource Center: https://airc.nist.gov/
- NIST TEVV-Athlon: https://www.nist.gov/artificial-intelligence/ai-research/tevv-athlon-framework-evaluating-ai-systems

### 13.1 Adapt the Google ML Test Score mindset

Google's "ML Test Score" proposes concrete tests and monitoring needs for production
ML systems. Adopt the principle: production readiness is a testable rubric spanning
more than offline model accuracy.

For the dispatcher, categories should include:

- feature/schema tests;
- training/serving feature equivalence;
- model-quality thresholds;
- calibration;
- slice/task-family performance;
- reproducibility;
- monitoring/drift;
- rollback and fallback behavior;
- dependency/configuration checks.

Reference:
https://research.google/pubs/the-ml-test-score-a-rubric-for-ml-production-readiness-and-technical-debt-reduction/

### 13.2 Prevent hidden ML technical debt

Telemetry-fed routing creates the classic risk described in "Hidden Technical Debt in
Machine Learning Systems": feedback loops, configuration debt, undeclared consumers,
entanglement and changing external conditions.

In Local Code Agent the most dangerous future loop is:

`scheduler chooses target -> only chosen target produces outcome -> scheduler trains on chosen outcomes -> unchosen targets disappear from evidence`

This is selection bias and can make the scheduler increasingly confident without
becoming increasingly correct.

Controls:

- periodic full benchmark matrices;
- controlled exploration on low-risk eligible work;
- log selection probability/propensity for randomized decisions;
- fixed holdout workloads;
- shadow recommendations;
- telemetry provenance and policy versioning.

Reference:
https://papers.nips.cc/paper/2015/hash/86df7dcfd896fcaf2674f757a2463eba-Abstract.html

---

# 14. Experimental design for agent/model comparisons

When comparing model/device/skill conditions, use an experimental protocol rather
than eyeballing averages.

### Repeated trials

LLM outputs are stochastic even under controlled sampling. One run measures one run.
Use repeats for claims about expected behavior.

### Paired design

Where possible, run the same task instance across compared targets/conditions. Analyze
the within-task difference rather than treating every observation as unrelated.

### Counterbalancing

Rotate condition order to reduce warm-cache, thermal, server-state and time-order
confounds.

### Frozen holdout

Keep tasks that are not used to tune skills, thresholds or routing rules. Once a task
becomes a debugging fixture, it is no longer an honest holdout for generalization.

### Report uncertainty

For success rates, report confidence/credible intervals and raw `n`, not just a
percentage. For latency, report distributions/quantiles, not only means.

### Effect size first

A tiny statistically detectable difference may be operationally meaningless. Report
absolute success delta, latency delta, energy/cost delta and escalation delta.

### Multiple dimensions

Do not collapse every objective into one score during research. Keep at least:

- success/verification;
- latency;
- model calls/tokens;
- energy/cost where measurable;
- escalation rate;
- scope/safety violations;
- invalid harness rate.

The future scheduler may combine them according to policy, but the raw dimensions
must remain recoverable.

---

# 15. Prediction-engine validation

When the dispatcher moves from rules to learned prediction, test the predictor as a
predictor before allowing it to control work.

### Data split

Use both:

- time-based holdout to detect future drift; and
- task-family/repository grouping so near-duplicate tasks do not leak between train
  and test.

### Calibration

A scheduler needs probabilities, not merely rankings.

Measure:

- Brier score;
- log loss;
- reliability diagram/calibration curve;
- calibration error by important task slices;
- coverage-risk curve when the system abstains.

### Ranking/decision quality

Measure:

- fraction of tasks routed to cheapest target that succeeds;
- escalation rate;
- unnecessary strong-model rate;
- latency/cost/energy regret against an offline benchmark matrix;
- catastrophic misroute rate for high-risk tasks.

### Shadow mode

Before the predictor controls execution:

- let current deterministic routing make the real decision;
- log what the predictor would have chosen;
- compare predicted and actual counterfactual evidence where available;
- only then canary the learned policy.

### Canary and rollback

Define hard guardrails before deployment. Examples:

- verified success must not fall more than X percentage points;
- safety/scope violations cannot increase;
- invalid harness rate cannot increase;
- p95 latency/cost ceilings;
- automatic rollback on breach.

The thresholds are deployment policy and should be chosen from baseline data, not
invented before measurements exist.

---

# 16. Golden rules for this project

1. Test the invariant, not the implementation accident.
2. A passing test is not proof if the test never reaches the risky branch.
3. A green mock is not evidence about CMake, Git, MSVC, GCC, CTest or Windows.
4. Every platform claim needs native execution evidence.
5. Every model capability claim excludes harness-invalid rows.
6. Every success claim requires deterministic verification where the task permits it.
7. Unknown telemetry stays unknown.
8. Every automated decision must be replayable from logged inputs, policy version and
   target candidates.
9. A learned scheduler is not allowed to silently train on its own selection bias.
10. Adversarial tests are first-class product tests, not research extras.
