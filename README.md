# Local Code Agent

**Chat locally. Give models controlled access to code. Verify their work independently.**

Local Code Agent is a deterministic control layer for local AI engineering workloads. The model can reason and propose actions. The controller decides what it may do, records the evidence and independently decides whether the result is valid.

> **The model can propose actions. It cannot mark its own homework.**

If this is your first time here, start with [`QUICKSTART.md`](QUICKSTART.md).

## The first five minutes

The repository root is deliberately small:

```text
install.ps1
chat.ps1
local-code-agent.ps1
README.md
QUICKSTART.md
demo/
internal/
```

The normal user-facing path is entirely at the root or under `demo/`. `internal/` contains implementation, tests, research tooling, documentation and frozen historical artifacts. You do not need to understand that tree to use the demo.

### 1. Prepare or validate the machine

```powershell
.\install.ps1
```

For a read-only preflight:

```powershell
.\install.ps1 -CheckOnly
```

### 2. Chat directly with a local model

```powershell
.\chat.ps1
.\chat.ps1 qwen3-8b-npu
```

Other configured choices:

```powershell
.\chat.ps1 qwen3-8b-gpu
.\chat.ps1 qwen3-8b-cpu
.\chat.ps1 qwen3-coder-30b
```

The three Qwen3-8B choices use the same model artifact and change only the requested execution device. `qwen3-coder-30b` selects a different model profile.

Direct chat has no repository tools. It starts or reuses the selected local model server through the same deterministic serving controller used by the rest of the project, then drops straight into the terminal conversation.

### 3. Use the controlled coding agent

```powershell
.\local-code-agent.ps1
.\local-code-agent.ps1 capabilities
```

A controlled repository task looks like:

```powershell
.\local-code-agent.ps1 run-task "Inspect this repository and summarize how it builds" --skill repo-navigation
```

Chat gives direct access to the model. Local Code Agent adds controlled repository access, approved tools, procedural skills and independent verification.

### 4. Run the demos

Prove the same Qwen3-8B model on each device:

```powershell
.\demo\run-qwen-on-npu.ps1
.\demo\run-qwen-on-gpu.ps1
.\demo\run-qwen-on-cpu.ps1
```

Show stale passing tests being rejected as invalid evidence:

```powershell
.\demo\show-stale-test-rejection.ps1
```

Or run the guided sequence:

```powershell
.\demo\run-complete-local-code-agent-demo.ps1
```

Accelerator timings shown by the demo are live observations on an uncontrolled machine, not benchmark results.

## What Local Code Agent adds beyond chat

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

## Architecture

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

The runtime below the client is a deployment choice. The current Panther Lake 8B path uses OpenVINO Model Server / OpenVINO. A future llama.cpp / GGML NPU backend can sit behind the same model-facing boundary without changing controller policy, task semantics or verification.

## What works today

Current implemented surfaces include:

- Qwen3-8B local serving paths for Panther Lake NPU, GPU and CPU
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
- explicit serving and accelerator qualification paths
- preserved experiment provenance and source identity

This describes implemented behaviour. It does not imply production readiness or general coding-model capability.

## Measured evidence so far

The completed generation-1 experiment used one local 30B model on CPU and ten synthetic C++ tasks:

| Condition | Verified completion |
|---|---:|
| Control | 3/10 |
| Narrow tools | 8/10 |
| Narrow tools + written skill | 8/10 |

The strongest measured signal was **action-space narrowing**. Restricting the model to the tools relevant to the task improved verified completion from 3/10 to 8/10, removed the four observed scope violations, and reduced tool calls and wall time substantially.

The experiment did **not** contain an 8B cell, so it provides no evidence that the same result transfers to the smaller model used in the current Panther Lake demo.

The historical research-first README and experiment interpretation are preserved under [`internal/docs/project-history.md`](internal/docs/project-history.md). Frozen experiment artifacts remain under [`internal/experiments/`](internal/experiments/).

## Developer / research internals

Everything that is not part of the first-run product surface lives under `internal/`:

```text
internal/
  local_agent/
  measurement/
  evaluation/
  scripts/
  tests/
  benchmark_fixture/
  docs/
  skills/
  experiments/
```

Useful deeper documentation:

| Document | Purpose |
|---|---|
| [`internal/docs/verification.md`](internal/docs/verification.md) | What counts as proof and why |
| [`internal/docs/serving-and-accelerators.md`](internal/docs/serving-and-accelerators.md) | Model/runtime/device configuration |
| [`internal/docs/review-history.md`](internal/docs/review-history.md) | Adversarial defects and regression history |
| [`internal/docs/project-history.md`](internal/docs/project-history.md) | Historical framing and earlier README material |
| [`AGENTS.md`](AGENTS.md) | Developer guidance for changing the codebase |

## Experimental integrity

Historical experiments are frozen. The physical move into `internal/` does not rewrite their file bytes or reinterpret old results. Historical tags remain the authoritative way to reproduce historical layouts.

Behaviour-affecting current source is covered by explicit source identity. Moving those files changes the current source identity because canonical repository paths are part of that hash. Model-facing or outcome-facing contract changes are tracked separately so experimental generations remain explicit.

## Licence

Not yet licensed. No rights are granted in the meantime.
