# Local Code Agent

**Chat locally. Give models controlled access to code. Verify their work independently.**

Local Code Agent is a deterministic control layer for local AI engineering workloads. The model reasons and proposes actions; the controller decides what it may do, records the evidence, and decides whether the result is actually valid.

The current demo runs Qwen locally on Panther Lake CPU, GPU and NPU through an OpenAI-compatible serving boundary. The same controller is intentionally independent of the runtime underneath it.

If this is your first time here, start with [`QUICKSTART.md`](QUICKSTART.md).

---

## Start here

### 1. See the available local models

```powershell
.\chat.ps1
```

### 2. Chat directly with Qwen3-8B on the NPU

```powershell
.\chat.ps1 qwen3-8b-npu
```

The same user-facing chat path can select:

```powershell
.\chat.ps1 qwen3-8b-gpu
.\chat.ps1 qwen3-8b-cpu
.\chat.ps1 qwen3-coder-30b
```

The three Qwen3-8B choices use the same model artifact while changing only the requested execution device. `qwen3-coder-30b` selects a different model profile.

### 3. See what Local Code Agent is allowed to do

```powershell
python scripts\capabilities.py
```

This reads the live tool registry and installed skills, then prints the supported actions and the boundaries enforced by the controller.

### 4. Prove the NPU / GPU / CPU route

```powershell
.\scripts\demo-accelerator.ps1 -Device NPU -Seconds 45
.\scripts\demo-accelerator.ps1 -Device GPU -Seconds 45
.\scripts\demo-accelerator.ps1 -Device CPU -Seconds 45
```

The demo reports the requested device and the device OpenVINO actually resolved, then runs repeated model inference long enough to see the matching hardware activity.

The timing numbers shown by this demo are observations on an uncontrolled machine, not benchmark results.

### 5. See independent verification reject stale test results

```powershell
python scripts\demo-trust-boundary.py
```

The demo deliberately creates a case where `ctest` reports passing tests against an old binary after the source has changed. Local Code Agent rejects that result as stale. An honest rebuild then fails.

> **The model can propose actions. It cannot mark its own homework.**

---

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

---

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

The runtime below the client is a deployment choice. Today the Panther Lake 8B path uses OVMS / OpenVINO. A future llama.cpp / GGML NPU backend can sit behind the same client contract without changing controller policy, task semantics or verification.

---

## What works today

Current implemented surfaces include:

- Qwen3-8B served locally on Panther Lake NPU, GPU and CPU paths
- direct terminal chat through friendly model/device names
- an OpenAI-compatible model boundary shared by the project
- 20 approved repository / Git / build / test tools
- 8 task procedures / skills
- deterministic policy and tool narrowing
- independent build/test verification
- stale-build detection
- canonical evidence IDs
- Windows and Linux test coverage
- explicit serving and accelerator qualification paths
- preserved experiment provenance and source identity

This list describes implemented behaviour. It does not imply production readiness or general coding-model capability.

---

## Measured evidence so far

The completed generation-1 experiment used one local 30B model on CPU and ten synthetic C++ tasks:

| Condition | Verified completion |
|---|---:|
| Control | 3/10 |
| Narrow tools | 8/10 |
| Narrow tools + written skill | 8/10 |

The strongest measured signal was **action-space narrowing**: restricting the model to the tools relevant to the task improved verified completion from 3/10 to 8/10, removed the four observed scope violations, and reduced tool calls and wall time substantially.

The experiment did **not** contain an 8B cell, so it provides no evidence that the same result transfers to the smaller model used in the current Panther Lake demo.

The full historical README and experiment interpretation are preserved in [`docs/project-history.md`](docs/project-history.md). Frozen datasets remain under [`experiments/`](experiments/).

---

## Repository orientation

A new user should need only a few entrypoints:

| Start here | Purpose |
|---|---|
| [`QUICKSTART.md`](QUICKSTART.md) | First-run path and demo sequence |
| `chat.ps1` | Talk directly to a configured local model |
| `scripts/capabilities.py` | Show what the controlled agent can and cannot do |
| `scripts/demo-accelerator.ps1` | Prove CPU / GPU / NPU execution |
| `scripts/demo-trust-boundary.py` | Demonstrate independent verification |

The rest of the repository contains the implementation, tests, research harness and frozen experiment history. Those details are deliberately not required to understand the first five minutes.

Useful deeper documentation:

| Document | Purpose |
|---|---|
| [`docs/verification.md`](docs/verification.md) | What counts as proof and why |
| [`docs/serving-and-accelerators.md`](docs/serving-and-accelerators.md) | Model/runtime/device configuration |
| [`docs/review-history.md`](docs/review-history.md) | Adversarial defects and regression history |
| [`docs/project-history.md`](docs/project-history.md) | Previous research-first README and historical framing |
| [`AGENTS.md`](AGENTS.md) | Developer guidance for changing the codebase |

---

## Experimental integrity

Historical experiments are frozen. New UX wrappers, serving work or instrumentation do not silently rewrite old evidence.

Behaviour-affecting source is covered by explicit source identity. When methodology or behavioural contracts change, that belongs to a new experiment generation rather than a retrospective reinterpretation of an old one.

Current code is authoritative in GitHub `main`; frozen experiment claims are authoritative in their recorded artifacts and hashes.

---

## Licence

Not yet licensed. No rights are granted in the meantime.
