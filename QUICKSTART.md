# Quickstart

Local Code Agent currently targets a managed Windows 11 local-AI workstation for its primary hardware path. Other runtimes/devices fit behind the same controller boundary, but support claims require their own acceptance evidence.

## 1. Clone and check the machine

```powershell
git clone https://github.com/aidenbarrett/Local-Code-Agent.git
cd Local-Code-Agent
.\install.ps1 -CheckOnly
```

Run setup when ready:

```powershell
.\install.ps1
```

Bare setup shows the machine-level changes it may make and asks first. `-InstallMissing` is the explicit non-interactive approval for approved prerequisite installation. Driver and optional Windows-feature changes remain explicit.

Setup creates/updates the managed Python environment, prepares the configured local runtime/model, runs protocol conformance checks and proves the local C++ toolchain can configure/build/test the validation fixture.

## 2. Open the product

```powershell
.\local-code-agent.ps1
```

That opens the Textual Session Hub, the default human interface for conversation plus controller-owned repository work.

Useful explicit modes:

```powershell
.\local-code-agent.ps1 help
.\local-code-agent.ps1 capabilities
.\local-code-agent.ps1 chat qwen3-8b-npu
```

Raw chat has no repository authority, tools or independent verification. The Session Hub is the product path for controlled engineering work.

## 3. Ask for work naturally

Examples:

```text
Inspect this repository.
Where is EndpointRuntime used?
What changed on my branch?
Build it.
Why did that fail?
```

The controller decides the permitted route/tools and owns verification. The model never receives arbitrary shell authority and cannot certify its own success.

## 4. Hardware demo

The demo scripts run the configured local model on a selected device for a visible smoke test:

```powershell
.\demo\run-qwen-on-npu.ps1
.\demo\run-qwen-on-gpu.ps1
.\demo\run-qwen-on-cpu.ps1
```

Their timing is live demo output, not benchmark evidence. Runtime comparison belongs to the neutral endpoint harness under `internal/perf/`.

## 5. Performance comparison

For engineering/runtime work, copy the generic example profile to an ignored local path and point it at the endpoint being measured:

```powershell
python internal/perf/endpoint_harness.py --profile .local-agent/perf/my-endpoint.toml --cold-start --cancellation --soak-hours 4 --output run.json
```

See `internal/perf/README.md`. Backend-specific commands and observation hooks belong in that local profile, not in harness source.

## What to read next

- `README.md`: product purpose and architecture
- `CURRENT_STATE.md`: current implemented state and open gaps
- `TRICKS.md`: first-release public journeys
- `internal/docs/verification.md`: independent proof semantics
- `internal/docs/serving.md`: product-owned serving lifecycle
- `internal/docs/product-roadmap.md`: longer product direction
- `AGENTS.md`: engineering rules for this repository

Historical research material is archived off the active branch and is not required for normal use or current development.
