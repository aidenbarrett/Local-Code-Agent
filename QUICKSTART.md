# Quickstart

This page is for somebody opening the repository for the first time.

The root is the product surface:

- **`install.ps1`** prepares and validates the local runtime.
- **`chat.ps1`** talks directly to a local model.
- **`local-code-agent.ps1`** gives a model controlled repository access, approved tools, task procedures / skills, and independent verification.
- **`demo/`** contains the demonstration entrypoints.

Everything else lives under `internal/` and is not required for normal first use.

All commands below are run from the repository root in PowerShell.

## 0. Prepare or check the Windows workstation

Read-only preflight:

```powershell
.\install.ps1 -CheckOnly
```

Full setup / validation:

```powershell
.\install.ps1
```

If prerequisites are missing and you are happy for the script to install the approved packages:

```powershell
.\install.ps1 -InstallMissing
```

The setup path prepares the managed runtime, model serving environment and local Python environment, then qualifies the serving path and runs the C++ validation project. It stops before any scored experiment.

## 1. See what Local Code Agent can do

```powershell
.\local-code-agent.ps1
.\local-code-agent.ps1 capabilities
```

This prints the live approved tools, installed task procedures / skills, and the things the controller deliberately does not permit.

No model is required just to inspect the capability surface.

## 2. Chat directly with a local model

List the friendly choices:

```powershell
.\chat.ps1
```

Start Qwen3-8B on the NPU:

```powershell
.\chat.ps1 qwen3-8b-npu
```

Other configured choices:

```powershell
.\chat.ps1 qwen3-8b-gpu
.\chat.ps1 qwen3-8b-cpu
.\chat.ps1 qwen3-coder-30b
```

The three Qwen3-8B choices use the same model artifact and change only the requested execution device. `qwen3-coder-30b` is a different model on a different profile.

You do **not** need to start an internal model-server command first. Chat reuses a compatible server owned by Local Code Agent or starts the requested model/device through the deterministic serving controller, waits for readiness, and then presents:

```text
You >
```

Direct chat has no repository tools, filesystem access or command execution. Exit with an empty line or Ctrl-C.

## 3. Prove which accelerator is running Qwen3-8B

Open **Task Manager > Performance** and select the device you want to watch, then run one of:

```powershell
.\demo\run-qwen-on-npu.ps1
.\demo\run-qwen-on-gpu.ps1
.\demo\run-qwen-on-cpu.ps1
```

You can change how long repeated inference runs:

```powershell
.\demo\run-qwen-on-npu.ps1 -Seconds 45
```

Add `-KeepServer` if you want the selected server left running afterwards:

```powershell
.\demo\run-qwen-on-npu.ps1 -Seconds 5 -KeepServer
```

The report shows the requested device and the device OpenVINO actually resolved. The first request after startup can be slower because it may include one-time runtime warm-up plus prompt processing. Later requests use an already-initialised runtime but still process their prompts.

Timing values are live demo observations on an uncontrolled machine, not benchmark results.

## 4. See independent verification reject stale test results

```powershell
.\demo\show-stale-test-rejection.ps1
```

This uses a disposable C++ project and no model. It demonstrates why passing test output is not accepted as proof by itself:

1. Build a clean project and run its tests successfully.
2. Change source without rebuilding the binary.
3. Restore the source timestamp so a timestamp-only check would see nothing suspicious.
4. Run `ctest` again. It still reports success because it executes the old binary.
5. Local Code Agent rejects that result as stale because the current source hashes no longer match the successful build record.
6. Rebuild honestly. Compilation fails, proving the earlier passing tests were invalid evidence for the current source.

The freshness gate is deliberately **not** a file-age comparison. A successful full build records a sha256 for every build input and verification compares the current source set against that record.

> **The model can propose actions. It cannot mark its own homework.**

## 5. Optional guided demo

```powershell
.\demo\run-complete-local-code-agent-demo.ps1
```

This walks through local NPU inference, the controlled capability surface and the stale-evidence rejection demo.

## What to read next

| File | Why it exists |
|---|---|
| `README.md` | Current product surface, architecture and measured evidence |
| `internal/docs/verification.md` | How independent proof is decided |
| `internal/docs/serving-and-accelerators.md` | Model, runtime and accelerator details |
| `internal/docs/project-history.md` | Experimental history and preserved earlier README material |
| `AGENTS.md` | Developer guidance for working on the codebase |

## If something does not work

**Chat cannot start the selected model.** Run:

```powershell
.\install.ps1
```

then retry the same chat command. Normal user guidance should never require an `internal/` command.

**An accelerator demo refuses immediately.** Run:

```powershell
.\install.ps1 -CheckOnly
```

and follow the reported setup requirement.

**A source-identity test fails.** Do not casually regenerate experiment identities. `internal/INSTRUMENT.json` declares the current measured source surface; model-facing and outcome-facing contract changes must remain explicit and historical generations must remain reproducible.
