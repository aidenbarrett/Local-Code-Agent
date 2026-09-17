# Local Code Agent

**Chat locally. Give models controlled access to code. Verify their work independently.**

Local Code Agent lets a local AI model work on a code repository using controlled tools and task procedures, while deterministic code decides what the model may do and whether its work actually passed verification.

> **The model can propose actions. It cannot mark its own homework.**

## Current setup target

The current first-run path targets **Windows 11 on Intel Panther Lake**. The friendly demo path serves **Qwen3-8B (INT4)** through OpenVINO Model Server and can request the NPU, GPU or CPU on that machine.

The controller architecture is designed to remain model/runtime/device independent, but this setup path is **not** a claim that arbitrary Windows, Linux or macOS machines and backends have been qualified.

If you want the detailed walkthrough, start with [`QUICKSTART.md`](QUICKSTART.md).

## Try it

From PowerShell after cloning the repository:

```powershell
.\install.ps1 -CheckOnly
.\install.ps1
.\chat.ps1 qwen3-8b-npu
.\local-code-agent.ps1 capabilities
```

That sequence checks the machine, prepares the local runtime, starts a direct chat with the local model, then shows the controlled coding-agent capability surface.

A controlled repository task looks like:

```powershell
.\local-code-agent.ps1 run-task "Inspect this repository and summarize how it builds" --skill repo-navigation
```

The public `run-task` path prepares/reuses the same Qwen3-8B NPU server used by chat and selects the matching `ptl-npu-8b` agent profile explicitly.

Examples of intended workloads include:

- inspect a repository and explain how it builds
- diagnose or fix build/test failures, then verify the current tree independently
- review repository/Git state and prepare a controlled change

Those are intended use cases, not claims that every task is solved successfully by every local model.

## Why this is different from local chat

Direct chat is intentionally simple: model in, text out.

Local Code Agent adds:

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

- Qwen3-8B local serving paths for the Panther Lake NPU, GPU and CPU
- direct terminal chat through friendly model/device names
- one-command chat server startup through the deterministic serving controller
- an OpenAI-compatible model boundary shared by the project
- 20 approved repository / Git / build / test tools
- 8 task procedures / skills
- deterministic policy and tool narrowing
- independent build/test verification
- stale-build detection based on source content hashes
- canonical evidence IDs
- Windows and Linux test coverage
- explicit model-serving and CPU/GPU/NPU hardware validation paths
- preserved experiment provenance and source identity

This describes implemented behaviour. It does not imply production readiness or general coding-model capability.

## Measured evidence so far

Generation 1 used one local Qwen3-Coder-30B model on CPU against the same ten synthetic C++ tasks under three controlled conditions. The initial smoke produced **3/10 control, 8/10 narrow, 8/10 skill**. A later frozen replication used the same instrument identity, three balanced repeats and 90 case rows:

| Condition | Per repeat | Pooled verified completion |
|---|---|---:|
| Control | 2/10, 4/10, 3/10 | **9/30 (0.300)** |
| Narrow tools | 7/10, 7/10, 8/10 | **22/30 (0.733)** |
| Narrow tools + written skill | 8/10, 7/9, 8/10 | **23/29 (0.793)** |

One Skill row was `INVALID_SERVER_UNAVAILABLE` and is excluded from its denominator rather than counted as a task failure.

The strongest measured signal remained **action-space narrowing**: `Narrow - Control = +0.433` in the balanced replication, with 11 scope violations in 30 Control rows and 0 in the 59 valid Narrow/Skill rows. The incremental written-procedure contrast was small and unresolved: `Skill - Narrow = +0.060`. The repeated data shows named, opposite per-case movements rather than a clear general procedure effect, so the defensible conclusion is **no clear aggregate procedure effect on this fixture**, not that written procedures have zero effect.

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

Not yet licensed. No rights are granted in the meantime.
