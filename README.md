# Local Code Agent

## Conversation product direction

The short-term priority is a continuous conversation that can inspect, build and
test this project through its deterministic controller. Measurement collection is
paused. [Project overview](PROJECT_OVERVIEW.md) and [current state](CURRENT_STATE.md)
describe the implemented limits and the active Session Hub work.

The [first-release user journeys](TRICKS.md) define the public behaviour and
evidence required before calling a workflow delivered.

The [product roadmap](internal/docs/product-roadmap.md) targets a dependable local
worker for finding, moving, Git and coding tasks, with replaceable models and no
required cloud AI dependency. **The model is a replaceable dependency. The agent
is the product.** Its milestones are future goals, not current feature claims.

The active product surface is the Textual Session Hub with conversation, activity
and watch panes backed by controller-owned durable state. The public product has
one root entrypoint: `.\local-code-agent.ps1` opens the Hub by default. Raw model
chat and lower-level automation/debug paths remain explicit subcommands of that
same entrypoint rather than competing top-level products.

**Chat locally. Give models controlled access to code. Verify their work independently.**

Local Code Agent lets a local AI model work on a code repository using controlled tools and task procedures, while deterministic code decides what the model may do and whether its work actually passed verification.

> **The model can propose actions. It cannot mark its own homework.**

## Current setup target

The current first-run path targets **Windows 11 on Intel Panther Lake**. The friendly local-model path serves **Qwen3-8B (INT4)** through OpenVINO Model Server and can request the NPU, GPU or CPU on that machine.

The controller architecture is designed to remain model/runtime/device independent, but this setup path is **not** a claim that arbitrary Windows, Linux or macOS machines and backends have been qualified.

If you want the detailed walkthrough, start with [`QUICKSTART.md`](QUICKSTART.md).

## Try it

From PowerShell after cloning the repository:

```powershell
.\install.ps1 -CheckOnly
.\install.ps1
.\local-code-agent.ps1
```

The bare product command opens the Session Hub. Raw local-model chat is still available when you explicitly want a model with no repository authority:

```powershell
.\local-code-agent.ps1 chat qwen3-8b-npu
```

The controlled capability and headless task surfaces remain available under the same launcher:

```powershell
.\local-code-agent.ps1 capabilities
.\local-code-agent.ps1 run-task "Inspect this repository and summarize how it builds" --skill repo-navigation
```

The public `chat` and `run-task` paths use the managed local serving stack. They remain distinct execution modes: raw chat has no repository tools or verification, while controlled tasks pass through the deterministic agent boundaries.

Examples of intended workloads include:

- inspect a repository and explain how it builds
- diagnose or fix build/test failures, then verify the current tree independently
- review repository/Git state and prepare a controlled change

Those are intended use cases, not claims that every task is solved successfully by every local model.

## Why this is different from raw local chat

Raw chat is intentionally simple: model in, text out. It is available as `local-code-agent.ps1 chat ...`, but it is not a second product.

The Session Hub and controlled task paths add:

- controlled repository access
- approved repository, Git, build and test tools
- task procedures / skills that can narrow the available action space
- policy and approval boundaries owned by deterministic code
- current-tree-aware build and test verification
- canonical evidence IDs and structured run evidence
- fail-closed behaviour when required evidence or configuration is missing
- model/runtime independence behind one OpenAI-compatible client boundary

The model never receives arbitrary shell access and never decides for itself that a task succeeded.

## What works today

Current implemented surfaces include:

- one root Local Code Agent launcher with the Textual Session Hub as the default human interface
- Qwen3-8B local serving paths for the Panther Lake NPU, GPU and CPU
- explicit raw-model chat through friendly model/device names
- persisted raw-chat conversations with lock-scoped ownership
- a synchronous conversation gateway and controlled coding-worker adapter
- closed product-side outcome semantics including explicit `NO_VERDICT`
- route-source provenance for current model-selected and user-direct work
- independent build/test verification and current-tree checks
- canonical evidence IDs and structured activity events
- Windows and Linux test coverage
- explicit model-serving and CPU/GPU/NPU hardware validation paths
- preserved experiment provenance and source identity

This describes implemented behaviour. It does not imply production readiness or general coding-model capability. The Session Hub still has active P1 correctness, scaling and cancellation work tracked in the project backlog.

## Measured evidence so far

Generation 1 used one local Qwen3-Coder-30B model on CPU against the same ten synthetic C++ tasks under three controlled conditions. The initial smoke produced **3/10 control, 8/10 narrow, 8/10 skill**. A later frozen replication used the same instrument identity, three balanced repeats and 90 case rows.

The table below preserves the **Generation-1 legacy weighted `succeeded` accounting that was recorded with those frozen results**. It is not the newer typed `verified_completion` endpoint.

