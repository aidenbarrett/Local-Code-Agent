# Quickstart

This is the detailed first-run walkthrough for somebody who has decided to try Local Code Agent.

## What this guide assumes

The current setup path targets **Windows 11 on Intel Panther Lake** and uses PowerShell. The friendly local-model path serves Qwen3-8B through OpenVINO Model Server and can request the NPU, GPU or CPU on that machine.

The architecture is intended to support other runtimes and devices, but this guide should not be read as a promise that unrelated hardware or operating systems have been qualified.

The public surface is deliberately small:

- **`install.ps1`** prepares and validates the local runtime.
- **`chat.ps1`** talks directly to a local model.
- **`local-code-agent.ps1`** gives a model controlled repository access, approved tools, task procedures / skills, and independent verification.
- **`demo/`** contains demonstration entrypoints.

Everything else lives under `internal/` and is not required for normal first use.

## 0. Clone the repository

From PowerShell:

```powershell
git clone https://github.com/aidenbarrett/Local-Code-Agent.git
cd Local-Code-Agent
```

All commands below are run from that repository root.

## 1. Check and prepare the Windows workstation

Start with the read-only preflight:

```powershell
.\install.ps1 -CheckOnly
```

This checks the machine without enabling setup changes.

Then run the full setup / validation path:

```powershell
.\install.ps1
```

Bare setup shows the exact machine-level prerequisites it is allowed to install through WinGet and asks before enabling those installs. The approved list currently includes Git, Python 3.12, CMake and Visual Studio 2022 C++ Build Tools.

It also explains that WSL installation and NPU-driver changes are not automatic. The NPU driver is never installed by the script; `-OpenDriverPage` only opens the relevant driver page. WSL installation is attempted only when `-AttemptWslInstall` is supplied explicitly.

If those prerequisite installs have already been approved and you need a non-interactive setup run:

```powershell
.\install.ps1 -InstallMissing
```

The setup path prepares the managed runtime, model serving environment and local Python environment, downloads the configured Qwen3-8B model when required, qualifies the serving path and runs the C++ validation project. It stops before any scored experiment.

If company policy blocks an approved WinGet package, setup stops with the package name so you can ask IT for that specific prerequisite rather than debugging an unexplained exit code.

## 2. Chat directly with the local model

List the friendly demo choices:

```powershell
.\chat.ps1
```

Start Qwen3-8B on the NPU:

```powershell
.\chat.ps1 qwen3-8b-npu
```

Other configured demo choices:

```powershell
.\chat.ps1 qwen3-8b-gpu
.\chat.ps1 qwen3-8b-cpu
```

The three choices use the same Qwen3-8B model artifact and change only the requested execution device. Unrehearsed model profiles are deliberately not advertised on the friendly demo surface.

You do **not** need to start an internal model-server command first. Chat reuses a compatible server owned by Local Code Agent or starts the requested model/device through the deterministic serving controller, waits for readiness, and then presents the chat prompt.

Direct chat has no repository tools, filesystem access or command execution. Use an empty line or Ctrl-C at the prompt to exit. Ctrl-C during generation stops the current reply cleanly and returns to the prompt.

Each direct-chat run creates a persisted conversation and prints its conversation ID. Resume that exact raw user/assistant history later with:

```powershell
.\chat.ps1 qwen3-8b-npu --persona aiden --conversation <ID>
```

Resume with the same persona used to create the conversation. The system contract and persona message are derived again on every request rather than stored in conversation history. A failed or interrupted generation is not appended as a half-completed exchange. Old complete exchanges may be omitted from the model prompt when the deterministic context budget is reached, while the raw persisted conversation remains intact.

Useful boundary checks for a first rehearsal are:

```text
What are you?
Are you connected to the internet?
What can you do?
Can you inspect this repository for me?
```

The direct chat should describe itself as a local model with no network, filesystem or tool access, and should not claim to be the controlled coding agent.

## 3. See the controlled coding-agent surface

```powershell
.\local-code-agent.ps1
.\local-code-agent.ps1 capabilities
```

This prints the live approved tools, installed task procedures / skills, and the things the controller deliberately does not permit.

No model is required just to inspect the capability surface.

The important distinction is:

- `chat.ps1` = direct model conversation
- `local-code-agent.ps1` = model + controlled repository access + approved tools + skills + deterministic verification

## 4. Run a controlled repository task

A simple first task is:

```powershell
.\local-code-agent.ps1 run-task "Inspect this repository and summarize how it builds" --skill repo-navigation
```

Examples of intended workloads include repository navigation, build/test diagnosis or repair with independent verification, and controlled Git/repository review. These are intended use cases, not guarantees that every model solves every task.

The model never receives arbitrary shell access and never decides for itself that its work passed verification.

## 5. See the same local model run on the NPU, GPU or CPU

This demo keeps the model fixed and changes only the hardware that runs it. On this laptop:

- **NPU** = the dedicated laptop AI accelerator
- **GPU** = the graphics processor
- **CPU** = the general-purpose processor

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

The report shows the requested hardware target and the device OpenVINO actually uses. The first request is labelled **COLD START** because it can include one-time runtime warm-up plus prompt processing. Later requests are labelled **WARM** because the runtime is already initialised, though each request still processes its prompt.

Timing values are live demo observations on an uncontrolled machine, not benchmark results.

## 6. See independent verification reject stale test results

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

## 7. Optional guided demo

```powershell
.\demo\run-complete-local-code-agent-demo.ps1
```

This walks through local NPU inference, the controlled capability surface and the stale-evidence rejection demo.

## What to read next

| File | Why it exists |
|---|---|
| `README.md` | Product purpose, shortest happy path, architecture and measured evidence |
| `internal/docs/verification.md` | How independent proof is decided |
| `internal/docs/serving-and-accelerators.md` | Model, runtime and accelerator details |
| `internal/docs/project-history.md` | Experimental history and preserved earlier README material |
| `AGENTS.md` | Developer guidance for working on the codebase |

## If something does not work

**The preflight says the machine is not ready.** Follow the named prerequisite or driver action. `-CheckOnly` does not install or open anything.

**Setup is blocked by company policy.** Use the package name printed by `install.ps1` when asking IT for approval or installation, then rerun:

```powershell
.\install.ps1 -CheckOnly
```

**Chat cannot start the selected model.** Run:

```powershell
.\install.ps1
```

then retry the same chat command. Normal user guidance should never require an `internal/` command.

**The CPU/GPU/NPU hardware demo cannot start.** Run:

```powershell
.\install.ps1 -CheckOnly
```

and follow the reported setup requirement.

**A source-identity test fails while developing the project.** Do not casually regenerate experiment identities. `internal/INSTRUMENT.json` declares the current measured source surface; model-facing and outcome-facing contract changes must remain explicit and historical generations must remain reproducible.
