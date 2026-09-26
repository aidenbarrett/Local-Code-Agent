# Local Code Agent

Local Code Agent is a local-first coding worker built around a deterministic controller. The model proposes work; deterministic code owns permissions, execution, verification, durable state and the final verdict.

**The model is replaceable. The agent is the product.**

## Current product surface

The active human interface is the Textual Session Hub:

```powershell
.\install.ps1
.\local-code-agent.ps1
```

Raw model chat remains available as an explicit mode:

```powershell
.\local-code-agent.ps1 chat qwen3-8b-npu
```

Controlled repository work uses the same root launcher and deterministic controller boundaries. The model never receives arbitrary shell authority and cannot certify its own success.

## What works today

Current implemented foundations include:

- one public launcher with the Session Hub as the default interface
- persisted conversation plus separate durable task/event state
- deterministic task admission before effects
- controlled repository, Git, build and test tooling
- independent verification tied to current repository state
- typed outcomes including explicit `NO_VERDICT`
- retained request/result artifacts and durable task identity
- explicit stop requests with execution-epoch fencing
- process-local endpoint ownership and arbitration
- replaceable model/runtime boundaries behind OpenAI-compatible HTTP
- exact product source identity for retained run provenance
- Windows and Linux CI

This is not a production-readiness claim. Open correctness and acceptance work is tracked in `CURRENT_STATE.md` and the internal product backlog.

## Endpoint performance harness

Performance comparison now has one neutral harness: [`internal/perf/endpoint_harness.py`](internal/perf/endpoint_harness.py).

It can target any OpenAI-compatible endpoint and records:

- cold start to first ready, plus backend-reported compile time when explicitly exposed
- client-boundary time to first token and decode tokens/second
- prefill/context measurements at a configurable size ladder
- endpoint-observed cancellation latency when the backend exposes request lifetime
- multi-hour soak results including errors, memory and latency drift
- exact configured model, runtime and device identity, with optional endpoint-side identity proof

Backend-specific launch details live in local profile files rather than in the harness. See [`internal/perf/README.md`](internal/perf/README.md).

Historical research artifacts are no longer carried on `main`. The pre-cleanup repository state is preserved on branch `archive/legacy-experiments-2026-09-26` at commit `ad07af071a62acfa4d0168c7a89d7110a52088f6`.

## Architecture

```text
engineering request
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
       local or remote endpoint

Independent verification evaluates tool evidence and repository state,
not the model's description of what happened.
```

## Useful docs

- [`QUICKSTART.md`](QUICKSTART.md) for setup and first run
- [`TRICKS.md`](TRICKS.md) for first-release user journeys
- [`PROJECT_OVERVIEW.md`](PROJECT_OVERVIEW.md) for durable architecture and product direction
- [`CURRENT_STATE.md`](CURRENT_STATE.md) for implemented state, known gaps and next work
- [`internal/docs/verification.md`](internal/docs/verification.md) for verification semantics
- [`internal/docs/product-roadmap.md`](internal/docs/product-roadmap.md) for the product roadmap
- [`AGENTS.md`](AGENTS.md) for repository engineering rules

## Licence

Copyright (c) 2026 Aiden Barrett. All rights reserved.

Local Code Agent is proprietary software. No permission is granted to use, copy, modify, distribute, sublicense, sell, host, deploy or create derivative works except under prior written permission or a separate written agreement from the copyright holder. See [`LICENSE`](LICENSE) for the full terms.