| Condition | Per repeat | Pooled Gen1 legacy `succeeded` |
|---|---|---:|
| Control | 2/10, 4/10, 3/10 | **9/30 (0.300)** |
| Narrow tools | 7/10, 7/10, 8/10 | **22/30 (0.733)** |
| Narrow tools + written skill | 8/10, 7/9, 8/10 | **23/29 (0.793)** |

One Skill row was `INVALID_SERVER_UNAVAILABLE` and is excluded from its denominator rather than counted as a task failure.

Under that frozen Gen1 accounting, the strongest measured signal remained **action-space narrowing**: `Narrow - Control = +0.433`, with 11 scope violations in 30 Control rows and 0 in the 59 valid Narrow/Skill rows. The incremental written-procedure contrast was small and unresolved: `Skill - Narrow = +0.060`. The repeated data shows named, opposite per-case movements rather than a clear general procedure effect, so the defensible conclusion is **no clear aggregate procedure effect on this fixture**, not that written procedures have zero effect.

For characterization only, the newer typed E3 `verified_completion` endpoint applied to the same frozen rows yields **1/30 Control, 12/30 Narrow and 9/29 Skill**. Under that accounting, `Narrow - Control = +0.367` while `Skill - Narrow = -0.090`. This does **not** replace or rescore the frozen Generation-1 result; it makes explicit that the historical `succeeded` field and the newer endpoint answer different questions. The narrowing signal has the same direction under both accountings, while the written-skill contrast does not.

These generation-1 runs were collected on the NUC under WSL2 Ubuntu with llama.cpp and the 30B UD-Q4_K_XL model. They are not measurements of the current Panther Lake / Windows / OVMS / Qwen3-8B demo stack, and they contained no 8B cell.

See [`internal/experiments/2026-09-08-30b-three-conditions-x3/findings.md`](internal/experiments/2026-09-08-30b-three-conditions-x3/findings.md) for the replication, caveats and per-case analysis.

Generation 1 is reproduced from the historical tag `instrument-08d5e0fe`. The current tree intentionally has a different repository layout and source identity; recomputing the generation-1 source hash from the current tree is not a valid reproduction procedure.

## How it works

```text
engineering task
      |
      v
+---------------------------+
| deterministic controller  |
| policy / routing / state   |
+-------------+-------------+
              |
       approved tools + skills
              |
              v
+---------------------------+
| replaceable model client  |
| OpenAI-compatible HTTP    |
+-------------+-------------+
              |
       +------+------+------+
       |             |      |
      CPU           GPU    NPU

Independent verification evaluates the evidence produced by the tools,
not the model's description of what happened.
```

The runtime below the client is a deployment choice. The current Panther Lake 8B path uses OpenVINO Model Server / OpenVINO. A future local or cloud backend can sit behind the same model-facing boundary without changing controller policy, task semantics or verification.

## Demonstrations

The public demos turn specific project properties into runnable features:

```powershell
.\demo\run-qwen-on-npu.ps1
.\demo\show-stale-test-rejection.ps1
.\demo\run-complete-local-code-agent-demo.ps1
```

The local AI hardware demos run the same Qwen3-8B model while changing only the requested CPU, GPU or NPU target. They show which device OpenVINO actually uses. Their timing values are live observations on an uncontrolled machine, not benchmark results.

The stale-test demo shows why passing test output is not accepted as proof when the source tree has changed underneath it.

See [`QUICKSTART.md`](QUICKSTART.md) for the full walkthrough, GPU/CPU variants, options and troubleshooting.

## Developer and research internals

The user-facing surface stays at the repository root and under `demo/`. Implementation, tests, measurement tooling, deeper documentation and frozen historical artifacts live under `internal/`.

Useful deeper documentation:

| Document | Purpose |
|---|---|
| [`internal/docs/verification.md`](internal/docs/verification.md) | What counts as proof and why |
| [`internal/docs/serving-and-accelerators.md`](internal/docs/serving-and-accelerators.md) | Model, runtime and accelerator details |
| [`internal/docs/review-history.md`](internal/docs/review-history.md) | Adversarial defects and regression history |
| [`internal/docs/project-history.md`](internal/docs/project-history.md) | Historical framing and preserved earlier README material |
| [`AGENTS.md`](AGENTS.md) | Developer guidance for changing the codebase |

Historical experiments are frozen. Historical tags remain the authoritative way to reproduce historical layouts. Behaviour-affecting current source has an explicit source identity, while model-facing and outcome-facing contracts are tracked separately.

## Licence

Copyright (c) 2026 Aiden Barrett. All rights reserved.

Local Code Agent is proprietary software. No permission is granted to use, copy,
modify, distribute, sublicense, sell, host, deploy or create derivative works except
under prior written permission or a separate written agreement from the copyright
holder. See [`LICENSE`](LICENSE) for the full terms.

External contributions are not accepted unless explicitly invited and ownership and
licensing terms are agreed in writing first. See [`CONTRIBUTING.md`](CONTRIBUTING.md).
