# Quickstart

This page is for somebody opening the repository for the first time.

The simplest mental model is:

- **`chat.ps1`** talks directly to a local model.
- **Local Code Agent** adds controlled repository access, approved tools, task procedures / skills, and independent verification.
- **The accelerator demo** proves which device is actually running the model.
- **The verification demo** proves that passing test output is not blindly trusted.

All commands below are run from the repository root. Windows commands are shown in PowerShell.

---

## 0. Check or prepare the Windows workstation

If this machine has already been prepared for the demo, skip to step 1.

```powershell
.\scripts\work-laptop-one-shot.ps1 -CheckOnly
```

This checks the workstation without installing or changing anything. If required:

```powershell
.\scripts\work-laptop-one-shot.ps1 -InstallMissing
```

That prepares the managed runtime, model artifacts and qualification path used by the Panther Lake demo. It stops before any scored experiment.

For the full setup details, see `docs/work-laptop-bootstrap.md`.

---

## 1. See what Local Code Agent can do

```powershell
python scripts\capabilities.py
```

This prints the approved tools available to the model, the installed task procedures / skills, and the things the controller deliberately does not permit.

The supported list comes from the live implementation rather than a hand-written feature list.

No model or accelerator is required for this step.

---

## 2. Chat directly with a local model

List the available user-facing choices:

```powershell
.\chat.ps1
```

Start Qwen3-8B on the NPU profile:

```powershell
.\chat.ps1 qwen3-8b-npu
```

Other configured choices:

```powershell
.\chat.ps1 qwen3-8b-gpu
.\chat.ps1 qwen3-8b-cpu
.\chat.ps1 qwen3-coder-30b
```

The three Qwen3-8B choices use the same model artifact and client while changing only the requested execution device. `qwen3-coder-30b` selects a different model profile.

Chat is deliberately separate from Local Code Agent: it gives you the model conversation without repository tools, skills or verification.

If the selected model server is not running, chat tells you which configured server must be started before retrying.

---

## 3. Prove which accelerator is running Qwen3-8B

Open **Task Manager > Performance** and select the device you want to watch, then run:

```powershell
.\scripts\demo-accelerator.ps1 -Device NPU -Seconds 45
```

The same demo can target the GPU or CPU:

```powershell
.\scripts\demo-accelerator.ps1 -Device GPU -Seconds 45
.\scripts\demo-accelerator.ps1 -Device CPU -Seconds 45
```

The script requests the device explicitly and reports the device OpenVINO actually resolved before generating repeated model responses.

It also shows time to first token and generation speed. The first request after model startup can be slower because it may include one-time runtime warm-up as well as prompt processing. Later requests use an already-initialised runtime, but still process their prompts.

These timings are demo observations on an uncontrolled machine, not benchmark results. Controlled performance and energy measurement is separate work.

---

## 4. See independent verification reject stale test results

```powershell
python scripts\demo-trust-boundary.py
```

This demo uses a disposable C++ project and does not involve a model. It shows why Local Code Agent does not treat passing test output as proof by itself:

1. Build a clean project and run its tests successfully.
2. Change a source file without rebuilding the binary.
3. Restore the source timestamp, deliberately hiding the edit from a simple timestamp-only freshness check.
4. Run `ctest` again. It still reports success because it executes the old binary.
5. Local Code Agent independently rejects that test result as stale and identifies the changed source file.
6. Rebuild honestly. Compilation fails, proving that the earlier passing tests were not valid evidence for the current source.

The point is simple:

> **The model can propose actions. It cannot mark its own homework.**

The script asserts every stage and aborts if the demonstration does not behave as described.

---

## What to read next

| File | Why it exists |
|---|---|
| `README.md` | Current implementation and measured evidence |
| `docs/verification.md` | How independent proof is decided |
| `docs/serving-and-accelerators.md` | Model, runtime and accelerator details |
| `docs/project-history.md` | Experimental history and preserved earlier README material |
| `AGENTS.md` | Developer guidance for working on the codebase |
| `experiments/` | Frozen experiment artifacts |

---

## If something does not work

**`chat.ps1` says the model server is not running.** The profile is known, but its configured serving endpoint is not active. Follow the start command printed by chat, then retry the same `chat.ps1` command.

**The accelerator demo refuses immediately.** The managed OVMS runtime may not be installed. Run:

```powershell
.\scripts\work-laptop-one-shot.ps1 -CheckOnly
```

and follow the reported setup requirement.

**`capabilities.py` says the benchmark fixture is missing.** Create it with:

```powershell
python benchmark_fixture\generate_project.py
```

**A source-identity test fails.** Do not casually regenerate experiment identities. `INSTRUMENT.json` defines the current measured source surface and historical experiment generations must remain reproducible.
